"""Integration tests for docker_tools. Hit a live dibo over SSH.

Run with:
    pytest tests/test_docker_tools.py -v
"""
from __future__ import annotations

import pytest

from homelab_agent.tools.docker_tools import (
    find_recently_restarted_containers,
    get_container_logs,
    get_container_stats,
    inspect_container,
    list_containers,
)
from homelab_agent.tools.system_tools import check_dibo_reachable


# The four containers we expect to be running on dibo.
# If you add/remove services, update this set.
EXPECTED_RUNNING = {"plex", "adguard", "transmission", "omada"}


@pytest.fixture(scope="session", autouse=True)
def dibo_must_be_reachable():
    """Skip the whole suite if dibo can't be reached."""
    result = check_dibo_reachable.invoke({})
    if not result.get("reachable"):
        pytest.skip(
            f"dibo unreachable: {result.get('error_type')}: "
            f"{result.get('error_message')}"
        )


# ---------- list_containers ----------

class TestListContainers:
    def test_returns_non_empty_list(self):
        containers = list_containers.invoke({})
        assert isinstance(containers, list)
        assert len(containers) > 0

    def test_all_containers_have_required_fields(self):
        containers = list_containers.invoke({})
        required = {"name", "image", "state", "status", "created", "ports"}
        for c in containers:
            assert required <= set(c.keys()), f"missing fields in {c}"

    def test_expected_services_running(self):
        """The four homelab services should all be present and running."""
        containers = list_containers.invoke({})
        running_names = {c["name"] for c in containers if c["state"] == "running"}
        missing = EXPECTED_RUNNING - running_names
        assert not missing, (
            f"expected services not running: {missing}. "
            f"Found running: {running_names}"
        )

    def test_states_are_known_values(self):
        containers = list_containers.invoke({})
        valid_states = {"running", "exited", "paused", "restarting", "created", "dead"}
        for c in containers:
            assert c["state"] in valid_states, f"unexpected state: {c}"

    def test_filtering_to_running_only(self):
        """all_containers=False should exclude stopped containers."""
        all_c = list_containers.invoke({"all_containers": True})
        running_only = list_containers.invoke({"all_containers": False})
        # every container in running_only should have state=running
        for c in running_only:
            assert c["state"] == "running"
        # running_only should be a subset of all
        all_names = {c["name"] for c in all_c}
        running_names = {c["name"] for c in running_only}
        assert running_names <= all_names


# ---------- get_container_stats ----------

class TestContainerStats:
    def test_plex_has_sensible_stats(self):
        """Plex should be running and consuming non-trivial memory."""
        stats = get_container_stats.invoke({"name": "plex"})
        assert "error" not in stats, f"unexpected error: {stats}"
        assert stats["name"] == "plex"
        # Plex always uses at least a few hundred MB
        assert stats["mem_usage_mb"] > 50, (
            f"plex memory suspiciously low: {stats['mem_usage_mb']} MB"
        )
        # CPU should be a valid percentage
        assert 0 <= stats["cpu_pct"] <= 100 * 16, (
            f"impossible CPU %: {stats['cpu_pct']}"
        )
        # Memory limit should be roughly host RAM (~16GB on dibo)
        assert stats["mem_limit_mb"] > 1000

    def test_all_stats_fields_present(self):
        stats = get_container_stats.invoke({"name": "plex"})
        required = {
            "name", "cpu_pct", "mem_usage_mb", "mem_limit_mb", "mem_pct",
            "net_in_mb", "net_out_mb", "block_in_mb", "block_out_mb",
        }
        assert required <= set(stats.keys())

    def test_mem_pct_consistent_with_usage_and_limit(self):
        """mem_pct should roughly equal usage/limit * 100."""
        stats = get_container_stats.invoke({"name": "plex"})
        expected_pct = stats["mem_usage_mb"] / stats["mem_limit_mb"] * 100
        # Docker's reported mem_pct can differ slightly due to internal accounting;
        # allow 1 percentage point of slack
        assert abs(stats["mem_pct"] - expected_pct) < 1.0, (
            f"mem_pct {stats['mem_pct']} inconsistent with "
            f"{stats['mem_usage_mb']}/{stats['mem_limit_mb']}"
        )

    def test_nonexistent_container_returns_error(self):
        """A bogus name should return an error dict, not crash."""
        stats = get_container_stats.invoke({"name": "definitelynotacontainer"})
        assert "error" in stats


# ---------- get_container_logs ----------

class TestContainerLogs:
    def test_returns_string(self):
        logs = get_container_logs.invoke({"name": "plex", "tail": 5})
        assert isinstance(logs, str)

    def test_tail_parameter_respected(self):
        """Asking for fewer lines should return less content."""
        short = get_container_logs.invoke({"name": "plex", "tail": 5})
        long = get_container_logs.invoke({"name": "plex", "tail": 100})
        # We can't assert exact line counts (Plex output varies), but
        # 100 lines should give meaningfully more content than 5
        assert len(long) >= len(short)

    def test_nonexistent_container_does_not_crash(self):
        logs = get_container_logs.invoke({"name": "definitelynotacontainer"})
        assert isinstance(logs, str)


# ---------- inspect_container ----------

class TestInspectContainer:
    def test_plex_inspect_shape(self):
        info = inspect_container.invoke({"name": "plex"})
        assert "error" not in info
        required = {
            "name", "image", "state", "started_at", "restart_count",
            "restart_policy", "health_status", "mounts",
        }
        assert required <= set(info.keys())

    def test_plex_is_healthy(self):
        """plex has a healthcheck defined and should report 'healthy'."""
        info = inspect_container.invoke({"name": "plex"})
        assert info["health_status"] == "healthy", (
            f"plex health is {info['health_status']!r} — investigate"
        )

    def test_adguard_has_no_healthcheck(self):
        """adguard has no Docker healthcheck — health_status should be None."""
        info = inspect_container.invoke({"name": "adguard"})
        assert info["health_status"] is None

    def test_plex_has_media_mount(self):
        """Plex should have /srv/storage/media or similar mounted somewhere."""
        info = inspect_container.invoke({"name": "plex"})
        sources = [m["source"] for m in info["mounts"]]
        assert any("/srv/storage" in s for s in sources), (
            f"plex mounts don't reference /srv/storage: {sources}"
        )

    def test_restart_count_is_int(self):
        info = inspect_container.invoke({"name": "plex"})
        assert isinstance(info["restart_count"], int)
        assert info["restart_count"] >= 0

    def test_nonexistent_container_returns_error(self):
        info = inspect_container.invoke({"name": "definitelynotacontainer"})
        assert "error" in info


# ---------- find_recently_restarted_containers ----------

class TestFindRecentlyRestarted:
    def test_returns_list(self):
        result = find_recently_restarted_containers.invoke({"since_hours": 24})
        assert isinstance(result, list)

    def test_no_restarts_on_stable_homelab(self):
        """On a stable homelab, no container should have restarted recently.
        If this fails, something is genuinely flapping — worth investigating."""
        result = find_recently_restarted_containers.invoke({"since_hours": 24})
        assert result == [], (
            f"unexpected restarts: {result}. "
            f"This isn't necessarily a bug — investigate the containers listed."
        )

    def test_flagged_containers_have_required_fields(self):
        """Even if empty in this run, the function should be queryable with
        no errors using a long lookback window."""
        result = find_recently_restarted_containers.invoke({"since_hours": 999_999})
        # results may or may not be empty depending on history; either is fine
        for c in result:
            assert {"name", "restart_count", "state"} <= set(c.keys())