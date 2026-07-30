"""
Step 1: Call enrich_buffered_match for the chosen match.
Step 2: Show every data layer that came back.
Step 3: Re-run the full AI prediction pipeline on the enriched doc.
"""
import sys, json, sqlite3, logging, time
from app.db import connect_db
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(level=logging.WARNING)

MATCH_ID = "sr:match:72946560"

# ── STEP 1: Enrich ────────────────────────────────────────────────────────────
print("=" * 60)
print("  STEP 1 — ENRICHMENT")
print("=" * 60)
t0 = time.monotonic()

from app.match_enrichment import enrich_buffered_match, MatchEnrichmentError
try:
    enrich_result = enrich_buffered_match(MATCH_ID, auto_predict=False)
    elapsed = time.monotonic() - t0
    print(f"\n  Enrichment completed in {elapsed:.1f}s")
    print(f"  matched_sofascore   : {enrich_result.get('matched_sofascore')}")
    print(f"  sofascore_id        : {enrich_result.get('sofascore_id')}")
    print(f"  fuzzy_score         : {enrich_result.get('fuzzy_score')}")
    print(f"  match_source        : {enrich_result.get('match_source')}")
    print(f"  has_detail          : {enrich_result.get('has_detail')}")
    print(f"  detail_source       : {enrich_result.get('sofascore_detail_source')}")
    print(f"  has_web_context     : {enrich_result.get('has_web_context')}")
    print(f"  has_sportradar      : {enrich_result.get('has_sportradar')}")
    print(f"  web_query           : {enrich_result.get('web_context_query')}")
    print(f"  sporty_refresh      : {enrich_result.get('sporty_refresh', {}).get('active')} / reason={enrich_result.get('sporty_refresh', {}).get('reason')}")
except MatchEnrichmentError as e:
    print(f"\n  [ENRICHMENT ERROR {e.status_code}] {e.detail}")
    print("  Falling back to buffered doc...")

# ── STEP 2: Load freshly enriched doc and inspect every layer ─────────────────
print("\n" + "=" * 60)
print("  STEP 2 — DATA LAYERS AFTER ENRICHMENT")
print("=" * 60)

conn = connect_db(timeout=10)
row = conn.execute("select raw_enriched from match_buffer where match_id=?", (MATCH_ID,)).fetchone()
conn.close()

doc = json.loads(row[0])
name = doc.get("name") or doc.get("sportybet_name") or MATCH_ID
home = doc.get("home_team") or ""
away = doc.get("away_team") or ""
if isinstance(home, dict): home = home.get("name", "")
if isinstance(away, dict): away = away.get("name", "")

print(f"\n  Match  : {name}")
print(f"  Teams  : {home}  vs  {away}")
print(f"  League : {doc.get('tournament')} / {doc.get('category')}")

# --- Odds ---
markets = doc.get("markets") or doc.get("sportybet_markets") or []
odds_1x2 = doc.get("odds_1x2") or {}
# Try to extract from markets if not in odds_1x2
if not odds_1x2 and markets:
    for m in markets:
        if m.get("id") == "1" or "1x2" in (m.get("name") or "").lower():
            sels = {s.get("name"): s.get("odds") for s in m.get("selections", [])}
            odds_1x2 = {"home": sels.get("Home") or sels.get("1"), "draw": sels.get("Draw") or sels.get("X"), "away": sels.get("Away") or sels.get("2")}
            break

print(f"\n  [Odds 1X2]")
print(f"    Home  : {odds_1x2.get('home')}")
print(f"    Draw  : {odds_1x2.get('draw')}")
print(f"    Away  : {odds_1x2.get('away')}")
print(f"    Total markets available: {len(markets)}")

# --- SofaScore detail ---
sd = doc.get("sofascore_detail") or {}
print(f"\n  [SofaScore Detail]")
print(f"    season          : {sd.get('season')}")
print(f"    round           : {sd.get('round')}")
print(f"    venue           : {sd.get('venue')}")
print(f"    home_manager    : {((sd.get('managers') or {}).get('home') or {}).get('name')}")
print(f"    away_manager    : {((sd.get('managers') or {}).get('away') or {}).get('name')}")
print(f"    pregame_form    : {sd.get('pregame_form') is not None}")
print(f"    lineups         : {sd.get('lineups') is not None}")
print(f"    statistics      : {sd.get('statistics') is not None}")

# --- H2H ---
h2h_raw = sd.get("h2h") or doc.get("h2h") or {}
print(f"\n  [H2H]")
if h2h_raw:
    events = h2h_raw.get("events") or []
    team_duel = h2h_raw.get("teamDuel") or h2h_raw.get("team_duel") or {}
    print(f"    events in h2h   : {len(events)}")
    print(f"    teamDuel        : {team_duel}")
    if events:
        print(f"    Last 3 meetings:")
        for ev in events[:3]:
            ht = (ev.get("homeTeam") or ev.get("home_team") or {})
            at = (ev.get("awayTeam") or ev.get("away_team") or {})
            sc = ev.get("homeScore") or ev.get("score") or {}
            hn = ht.get("name","?") if isinstance(ht,dict) else str(ht)
            an = at.get("name","?") if isinstance(at,dict) else str(at)
            hg = sc.get("current") or sc.get("home") or "?"
            ag = sc.get("away") or "?"
            if isinstance(sc, dict) and "current" in sc:
                # sofascore format: homeScore.current / awayScore.current
                asc = ev.get("awayScore") or {}
                ag = asc.get("current","?")
            print(f"      {hn} {hg} - {ag} {an}")
else:
    print("    NONE")

# --- Last matches ---
hlm = doc.get("home_last_matches") or []
alm = doc.get("away_last_matches") or []
print(f"\n  [Last Matches]")
print(f"    {home} last matches  : {len(hlm)} events")
print(f"    {away} last matches  : {len(alm)} events")

def summarise_last(matches, team_name, n=5):
    results = []
    for ev in matches[:n]:
        ht = ev.get("homeTeam") or ev.get("home_team") or {}
        at = ev.get("awayTeam") or ev.get("away_team") or {}
        hn = ht.get("name","?") if isinstance(ht,dict) else str(ht)
        an = at.get("name","?") if isinstance(at,dict) else str(at)
        hsc = ev.get("homeScore") or {}
        asc = ev.get("awayScore") or {}
        hg = hsc.get("current","?") if isinstance(hsc,dict) else "?"
        ag = asc.get("current","?") if isinstance(asc,dict) else "?"
        status = (ev.get("status") or {}).get("type","?")
        results.append(f"{hn} {hg}-{ag} {an} [{status}]")
    return results

print(f"\n    {home} recent 5:")
for r in summarise_last(hlm, home):
    print(f"      {r}")
print(f"\n    {away} recent 5:")
for r in summarise_last(alm, away):
    print(f"      {r}")

# --- Standings ---
standings = doc.get("standings") or []
print(f"\n  [Standings]  : {len(standings)} rows")
if standings:
    for row_s in standings[:5]:
        t = (row_s.get("team") or {})
        tn = t.get("name","?") if isinstance(t,dict) else str(t)
        print(f"    pos={row_s.get('position')} {tn} pts={row_s.get('points')}")

# --- Web context ---
wc = doc.get("web_context") or {}
snippets = wc.get("snippets") or []
print(f"\n  [Web Context]")
print(f"    query    : {wc.get('query')}")
print(f"    snippets : {len(snippets)}")
for s in snippets[:3]:
    text = s.get("snippet") or s.get("text") or str(s)
    print(f"      - {str(text)[:120]}")

# --- Sportradar ---
sr = doc.get("sportradar_detail") or {}
print(f"\n  [Sportradar]")
print(f"    available  : {sr.get('available')}")
print(f"    has_match  : {bool(sr.get('match'))}")
print(f"    standings  : {bool(sr.get('standings'))}")
if sr.get("error"):
    print(f"    error      : {sr.get('error')}")

# --- Competition context ---
cc = doc.get("competition_special") or doc.get("known_competition") or {}
print(f"\n  [Competition Context]  : {cc.get('key') or 'NONE'}")

# ── STEP 3: Re-predict with full enriched doc ─────────────────────────────────
print("\n" + "=" * 60)
print("  STEP 3 — AI PREDICTION (full enriched doc)")
print("=" * 60)

# Inject last_matches into h2h field so _step_h2h can use sofascore h2h events
if h2h_raw and not doc.get("h2h"):
    doc["h2h"] = h2h_raw

# Inject standings if present in sofascore_detail
if not doc.get("standings") and sd.get("standings"):
    doc["standings"] = sd["standings"]

print(f"\n  Running pipeline...")
t1 = time.monotonic()

from app.ai_prediction_pipeline import run_ai_prediction_with_fallback
result = run_ai_prediction_with_fallback(
    doc,
    match_id=MATCH_ID,
    match_date=doc.get("match_date"),
    allow_repeat=True,
)
elapsed2 = time.monotonic() - t1
print(f"  Pipeline completed in {elapsed2:.1f}s")

print(f"\n{'='*60}")
print(f"  PREDICTION RESULT")
print(f"{'='*60}")
print(f"  Status          : {result.get('status')}")
print(f"  Source          : {result.get('prediction_source')}")
print(f"  Competition ctx : {result.get('competition_analysis_used')}")

pred = result.get("prediction") or {}
if pred:
    print(f"\n  Market          : {pred.get('market')}")
    print(f"  Outcome         : {pred.get('outcome')}")
    print(f"  Confidence      : {pred.get('confidence')}%")
    print(f"  Value bet       : {pred.get('value_bet')}")
    print(f"  BTTS            : {pred.get('btts')}")
    print(f"  Over 2.5        : {pred.get('over_2_5')}")
    print(f"\n  Key factors:")
    for f in (pred.get("key_factors") or []):
        print(f"    * {f}")
    print(f"\n  Reasoning:")
    print(f"    {pred.get('reasoning')}")
else:
    print(f"\n  [NO PREDICTION] result keys: {list(result.keys())}")

rc = result.get("reasoning_context") or pred.get("reasoning_context") or {}
analysts = rc.get("analysts") or []
if analysts:
    print(f"\n  ANALYST FINDINGS")
    print(f"  {'-'*56}")
    for a in analysts:
        avail = "OK" if a.get("evidence_status") == "available" else "--"
        print(f"  [{avail}] {a['name']:<35} w={a.get('weight',1.0):.2f}")
        finding = a.get("finding","")
        # wrap at 100 chars
        while len(finding) > 100:
            print(f"       {finding[:100]}")
            finding = finding[100:]
        if finding:
            print(f"       {finding}")

print(f"\n{'='*60}")
print("  DONE")
print(f"{'='*60}\n")
