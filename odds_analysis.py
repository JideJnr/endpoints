import sqlite3, json
from collections import defaultdict

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT signals_json, picks_json, selection, result FROM prediction_history WHERE result IN ('win','loss')")
rows = cur.fetchall()
conn.close()

def get_odds_profile(signals_json):
    try:
        signals = json.loads(signals_json or '[]')
        for s in signals:
            if isinstance(s, dict):
                val = s.get('value', {})
                if isinstance(val, dict) and 'odds_profile' in val:
                    return val['odds_profile']
    except:
        pass
    return None

def odds_bucket(odd):
    if odd is None: return None
    if odd < 1.3: return '1.01-1.29'
    if odd < 1.5: return '1.30-1.49'
    if odd < 1.7: return '1.50-1.69'
    if odd < 2.0: return '1.70-1.99'
    if odd < 2.5: return '2.00-2.49'
    if odd < 3.0: return '2.50-2.99'
    if odd < 4.0: return '3.00-3.99'
    if odd < 6.0: return '4.00-5.99'
    return '6.00+'

# Collect data
fav_buckets_w = defaultdict(int)
fav_buckets_t = defaultdict(int)
home_buckets_w = defaultdict(int)
home_buckets_t = defaultdict(int)
draw_buckets_w = defaultdict(int)
draw_buckets_t = defaultdict(int)
away_buckets_w = defaultdict(int)
away_buckets_t = defaultdict(int)

no_odds = 0
for row in rows:
    profile = get_odds_profile(row['signals_json'])
    if not profile:
        no_odds += 1
        continue

    result = row['result']
    home_o = profile.get('home_odds')
    draw_o = profile.get('draw_odds')
    away_o = profile.get('away_odds')
    fav_o  = profile.get('favorite_odds')

    for o, bw, bt in [
        (home_o, home_buckets_w, home_buckets_t),
        (draw_o, draw_buckets_w, draw_buckets_t),
        (away_o, away_buckets_w, away_buckets_t),
        (fav_o,  fav_buckets_w,  fav_buckets_t),
    ]:
        b = odds_bucket(o)
        if b:
            bt[b] += 1
            if result == 'win': bw[b] += 1

print(f"Records with odds: {len(rows) - no_odds} / {len(rows)}  (no odds: {no_odds})\n")

def print_table(label, bw, bt):
    print(f"=== {label} ===")
    order = ['1.01-1.29','1.30-1.49','1.50-1.69','1.70-1.99','2.00-2.49','2.50-2.99','3.00-3.99','4.00-5.99','6.00+']
    for b in order:
        if b in bt:
            w, t = bw[b], bt[b]
            print(f"  {b}:  {w}/{t} = {w/t*100:.1f}% win rate")
    print()

print_table("FAVORITE ODDS - WIN RATE", fav_buckets_w, fav_buckets_t)
print_table("HOME ODDS - WIN RATE", home_buckets_w, home_buckets_t)
print_table("DRAW ODDS - WIN RATE", draw_buckets_w, draw_buckets_t)
print_table("AWAY ODDS - WIN RATE", away_buckets_w, away_buckets_t)

# Favorite side breakdown
print("=== FAVORITE SIDE - WIN RATE ===")
side_w = defaultdict(int); side_t = defaultdict(int)
for row in rows:
    profile = get_odds_profile(row['signals_json'])
    if not profile: continue
    side = profile.get('favorite_side', 'unknown')
    side_t[side] += 1
    if row['result'] == 'win': side_w[side] += 1
for s in sorted(side_t, key=lambda x: -side_t[x]):
    w, t = side_w[s], side_t[s]
    print(f"  {s}: {w}/{t} = {w/t*100:.1f}%")
