import sys
sys.path.insert(0, '.')
from app.team_watcher import rebuild_all_profiles
import sqlite3
from app.db import db_conn

result = rebuild_all_profiles()
print('rebuild result:', result)

with db_conn() as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select team_key, team_name, match_count, profile_json from ai_team_watchers order by match_count desc limit 5"
    ).fetchall()
    for r in rows:
        import json
        p = json.loads(r['profile_json']) if r['profile_json'] != '{}' else {}
        print(r['team_name'], '| matches:', r['match_count'], '| score:', p.get('analyst_score'), '| sample:', p.get('sample_size'))
