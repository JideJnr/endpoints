"""Check what stat names SofaScore returns in sofascore_detail.statistics."""
import sqlite3, json

conn = sqlite3.connect('data/predictx_memory.sqlite3', timeout=5)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "select match_id, raw_enriched from match_buffer "
    "where raw_enriched is not null order by enriched_at desc limit 50"
).fetchall()

all_names = set()
found = 0
for row in rows:
    try:
        doc = json.loads(row['raw_enriched'])
        detail = doc.get('sofascore_detail') or {}
        stats = detail.get('statistics') or detail.get('match_statistics') or []
        if not stats:
            continue
        def walk(node):
            if isinstance(node, dict):
                name = node.get('name') or node.get('key') or node.get('groupName')
                if name and ('home' in node or 'homeValue' in node or 'away' in node):
                    all_names.add(str(name))
                for ck in ('groups','statisticsItems','items','statistics'):
                    for c in (node.get(ck) or []):
                        walk(c)
            elif isinstance(node, list):
                for c in node:
                    walk(c)
        walk(stats)
        found += 1
        if found >= 10:
            break
    except Exception as e:
        pass

print(f'Matches with stats found: {found}')
print('All stat names:')
for n in sorted(all_names):
    print(' ', n)

# Also check live_stat_snapshots for real data
snap_count = conn.execute('select count(*) from live_stat_snapshots').fetchone()[0]
print(f'\nlive_stat_snapshots rows: {snap_count}')
if snap_count:
    cols = [r[1] for r in conn.execute('pragma table_info(live_stat_snapshots)').fetchall()]
    print('Columns:', cols)

conn.close()
