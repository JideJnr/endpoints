"""
Shallow overview: pick a real match from the DB and run the full AI prediction pipeline.
Prints every step so we can see if the system makes sense.
"""
import sys, json, sqlite3, logging
from app.db import connect_db
sys.stdout.reconfigure(line_buffering=True)
logging.basicConfig(level=logging.WARNING)  # suppress noise, we print manually

# ── 1. Pick a match ──────────────────────────────────────────────────────────
conn = connect_db(timeout=10)

rows = conn.execute("""
    select match_id, match_date, raw_enriched
    from match_buffer
    where raw_enriched is not null
      and is_finished = 0
      and is_live = 0
    order by match_date asc
    limit 20
""").fetchall()

print(f"[DB] {len(rows)} upcoming unfinished matches found")

# Pick the first one that has a real name
chosen = None
for r in rows:
    try:
        doc = json.loads(r["raw_enriched"])
        name = doc.get("name") or doc.get("sportybet_name") or ""
        if name and " vs " in name:
            chosen = (r["match_id"], r["match_date"], doc)
            break
    except Exception:
        continue

if not chosen:
    # Fall back: just take the first row
    for r in rows:
        try:
            doc = json.loads(r["raw_enriched"])
            chosen = (r["match_id"], r["match_date"], doc)
            break
        except Exception:
            continue

if not chosen:
    print("[ERROR] No usable match found in match_buffer")
    sys.exit(1)

match_id, match_date, doc = chosen
name = doc.get("name") or doc.get("sportybet_name") or match_id
tournament = doc.get("tournament") or doc.get("league_name") or "Unknown"

print(f"\n{'='*60}")
print(f"  MATCH  : {name}")
print(f"  LEAGUE : {tournament}")
print(f"  DATE   : {match_date}")
print(f"  ID     : {match_id}")
print(f"{'='*60}\n")

# ── 2. Show what raw evidence exists ─────────────────────────────────────────
print("[Evidence check]")
print(f"  h2h data      : {'YES' if doc.get('h2h') or doc.get('h2h_matches') else 'NO'}")
print(f"  standings     : {'YES' if doc.get('standings') or (doc.get('sofascore_detail') or {}).get('standings') else 'NO'}")
print(f"  odds          : {'YES' if doc.get('odds') or doc.get('markets') else 'NO'}")
print(f"  sofascore_id  : {doc.get('sofascore_id') or (doc.get('sofascore_detail') or {}).get('id') or 'NONE'}")
print(f"  sportybet_id  : {doc.get('sportybet_id') or 'NONE'}")
existing_pred = doc.get("prediction")
print(f"  existing pred : {'YES — ' + str(existing_pred.get('outcome','?')) if existing_pred else 'NONE'}")

# ── 3. Check historical data in matches table ─────────────────────────────────
from app.ai_prediction_pipeline import _teams
home, away = _teams(doc)
print(f"\n[Teams] home='{home}'  away='{away}'")

if home and away:
    h_count = conn.execute(
        "select count(*) from matches where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?))",
        (home, home)
    ).fetchone()[0]
    a_count = conn.execute(
        "select count(*) from matches where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?))",
        (away, away)
    ).fetchone()[0]
    h2h_count = conn.execute(
        """select count(*) from matches where is_finished=1
           and (lower(home_team)=lower(?) and lower(away_team)=lower(?))
            or (lower(home_team)=lower(?) and lower(away_team)=lower(?))""",
        (home, away, away, home)
    ).fetchone()[0]
    print(f"  {home} finished matches in DB : {h_count}")
    print(f"  {away} finished matches in DB : {a_count}")
    print(f"  Direct H2H in DB             : {h2h_count}")

conn.close()

# ── 4. Check specialist weights ───────────────────────────────────────────────
from app.ai_prediction_pipeline import get_specialist_weights
weights = get_specialist_weights(league=tournament)
print(f"\n[Specialist weights for '{tournament}']")
for spec, w in weights.items():
    status = "TRUSTED" if w != 1.0 else "neutral (no data yet)"
    print(f"  {spec:<35} weight={w:.3f}  {status}")

# ── 5. Run the full AI pipeline ───────────────────────────────────────────────
print(f"\n[Running AI prediction pipeline...]\n")
from app.ai_prediction_pipeline import run_ai_prediction_with_fallback

result = run_ai_prediction_with_fallback(
    doc,
    match_id=match_id,
    match_date=match_date,
    allow_repeat=True,   # force re-run even if prediction exists
)

# ── 6. Print results ──────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RESULT SUMMARY")
print(f"{'='*60}")
print(f"  Status          : {result.get('status')}")
print(f"  Source          : {result.get('prediction_source')}")
print(f"  Competition ctx : {result.get('competition_analysis_used')} (key={result.get('competition_analysis_key')})")

pred = result.get("prediction") or {}
if pred:
    print(f"\n  --- PREDICTION ---")
    print(f"  Market          : {pred.get('market')}")
    print(f"  Outcome         : {pred.get('outcome')}")
    print(f"  Confidence      : {pred.get('confidence')}%")
    print(f"  Value bet       : {pred.get('value_bet')}")
    print(f"  BTTS            : {pred.get('btts')}")
    print(f"  Over 2.5        : {pred.get('over_2_5')}")
    print(f"\n  Key factors:")
    kf = pred.get("key_factors") or []
    if isinstance(kf, list):
        for f in kf:
            print(f"    • {f}")
    else:
        print(f"    {kf}")
    print(f"\n  Reasoning: {pred.get('reasoning')}")
else:
    print(f"\n  [NO PREDICTION] — deferred or failed")
    print(f"  Full result keys: {list(result.keys())}")

# ── 7. Show analyst findings ──────────────────────────────────────────────────
rc = result.get("reasoning_context") or pred.get("reasoning_context") or {}
analysts = rc.get("analysts") or []
if analysts:
    print(f"\n  --- ANALYST FINDINGS ---")
    for a in analysts:
        status_icon = "OK" if a.get("evidence_status") == "available" else "--"
        print(f"  [{status_icon}] {a['name']:<35} w={a.get('weight',1.0):.2f}")
        print(f"       {a['finding'][:120]}")
else:
    print(f"\n  [No analyst findings in result]")

print(f"\n{'='*60}")
print("  DONE")
print(f"{'='*60}\n")
