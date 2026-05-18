import sys, sqlite3, time
sys.path.insert(0, '.')
from app.config import get_settings

db = str(get_settings().database_path)
conn = sqlite3.connect(db)

total      = conn.execute('select count(*) from match_buffer').fetchone()[0]
finished   = conn.execute('select count(*) from match_buffer where is_finished=1').fetchone()[0]
live       = conn.execute('select count(*) from match_buffer where is_live=1').fetchone()[0]
unenriched = conn.execute('select count(*) from match_buffer where enriched_at is null').fetchone()[0]

ghost_cutoff_ms = (time.time() - 120*60) * 1000
ghosts = conn.execute("""
    select count(*) from match_buffer
    where is_live=0 and is_finished=0
      and start_time is not null
      and cast(start_time as real) < ?
      and (period is null or lower(period) in ('not start','not started',''))
""", (ghost_cutoff_ms,)).fetchone()[0]

over90 = conn.execute("""
    select count(*) from match_buffer
    where is_live=1
      and cast(json_extract(raw_sporty, '$.played_seconds') as integer) >= 5400
""").fetchone()[0]

stale = conn.execute("""
    select count(*) from match_buffer
    where enriched_at is null
      and datetime(ingested_at) < datetime('now', '-1 day')
""").fetchone()[0]

print(f'Total in buffer              : {total}')
print(f'  Finished (is_finished=1)   : {finished}')
print(f'  Live                       : {live}')
print(f'  Unenriched                 : {unenriched}')
print(f'  Ghost (time passed)        : {ghosts}')
print(f'  90+ minutes                : {over90}')
print(f'  Stale unenriched (>1 day)  : {stale}')
print(f'  Cleanable total            : {finished + ghosts + over90 + stale}')
