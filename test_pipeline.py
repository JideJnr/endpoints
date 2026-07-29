"""Quick smoke test of the DeepSeek multi-stage prediction pipeline."""
import sys
sys.stdout.reconfigure(line_buffering=True)

from app.ollama_pipeline import (
    is_ollama_available,
    run_form_specialist,
    run_h2h_specialist,
    run_odds_specialist,
    run_standings_specialist,
    run_model_specialist,
    run_ollama_pipeline,
)

print("=== DeepSeek Pipeline Test ===")
print(f"DeepSeek available: {is_ollama_available()}")

# Minimal test document
doc = {
    "name": "Arsenal vs Chelsea",
    "sportybet_name": "Arsenal vs Chelsea",
    "tournament": "Premier League",
    "category": "England",
    "sofascore_detail": {
        "home_team": {"name": "Arsenal", "id": 1},
        "away_team": {"name": "Chelsea", "id": 2},
        "home_last_matches": [
            {"status": {"type": "finished"}, "score": {"home": 2, "away": 1}, "home_team": {"id": 1}},
            {"status": {"type": "finished"}, "score": {"home": 1, "away": 1}, "home_team": {"id": 1}},
            {"status": {"type": "finished"}, "score": {"home": 3, "away": 0}, "home_team": {"id": 1}},
            {"status": {"type": "finished"}, "score": {"home": 0, "away": 2}, "home_team": {"id": 1}},
            {"status": {"type": "finished"}, "score": {"home": 2, "away": 2}, "home_team": {"id": 1}},
        ],
        "away_last_matches": [
            {"status": {"type": "finished"}, "score": {"home": 1, "away": 2}, "away_team": {"id": 2}},
            {"status": {"type": "finished"}, "score": {"home": 0, "away": 1}, "away_team": {"id": 2}},
            {"status": {"type": "finished"}, "score": {"home": 2, "away": 2}, "away_team": {"id": 2}},
            {"status": {"type": "finished"}, "score": {"home": 3, "away": 1}, "away_team": {"id": 2}},
            {"status": {"type": "finished"}, "score": {"home": 1, "away": 0}, "away_team": {"id": 2}},
        ],
        "h2h": {
            "team_duel": {"homeWins": 5, "awayWins": 3, "draws": 2}
        },
        "standings": [
            {"team": {"name": "Arsenal"}, "position": 1, "points": 75},
            {"team": {"name": "Chelsea"}, "position": 4, "points": 55},
        ],
    },
    "sportybet_markets": [
        {"id": "1", "name": "1x2", "selections": [
            {"name": "Home", "odds": "1.85"},
            {"name": "Draw", "odds": "3.50"},
            {"name": "Away", "odds": "4.20"},
        ]}
    ],
}

print("\n--- Testing individual specialists ---")

print("\n1. Form specialist:")
form = run_form_specialist(doc)
print(f"   Full result: {form}")
print(f"   Status: {form.get('status')}")
if form.get("status") == "success":
    r = form.get("result", {})
    print(f"   Advantage: {r.get('advantage')}, Confidence: {r.get('confidence')}%")
    print(f"   Reasoning: {r.get('reasoning')}")

print("\n2. H2H specialist:")
h2h = run_h2h_specialist(doc)
print(f"   Full result: {h2h}")
print(f"   Status: {h2h.get('status')}")
if h2h.get("status") == "success":
    r = h2h.get("result", {})
    print(f"   Advantage: {r.get('advantage')}, Confidence: {r.get('confidence')}%")

print("\n3. Odds specialist:")
odds = run_odds_specialist(doc)
print(f"   Full result: {odds}")
print(f"   Status: {odds.get('status')}")
if odds.get("status") == "success":
    r = odds.get("result", {})
    print(f"   Advantage: {r.get('advantage')}, Confidence: {r.get('confidence')}%")
    print(f"   Market signal: {r.get('market_signal')}")

print("\n4. Standings specialist:")
standings = run_standings_specialist(doc)
print(f"   Full result: {standings}")
print(f"   Status: {standings.get('status')}")
if standings.get("status") == "success":
    r = standings.get("result", {})
    print(f"   Advantage: {r.get('advantage')}, Confidence: {r.get('confidence')}%")

print("\n5. Model specialist:")
model = run_model_specialist(doc)
print(f"   Full result: {model}")
print(f"   Status: {model.get('status')}")

print("\n--- Running full pipeline ---")
result = run_ollama_pipeline(doc, attach_brain=False)
print(f"Pipeline status: {result.get('status')}")
print(f"Prediction: {result.get('recommendation')}")
print(f"Confidence: {result.get('confidence')}%")
print(f"Value bet: {result.get('value_bet')}")
print(f"Key factors: {result.get('key_factors')}")
print(f"Source: {result.get('source')}")

if result.get("aggregation"):
    agg = result["aggregation"]
    print(f"\nAggregation:")
    print(f"  Consensus: {agg.get('consensus_advantage')}")
    print(f"  Avg confidence: {agg.get('avg_confidence')}%")
    print(f"  Specialists run: {agg.get('specialist_count')}")

print("\n=== Test complete ===")
