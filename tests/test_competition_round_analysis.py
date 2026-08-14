"""
Unit tests for competition round analysis wiring (R14).

---------------------------------------------------------------------------
R14.2  — apply_known_competition_context() attaches competition_round_analysis
          when a recent analysis row (age <= 7 days) exists.
R14.3  — Does NOT attach when the row is absent or older than 7 days.
R14.5  — _rules_prediction() sets audit["competition_context_applied"] = True
          when competition_round_analysis is present.
R14.6  — A clear form leader (one team in top 2 of competition_round_analysis)
          results in a competition_momentum signal with impact = +1.
R14.7  — No clear form leader (neither team found, or both found) →
          no competition_momentum signal emitted.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_analysis_conn(
    competition_key: str = "premier-league",
    analysis_text: str = "{}",
    generated_at: str | None = None,
) -> sqlite3.Connection:
    """Return an in-memory connection pre-populated with one competition_analysis row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS competition_analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_key TEXT    NOT NULL,
            round_name      TEXT    NOT NULL DEFAULT '',
            analysis_text   TEXT    NOT NULL DEFAULT '{}',
            model_used      TEXT    NOT NULL DEFAULT '',
            match_count     INTEGER NOT NULL DEFAULT 0,
            matchday_date   TEXT    NOT NULL DEFAULT '',
            generated_at    TEXT    NOT NULL DEFAULT current_timestamp
        )
        """
    )
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO competition_analysis
            (competition_key, round_name, analysis_text, model_used,
             match_count, matchday_date, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (competition_key, "Round 10", analysis_text, "gpt-4", 5, "2026-08-10", generated_at),
    )
    conn.commit()
    return conn


@contextmanager
def _patch_competition_special_db(conn: sqlite3.Connection) -> Iterator[None]:
    """Patch db_conn and init helpers inside competition_special so they use *conn*."""

    @contextmanager
    def _mock_db_conn(timeout: int = 5) -> Iterator[sqlite3.Connection]:
        yield conn

    def _mock_init_analysis_table(c: sqlite3.Connection) -> None:
        pass  # table already created in _make_analysis_conn

    with (
        patch("app.competition.competition_special.db_conn", _mock_db_conn),
        patch(
            "app.competition.competition_special._init_db", lambda: None
        ),
        patch(
            "app.competition.competition_analyser.init_competition_analysis_table",
            _mock_init_analysis_table,
        ),
    ):
        yield


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — apply_known_competition_context() attachment behaviour
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyKnownCompetitionContextAnalysisAttachment:
    """
    Tests for the R14.1–R14.3 block inside apply_known_competition_context().
    We only exercise the competition_round_analysis attachment; the rest of the
    function (intelligence context, team watchers …) is not under test here.

    Because apply_known_competition_context() uses a lazy import inside the
    try/except block, we patch the functions at their *source* module
    (app.competition.competition_analyser) rather than on competition_special.
    """

    def _minimal_doc(self, tournament_id: int = 17) -> dict[str, Any]:
        """Build the minimal doc that resolves to a known competition key."""
        return {
            "sofascore_event": {
                "tournament": {"id": tournament_id, "name": "Premier League"},
            },
            "sofascore_detail": {},
        }

    def _standard_patches(self) -> list:
        """Return the patches common to all tests in this class."""
        return [
            patch(
                "app.competition.competition_special._competition_intelligence_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special._match_importance_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special.init_competition_tables",
                lambda c: None,
            ),
            patch("app.competition.competition_special._init_db", lambda: None),
        ]

    def test_attaches_analysis_when_recent(self) -> None:
        """R14.2 — row with generated_at within 7 days is attached."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        analysis_text = json.dumps({"top_table": [{"team": "Arsenal"}, {"team": "Chelsea"}]})
        fake_row = {
            "competition_key": "premier-league",
            "round_name": "Round 10",
            "analysis_text": analysis_text,
            "generated_at": recent_ts,
        }

        from app.competition.competition_special import apply_known_competition_context

        doc = self._minimal_doc(tournament_id=17)

        @contextmanager
        def _mock_db_conn(timeout: int = 5) -> Iterator[MagicMock]:
            yield MagicMock()  # connection not actually used when get_latest_analysis is patched

        with (
            patch(
                "app.competition.competition_special._competition_intelligence_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special._match_importance_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special.init_competition_tables",
                lambda c: None,
            ),
            patch("app.competition.competition_special._init_db", lambda: None),
            patch("app.competition.competition_special.db_conn", _mock_db_conn),
            patch(
                "app.competition.competition_analyser.init_competition_analysis_table",
                lambda c: None,
            ),
            patch(
                "app.competition.competition_analyser.get_latest_analysis",
                return_value=fake_row,
            ),
        ):
            result = apply_known_competition_context(doc)

        assert "competition_round_analysis" in result, (
            "Expected competition_round_analysis to be attached for a recent analysis row"
        )
        attached = result["competition_round_analysis"]
        assert attached["competition_key"] == "premier-league"

    def test_does_not_attach_when_stale(self) -> None:
        """R14.3 — row older than 7 days must NOT be attached."""
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        fake_row = {
            "competition_key": "premier-league",
            "round_name": "Round 9",
            "analysis_text": "{}",
            "generated_at": stale_ts,
        }

        from app.competition.competition_special import apply_known_competition_context

        doc = self._minimal_doc(tournament_id=17)

        @contextmanager
        def _mock_db_conn(timeout: int = 5) -> Iterator[MagicMock]:
            yield MagicMock()

        with (
            patch(
                "app.competition.competition_special._competition_intelligence_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special._match_importance_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special.init_competition_tables",
                lambda c: None,
            ),
            patch("app.competition.competition_special._init_db", lambda: None),
            patch("app.competition.competition_special.db_conn", _mock_db_conn),
            patch(
                "app.competition.competition_analyser.init_competition_analysis_table",
                lambda c: None,
            ),
            patch(
                "app.competition.competition_analyser.get_latest_analysis",
                return_value=fake_row,
            ),
        ):
            result = apply_known_competition_context(doc)

        assert "competition_round_analysis" not in result, (
            "Expected competition_round_analysis NOT to be attached for a stale row"
        )

    def test_does_not_attach_when_no_row(self) -> None:
        """R14.3 — no row in DB should not attach anything and not raise."""
        from app.competition.competition_special import apply_known_competition_context

        doc = self._minimal_doc(tournament_id=17)

        @contextmanager
        def _mock_db_conn(timeout: int = 5) -> Iterator[MagicMock]:
            yield MagicMock()

        with (
            patch(
                "app.competition.competition_special._competition_intelligence_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special._match_importance_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special.init_competition_tables",
                lambda c: None,
            ),
            patch("app.competition.competition_special._init_db", lambda: None),
            patch("app.competition.competition_special.db_conn", _mock_db_conn),
            patch(
                "app.competition.competition_analyser.init_competition_analysis_table",
                lambda c: None,
            ),
            patch(
                "app.competition.competition_analyser.get_latest_analysis",
                return_value=None,
            ),
        ):
            result = apply_known_competition_context(doc)

        assert "competition_round_analysis" not in result

    def test_does_not_raise_on_db_error(self) -> None:
        """R14.3 — an exception during DB access must be swallowed gracefully."""
        from app.competition.competition_special import apply_known_competition_context

        doc = self._minimal_doc(tournament_id=17)

        @contextmanager
        def _mock_db_conn(timeout: int = 5) -> Iterator[MagicMock]:
            yield MagicMock()

        def _exploding_get_latest(key: str, c: Any) -> None:
            raise RuntimeError("simulated DB failure")

        with (
            patch(
                "app.competition.competition_special._competition_intelligence_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special._match_importance_context",
                return_value={},
            ),
            patch(
                "app.competition.competition_special.init_competition_tables",
                lambda c: None,
            ),
            patch("app.competition.competition_special._init_db", lambda: None),
            patch("app.competition.competition_special.db_conn", _mock_db_conn),
            patch(
                "app.competition.competition_analyser.init_competition_analysis_table",
                lambda c: None,
            ),
            patch(
                "app.competition.competition_analyser.get_latest_analysis",
                _exploding_get_latest,
            ),
        ):
            result = apply_known_competition_context(doc)  # must not raise

        assert "competition_round_analysis" not in result


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — _rules_prediction() competition momentum signal injection
# ══════════════════════════════════════════════════════════════════════════════

def _minimal_rules_result(extra_signals: list[dict] | None = None) -> dict[str, Any]:
    """Fake return value from predict_sofascore_event."""
    return {
        "picks": [{"type": "match_result", "selection": "home win", "confidence": 65}],
        "signals": list(extra_signals or []),
        "audit": {},
    }


def _detail_with_teams(
    home_name: str = "Arsenal",
    away_name: str = "Chelsea",
    last_meetings: list | None = None,
) -> dict[str, Any]:
    """Minimal sofascore_detail for rules prediction tests."""
    return {
        "home_team": {"id": 1, "name": home_name},
        "away_team": {"id": 2, "name": away_name},
        "home_last_matches": [],
        "away_last_matches": [],
        "last_meetings": last_meetings or [],
        "status": {"type": "notstarted", "description": "Not started"},
    }


def _doc_with_cra(analysis_text: str, detail: dict | None = None) -> dict[str, Any]:
    """Build a doc with competition_round_analysis pre-attached."""
    return {
        "competition_round_analysis": {
            "competition_key": "premier-league",
            "analysis_text": analysis_text,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "sofascore_detail": detail or _detail_with_teams(),
    }


class TestRulesPredictionCompetitionMomentum:
    """
    Tests for the R14.4–R14.7 block inside _rules_prediction().
    predict_sofascore_event is stubbed to return a fixed base result so we can
    isolate the competition_round_analysis injection logic.
    """

    def test_competition_context_applied_flag_set_when_cra_present(self) -> None:
        """R14.5 — audit["competition_context_applied"] = True whenever CRA is present."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps({"top_table": []})
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams()

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        assert result.get("audit", {}).get("competition_context_applied") is True, (
            "Expected audit['competition_context_applied'] = True when CRA is present"
        )

    def test_competition_context_applied_not_set_when_cra_absent(self) -> None:
        """No audit flag when competition_round_analysis key is not on the doc."""
        from app.enrichment.enriched_prediction import _rules_prediction

        doc: dict[str, Any] = {}
        detail = _detail_with_teams()

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        assert not result.get("audit", {}).get("competition_context_applied"), (
            "Expected no competition_context_applied flag when CRA is absent"
        )

    def test_momentum_signal_home_when_home_team_in_top2(self) -> None:
        """R14.6 — home team in top 2 → competition_momentum signal with impact = +1."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps(
            {"top_table": [{"team": "Arsenal"}, {"team": "Liverpool"}]}
        )
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams(home_name="Arsenal", away_name="Chelsea")

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 1, "Expected exactly one competition_momentum signal"
        sig = momentum_signals[0]
        assert sig["impact"] == 1
        assert (sig.get("value") or {}).get("direction") == "home"

    def test_momentum_signal_away_when_away_team_in_top2(self) -> None:
        """R14.6 — away team in top 2 (not home) → competition_momentum signal for away."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps(
            {"top_table": [{"team": "Chelsea"}, {"team": "Tottenham"}]}
        )
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams(home_name="Arsenal", away_name="Chelsea")

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 1
        assert (momentum_signals[0].get("value") or {}).get("direction") == "away"

    def test_no_momentum_signal_when_neither_team_in_top2(self) -> None:
        """R14.7 — neither team in top 2 → no competition_momentum signal."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps(
            {"top_table": [{"team": "Manchester City"}, {"team": "Liverpool"}]}
        )
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams(home_name="Arsenal", away_name="Chelsea")

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 0, (
            "Expected no competition_momentum signal when neither team appears in top 2"
        )

    def test_no_momentum_signal_when_both_teams_in_top2(self) -> None:
        """R14.7 — both teams in top 2 is not a 'clear' leader → no signal."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps(
            {"top_table": [{"team": "Arsenal"}, {"team": "Chelsea"}]}
        )
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams(home_name="Arsenal", away_name="Chelsea")

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 0, (
            "Expected no signal when both teams appear in top 2 (no clear leader)"
        )

    def test_no_momentum_signal_when_top_table_empty(self) -> None:
        """R14.7 — empty top_table → no competition_momentum signal."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps({"top_table": []})
        doc = _doc_with_cra(analysis_text)
        detail = _detail_with_teams()

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 0

    def test_no_momentum_signal_when_analysis_text_invalid_json(self) -> None:
        """R14.7 — malformed analysis_text JSON must not raise and must not emit signal."""
        from app.enrichment.enriched_prediction import _rules_prediction

        doc = _doc_with_cra("NOT_VALID_JSON")
        detail = _detail_with_teams()

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)  # must not raise

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 0

    def test_partial_name_match_triggers_signal(self) -> None:
        """Partial name matching — 'Manchester United' matches entry 'Manchester United FC'."""
        from app.enrichment.enriched_prediction import _rules_prediction

        analysis_text = json.dumps(
            {"top_table": [{"team": "Arsenal FC"}, {"team": "Chelsea FC"}]}
        )
        doc = _doc_with_cra(analysis_text)
        # "Arsenal" is a substring of "Arsenal FC" → should match
        detail = _detail_with_teams(home_name="Arsenal", away_name="Tottenham")

        with patch(
            "app.enrichment.enriched_prediction.predict_sofascore_event",
            return_value=_minimal_rules_result(),
        ):
            result = _rules_prediction(doc, detail)

        momentum_signals = [
            s for s in (result.get("signals") or []) if s.get("name") == "competition_momentum"
        ]
        assert len(momentum_signals) == 1
        assert (momentum_signals[0].get("value") or {}).get("direction") == "home"
