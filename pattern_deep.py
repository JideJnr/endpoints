import sqlite3
from collections import Counter, defaultdict

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT league_name, country_name, pick_type, selection, confidence, prediction_mode, source, result, final_home, final_away FROM prediction_history WHERE result IN ('win','loss','void')")
rows = [dict(r) for r in cur.fetchall()]
conn.close()

wins = [r for r in rows if r['result'] == 'win']
losses = [r for r in rows if r['result'] == 'loss']
graded = wins + losses

print(f"Total graded (excl void): {len(graded)} | Wins: {len(wins)} | Losses: {len(losses)}")
print(f"Overall win rate: {len(wins)/len(graded)*100:.1f}%")

print("\n=== WIN RATE BY LEAGUE (min 5 graded) ===")
lw = defaultdict(int); lt = defaultdict(int)
for r in graded:
    lt[r['league_name']] += 1
    if r['result'] == 'win': lw[r['league_name']] += 1
rates = [(l, lw[l], lt[l], lw[l]/lt[l]*100) for l in lt if lt[l] >= 5]
for l, w, t, rate in sorted(rates, key=lambda x: -x[3])[:20]:
    print(f"  {rate:.0f}%  {w}/{t}  {l}")

print("\n=== WIN RATE BY LEAGUE (worst, min 5) ===")
for l, w, t, rate in sorted(rates, key=lambda x: x[3])[:10]:
    print(f"  {rate:.0f}%  {w}/{t}  {l}")

print("\n=== LIVE vs PREMATCH ===")
for mode in ('prematch', 'live'):
    w = sum(1 for r in graded if r['prediction_mode'] == mode and r['result'] == 'win')
    t = sum(1 for r in graded if r['prediction_mode'] == mode)
    if t: print(f"  {mode}: {w}/{t} = {w/t*100:.1f}%")

print("\n=== WIN RATE BY SELECTION (min 10) ===")
sw = defaultdict(int); st = defaultdict(int)
for r in graded:
    st[r['selection']] += 1
    if r['result'] == 'win': sw[r['selection']] += 1
for s in sorted(st, key=lambda x: -st[x]):
    if st[s] >= 10:
        print(f"  {sw[s]}/{st[s]} = {sw[s]/st[s]*100:.1f}%  [{s}]")

print("\n=== TOTAL GOALS IN WINNING MATCHES ===")
gc = Counter()
for r in wins:
    if r['final_home'] is not None and r['final_away'] is not None:
        gc[r['final_home'] + r['final_away']] += 1
for g in sorted(gc):
    print(f"  {g} goals: {gc[g]} ({gc[g]/len(wins)*100:.1f}%)")

print("\n=== CONFIDENCE vs WIN RATE (fine-grained) ===")
cw = defaultdict(int); ct = defaultdict(int)
for r in graded:
    c = r['confidence'] or 0
    ct[c] += 1
    if r['result'] == 'win': cw[c] += 1
for c in sorted(ct):
    print(f"  conf {c}: {cw[c]}/{ct[c]} = {cw[c]/ct[c]*100:.0f}%")

print("\n=== WIN RATE BY COUNTRY (min 10) ===")
cow = defaultdict(int); cot = defaultdict(int)
for r in graded:
    cot[r['country_name']] += 1
    if r['result'] == 'win': cow[r['country_name']] += 1
for cn in sorted(cot, key=lambda x: -cot[x]):
    if cot[cn] >= 10:
        print(f"  {cow[cn]}/{cot[cn]} = {cow[cn]/cot[cn]*100:.1f}%  {cn}")
