import sqlite3
import csv
from collections import Counter

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT id, match_name, league_name, country_name,
        pick_type, selection, confidence,
        final_home, final_away,
        prediction_mode, data_source, source,
        created_at, graded_at, reason
    FROM prediction_history
    WHERE result = 'win'
    ORDER BY graded_at DESC
""")
rows = [dict(r) for r in cur.fetchall()]
conn.close()

# Export CSV
with open(r'data\wins_analysis.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"Exported {len(rows)} wins to data/wins_analysis.csv\n")

print("=== BY PICK TYPE ===")
for k, v in Counter(r['pick_type'] for r in rows).most_common():
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

print("\n=== BY SELECTION (top 20) ===")
for k, v in Counter(r['selection'] for r in rows).most_common(20):
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

print("\n=== BY CONFIDENCE BUCKET ===")
buckets = Counter()
for r in rows:
    c = r['confidence'] or 0
    label = f"{(c // 10) * 10}-{(c // 10) * 10 + 9}%"
    buckets[label] += 1
for k, v in sorted(buckets.items()):
    pct = v / len(rows) * 100
    print(f"  {k}: {v} ({pct:.1f}%)")

print("\n=== TOP 15 LEAGUES ===")
for k, v in Counter(r['league_name'] for r in rows).most_common(15):
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

print("\n=== TOP 10 COUNTRIES ===")
for k, v in Counter(r['country_name'] for r in rows).most_common(10):
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

print("\n=== BY SOURCE ===")
for k, v in Counter(r['source'] for r in rows).most_common():
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

print("\n=== BY PREDICTION MODE ===")
for k, v in Counter(r['prediction_mode'] for r in rows).most_common():
    pct = v / len(rows) * 100
    print(f"  {k or 'None'}: {v} ({pct:.1f}%)")

# Score patterns for wins
print("\n=== SCORE PATTERNS (top 15) ===")
scores = Counter()
for r in rows:
    if r['final_home'] is not None and r['final_away'] is not None:
        scores[f"{r['final_home']}-{r['final_away']}"] += 1
for k, v in scores.most_common(15):
    pct = v / len(rows) * 100
    print(f"  {k}: {v} ({pct:.1f}%)")

# Win rate by confidence bucket
print("\n=== WIN RATE BY CONFIDENCE (vs all graded) ===")
conn2 = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn2.row_factory = sqlite3.Row
cur2 = conn2.cursor()
cur2.execute("SELECT confidence, result FROM prediction_history WHERE result IN ('win','loss')")
all_graded = cur2.fetchall()
conn2.close()

bucket_wins = Counter()
bucket_total = Counter()
for r in all_graded:
    c = r['confidence'] or 0
    label = f"{(c // 10) * 10}-{(c // 10) * 10 + 9}%"
    bucket_total[label] += 1
    if r['result'] == 'win':
        bucket_wins[label] += 1

for label in sorted(bucket_total.keys()):
    total = bucket_total[label]
    wins = bucket_wins[label]
    print(f"  {label}: {wins}/{total} = {wins/total*100:.1f}% win rate")
