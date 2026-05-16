"""
Check prediction results against actual match outcomes.
Fetches final scores from SofaScore for all 10 predicted matches.
"""
from curl_cffi import requests as creq
import json

HEADERS = {
    "Accept": "*/*",
    "Referer": "https://www.sofascore.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

# The 10 matches we predicted
PREDICTIONS = [
    {
        "id": 14023956,
        "name": "Manchester United vs Nottingham Forest",
        "tournament": "Premier League",
        "picks": [
            {"selection": "Manchester United or draw protection", "market": "double_chance", "confidence": 85},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 85},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 78},
        ]
    },
    {
        "id": 13980063,
        "name": "AS Roma vs Lazio",
        "tournament": "Serie A",
        "picks": [
            {"selection": "AS Roma or draw protection", "market": "double_chance", "confidence": 85},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 85},
        ]
    },
    {
        "id": 13980075,
        "name": "Juventus vs Fiorentina",
        "tournament": "Serie A",
        "picks": [
            {"selection": "Juventus or draw protection", "market": "double_chance", "confidence": 85},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 85},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 78},
        ]
    },
    {
        "id": 13980068,
        "name": "Genoa vs Milan",
        "tournament": "Serie A",
        "picks": [
            {"selection": "Milan or draw protection", "market": "double_chance", "confidence": 76},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 76},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 69},
        ]
    },
    {
        "id": 15235564,
        "name": "Palmeiras vs Cruzeiro",
        "tournament": "Brasileirao",
        "picks": [
            {"selection": "Palmeiras or draw protection", "market": "double_chance", "confidence": 85},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 85},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 78},
        ]
    },
    {
        "id": 16177635,
        "name": "River Plate vs Rosario Central",
        "tournament": "Liga Profesional",
        "picks": [
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 81},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 74},
        ]
    },
    {
        "id": 14336198,
        "name": "Estoril Praia vs Benfica",
        "tournament": "Liga Portugal",
        "picks": [
            {"selection": "Benfica or draw protection", "market": "double_chance", "confidence": 76},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 76},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 69},
        ]
    },
    {
        "id": 14288901,
        "name": "Sporting CP vs Gil Vicente",
        "tournament": "Liga Portugal",
        "picks": [
            {"selection": "Sporting CP or draw protection", "market": "double_chance", "confidence": 85},
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 85},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 78},
        ]
    },
    {
        "id": 15858581,
        "name": "RSC Anderlecht vs KV Mechelen",
        "tournament": "Pro League",
        "picks": [
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 81},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 74},
            {"selection": "RSC Anderlecht double chance", "market": "double_chance", "confidence": 63},
        ]
    },
    {
        "id": 16166280,
        "name": "CD Guadalajara vs Cruz Azul",
        "tournament": "Liga MX",
        "picks": [
            {"selection": "Over 1.5 goals", "market": "goals", "confidence": 82},
            {"selection": "Over 2.5 goals", "market": "goals", "confidence": 75},
        ]
    },
]


def fetch_event(event_id):
    url = "https://www.sofascore.com/api/v1/event/{}".format(event_id)
    r = creq.get(url, headers=HEADERS, impersonate="chrome124", timeout=15)
    r.raise_for_status()
    return r.json().get("event", {})


def evaluate(pred, event):
    home_score = event.get("homeScore", {}).get("current")
    away_score = event.get("awayScore", {}).get("current")
    status_type = event.get("status", {}).get("type", "")
    status_desc = event.get("status", {}).get("description", "")
    winner_code = event.get("winnerCode")  # 1=home, 2=draw, 3=away

    if home_score is None or away_score is None:
        return None, None, status_desc

    total_goals = int(home_score) + int(away_score)
    home_name = event.get("homeTeam", {}).get("name", "Home")
    away_name = event.get("awayTeam", {}).get("name", "Away")

    results = []
    for pick in pred["picks"]:
        sel = pick["selection"].lower()
        hit = None

        if "over 2.5" in sel:
            hit = total_goals > 2
        elif "over 1.5" in sel:
            hit = total_goals > 1
        elif "or draw" in sel:
            # double chance — passes if home wins OR draw (for home picks), or away wins OR draw
            if home_name.lower() in sel or pred["name"].split(" vs ")[0].lower() in sel:
                hit = winner_code in (1, 2)  # home win or draw
            else:
                hit = winner_code in (2, 3)  # draw or away win
        elif "double chance" in sel:
            # Anderlecht double chance = home win or draw
            hit = winner_code in (1, 2)

        results.append({
            "selection": pick["selection"],
            "confidence": pick["confidence"],
            "hit": hit,
        })

    return home_score, away_score, status_desc, results, total_goals, winner_code


print("=" * 70)
print("PREDICTION RESULTS CHECK — May 16 2026")
print("=" * 70)

total_picks = 0
correct_picks = 0
pending_picks = 0
match_summary = []

for pred in PREDICTIONS:
    try:
        event = fetch_event(pred["id"])
        home_score = event.get("homeScore", {}).get("current", "?")
        away_score = event.get("awayScore", {}).get("current", "?")
        status_type = event.get("status", {}).get("type", "")
        status_desc = event.get("status", {}).get("description", "")
        winner_code = event.get("winnerCode")
        home_name = event.get("homeTeam", {}).get("name", "Home")
        away_name = event.get("awayTeam", {}).get("name", "Away")

        is_finished = status_type == "finished"
        is_live = status_type == "inprogress"

        try:
            total_goals = int(home_score) + int(away_score)
        except Exception:
            total_goals = None

        print("\n{} | {}".format(pred["name"], pred["tournament"]))
        print("  Score: {}-{} | Status: {}".format(home_score, away_score, status_desc))

        pick_results = []
        for pick in pred["picks"]:
            sel = pick["selection"].lower()
            hit = None

            if total_goals is not None and is_finished:
                if "over 2.5" in sel:
                    hit = total_goals > 2
                elif "over 1.5" in sel:
                    hit = total_goals > 1
                elif "or draw" in sel:
                    pred_name_lower = pred["name"].lower()
                    home_lower = pred_name_lower.split(" vs ")[0].strip()
                    if home_lower in sel:
                        hit = winner_code in (1, 2)
                    else:
                        hit = winner_code in (2, 3)
                elif "double chance" in sel:
                    hit = winner_code in (1, 2)

            icon = "✅" if hit is True else ("❌" if hit is False else "⏳")
            status_tag = "HIT" if hit is True else ("MISS" if hit is False else ("LIVE" if is_live else "PENDING"))
            print("  {} [{}%] {} — {}".format(icon, pick["confidence"], pick["selection"], status_tag))

            if hit is True:
                correct_picks += 1
            if hit is not None:
                total_picks += 1
            else:
                pending_picks += 1

            pick_results.append({"selection": pick["selection"], "confidence": pick["confidence"], "result": status_tag})

        match_summary.append({
            "match": pred["name"],
            "score": "{}-{}".format(home_score, away_score),
            "status": status_desc,
            "picks": pick_results,
        })

    except Exception as ex:
        print("\n{} | FETCH ERROR: {}".format(pred["name"], ex))

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print("Settled picks : {}/{}  ({:.0f}% hit rate)".format(
    correct_picks, total_picks,
    (correct_picks / total_picks * 100) if total_picks else 0
))
print("Pending/Live  : {}".format(pending_picks))

with open("results_check.json", "w", encoding="utf-8") as f:
    json.dump(match_summary, f, ensure_ascii=False, indent=2)
print("Full results saved to results_check.json")
