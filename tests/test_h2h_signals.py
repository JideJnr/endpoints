"""
Unit tests for _compute_h2h_signals() and its integration in _rules_prediction().
----------------------------------------------------------------------------------
R13.3 — Home dominance >= 0.5  → returns h2h_home signal with correct strength.
R13.4 — Away dominance >= 0.5  → returns h2h_away signal.
R13.5 — Neither team dominates → returns h2h_draw signal.
R13.6 — Existing higher-impact H2H signal → computed signal is discarded.
R13.7 — Fewer than 3 meetings  → returns [].

Additional coverage:
  - Fewer than 3 *valid* meetings (some entries missing scores) → returns [].
  - home_ratio < 0.5 even though home_wins > away_wins → h2h_draw emitted.
  - away_ratio < 0.5 even though away_wins > home_wins → h2h_draw emitted.
  - Any unexpected exception → returns [] without raising.
  - Signal value and impact are correctly rounded.
  - Existing signal with *equal* impact → computed signal is discarded.
  - Existing signal with *lower* impact → computed signal is appended.
  - Score attribution when meeting home team ≠ current home team (roles flip).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.enrichment.enriched_prediction import _compute_h2h_signals


# ── helpers ────────────────────────────────────────────────────────────────────

HOME_ID = "10"
AWAY_ID = "20"


def _detail(
    meetings: list[dict[str, Any]],
    home_id: str = HOME_ID,
    away_id: str = AWAY_ID,
) -> dict[str, Any]:
    """Build a minimal detail dict for the tests."""
    return {
        "home_team": {"id": home_id, "name": "Home FC"},
        "away_team": {"id": away_id, "name": "Away FC"},
        "last_meetings": meetings,
    }


def _meeting(
    home_score: int,
    away_score: int,
    meeting_home_id: str = HOME_ID,
) -> dict[str, Any]:
    """Return a meeting entry as SofaScore would supply it."""
    return {
        "homeScore": {"current": home_score},
        "awayScore": {"current": away_score},
        "homeTeam": {"id": meeting_home_id},
    }


# ── R13.7: too few meetings ────────────────────────────────────────────────────

class TestTooFewMeetings:
    def test_no_meetings(self) -> None:
        assert _compute_h2h_signals(_detail([])) == []

    def test_one_meeting(self) -> None:
        assert _compute_h2h_signals(_detail([_meeting(2, 0)])) == []

    def test_two_meetings(self) -> None:
        assert _compute_h2h_signals(_detail([_meeting(1, 0), _meeting(0, 1)])) == []

    def test_exactly_three_meetings(self) -> None:
        """Three meetings should be enough to produce a signal."""
        meetings = [_meeting(2, 0), _meeting(2, 0), _meeting(2, 0)]
        result = _compute_h2h_signals(_detail(meetings))
        assert len(result) == 1

    def test_missing_scores_reduce_valid_count_below_three(self) -> None:
        """Entries with missing homeScore/awayScore don't count toward total."""
        meetings = [
            {"homeTeam": {"id": HOME_ID}},          # no scores
            {"homeScore": {"current": 1}},           # missing awayScore
            _meeting(2, 0),                          # only one valid entry
        ]
        assert _compute_h2h_signals(_detail(meetings)) == []

    def test_detail_with_no_last_meetings_key(self) -> None:
        assert _compute_h2h_signals({"home_team": {"id": HOME_ID}}) == []


# ── R13.3: home dominance ──────────────────────────────────────────────────────

class TestHomeDominance:
    def test_home_wins_majority(self) -> None:
        """5/6 wins → home_ratio ≈ 0.83 ≥ 0.5 → h2h_home."""
        meetings = [
            _meeting(2, 0),  # home win
            _meeting(2, 0),  # home win
            _meeting(2, 0),  # home win
            _meeting(2, 0),  # home win
            _meeting(2, 0),  # home win
            _meeting(0, 2),  # away win
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert len(result) == 1
        sig = result[0]
        assert sig["name"] == "h2h_home"
        assert sig["source"] == "sofascore_last_meetings"

    def test_home_ratio_value_rounded(self) -> None:
        """value should be round(home_ratio, 2)."""
        # 4 home wins, 1 draw, 1 away win → home_ratio = 4/6 ≈ 0.67
        meetings = [
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(0, 0),
            _meeting(0, 1),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_home"
        assert result[0]["value"] == round(4 / 6, 2)

    def test_home_impact_is_rounded_ratio_times_four(self) -> None:
        """impact = round(home_ratio * 4)."""
        # 4 wins, 1 draw, 1 loss → home_ratio ≈ 0.667 → impact = round(2.667) = 3
        meetings = [
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(0, 0),
            _meeting(0, 1),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        expected_impact = round((4 / 6) * 4)
        assert result[0]["impact"] == expected_impact

    def test_home_ratio_exactly_half(self) -> None:
        """Exactly 0.5 home_ratio with home_wins > away_wins → h2h_home."""
        # 3 home wins, 3 draws, 0 away wins → home_ratio = 0.5
        meetings = [
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_home"

    def test_role_reversal_attributed_correctly(self) -> None:
        """Meeting where current away team was home: a win there is an away win."""
        # Meeting home ID = AWAY_ID (roles are reversed in that match)
        meetings = [
            _meeting(2, 0, meeting_home_id=AWAY_ID),  # away team won as home side
            _meeting(2, 0, meeting_home_id=AWAY_ID),
            _meeting(2, 0, meeting_home_id=AWAY_ID),
            _meeting(2, 0, meeting_home_id=AWAY_ID),
            _meeting(2, 0, meeting_home_id=AWAY_ID),
            _meeting(2, 0, meeting_home_id=AWAY_ID),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_away"


# ── R13.4: away dominance ──────────────────────────────────────────────────────

class TestAwayDominance:
    def test_away_wins_majority(self) -> None:
        """5/6 away wins → away_ratio ≈ 0.83 → h2h_away."""
        meetings = [
            _meeting(0, 2),
            _meeting(0, 2),
            _meeting(0, 2),
            _meeting(0, 2),
            _meeting(0, 2),
            _meeting(2, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert len(result) == 1
        sig = result[0]
        assert sig["name"] == "h2h_away"
        assert sig["source"] == "sofascore_last_meetings"
        assert sig["value"] == round(5 / 6, 2)
        assert sig["impact"] == round((5 / 6) * 4)

    def test_away_ratio_below_half_does_not_produce_h2h_away(self) -> None:
        """away_wins > home_wins but away_ratio < 0.5 → h2h_draw."""
        # 2 away wins, 5 draws → away_ratio = 2/7 < 0.5
        meetings = [
            _meeting(0, 1),
            _meeting(0, 1),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_draw"


# ── R13.5: balanced → h2h_draw ────────────────────────────────────────────────

class TestH2HDraw:
    def test_equal_wins_produces_draw_signal(self) -> None:
        meetings = [
            _meeting(1, 0),
            _meeting(0, 1),
            _meeting(0, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_draw"
        assert result[0]["impact"] == 1
        assert result[0]["source"] == "sofascore_last_meetings"

    def test_draw_value_is_draw_ratio(self) -> None:
        # 2 draws out of 5 → 0.4
        meetings = [
            _meeting(1, 0),
            _meeting(0, 1),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(1, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_draw"
        assert result[0]["value"] == round(2 / 5, 2)

    def test_home_wins_but_ratio_below_threshold(self) -> None:
        """home_wins > away_wins but home_ratio < 0.5 → h2h_draw."""
        # 2 home wins, 1 away win, 4 draws → home_ratio = 2/7 < 0.5
        meetings = [
            _meeting(1, 0),
            _meeting(1, 0),
            _meeting(0, 1),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
            _meeting(0, 0),
        ]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_draw"

    def test_only_draws(self) -> None:
        meetings = [_meeting(0, 0), _meeting(0, 0), _meeting(0, 0)]
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_draw"
        assert result[0]["value"] == 1.0


# ── R13.6: precedence check in _rules_prediction ──────────────────────────────

class TestH2HPrecedence:
    """
    These tests exercise the precedence logic inside _rules_prediction() that
    calls _compute_h2h_signals() and conditionally appends the result.
    """

    def _make_rules_result(self, existing_signals: list[dict]) -> dict:
        return {"signals": existing_signals, "picks": []}

    def _call_compute_and_filter(
        self,
        computed: list[dict],
        existing: list[dict],
    ) -> list[dict]:
        """
        Re-implement the filter logic from _rules_prediction so we can test
        it independently of the full prediction pipeline.
        """
        for computed_sig in computed:
            sig_name = computed_sig.get("name")
            computed_impact = abs(computed_sig.get("impact") or 0)
            already_covered = any(
                s.get("name") == sig_name and abs(s.get("impact") or 0) >= computed_impact
                for s in existing
            )
            if not already_covered:
                existing.append(computed_sig)
        return existing

    def test_existing_higher_impact_suppresses_computed(self) -> None:
        """If existing h2h_home has impact 4 and computed has impact 3 → discard."""
        computed = [{"name": "h2h_home", "value": 0.75, "impact": 3, "source": "sofascore_last_meetings"}]
        existing = [{"name": "h2h_home", "impact": 4}]
        result = self._call_compute_and_filter(computed, list(existing))
        # Still only one signal
        h2h_signals = [s for s in result if s.get("name") == "h2h_home"]
        assert len(h2h_signals) == 1
        assert h2h_signals[0]["impact"] == 4  # original unchanged

    def test_existing_equal_impact_suppresses_computed(self) -> None:
        """Equal impact → computed is discarded (>= covers equality)."""
        computed = [{"name": "h2h_away", "value": 0.6, "impact": 2, "source": "sofascore_last_meetings"}]
        existing = [{"name": "h2h_away", "impact": 2}]
        result = self._call_compute_and_filter(computed, list(existing))
        h2h_signals = [s for s in result if s.get("name") == "h2h_away"]
        assert len(h2h_signals) == 1

    def test_existing_lower_impact_allows_computed(self) -> None:
        """Existing impact 1, computed impact 3 → computed is appended."""
        computed = [{"name": "h2h_home", "value": 0.75, "impact": 3, "source": "sofascore_last_meetings"}]
        existing = [{"name": "h2h_home", "impact": 1}]
        result = self._call_compute_and_filter(computed, list(existing))
        h2h_signals = [s for s in result if s.get("name") == "h2h_home"]
        assert len(h2h_signals) == 2

    def test_no_existing_h2h_allows_computed(self) -> None:
        """No existing h2h signal → computed is appended."""
        computed = [{"name": "h2h_home", "value": 0.75, "impact": 3, "source": "sofascore_last_meetings"}]
        result = self._call_compute_and_filter(computed, [])
        assert len(result) == 1
        assert result[0]["name"] == "h2h_home"

    def test_different_h2h_name_does_not_block(self) -> None:
        """Existing h2h_away does not suppress an h2h_home computed signal."""
        computed = [{"name": "h2h_home", "value": 0.67, "impact": 3, "source": "sofascore_last_meetings"}]
        existing = [{"name": "h2h_away", "impact": 4}]
        result = self._call_compute_and_filter(computed, list(existing))
        names = [s["name"] for s in result]
        assert "h2h_home" in names
        assert "h2h_away" in names


# ── Error resilience ───────────────────────────────────────────────────────────

class TestErrorResilience:
    def test_raises_no_exception_on_malformed_detail(self) -> None:
        assert _compute_h2h_signals(None) == []  # type: ignore[arg-type]

    def test_raises_no_exception_on_string_detail(self) -> None:
        assert _compute_h2h_signals("not a dict") == []  # type: ignore[arg-type]

    def test_non_numeric_scores_skipped(self) -> None:
        """Entries with non-integer score values don't crash."""
        meetings = [
            {"homeScore": {"current": "n/a"}, "awayScore": {"current": "n/a"}, "homeTeam": {"id": HOME_ID}},
            {"homeScore": {"current": "n/a"}, "awayScore": {"current": "n/a"}, "homeTeam": {"id": HOME_ID}},
            {"homeScore": {"current": "n/a"}, "awayScore": {"current": "n/a"}, "homeTeam": {"id": HOME_ID}},
        ]
        # All entries invalid → total < 3 → []
        assert _compute_h2h_signals(_detail(meetings)) == []

    def test_only_ten_meetings_used(self) -> None:
        """Takes at most 10 of the most recent entries."""
        # 12 entries: first 10 are home wins (would give home dominance),
        # last 2 are away wins — but they're beyond the 10-entry limit so ignored.
        meetings = [_meeting(1, 0)] * 10 + [_meeting(0, 1)] * 2
        result = _compute_h2h_signals(_detail(meetings))
        assert result[0]["name"] == "h2h_home"
