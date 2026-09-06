from app.ai.prediction_agent import _table_edge
from app.enrichment.similar_matches import _development_competition


def test_cross_competition_table_positions_do_not_create_side_edge():
    event = {
        "tournament": {"name": "LaLiga"},
        "pregame_form": {"home_team": {"position": 18}, "away_team": {"position": 10}},
        "home_last_matches": [{"status": {"type": "finished"}, "tournament": {"name": "LaLiga"}}] * 3,
        "away_last_matches": [{"status": {"type": "finished"}, "tournament": {"name": "LaLiga 2"}}] * 3,
    }
    signals = []
    assert _table_edge(event, signals, []) == 0.0
    assert signals[-1]["name"] == "cross_competition_table_context"


def test_development_competitions_are_identified_for_similarity_filtering():
    assert _development_competition("Spain U19 Division de Honor Juvenil")
    assert _development_competition("Premier League 2")
    assert not _development_competition("LaLiga")
