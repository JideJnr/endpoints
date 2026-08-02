"""
Unit tests for TTL boundary behaviour of the DB-backed SofaScore event-list cache.

Tests that a cache row at exactly TTL - 1 second is a hit, and at TTL + 1 second
is a miss, for each date category (today, future, past).  Also tests that a
corrupt ``fetched_at`` string causes the row to be treated as expired.

Validates: Requirements 1.3, 1.5, 1.6, 1.7
"""

import json
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out curl_cffi before importing sofascore_client so the module loads
# even when the wheel is not installed in the current Python environment.
# ---------------------------------------------------------------------------
if "curl_cffi" not in sys.modules:
    _cffi_stub = types.ModuleType("curl_cffi")
    _requests_stub = types.ModuleType("curl_cffi.requests")

    class _StubResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class _StubSession:
        def __init__(self, *args, **kwargs):
            self.trust_env = True

        def get(self, *args, **kwargs):  # pragma: no cover
            raise RuntimeError("Network not available in stub")

    _requests_stub.Session = _StubSession
    _requests_stub.Response = _StubResponse
    _cffi_stub.requests = _requests_stub
    sys.modules["curl_cffi"] = _cffi_stub
    sys.modules["curl_cffi.requests"] = _requests_stub

import app.sofascore_client as sc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — in-memory SQLite DB with the sofa_event_list_cache table
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with the cache table already set up."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sofa_event_list_cache (
            date        TEXT PRIMARY KEY,
            fetched_at  TEXT NOT NULL,
            events_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


@contextmanager
def _mem_db_conn(conn: sqlite3.Connection, timeout: int = 5):
    """Context-manager shim that yields the shared in-memory connection."""
    yield conn


def _insert_cache_row(conn: sqlite3.Connection, date: str, fetched_at: str, events: list) -> None:
    """Insert a row directly into the in-memory cache table."""
    conn.execute(
        "INSERT OR REPLACE INTO sofa_event_list_cache (date, fetched_at, events_json) VALUES (?, ?, ?)",
        (date, fetched_at, json.dumps(events)),
    )
    conn.commit()


# A minimal event list to use as fixture data in all tests.
_SAMPLE_EVENTS = [
    {"id": 1, "name": "Home FC vs Away FC"},
    {"id": 2, "name": "Alpha United vs Beta City"},
]


# ---------------------------------------------------------------------------
# Helper: build a patched datetime class that returns a fixed "now"
# ---------------------------------------------------------------------------

def _make_datetime_mock(fixed_now: datetime):
    """Return a mock that replaces ``app.sofascore_client.datetime``.

    ``datetime.now(timezone.utc)`` returns *fixed_now*.
    ``datetime.fromisoformat(s)`` delegates to the real implementation so
    the ``fetched_at`` string can still be parsed inside ``_db_cache_get``.
    """
    mock_dt = MagicMock()
    mock_dt.now.return_value = fixed_now
    mock_dt.fromisoformat.side_effect = datetime.fromisoformat
    return mock_dt


# ---------------------------------------------------------------------------
# TTL boundary tests
# ---------------------------------------------------------------------------

class TestTTLBoundaryToday(unittest.TestCase):
    """TTL boundary tests for date == today."""

    # Use a fixed date string so the test is deterministic.
    # We control "today" by patching _ttl_for_date to return DB_CACHE_TTL_TODAY_SECONDS
    # and by using a date string that matches today.  Because _db_cache_get calls
    # _ttl_for_date(date) — which internally calls _date_cls.today() — we patch
    # _ttl_for_date directly to always return the today-TTL constant.

    DATE = datetime.now(timezone.utc).date().isoformat()
    TTL = sc.DB_CACHE_TTL_TODAY_SECONDS  # 600 seconds

    def _run(self, age_seconds: int) -> Optional[List]:
        """
        Insert a cache row whose age equals *age_seconds* and call _db_cache_get.

        ``fetched_at`` is set to ``now - age``.  ``now`` is then patched to
        return the same fixed ``now`` value so the age computation inside
        ``_db_cache_get`` is fully deterministic.
        """
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        fetched_at = now - timedelta(seconds=age_seconds)

        mem_conn = _make_mem_conn()
        _insert_cache_row(mem_conn, self.DATE, fetched_at.isoformat(), _SAMPLE_EVENTS)

        mock_dt = _make_datetime_mock(now)

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        with (
            patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn),
            patch("app.sofascore_client.datetime", mock_dt),
            patch("app.sofascore_client._ttl_for_date", return_value=self.TTL),
        ):
            return sc._db_cache_get(self.DATE)

    def test_today_cache_hit_before_ttl(self):
        """A row with age = TTL_TODAY - 1s must be returned (cache hit)."""
        result = self._run(age_seconds=self.TTL - 1)
        self.assertEqual(result, _SAMPLE_EVENTS)

    def test_today_cache_miss_after_ttl(self):
        """A row with age = TTL_TODAY + 1s must return None (cache miss)."""
        result = self._run(age_seconds=self.TTL + 1)
        self.assertIsNone(result)


class TestTTLBoundaryFuture(unittest.TestCase):
    """TTL boundary tests for date strictly after today (future)."""

    DATE = "2030-01-01"
    TTL = sc.DB_CACHE_TTL_FUTURE_SECONDS  # 3600 seconds

    def _run(self, age_seconds: int) -> Optional[List]:
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        fetched_at = now - timedelta(seconds=age_seconds)

        mem_conn = _make_mem_conn()
        _insert_cache_row(mem_conn, self.DATE, fetched_at.isoformat(), _SAMPLE_EVENTS)

        mock_dt = _make_datetime_mock(now)

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        with (
            patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn),
            patch("app.sofascore_client.datetime", mock_dt),
            patch("app.sofascore_client._ttl_for_date", return_value=self.TTL),
        ):
            return sc._db_cache_get(self.DATE)

    def test_future_cache_hit_before_ttl(self):
        """A row with age = TTL_FUTURE - 1s must be returned (cache hit)."""
        result = self._run(age_seconds=self.TTL - 1)
        self.assertEqual(result, _SAMPLE_EVENTS)

    def test_future_cache_miss_after_ttl(self):
        """A row with age = TTL_FUTURE + 1s must return None (cache miss)."""
        result = self._run(age_seconds=self.TTL + 1)
        self.assertIsNone(result)


class TestTTLBoundaryPast(unittest.TestCase):
    """TTL boundary tests for date strictly before today (past)."""

    DATE = "2020-01-01"
    TTL = sc.DB_CACHE_TTL_PAST_SECONDS  # 86400 seconds

    def _run(self, age_seconds: int) -> Optional[List]:
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        fetched_at = now - timedelta(seconds=age_seconds)

        mem_conn = _make_mem_conn()
        _insert_cache_row(mem_conn, self.DATE, fetched_at.isoformat(), _SAMPLE_EVENTS)

        mock_dt = _make_datetime_mock(now)

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        with (
            patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn),
            patch("app.sofascore_client.datetime", mock_dt),
            patch("app.sofascore_client._ttl_for_date", return_value=self.TTL),
        ):
            return sc._db_cache_get(self.DATE)

    def test_past_cache_hit_before_ttl(self):
        """A row with age = TTL_PAST - 1s must be returned (cache hit)."""
        result = self._run(age_seconds=self.TTL - 1)
        self.assertEqual(result, _SAMPLE_EVENTS)

    def test_past_cache_miss_after_ttl(self):
        """A row with age = TTL_PAST + 1s must return None (cache miss)."""
        result = self._run(age_seconds=self.TTL + 1)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Corrupt fetched_at edge case
# ---------------------------------------------------------------------------

class TestCorruptFetchedAt(unittest.TestCase):
    """A corrupt fetched_at string must cause _db_cache_get to return None."""

    DATE = "2025-06-15"

    def test_corrupt_fetched_at_treated_as_expired(self):
        """Insert a row with fetched_at = 'not-a-datetime' — must return None."""
        mem_conn = _make_mem_conn()
        _insert_cache_row(mem_conn, self.DATE, "not-a-datetime", _SAMPLE_EVENTS)

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        with patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn):
            result = sc._db_cache_get(self.DATE)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Unit test: missing raw_sporty edge case (Task 4.5)
# ---------------------------------------------------------------------------

class TestMissingRawSporty(unittest.TestCase):
    """
    A prematch item with raw_sporty = None (empty sporty dict) is skipped
    with a logged warning and does not crash or call the SportyBet API.

    Validates: Requirements 2.5
    """

    def _run_worker_with_batch(self, batch):
        """Run run_enrichment_worker with all I/O mocked, return result."""
        import sys
        import app.buffer as buf
        from unittest.mock import MagicMock, patch

        snap_mock = MagicMock(return_value=False)

        stub_modules = {
            "app.sofascore_client": MagicMock(
                fetch_all_scheduled_events=MagicMock(return_value=[]),
                fetch_event_detail=MagicMock(return_value={}),
                fetch_live_events=MagicMock(return_value=[]),
                fetch_event_detail_live_refresh=MagicMock(return_value={}),
                fetch_event=MagicMock(return_value=None),
                is_terminal_event=MagicMock(return_value=False),
            ),
            "app.enrichment": MagicMock(
                _fuzzy_match=MagicMock(return_value=(None, 0.0)),
                _llm_match=MagicMock(return_value=None),
                _is_junk=MagicMock(return_value=False),
                FUZZY_THRESHOLD=0.75,
                LLM_FALLBACK_THRESHOLD=0.55,
            ),
            "app.sportradar_client": MagicMock(fetch_match_intelligence=MagicMock(return_value={})),
            "app.web_context": MagicMock(
                search_league_sentiment=MagicMock(return_value={}),
                search_match_context=MagicMock(return_value={"query": "", "snippets": [], "scraped": []}),
            ),
            "app.time_context": MagicMock(match_time_context=MagicMock(return_value={"local_date": "2025-01-01"})),
            "app.activity_log": MagicMock(record_activity=MagicMock(return_value=None)),
            "app.pipeline_registry": MagicMock(is_pipeline_enabled=MagicMock(return_value=False)),
            "app.prediction_flow": MagicMock(apply_prediction_state=MagicMock(return_value={"status": "skipped"})),
            "app.enriched_prediction": MagicMock(prediction_readiness=MagicMock(return_value={})),
            "app.config": MagicMock(get_settings=MagicMock(return_value=MagicMock(
                web_search_league_sentiment_enabled=False,
            ))),
        }

        original_modules = {}
        for mod_name, stub in stub_modules.items():
            original_modules[mod_name] = sys.modules.get(mod_name)
            sys.modules[mod_name] = stub

        try:
            patch_targets = {
                "app.buffer.get_unenriched_batch":              MagicMock(return_value=batch),
                "app.buffer.store_enriched":                    MagicMock(return_value=None),
                "app.buffer.record_activity":                   MagicMock(return_value=None),
                "app.buffer._candidate_sofascore_events":       MagicMock(return_value=[]),
                "app.buffer._with_search_fallback_candidates":  MagicMock(return_value=[]),
                "app.buffer._fuzzy_match":                      MagicMock(return_value=(None, 0.0)),
                "app.buffer._llm_match":                        MagicMock(return_value=None),
                "app.buffer._sofascore_date_candidates":        MagicMock(return_value=["2025-01-01"]),
                "app.buffer._sporty_detail_doc":                MagicMock(return_value={}),
                "app.buffer._sporty_live_data":                 MagicMock(return_value={}),
                "app.buffer._sofa_live_data":                   MagicMock(return_value={}),
                "app.buffer._data_sources":                     MagicMock(return_value={}),
                "app.buffer._lifecycle_state":                  MagicMock(return_value={}),
                "app.buffer._track_live_data_availability":     MagicMock(return_value=None),
                "app.buffer.classify_match_state":              MagicMock(return_value="prematch"),
                "app.buffer.detect_season_stage":               MagicMock(return_value="regular"),
                "app.buffer.snapshot_odds":                     snap_mock,
                "app.buffer.refresh_sporty_match_state":        MagicMock(return_value={}),
            }
            patchers = [patch(k, v) for k, v in patch_targets.items()]
            for p in patchers:
                p.start()
            try:
                result = buf.run_enrichment_worker(fetch_web_context=False)
            finally:
                for p in patchers:
                    p.stop()
        finally:
            for mod_name, original in original_modules.items():
                if original is None:
                    sys.modules.pop(mod_name, None)
                else:
                    sys.modules[mod_name] = original

        return result, snap_mock

    def test_prematch_item_with_empty_sporty_is_skipped(self):
        """
        A prematch item where item["sporty"] is an empty dict (falsy) must be
        skipped: the worker should not crash, must log a warning, and must not
        call snapshot_odds or store_enriched for that item.

        Validates: Requirements 2.5
        """
        # Empty dict is falsy → triggers the prematch guard
        batch = [
            {
                "match_id": "match_no_raw_sporty",
                "match_date": "2025-01-01",
                "is_live": False,
                "sporty": {},   # falsy — simulates absent raw_sporty
                "existing": {},
            }
        ]

        with self.assertLogs("app.buffer", level="WARNING") as log_ctx:
            result, snap_mock = self._run_worker_with_batch(batch)

        # Worker must complete without crashing
        self.assertEqual(result.get("status"), "ok",
                         f"Worker crashed or returned wrong status: {result}")

        # The item was skipped — stored count must be 0
        self.assertEqual(result.get("stored", 0), 0,
                         "Skipped item should not be stored; stored count must be 0.")

        # snapshot_odds must not have been called for the skipped item
        self.assertEqual(snap_mock.call_count, 0,
                         "snapshot_odds must not be called for a skipped (no raw_sporty) item.")

        # A WARNING must have been logged mentioning the match_id
        warning_messages = " ".join(log_ctx.output)
        self.assertIn("match_no_raw_sporty", warning_messages,
                      "Warning log must mention the match_id of the skipped item.")
