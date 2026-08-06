import sqlite3, json, csv
from collections import defaultdict, Counter

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
    SELECT signals_json, picks_json, result, selection, pick_type,
           confidence, league_name, country_name, source, prediction_mode,
           final_home, final_away
    FROM prediction_history WHERE result IN ('win','loss')
""")
rows = [dict(r) for r in cur.fetchall()]
conn.close()

wins  = [r for r in rows if r['result'] == 'win']
losses = [r for r in rows if r['result'] == 'loss']
total = len(rows)

def get_odds_profile(signals_json):
    try:
        for s in json.loads(signals_json or '[]'):
            if isinstance(s, dict):
                val = s.get('value', {})
                if isinstance(val, dict) and 'odds_profile' in val:
                    return val['odds_profile']
    except: pass
    return None

def odds_bucket(odd):
    if odd is None: return None
    if odd < 1.3:  return '1.01-1.29'
    if odd < 1.5:  return '1.30-1.49'
    if odd < 1.7:  return '1.50-1.69'
    if odd < 2.0:  return '1.70-1.99'
    if odd < 2.5:  return '2.00-2.49'
    if odd < 3.0:  return '2.50-2.99'
    if odd < 4.0:  return '3.00-3.99'
    if odd < 6.0:  return '4.00-5.99'
    return '6.00+'

print("=" * 60)
print("LOSS ANALYSIS")
print("=" * 60)
print(f"Total graded: {total} | Wins: {len(wins)} | Losses: {len(losses)}")
print(f"Overall win rate: {len(wins)/total*100:.1f}%  |  Loss rate: {len(losses)/total*100:.1f}%\n")

# --- BY PICK TYPE ---
print("=== LOSS RATE BY PICK TYPE ===")
pt_l = Counter(r['pick_type'] for r in losses)
pt_t = Counter(r['pick_type'] for r in rows)
for k, t in pt_t.most_common():
    l = pt_l[k]
    print(f"  {k or 'None'}: {l}/{t} losses = {l/t*100:.1f}% loss rate")

# --- BY SELECTION ---
print("\n=== LOSS RATE BY SELECTION (min 10) ===")
sl = defaultdict(int); st = defaultdict(int)
for r in rows:
    st[r['selection']] += 1
    if r['result'] == 'loss': sl[r['selection']] += 1
for s in sorted(st, key=lambda x: -st[x]):
    if st[s] >= 10:
        print(f"  {s}: {sl[s]}/{st[s]} = {sl[s]/st[s]*100:.1f}% loss rate")

# --- BY CONFIDENCE ---
print("\n=== LOSS RATE BY CONFIDENCE BUCKET ===")
cb_l = defaultdict(int); cb_t = defaultdict(int)
for r in rows:
    c = r['confidence'] or 0
    b = f"{(c//10)*10}-{(c//10)*10+9}%"
    cb_t[b] += 1
    if r['result'] == 'loss': cb_l[b] += 1
for b in sorted(cb_t):
    print(f"  {b}: {cb_l[b]}/{cb_t[b]} = {cb_l[b]/cb_t[b]*100:.1f}% loss rate")

# --- BY COUNTRY ---
print("\n=== LOSS RATE BY COUNTRY (min 10) ===")
col = defaultdict(int); cot = defaultdict(int)
for r in rows:
    cot[r['country_name']] += 1
    if r['result'] == 'loss': col[r['country_name']] += 1
for cn in sorted(cot, key=lambda x: -col[x]/cot[x] if cot[x] >= 10 else 0):
    if cot[cn] >= 10:
        print(f"  {col[cn]}/{cot[cn]} = {col[cn]/cot[cn]*100:.1f}% loss  [{cn}]")

# --- BY LEAGUE (worst, min 5) ---
print("\n=== HIGHEST LOSS RATE LEAGUES (min 5) ===")
ll = defaultdict(int); lt = defaultdict(int)
for r in rows:
    lt[r['league_name']] += 1
    if r['result'] == 'loss': ll[r['league_name']] += 1
rates = [(l, ll[l], lt[l], ll[l]/lt[l]*100) for l in lt if lt[l] >= 5]
for l, lo, t, rate in sorted(rates, key=lambda x: -x[3])[:20]:
    print(f"  {rate:.0f}% loss  {lo}/{t}  {l}")

# --- BY SOURCE ---
print("\n=== LOSS RATE BY SOURCE ===")
src_l = defaultdict(int); src_t = defaultdict(int)
for r in rows:
    src_t[r['source']] += 1
    if r['result'] == 'loss': src_l[r['source']] += 1
for s in sorted(src_t, key=lambda x: -src_t[x]):
    print(f"  {src_l[s]}/{src_t[s]} = {src_l[s]/src_t[s]*100:.1f}% loss  [{s}]")

# --- BY MODE ---
print("\n=== LOSS RATE BY PREDICTION MODE ===")
for mode in ('prematch', 'live'):
    l = sum(1 for r in rows if r['prediction_mode'] == mode and r['result'] == 'loss')
    t = sum(1 for r in rows if r['prediction_mode'] == mode)
    if t: print(f"  {mode}: {l}/{t} = {l/t*100:.1f}% loss rate")

# --- SCORE PATTERNS IN LOSSES ---
print("\n=== SCORE PATTERNS IN LOSSES (top 15) ===")
sc = Counter()
for r in losses:
    if r['final_home'] is not None and r['final_away'] is not None:
        sc[f"{r['final_home']}-{r['final_away']}"] += 1
for k, v in sc.most_common(15):
    print(f"  {k}: {v} ({v/len(losses)*100:.1f}%)")

# --- GOALS IN LOSSES ---
print("\n=== TOTAL GOALS IN LOSING MATCHES ===")
gc = Counter()
for r in losses:
    if r['final_home'] is not None and r['final_away'] is not None:
        gc[r['final_home'] + r['final_away']] += 1
for g in sorted(gc):
    print(f"  {g} goals: {gc[g]} ({gc[g]/len(losses)*100:.1f}%)")

# --- ODDS ANALYSIS FOR LOSSES ---
print("\n=== ODDS RANGES IN LOSSES vs WINS ===")
order = ['1.01-1.29','1.30-1.49','1.50-1.69','1.70-1.99','2.00-2.49','2.50-2.99','3.00-3.99','4.00-5.99','6.00+']

fav_l = defaultdict(int); fav_t = defaultdict(int)
home_l = defaultdict(int); home_t = defaultdict(int)
draw_l = defaultdict(int); draw_t = defaultdict(int)
away_l = defaultdict(int); away_t = defaultdict(int)
fav_side_l = defaultdict(int); fav_side_t = defaultdict(int)

for r in rows:
    p = get_odds_profile(r['signals_json'])
    if not p: continue
    is_loss = r['result'] == 'loss'
    for o, bl, bt in [
        (p.get('home_odds'), home_l, home_t),
        (p.get('draw_odds'), draw_l, draw_t),
        (p.get('away_odds'), away_l, away_t),
        (p.get('favorite_odds'), fav_l, fav_t),
    ]:
        b = odds_bucket(o)
        if b:
            bt[b] += 1
            if is_loss: bl[b] += 1
    side = p.get('favorite_side', 'unknown')
    fav_side_t[side] += 1
    if is_loss: fav_side_l[side] += 1

def print_odds_loss(label, bl, bt):
    print(f"\n  [{label}]")
    for b in order:
        if b in bt:
            print(f"    {b}: {bl[b]}/{bt[b]} = {bl[b]/bt[b]*100:.1f}% loss rate")

print_odds_loss("FAVORITE ODDS", fav_l, fav_t)
print_odds_loss("HOME ODDS", home_l, home_t)
print_odds_loss("DRAW ODDS", draw_l, draw_t)
print_odds_loss("AWAY ODDS", away_l, away_t)

print("\n  [FAVORITE SIDE]")
for s in sorted(fav_side_t, key=lambda x: -fav_side_l[x]/fav_side_t[x] if fav_side_t[x] else 0):
    l, t = fav_side_l[s], fav_side_t[s]
    print(f"    {s}: {l}/{t} = {l/t*100:.1f}% loss rate")
