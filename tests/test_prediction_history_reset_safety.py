import sqlite3
from contextlib import contextmanager

from app.ai.prediction_agent import _common_opponent_edge, _h2h_edge, _apply_conflict_safety_gate
from app.storage.league_memory import crud


def _event(home, away, home_score, away_score, ts, tournament_id=99):
    return {
        "home_team": {"id": hash(home) % 10000, "name": home},
        "away_team": {"id": hash(away) % 10000, "name": away},
        "score": {"home": home_score, "away": away_score},
        "status": {"type": "finished"},
        "start_timestamp": ts,
        "tournament": {"id": tournament_id, "name": "Test League"},
        "season": {"id": "2026"},
    }


def test_common_opponent_uses_recent_same_competition_meeting_not_best_history():
    match = {
        "start_timestamp": 1_755_000_000,
        "tournament": {"id": 99, "name": "Test League"},
        "season": {"id": "2026"},
    }
    home_history = [
        _event("Home", "Shared", 0, 2, 1_754_000_000),
        _event("Home", "Shared", 5, 0, 1_620_000_000),
    ]
    away_history = [
        _event("Away", "Shared", 3, 0, 1_754_500_000),
    ]
    signals = []

    edge = _common_opponent_edge("Home", "Away", home_history, away_history, signals, [], match)

    assert edge < 0
    common = next(sig for sig in signals if sig["name"] == "common_opponent_edge")
    comparison = common["value"]["comparisons"][0]
    assert comparison["home_score"] == "0-2"
    assert common["value"]["competition_scope"] == "same_competition"
    assert common["value"]["selection_rule"] == "most_recent_meeting_per_opponent"


def test_h2h_aggregate_fallback_has_reduced_influence():
    signals = []
    edge = _h2h_edge({"h2h": {"teamDuel": {"homeWins": 8, "awayWins": 0, "draws": 0}}}, signals)

    assert edge == 4
    assert signals[0]["value"]["decay"] == "aggregate_fallback_reduced_influence"


def test_conflict_gate_caps_high_confidence_pick():
    picks = [{"type": "match_result", "selection": "Home Win", "confidence": 78, "reason": "strong edge"}]
    signals = [
        {"name": "h2h_edge", "impact": 4},
        {"name": "odds_edge", "impact": -5},
        {"name": "common_opponent_edge", "impact": 3},
    ]

    gated = _apply_conflict_safety_gate(picks, signals)

    assert gated[0]["confidence"] == 64
    assert any(sig["name"] == "directional_signal_conflict" for sig in signals)


def test_record_prediction_dedups_reset_match_by_name_league_pick(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        create table prediction_history (
            id integer primary key autoincrement,
            source text not null,
            match_id text not null,
            match_name text,
            league_name text,
            pick_type text,
            selection text,
            confidence integer,
            reason text,
            signals_json text not null,
            picks_json text not null,
            audit_json text not null default '{}',
            country_name text,
            sofascore_id text,
            sportybet_id text,
            prediction_mode text not null default 'prematch',
            data_source text,
            live_data_sources_json text not null default '[]',
            models_json text not null default '{}',
            signal_combination_key text,
            signal_combination_json text not null default '{}',
            live_context_json text not null default '{}',
            result text,
            created_at text not null default current_timestamp
        )
        """
    )

    @contextmanager
    def fake_conn(*args, **kwargs):
        yield conn

    monkeypatch.setattr(crud, "_init_db", lambda: None)
    monkeypatch.setattr(crud, "_conn", fake_conn)
    monkeypatch.setattr(crud, "_record_prediction_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(crud, "_record_prediction_candidates", lambda *args, **kwargs: None)
    monkeypatch.setattr(crud, "build_prediction_audit", lambda prediction: {})
    monkeypatch.setattr(crud, "live_context_from_prediction", lambda prediction: {})
    monkeypatch.setattr(crud, "build_signal_combination", lambda **kwargs: {"key": "k", "payload": {}})

    base = {
        "source": "sofascore",
        "match_id": "old-id",
        "name": "Home vs Away",
        "league_name": "Test League",
        "picks": [{"type": "match_result", "selection": "Home Win", "confidence": 70, "reason": "edge"}],
        "signals": [],
    }
    crud.record_prediction(base)
    crud.record_prediction({**base, "match_id": "new-id"})

    count = conn.execute("select count(*) from prediction_history").fetchone()[0]
    assert count == 1
