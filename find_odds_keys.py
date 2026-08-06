import sqlite3, json
from collections import defaultdict

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT picks_json, signals_json, audit_json, result FROM prediction_history WHERE result IN ('win','loss')")
rows = cur.fetchall()
conn.close()

# Recursively find any key that looks like odds
def find_odds_keys(obj, path='', found=None):
    if found is None:
        found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if any(x in k.lower() for x in ('odd', 'price', 'prob', 'rate', 'decimal', 'fractional', 'implied')):
                found[new_path] = v
            find_odds_keys(v, new_path, found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_odds_keys(v, f"{path}[{i}]", found)
    return found

# Sample first 10 to find odds keys
print("=== SCANNING FOR ODDS KEYS ===")
seen_keys = set()
for row in rows[:50]:
    for field in ('picks_json', 'signals_json', 'audit_json'):
        try:
            obj = json.loads(row[field] or '{}')
            found = find_odds_keys(obj)
            for k in found:
                # normalize path to remove array indices
                norm = k.replace('[0]','[n]').replace('[1]','[n]').replace('[2]','[n]')
                if norm not in seen_keys:
                    seen_keys.add(norm)
                    print(f"  {field} -> {k}: {found[k]}")
        except:
            pass

# Also check picks_json for 'odds' key directly
print("\n=== SAMPLE picks_json KEYS ===")
for row in rows[:5]:
    try:
        picks = json.loads(row['picks_json'] or '[]')
        if picks:
            print(json.dumps(list(picks[0].keys())))
    except:
        pass
