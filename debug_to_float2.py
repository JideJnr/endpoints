import sys, traceback
sys.path.insert(0, r'c:\Users\Victor\Documents\Personal Workstation\football\predictx')

out = []

# Simulate a minimal doc that would exercise the rules path
doc = {
    "sofascore_id": "12345",
    "sportybet_id": "99999",
    "tournament": "Premier League",
    "category": "England",
    "match_date": "2025-01-01",
    "sportybet_markets": [
        {
            "id": "1",
            "name": "Match Result",
            "selections": [
                {"name": "Home", "odds": "1.85"},
                {"name": "Draw", "odds": "3.50"},
                {"name": "Away", "odds": "4.20"},
            ]
        }
    ],
    "sofascore_detail": {
        "id": "12345",
        "home_team": {"id": 1, "name": "Team A"},
        "away_team": {"id": 2, "name": "Team B"},
        "home_last_matches": [
            {"status": {"type": "finished"}, "score": {"home": 2, "away": 1}},
            {"status": {"type": "finished"}, "score": {"home": 1, "away": 0}},
            {"status": {"type": "finished"}, "score": {"home": 0, "away": 0}},
        ],
        "away_last_matches": [
            {"status": {"type": "finished"}, "score": {"home": 1, "away": 2}},
            {"status": {"type": "finished"}, "score": {"home": 0, "away": 1}},
            {"status": {"type": "finished"}, "score": {"home": 2, "away": 2}},
        ],
        "status": {"type": "notstarted", "description": "Not started"},
    },
}

try:
    from app.ai.prediction_agent import predict_sofascore_event
    detail = doc["sofascore_detail"]
    result = predict_sofascore_event(
        detail,
        detail.get("home_last_matches", []),
        detail.get("away_last_matches", []),
    )
    out.append(f"predict_sofascore_event OK: {list(result.keys())}")
except NameError as e:
    out.append(f"NameError in predict_sofascore_event: {e}")
    out.append(traceback.format_exc())
except Exception as e:
    out.append(f"{type(e).__name__}: {e}")
    out.append(traceback.format_exc())

# Also try predict_sporty_match
try:
    from app.ai.prediction_agent import predict_sporty_match
    from app.storage.league_memory._helpers import build_sporty_doc
    sporty_doc = build_sporty_doc(doc)
    result2 = predict_sporty_match(sporty_doc)
    out.append(f"predict_sporty_match OK: {list(result2.keys())}")
except NameError as e:
    out.append(f"NameError in predict_sporty_match: {e}")
    out.append(traceback.format_exc())
except Exception as e:
    out.append(f"{type(e).__name__} in predict_sporty_match: {e}")

with open('debug_to_float2_out.txt', 'w') as f:
    f.write('\n'.join(out))

print("done")
