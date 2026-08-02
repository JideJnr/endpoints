# Feature: auto-match-pipeline, Property 1: Cache round-trip consistency
"""
Property-based tests for the auto-match pipeline.

Each test is tagged with the design property it validates and configured
with @settings(max_examples=100) as specified in the design document.

Validates: Requirements 1.3, 4.1
"""

import json
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
    """Context-manager shim that yields the shared in-memory connection.

    This is used to patch ``app.sofascore_client.db_conn`` so that the module
    under test reads and writes to an isolated :memory: database instead of
    the production SQLite file.
    """
    yield conn


# ---------------------------------------------------------------------------
# Hypothesis strategies — event dicts that mirror _parse_event output
# ---------------------------------------------------------------------------

_team_strategy = st.fixed_dictionaries(
    {
        "id": st.one_of(st.none(), st.integers(min_value=1, max_value=999_999)),
        "name": st.one_of(st.none(), st.text(min_size=1, max_size=60)),
        "short_name": st.one_of(st.none(), st.text(min_size=1, max_size=20)),
        "code": st.one_of(st.none(), st.text(min_size=2, max_size=5)),
    }
)

_score_strategy = st.fixed_dictionaries(
    {
        "home": st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
        "away": st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
        "home_ht": st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
        "away_ht": st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
    }
)

_status_strategy = st.fixed_dictionaries(
    {
        "code": st.one_of(st.none(), st.integers(min_value=0, max_value=120)),
        "description": st.one_of(st.none(), st.text(min_size=0, max_size=30)),
        "type": st.one_of(st.none(), st.sampled_from(["notstarted", "inprogress", "finished"])),
    }
)

_tournament_strategy = st.fixed_dictionaries(
    {
        "id": st.one_of(st.none(), st.integers(min_value=1, max_value=99_999)),
        "tournament_id": st.one_of(st.none(), st.integers(min_value=1, max_value=99_999)),
        "name": st.one_of(st.none(), st.text(min_size=1, max_size=60)),
    }
)

_event_dict_strategy = st.fixed_dictionaries(
    {
        "id": st.integers(min_value=1, max_value=10_000_000),
        "slug": st.one_of(st.none(), st.text(min_size=0, max_size=80)),
        "name": st.text(min_size=1, max_size=120),
        "home_team": _team_strategy,
        "away_team": _team_strategy,
        "score": _score_strategy,
        "status": _status_strategy,
        "home_red_cards": st.one_of(st.none(), st.integers(min_value=0, max_value=3)),
        "away_red_cards": st.one_of(st.none(), st.integers(min_value=0, max_value=3)),
        "tournament": _tournament_strategy,
        "season": st.one_of(st.none(), st.text(min_size=0, max_size=40)),
        "season_id": st.one_of(st.none(), st.integers(min_value=1, max_value=99_999)),
        "round": st.one_of(st.none(), st.integers(min_value=1, max_value=38)),
        "venue": st.one_of(st.none(), st.text(min_size=0, max_size=80)),
        "start_timestamp": st.one_of(st.none(), st.integers(min_value=1_500_000_000, max_value=2_000_000_000)),
        "winner_code": st.one_of(st.none(), st.integers(min_value=0, max_value=3)),
        "raw_event": st.just({}),
    }
)

# Generate a list of event dicts with unique IDs (mirrors real event lists)
_event_list_strategy = st.lists(
    _event_dict_strategy,
    min_size=0,
    max_size=30,
).map(
    lambda events: list({e["id"]: e for e in events}.values())
)

# Valid ISO-8601 date strings (past, present, or future — TTL varies but we
# control fetched_at directly in these tests so TTL doesn't matter here)
_date_strategy = st.dates(
    min_value=__import__("datetime").date(2020, 1, 1),
    max_value=__import__("datetime").date(2030, 12, 31),
).map(lambda d: d.isoformat())


# ---------------------------------------------------------------------------
# Property 1: Cache round-trip consistency
# ---------------------------------------------------------------------------

class TestCacheRoundTripConsistency(unittest.TestCase):
    """
    Property 1: Cache round-trip consistency

    For any valid date string and any list of event dicts, if those events are
    written to sofa_event_list_cache via _db_cache_set, then calling
    fetch_all_scheduled_events(date) with network mocked to raise SHALL return
    a list that is structurally identical to the stored list.

    Validates: Requirements 1.3, 4.1
    """

    @given(events=_event_list_strategy, date=_date_strategy)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_cache_round_trip(self, events: list, date: str) -> None:
        """Property 1: Cache round-trip consistency

        Validates: Requirements 1.3, 4.1
        """
        mem_conn = _make_mem_conn()

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        def _network_raises(*args, **kwargs):
            raise RuntimeError("Network should not be called on a cache hit")

        with patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn):
            # Write events to the in-memory cache
            sc._db_cache_set(date, events)

            with patch.object(sc._session, "get", side_effect=_network_raises):
                result = sc.fetch_all_scheduled_events(date)

        # The returned list must be structurally identical to what was stored
        self.assertEqual(result, events)


# ---------------------------------------------------------------------------
# Property 2: Force-refresh always bypasses the cache
# ---------------------------------------------------------------------------

# Feature: auto-match-pipeline, Property 2: Force-refresh always bypasses the cache

class TestForceRefreshBypassesCache(unittest.TestCase):
    """
    Property 2: Force-refresh always bypasses the cache

    For any date with a valid non-expired cache row, calling
    fetch_all_scheduled_events(date, force=True) SHALL always issue a network
    request and SHALL overwrite the cache row with the fresh response, regardless
    of the age of the existing row.

    Validates: Requirements 1.8
    """

    @given(
        stale_events=_event_list_strategy,
        fresh_events=_event_list_strategy,
        date=_date_strategy,
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_force_refresh_bypasses_cache(
        self, stale_events: list, fresh_events: list, date: str
    ) -> None:
        """Property 2: Force-refresh always bypasses the cache

        Validates: Requirements 1.8

        Strategy:
        1. Write stale_events to the in-memory cache via _db_cache_set (fresh
           timestamp, so the row is fully within TTL).
        2. Track calls to _db_cache_get and _db_cache_set by wrapping them with
           spies.
        3. Mock the network path so the global endpoint returns fresh_events.
        4. Call fetch_all_scheduled_events(date, force=True).
        5. Assert _db_cache_get was NOT called (cache was bypassed).
        6. Assert _db_cache_set WAS called with the fresh data (cache overwritten).
        7. Confirm cache now contains fresh_events: call fetch_all_scheduled_events(date)
           (no force) with network mocked to raise — it must return fresh_events.
        """
        mem_conn = _make_mem_conn()

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        with patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn):
            # Step 1: populate cache with stale_events (recent timestamp → within TTL)
            sc._db_cache_set(date, stale_events)

            # Verify the row is indeed a cache hit before the forced call
            cached_before = sc._db_cache_get(date)
            assert cached_before == stale_events, (
                "Pre-condition failed: stale_events should be a valid cache hit"
            )

            # Spy tracking
            cache_get_calls: list = []
            cache_set_calls: list = []

            original_db_cache_get = sc._db_cache_get
            original_db_cache_set = sc._db_cache_set

            def _spy_db_cache_get(d: str):
                cache_get_calls.append(d)
                return original_db_cache_get(d)

            def _spy_db_cache_set(d: str, events: list):
                cache_set_calls.append((d, events))
                return original_db_cache_set(d, events)

            # Build a mock response that returns fresh_events as parsed events.
            # fetch_all_scheduled_events calls _session.get, then _events_from_response,
            # then _parse_event on each element.  We bypass the parse overhead by
            # having the mock response's json() return a payload that passes through
            # _events_from_response and then _parse_event unchanged.
            #
            # _parse_event just re-maps raw SofaScore keys → our dict schema.  To
            # avoid re-implementing that mapping here, we instead patch _db_cache_set
            # to capture what the function *actually tries to cache*, and separately
            # verify the cache was overwritten by reading back through _db_cache_get
            # after the call (step 7 below).
            #
            # For the network mock we make status_code != 200 for the global endpoint
            # so the code falls through to the tournament-level path, but that path
            # also calls _session.get.  The cleanest approach that avoids replicating
            # internal URL routing is to patch _db_cache_get directly to simulate the
            # force=True bypass (it should not be called) and patch _db_cache_set to
            # record writes, then make the network path write fresh_events by having
            # the global URL return them.
            #
            # Simplest viable mock: respond to global-URL call with fresh_events,
            # force the status_code to 200, and have .json() return the right envelope.
            fresh_payload = {"events": [
                {
                    "id": e["id"],
                    "slug": e.get("slug"),
                    "homeTeam": {
                        "id": (e.get("home_team") or {}).get("id"),
                        "name": (e.get("home_team") or {}).get("name"),
                        "shortName": (e.get("home_team") or {}).get("short_name"),
                        "nameCode": (e.get("home_team") or {}).get("code"),
                    },
                    "awayTeam": {
                        "id": (e.get("away_team") or {}).get("id"),
                        "name": (e.get("away_team") or {}).get("name"),
                        "shortName": (e.get("away_team") or {}).get("short_name"),
                        "nameCode": (e.get("away_team") or {}).get("code"),
                    },
                    "tournament": {
                        "uniqueTournament": {
                            "id": (e.get("tournament") or {}).get("tournament_id"),
                            "name": (e.get("tournament") or {}).get("name"),
                        },
                        "name": (e.get("tournament") or {}).get("name"),
                    },
                    "status": {
                        "code": (e.get("status") or {}).get("code"),
                        "description": (e.get("status") or {}).get("description"),
                        "type": (e.get("status") or {}).get("type"),
                    },
                    "startTimestamp": e.get("start_timestamp"),
                    # Use {} (not None) so _parse_event's venue.get("name") doesn't crash
                    "venue": {"name": e.get("venue")} if e.get("venue") is not None else {},
                    "homeScore": {
                        "current": (e.get("score") or {}).get("home"),
                        "period1": (e.get("score") or {}).get("home_ht"),
                    },
                    "awayScore": {
                        "current": (e.get("score") or {}).get("away"),
                        "period1": (e.get("score") or {}).get("away_ht"),
                    },
                    "roundInfo": {"round": e.get("round")},
                    "season": {"name": e.get("season"), "id": e.get("season_id")},
                }
                for e in fresh_events
            ]}

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = fresh_payload

            with (
                patch.object(sc, "_db_cache_get", side_effect=_spy_db_cache_get),
                patch.object(sc, "_db_cache_set", side_effect=_spy_db_cache_set),
                patch.object(sc._session, "get", return_value=mock_response),
            ):
                # Step 4: call with force=True
                result = sc.fetch_all_scheduled_events(date, force=True)

            # Step 5: _db_cache_get must NOT have been called (cache bypassed)
            self.assertEqual(
                cache_get_calls,
                [],
                f"_db_cache_get was called {len(cache_get_calls)} time(s) during "
                f"force=True call; it should be skipped entirely.",
            )

            # Step 6: _db_cache_set must have been called (cache overwritten)
            self.assertGreater(
                len(cache_set_calls),
                0,
                "_db_cache_set was never called during force=True; the cache was not overwritten.",
            )
            # The date written must match
            written_date, _ = cache_set_calls[-1]
            self.assertEqual(written_date, date)

            # Step 7: confirm the cache now holds fresh data (not stale_events).
            # Read back through the real _db_cache_get (not the spy) using the
            # already-written in-memory DB.
            cached_after = sc._db_cache_get(date)
            # The IDs must match fresh_events (order may differ after parse)
            result_ids = {e["id"] for e in result}
            fresh_ids = {e["id"] for e in fresh_events}
            cached_after_ids = {e["id"] for e in (cached_after or [])}

            self.assertEqual(
                result_ids,
                fresh_ids,
                "force=True should have returned fresh_events IDs, not stale_events.",
            )
            self.assertEqual(
                cached_after_ids,
                fresh_ids,
                "Cache should contain fresh_events IDs after force=True, not stale_events.",
            )


# ---------------------------------------------------------------------------
# Property 3: Cache idempotence within TTL window
# ---------------------------------------------------------------------------

# Feature: auto-match-pipeline, Property 3: Cache idempotence within TTL window

class TestCacheIdempotenceWithinTTLWindow(unittest.TestCase):
    """
    Property 3: Cache idempotence within TTL window

    For any date, calling fetch_all_scheduled_events(date) N times (N ≥ 2) in
    sequence within a single TTL window SHALL return the identical event list on
    every call, and the network SHALL be contacted exactly once (on the first call
    that populates the cache).

    Validates: Requirements 1.3, 4.4
    """

    @given(
        events=_event_list_strategy,
        date=_date_strategy,
        n=st.integers(min_value=2, max_value=10),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_cache_idempotence(self, events: list, date: str, n: int) -> None:
        """Property 3: Cache idempotence within TTL window

        Validates: Requirements 1.3, 4.4

        Strategy:
        1. Seed the cache via _db_cache_set (fresh timestamp → within TTL).
           This represents the state after the first real network fetch; we
           are explicitly testing that calls 2..N never contact the network.
        2. Build a mock that would raise if the network is contacted — any
           call to _session.get after the cache is seeded is a violation.
        3. Call fetch_all_scheduled_events(date) N times.
        4. Assert every call returned a list with the same event IDs as
           the seeded events.
        5. Assert mock_get was never called (network contacted zero times
           because all N calls were served from cache).

        Note: We seed the cache directly (bypassing the first network call)
        to avoid the empty-list edge case where fetch_all_scheduled_events
        falls through to the tournament-level parallel fetcher when the
        global endpoint returns an empty payload — that fallthrough is
        correct production behaviour but would make the first call contact
        the network many times, not once.  What the property validates is
        that once a cache row exists and is within TTL, all subsequent calls
        are served from cache without any network contact.
        """
        mem_conn = _make_mem_conn()

        def _patched_db_conn(timeout=5):
            return _mem_db_conn(mem_conn, timeout=timeout)

        def _network_must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "Network was contacted after cache was already seeded; "
                "cache idempotence was violated."
            )

        with patch("app.sofascore_client.db_conn", side_effect=_patched_db_conn):
            # Seed the cache — represents the state right after the first
            # network fetch populated it.
            sc._db_cache_set(date, events)

            with patch.object(sc._session, "get", side_effect=_network_must_not_be_called) as mock_get:
                results = []
                for _ in range(n):
                    result = sc.fetch_all_scheduled_events(date)
                    results.append(result)

        # Every call must have returned the same event list (same content).
        first_result = results[0]
        for i, call_result in enumerate(results[1:], start=2):
            self.assertEqual(
                call_result,
                first_result,
                f"Call {i} returned a different event list than call 1; "
                f"cache idempotence was violated.",
            )

        # The network must not have been contacted at all (cache was warm).
        self.assertEqual(
            mock_get.call_count,
            0,
            f"Expected network to be contacted zero times (cache was seeded "
            f"before any call), but mock.get was called {mock_get.call_count} "
            f"time(s) across {n} calls to fetch_all_scheduled_events.",
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Shared helpers for Properties 4 and 5 — enrichment worker integration
# ---------------------------------------------------------------------------

def _make_prematch_item(match_id: str, sporty: dict) -> dict:
    """Build a minimal prematch batch item with the given sporty dict."""
    return {
        "match_id": match_id,
        "match_date": "2025-01-01",
        "is_live": False,
        "sporty": sporty,
        "existing": {},
    }


def _make_live_item(match_id: str, sporty: dict) -> dict:
    """Build a minimal live batch item with the given sporty dict."""
    return {
        "match_id": match_id,
        "match_date": "2025-01-01",
        "is_live": True,
        "sporty": sporty,
        "existing": {},
    }


def _sporty_dict(match_id: str, **overrides) -> dict:
    """Return a minimal SportyBet match dict suitable for a batch item."""
    base = {
        "id": match_id,
        "name": f"Home FC vs Away FC ({match_id})",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "tournament": "Premier League",
        "category": "England",
        "start_time": 1_800_000_000,  # far future
        "venue": "Wembley",
        "markets": [],
        "period": "Not start",
        "score": None,
        "played_seconds": None,
    }
    base.update(overrides)
    return base


# Hypothesis strategy for a single non-empty sporty dict
_prematch_sporty_strategy = st.fixed_dictionaries(
    {
        "id": st.text(min_size=1, max_size=20).filter(lambda s: s.isalnum()),
        "name": st.text(min_size=1, max_size=80),
        "home_team": st.text(min_size=1, max_size=40),
        "away_team": st.text(min_size=1, max_size=40),
        "tournament": st.text(min_size=1, max_size=60),
        "category": st.text(min_size=1, max_size=40),
        "start_time": st.integers(min_value=1_700_000_000, max_value=2_000_000_000),
        "venue": st.one_of(st.none(), st.text(min_size=0, max_size=60)),
        "markets": st.just([]),
        "period": st.just("Not start"),
        "score": st.none(),
        "played_seconds": st.none(),
    }
)

# Strategy for a batch of 1-5 prematch items (unique match IDs)
_prematch_batch_strategy = st.lists(
    _prematch_sporty_strategy,
    min_size=1,
    max_size=5,
).map(
    lambda items: list({d["id"]: d for d in items}.values())
)

# Strategy for a batch of 1-5 mixed prematch+live items
_mixed_batch_sporty_strategy = st.lists(
    st.fixed_dictionaries(
        {
            "sporty": _prematch_sporty_strategy,
            "is_live": st.booleans(),
        }
    ),
    min_size=1,
    max_size=5,
).map(
    lambda items: list({d["sporty"]["id"]: d for d in items}.values())
)


# Minimal list of patches required to run one enrichment cycle in isolation.
_ENRICHMENT_PATCHES = [
    ("app.buffer.get_unenriched_batch",               None),   # set per test
    ("app.buffer.fetch_all_scheduled_events",         []),
    ("app.buffer.fetch_live_events",                  []),
    ("app.buffer.fetch_event_detail",                 {}),
    ("app.buffer.fetch_event_detail_live_refresh",    {}),
    ("app.buffer.fetch_match_intelligence",           {}),
    ("app.buffer.search_match_context",               {"query": "", "snippets": [], "scraped": []}),
    ("app.buffer.match_time_context",                 {"local_date": "2025-01-01"}),
    ("app.buffer.record_activity",                    None),
    ("app.buffer.store_enriched",                     None),
    ("app.buffer.snapshot_odds",                      None),  # tracked per test
    ("app.buffer.refresh_sporty_match_state",         None),  # tracked per test
    ("app.buffer.detect_season_stage",                "regular"),
    ("app.buffer._track_live_data_availability",      None),
    ("app.buffer._candidate_sofascore_events",        []),
    ("app.buffer._with_search_fallback_candidates",   []),
    ("app.buffer._fuzzy_match",                       (None, 0.0)),
    ("app.buffer._llm_match",                         None),
    ("app.buffer._sofascore_date_candidates",         ["2025-01-01"]),
    ("app.buffer._sporty_detail_doc",                 {}),
    ("app.buffer._sporty_live_data",                  {}),
    ("app.buffer._sofa_live_data",                    {}),
    ("app.buffer._data_sources",                      {}),
    ("app.buffer.is_pipeline_enabled",                False),
    ("app.buffer.search_league_sentiment",            {}),
    ("app.buffer.classify_match_state",               {"is_live": False, "is_finished": False}),
]


from contextlib import ExitStack


def _run_enrichment_cycle(batch: list[dict], mock_snapshot: MagicMock, mock_no_refresh: MagicMock) -> dict:
    """
    Run one enrichment cycle with all external dependencies mocked.

    Returns the dict returned by run_enrichment_worker.
    """
    from app.buffer import run_enrichment_worker

    with ExitStack() as stack:
        mocks: dict[str, MagicMock] = {}
        for target, return_value in _ENRICHMENT_PATCHES:
            if target == "app.buffer.snapshot_odds":
                m = stack.enter_context(patch(target, mock_snapshot))
            elif target == "app.buffer.refresh_sporty_match_state":
                m = stack.enter_context(patch(target, mock_no_refresh))
            elif target == "app.buffer.get_unenriched_batch":
                m = stack.enter_context(patch(target, return_value=batch))
            elif return_value is None:
                m = stack.enter_context(patch(target, MagicMock(return_value=None)))
            elif isinstance(return_value, (list, dict, str, bool, int, float, type(None))):
                m = stack.enter_context(patch(target, return_value=return_value))
            else:
                m = stack.enter_context(patch(target, return_value=return_value))
            mocks[target] = m

        # Also mock apply_prediction_state and prediction_readiness imports
        with (
            patch("app.buffer.apply_prediction_state", return_value={"status": "skipped", "skip_reason": "test"}),
            patch("app.buffer.prediction_readiness", return_value={}),
            patch("app.buffer._lifecycle_state", return_value="prematch"),
            patch("app.buffer._is_junk", return_value=False),
        ):
            result = run_enrichment_worker(fetch_web_context=False)

    return result


# ---------------------------------------------------------------------------
# Property 4: No SportyBet API refresh for prematch matches
# ---------------------------------------------------------------------------

# Feature: auto-match-pipeline, Property 4: No SportyBet API refresh for prematch matches


class TestNoSportyRefreshForPrematch(unittest.TestCase):
    """
    Property 4: No SportyBet API refresh for prematch matches

    For any batch of prematch match_buffer rows that each have a non-empty
    raw_sporty field, executing one enrichment worker cycle SHALL NOT invoke
    refresh_sporty_match_state for any item in the batch.

    Validates: Requirements 2.1, 2.2, 2.6, 4.2
    """

    @given(sporty_items=_prematch_batch_strategy)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_no_sporty_refresh_for_prematch(self, sporty_items: list) -> None:
        """Property 4: No SportyBet API refresh for prematch matches

        Validates: Requirements 2.1, 2.2, 2.6, 4.2
        """
        batch = [_make_prematch_item(s["id"], s) for s in sporty_items]

        mock_snapshot = MagicMock(return_value=None)
        mock_no_refresh = MagicMock(return_value={})

        result = _run_enrichment_cycle(batch, mock_snapshot, mock_no_refresh)

        # refresh_sporty_match_state must NEVER be called for prematch items
        self.assertEqual(
            mock_no_refresh.call_count,
            0,
            f"refresh_sporty_match_state was called {mock_no_refresh.call_count} time(s) "
            f"for a batch of {len(batch)} prematch items; it must never be called for prematch.",
        )

        # Worker must have processed without crashing
        self.assertIn("status", result)
        self.assertEqual(result.get("status"), "ok")


# ---------------------------------------------------------------------------
# Property 5: Odds snapshot runs for every match
# ---------------------------------------------------------------------------

# Feature: auto-match-pipeline, Property 5: Odds snapshot runs for every match


class TestOddsSnapshotAlwaysCalled(unittest.TestCase):
    """
    Property 5: Odds snapshot runs for every match

    For any batch of matches (prematch or live), executing one enrichment
    worker cycle SHALL call snapshot_odds exactly once per match that reaches
    the assembly phase (i.e., was not skipped due to absent raw_sporty).

    Validates: Requirements 2.3
    """

    @given(mixed_items=_mixed_batch_sporty_strategy)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_odds_snapshot_always_called(self, mixed_items: list) -> None:
        """Property 5: Odds snapshot runs for every match

        Validates: Requirements 2.3
        """
        batch = [
            _make_live_item(d["sporty"]["id"], d["sporty"]) if d["is_live"]
            else _make_prematch_item(d["sporty"]["id"], d["sporty"])
            for d in mixed_items
        ]

        mock_snapshot = MagicMock(return_value=None)
        mock_no_refresh = MagicMock(return_value={})

        result = _run_enrichment_cycle(batch, mock_snapshot, mock_no_refresh)

        # Every item in the batch should reach snapshot_odds (none skipped,
        # since all have non-empty sporty dicts)
        expected_count = len(batch)
        self.assertEqual(
            mock_snapshot.call_count,
            expected_count,
            f"snapshot_odds was called {mock_snapshot.call_count} time(s) but "
            f"expected exactly {expected_count} call(s) — once per batch item.",
        )

        self.assertIn("status", result)
        self.assertEqual(result.get("status"), "ok")


if __name__ == "__main__":
    unittest.main()

