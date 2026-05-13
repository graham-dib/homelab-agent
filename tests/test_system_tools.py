"""Integration tests for system_tools. These hit a live dibo over SSH —
they're slow (~1-2s each) and not hermetic. Skip in CI; run locally
before commits that touch tool parsing logic.

Run with:
    pytest tests/test_system_tools.py -v
"""
from __future__ import annotations

import pytest

from homelab_agent.tools.system_tools import (
    check_dibo_reachable,
    get_disk_usage,
    get_journal_logs,
    get_load_average,
    get_memory_stats,
    get_service_status,
    get_top_processes,
)


# ---------- Connectivity gate ----------

@pytest.fixture(scope="session", autouse=True)
def dibo_must_be_reachable():
    """If dibo isn't reachable, skip the whole suite rather than failing every test."""
    result = check_dibo_reachable.invoke({})
    if not result.get("reachable"):
        pytest.skip(
            f"dibo unreachable: {result.get('error_type')}: "
            f"{result.get('error_message')} - check Tailscale, SSH key, .env"
        )


# ---------- check_dibo_reachable ----------

class TestDiboReachable:
    def test_basic_shape(self):
        result = check_dibo_reachable.invoke({})
        assert result["reachable"] is True
        assert result["hostname"]                       # non-empty string
        assert result["kernel"]                         # non-empty string
        assert isinstance(result["uptime_seconds"], int)
        assert result["uptime_seconds"] > 0

    def test_uptime_is_plausible(self):
        """Uptime should be at least a few seconds but less than 10 years."""
        result = check_dibo_reachable.invoke({})
        assert 1 < result["uptime_seconds"] < 10 * 365 * 86400


# ---------- get_disk_usage ----------

class TestDiskUsage:
    def test_returns_non_empty_list(self):
        disks = get_disk_usage.invoke({})
        assert isinstance(disks, list)
        assert len(disks) > 0

    def test_root_filesystem_present(self):
        disks = get_disk_usage.invoke({})
        root = next((d for d in disks if d["mount"] == "/"), None)
        assert root is not None, f"no root filesystem in {[d['mount'] for d in disks]}"

    def test_all_disks_have_required_fields(self):
        disks = get_disk_usage.invoke({})
        required = {"filesystem", "size_gb", "used_gb", "available_gb", "pct_used", "mount"}
        for disk in disks:
            assert required <= set(disk.keys()), f"missing fields in {disk}"

    def test_percentages_in_range(self):
        disks = get_disk_usage.invoke({})
        for disk in disks:
            assert 0 <= disk["pct_used"] <= 100, f"impossible pct: {disk}"

    def test_used_plus_available_approximates_size(self):
        """Filesystems reserve ~5% for root, so used + available is typically
        slightly less than size. Tolerate up to 10% slack."""
        disks = get_disk_usage.invoke({})
        for disk in disks:
            if disk["size_gb"] < 1:
                continue  # tiny filesystems have rounding issues
            total = disk["used_gb"] + disk["available_gb"]
            slack = disk["size_gb"] * 0.10
            assert total <= disk["size_gb"] + 0.5, (
                f"{disk['mount']}: used+avail ({total}) > size ({disk['size_gb']})"
            )
            assert total >= disk["size_gb"] - slack, (
                f"{disk['mount']}: used+avail ({total}) << size ({disk['size_gb']})"
            )

    def test_excludes_pseudo_filesystems(self):
        """We filter tmpfs/devtmpfs/overlay in the tool - none should appear."""
        disks = get_disk_usage.invoke({})
        for disk in disks:
            assert "tmpfs" not in disk["filesystem"]
            assert "overlay" not in disk["filesystem"]


# ---------- get_memory_stats ----------

class TestMemoryStats:
    def test_basic_shape(self):
        mem = get_memory_stats.invoke({})
        for field in ["total_mb", "used_mb", "free_mb", "available_mb",
                      "buffers_cache_mb", "swap_total_mb", "swap_used_mb", "pct_used"]:
            assert field in mem, f"missing: {field}"

    def test_total_is_positive(self):
        mem = get_memory_stats.invoke({})
        assert mem["total_mb"] > 0

    def test_used_does_not_exceed_total(self):
        mem = get_memory_stats.invoke({})
        assert mem["used_mb"] <= mem["total_mb"]
        assert mem["available_mb"] <= mem["total_mb"]

    def test_pct_used_in_range(self):
        mem = get_memory_stats.invoke({})
        assert 0 <= mem["pct_used"] <= 100

    def test_swap_used_not_exceeds_total(self):
        mem = get_memory_stats.invoke({})
        assert mem["swap_used_mb"] <= mem["swap_total_mb"]


# ---------- get_load_average ----------

class TestLoadAverage:
    def test_basic_shape(self):
        load = get_load_average.invoke({})
        for field in ["uptime_human", "users_logged_in", "load_1min",
                      "load_5min", "load_15min", "cpu_count", "load_pct_1min"]:
            assert field in load

    def test_cpu_count_positive(self):
        load = get_load_average.invoke({})
        assert load["cpu_count"] >= 1

    def test_loads_non_negative(self):
        load = get_load_average.invoke({})
        assert load["load_1min"] >= 0
        assert load["load_5min"] >= 0
        assert load["load_15min"] >= 0

    def test_load_pct_consistent_with_load_and_cpus(self):
        load = get_load_average.invoke({})
        expected = round(load["load_1min"] / load["cpu_count"] * 100, 1)
        assert abs(load["load_pct_1min"] - expected) < 0.2  # tolerate rounding

    def test_uptime_human_stripped(self):
        """We strip the leading 'up ' from the uptime string."""
        load = get_load_average.invoke({})
        assert not load["uptime_human"].startswith("up")


# ---------- get_service_status ----------

class TestServiceStatus:
    def test_docker_is_running(self):
        """Docker is a known-running service on dibo per the homelab setup."""
        status = get_service_status.invoke({"unit": "docker"})
        assert status["unit"] == "docker"
        assert status["is_running"] is True
        assert status["active_state"] == "active"
        assert status["main_pid"] is not None

    def test_nonexistent_service_does_not_crash(self):
        """Bogus unit name should return cleanly, not raise."""
        status = get_service_status.invoke({"unit": "definitelynotaservice"})
        assert status["unit"] == "definitelynotaservice"
        assert status["is_running"] is False
        # active_state will be "inactive" or "unknown" depending on systemd version

    def test_running_service_has_uptime(self):
        status = get_service_status.invoke({"unit": "docker"})
        assert status["uptime_seconds"] is not None
        assert status["uptime_seconds"] > 0


# ---------- get_journal_logs ----------

class TestJournalLogs:
    def test_returns_string(self):
        result = get_journal_logs.invoke({
            "unit": "docker", "since": "1 hour ago", "lines": 5,
        })
        assert isinstance(result, str)

    def test_empty_priority_filter_returns_no_entries_marker(self):
        """When the priority filter excludes everything, we return a marker
        rather than empty string - so the LLM gets something useful."""
        # Use a unit that almost certainly has no err-level logs in the last second
        result = get_journal_logs.invoke({
            "unit": "docker", "since": "1 second ago", "priority": "err", "lines": 5,
        })
        assert result  # not empty


# ---------- get_top_processes ----------

class TestTopProcesses:
    def test_returns_requested_number(self):
        procs = get_top_processes.invoke({"by": "memory", "n": 5})
        assert len(procs) == 5

    def test_sorted_descending_by_memory(self):
        procs = get_top_processes.invoke({"by": "memory", "n": 10})
        mems = [p["mem_pct"] for p in procs]
        assert mems == sorted(mems, reverse=True), (
            f"not sorted desc: {mems}"
        )

    def test_sorted_descending_by_cpu(self):
        procs = get_top_processes.invoke({"by": "cpu", "n": 10})
        cpus = [p["cpu_pct"] for p in procs]
        assert cpus == sorted(cpus, reverse=True)

    def test_all_processes_have_required_fields(self):
        procs = get_top_processes.invoke({"by": "memory", "n": 5})
        required = {"pid", "user", "cpu_pct", "mem_pct", "rss_mb", "command"}
        for p in procs:
            assert required <= set(p.keys())
            assert p["pid"] > 0
            assert p["rss_mb"] >= 0