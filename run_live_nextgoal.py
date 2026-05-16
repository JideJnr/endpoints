"""
Live Next Goal Predictions
Fetches all live senior men's matches and runs the prediction engine on each.
Focuses on: next goal / over 0.5 live / live chase pressure signals.
"""
import urllib.request
import json
from curl_cffi import requests as creq

SOFASCORE_HEADERS = {
    "Accept": "*/*",
    "Referer": "https://www.sofascore.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP = ["women","u19","u20","u21","u23","u18","u17","reserve","srl","virtual","primavera","feminin","nwsl","wsl"]

def get_live_events():
    url = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/2026-05-16"
    r = creq.get(url, headers=SOFASCORE_HEADERS, impersonate="chrome124", timeout=20)
    events = r.json().get("events", [])
    live = []
    for e in events:
        t = e.get("tournament", {}).get("name", "")
        st = e.get("status", {}).get("type", "")
        if st == "inprogress" and not any(s in t.lower() for s in SKIP):
            live.append(e)
    return live

def run_prediction(event_id):
    url = "http://localhost:8000/agent/sofascore/event/{}/prediction?include_history=true".format(event_id)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def score_next_goal(pred):
    """Score how strong the next-goal signal is."""
    picks = pred.get("picks", [])
    signals = pred.get("signals", [])
    score = 0
    best_pick = None

    # Look for live goal picks
    for pk in picks:
        market = pk.get("market", "")
        sel = pk.get("selection", "")
        conf = pk.get("confidence", 0)
        if market in ("live_goals", "goals") and "over" in sel.lower():
            if best_pick is None or conf > best_pick.get("confidence", 0):
                best_pick = pk
            score = max(score, conf)

    # Boost from signals
    for s in signals:
        name = s.get("name", "")
        impact = s.get("impact", 0)
        if name in ("live_chase_pressure", "late_goal_league", "goal_pressure", "late_goal_memory"):
            score += abs(impact) * 0.3

    return round(score, 1), best_pick

def main():
    print("Fetching live matches...")
    live_events = get_live_events()
    print("Live senior men's matches: {}\n".format(len(live_events)))

    results = []
    for e in live_events:
        eid = e.get("id")
        home = e.get("homeTeam", {}).get("name", "?")
        away = e.get("awayTeam", {}).get("name", "?")
        tourn = e.get("tournament", {}).get("name", "?")
        hs = e.get("homeScore", {}).get("current", "?")
        aws = e.get("awayScore", {}).get("current", "?")
        name = "{} vs {}".format(home, away)

        try:
            pred = run_prediction(eid)
            ng_score, best_pick = score_next_goal(pred)
            picks = pred.get("picks", [])
            signals = pred.get("signals", [])
            minute = pred.get("minute")

            results.append({
                "id": eid,
                "name": name,
                "tournament": tourn,
                "score": "{}-{}".format(hs, aws),
                "minute": minute,
                "ng_score": ng_score,
                "best_pick": best_pick,
                "all_picks": picks,
                "signals": signals,
            })
            status = best_pick.get("selection", "no live pick") if best_pick else "no live pick"
            conf = best_pick.get("confidence", "-") if best_pick else "-"
            print("  OK | {} | {}-{} | {} | {}% | score={}".format(name, hs, aws, status, conf, ng_score))
        except Exception as ex:
            print("  FAIL | {} | {}".format(name, ex))

    # Sort by next-goal score descending
    results.sort(key=lambda x: x["ng_score"], reverse=True)

    # Save full output
    with open("live_nextgoal_output.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n" + "="*65)
    print("TOP NEXT GOAL PREDICTIONS (Live)")
    print("="*65)
    shown = 0
    for r in results:
        bp = r.get("best_pick")
        if not bp:
            continue
        shown += 1
        print("\n#{} | {} | {}".format(shown, r["name"], r["tournament"]))
        print("    Score: {} | Minute: {}".format(r["score"], r.get("minute", "?")))
        print("    PICK: [{}%] {}".format(bp.get("confidence","?"), bp.get("selection","?")))
        print("    Reason: {}".format(bp.get("reason","")))

        # Show supporting signals
        sigs = sorted(r["signals"], key=lambda x: abs(x.get("impact",0)), reverse=True)
        for s in sigs[:3]:
            print("    Signal: {} = {} (impact {})".format(s.get("name","?"), s.get("value","?"), s.get("impact","?")))

        if shown >= 10:
            break

    print("\nSaved full results to live_nextgoal_output.json")

if __name__ == "__main__":
    main()
