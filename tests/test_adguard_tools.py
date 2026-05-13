"""Integration tests for adguard_tools. Hit a live AdGuard Home instance on dibo.

Run with:
    pytest tests/test_adguard_tools.py -v
"""
from __future__ import annotations

import pytest

from homelab_agent.tools.adguard_tools import (
    get_adguard_query_log,
    get_adguard_stats,
    get_adguard_status,
    get_adguard_top_blocked,
    get_adguard_top_clients,
)


@pytest.fixture(scope="session", autouse=True)
def adguard_must_be_reachable():
    """Skip the whole suite if AdGuard isn't responding."""
    try:
        status = get_adguard_status.invoke({})
    except Exception as e:
        pytest.skip(
            f"AdGuard unreachable: {type(e).__name__}: {e} - "
            f"check the container is running, password in .env, and Tailscale is up"
        )
    if not status.get("running"):
        pytest.skip(f"AdGuard reports not running: {status}")


# ---------- get_adguard_status ----------

class TestStatus:
    def test_basic_shape(self):
        status = get_adguard_status.invoke({})
        # core fields every version has
        for field in ["version", "dns_port", "http_port", "running", "protection_enabled"]:
            assert field in status, f"missing field: {field}"

    def test_is_running(self):
        status = get_adguard_status.invoke({})
        assert status["running"] is True

    def test_protection_is_enabled(self):
        """If protection is off, AdGuard is letting everything through — alert."""
        status = get_adguard_status.invoke({})
        assert status["protection_enabled"] is True, (
            "AdGuard protection is disabled — DNS filtering is OFF"
        )

    def test_dns_port_is_53(self):
        status = get_adguard_status.invoke({})
        assert status["dns_port"] == 53

    def test_version_string_present(self):
        status = get_adguard_status.invoke({})
        assert isinstance(status["version"], str)
        assert status["version"]  # non-empty


# ---------- get_adguard_stats ----------

class TestStats:
    def test_has_query_counts(self):
        stats = get_adguard_stats.invoke({})
        for field in ["num_dns_queries", "num_blocked_filtering", "avg_processing_time"]:
            assert field in stats

    def test_query_counts_are_non_negative(self):
        stats = get_adguard_stats.invoke({})
        assert stats["num_dns_queries"] >= 0
        assert stats["num_blocked_filtering"] >= 0

    def test_blocked_does_not_exceed_total(self):
        """Sanity: can't block more queries than you've seen."""
        stats = get_adguard_stats.invoke({})
        assert stats["num_blocked_filtering"] <= stats["num_dns_queries"]

    def test_avg_processing_time_is_sensible(self):
        """Average DNS processing should be sub-second (typically tens of ms)."""
        stats = get_adguard_stats.invoke({})
        assert 0 <= stats["avg_processing_time"] < 1.0, (
            f"avg processing time {stats['avg_processing_time']}s seems wrong"
        )

    def test_top_lists_present(self):
        stats = get_adguard_stats.invoke({})
        # These should always be lists, even if empty
        assert isinstance(stats.get("top_queried_domains"), list)
        assert isinstance(stats.get("top_blocked_domains"), list)
        assert isinstance(stats.get("top_clients"), list)


# ---------- get_adguard_query_log ----------

class TestQueryLog:
    def test_returns_list(self):
        log = get_adguard_query_log.invoke({"limit": 10})
        assert isinstance(log, list)

    def test_respects_limit(self):
        """Asking for 5 entries shouldn't return 50."""
        log = get_adguard_query_log.invoke({"limit": 5})
        assert len(log) <= 5

    def test_entries_have_expected_shape(self):
        """Each query log entry should have at least a question and client."""
        log = get_adguard_query_log.invoke({"limit": 10})
        if not log:
            pytest.skip("query log is empty — AdGuard may have just started")
        for entry in log:
            assert "question" in entry, f"missing 'question': {entry}"
            assert "client" in entry, f"missing 'client': {entry}"
            # 'question' should have a 'name' (the domain queried)
            assert "name" in entry["question"]

    def test_search_filter_narrows_results(self):
        """Searching for an unlikely string should return zero or very few results."""
        # 'zzzzzzz' is unlikely to be a real domain
        log = get_adguard_query_log.invoke({"limit": 50, "search": "zzzzzzz"})
        # search semantics in AdGuard are loose — accept up to a handful of matches
        assert len(log) <= 5


# ---------- get_adguard_top_blocked ----------

class TestTopBlocked:
    def test_returns_list(self):
        blocked = get_adguard_top_blocked.invoke({})
        assert isinstance(blocked, list)

    def test_entries_have_single_key_dict(self):
        """AdGuard returns top-blocked as [{"domain.com": count}, ...]"""
        blocked = get_adguard_top_blocked.invoke({})
        if not blocked:
            pytest.skip("no blocked domains yet")
        for entry in blocked:
            assert isinstance(entry, dict)
            assert len(entry) == 1, f"expected single-key dict, got: {entry}"

    def test_counts_are_positive(self):
        blocked = get_adguard_top_blocked.invoke({})
        if not blocked:
            pytest.skip("no blocked domains yet")
        for entry in blocked:
            count = list(entry.values())[0]
            assert count > 0


# ---------- get_adguard_top_clients ----------

class TestTopClients:
    def test_returns_list(self):
        clients = get_adguard_top_clients.invoke({})
        assert isinstance(clients, list)

    def test_entries_are_single_key_dicts(self):
        """top_clients is [{"ip": count}, ...] in this AdGuard version."""
        clients = get_adguard_top_clients.invoke({})
        if not clients:
            pytest.skip("no client activity yet")
        for entry in clients:
            assert isinstance(entry, dict)
            assert len(entry) == 1

    def test_sorted_descending_by_count(self):
        """Top clients should be ordered most-active first."""
        clients = get_adguard_top_clients.invoke({})
        if len(clients) < 2:
            pytest.skip("need at least 2 clients to test ordering")
        counts = [list(c.values())[0] for c in clients]
        assert counts == sorted(counts, reverse=True), (
            f"clients not sorted descending: {counts}"
        )

    def test_top_clients_match_stats(self):
        """get_adguard_top_clients() should return the same data as
        get_adguard_stats()['top_clients'] — they hit the same endpoint."""
        from_tool = get_adguard_top_clients.invoke({})
        from_stats = get_adguard_stats.invoke({}).get("top_clients", [])
        # they may not be byte-identical due to live data shifting between calls,
        # but should have the same length and roughly matching top entry
        if from_tool and from_stats:
            assert len(from_tool) == len(from_stats)
            # top entry IP should match (counts may differ by a few queries)
            top_ip_tool = list(from_tool[0].keys())[0]
            top_ip_stats = list(from_stats[0].keys())[0]
            assert top_ip_tool == top_ip_stats