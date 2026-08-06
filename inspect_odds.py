import sqlite3, json

conn = sqlite3.connect(r'data\predictx_memory.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT picks_json, signals_json, audit_json, models_json FROM prediction_history WHERE result IN ('win','loss') LIMIT 5")
rows = cur.fetchall()
conn.close()

for i, row in enumerate(rows):
    print(f"\n====== ROW {i+1} ======")
    for field in ('picks_json', 'signals_json', 'audit_json', 'models_json'):
        val = row[field]
        if val and val not in ('{}', '[]', 'null', None):
            try:
                parsed = json.loads(val)
                print(f"  [{field}]: {json.dumps(parsed)[:500]}")
            except:
                print(f"  [{field}]: {str(val)[:300]}")
