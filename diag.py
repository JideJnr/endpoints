import sqlite3, json, sys
from datetime import date
from app.config import get_settings

db = str(get_settings().database_path)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

today = date.today().isoformat()
sys.stdout.write(f"Today: {today}\n\n")

# Buffer overview
total = conn.execute("select count(*) from match_buffer").fetchone()[0]
by_date = conn.execute("select match_date, count(*) c from match_buffer group by match_date order by match_date desc").fetchall()
by_period = conn.execute("select period, is_live, is_finished, count(*) c from match_buffer group by period, is_live, is_finished order by c desc limit 15").fetchall()
today_count = conn.execute("select count(*) from match_buffer where match_date = ?", (today,)).fetchone()[0]
yesterday_count = conn.execute("select count(*) from match_buffer where match_date < ?", (today,)).fetchone()[0]
last_ingest = conn.execute("select max(ingested_at) from match_buffer").fetchone()[0]
last_enrich = conn.execute("select max(enriched_at) from match_buffer").fetchone()[0]

sys.stdout.write(f"=== BUFFER ===\n")
sys.stdout.write(f"Total rows     : {total}\n")
sys.stdout.write(f"Today ({today}): {today_count}\n")
sys.stdout.write(f"Yesterday/old  : {yesterday_count}\n")
sys.stdout.write(f"Last ingested  : {last_ingest}\n")
sys.stdout.write(f"Last enriched  : {last_enrich}\n\n")

sys.stdout.write("By date:\n")
for r in by_date:
    sys.stdout.write(f"  {r['match_date']}: {r['c']} matches\n")

sys.stdout.write("\nBy period/status:\n")
for r in by_period:
    sys.stdout.write(f"  period={r['period']}  live={r['is_live']}  finished={r['is_finished']}  count={r['c']}\n")

# Sample yesterday matches still in buffer
old = conn.execute("select match_id, name, match_date, period, is_live, is_finished, ingested_at from match_buffer where match_date < ? limit 5", (today,)).fetchall()
if old:
    sys.stdout.write(f"\nSample old matches still in buffer:\n")
    for r in old:
        sys.stdout.write(f"  {r['match_date']} | {r['name'][:40]} | period={r['period']} live={r['is_live']} fin={r['is_finished']} ingested={r['ingested_at']}\n")

# Check MongoDB config
sys.stdout.write(f"\n=== MONGO ===\n")
from app.config import get_settings
s = get_settings()
sys.stdout.write(f"MONGODB_URI set : {bool(s.mongodb_uri)}\n")

# Check finished_matches local table
try:
    fm = conn.execute("select count(*) from finished_matches").fetchone()[0]
    fm_today = conn.execute("select count(*) from finished_matches where match_date = ?", (today,)).fetchone()[0]
    fm_yest = conn.execute("select count(*) from finished_matches where match_date < ?", (today,)).fetchone()[0]
    sys.stdout.write(f"\n=== LOCAL finished_matches ===\n")
    sys.stdout.write(f"Total: {fm}  today: {fm_today}  old: {fm_yest}\n")
except:
    sys.stdout.write("finished_matches table: not found\n")

# Check prediction_history dates
ph_dates = conn.execute("select date(created_at) d, count(*) c from prediction_history group by d order by d desc limit 5").fetchall()
sys.stdout.write(f"\n=== PREDICTION HISTORY ===\n")
for r in ph_dates:
    sys.stdout.write(f"  {r['d']}: {r['c']} predictions\n")

conn.close()
sys.stdout.flush()
