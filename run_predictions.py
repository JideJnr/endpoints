import urllib.request
import json

picks = [
    (14023956, "Manchester United vs Nottingham Forest", "Premier League"),
    (13980063, "AS Roma vs Lazio", "Serie A"),
    (13980075, "Juventus vs Fiorentina", "Serie A"),
    (13980068, "Genoa vs Milan", "Serie A"),
    (15235564, "Palmeiras vs Cruzeiro", "Brasileirao"),
    (16177635, "River Plate vs Rosario Central", "Liga Profesional"),
    (14336198, "Estoril Praia vs Benfica", "Liga Portugal"),
    (14288901, "Sporting CP vs Gil Vicente", "Liga Portugal"),
    (15858581, "RSC Anderlecht vs KV Mechelen", "Pro League"),
    (16166280, "CD Guadalajara vs Cruz Azul", "Liga MX"),
]

results = []
for eid, name, league in picks:
    try:
        url = "http://localhost:8000/agent/sofascore/event/{}/prediction?include_history=true".format(eid)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        results.append(data)
        top_pick = data.get("picks", [{}])[0]
        sel = top_pick.get("selection", "?")
        conf = top_pick.get("confidence", "?")
        print("OK | {} | {} | {}%".format(name, sel, conf))
    except Exception as e:
        print("FAIL | {} | {}".format(name, e))

with open("predictions_output.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("Saved {} predictions.".format(len(results)))
