import sys
sys.path.insert(0, '.')
import sqlite3, json, traceback

try:
    from app.db import db_conn
    from app.team_watcher import _build_profile, init_team_watcher_tables

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        profile = _build_profile(conn, 'botafogo')
        print('profile sample_size:', profile.get('sample_size'))
        print('profile analyst_score:', profile.get('analyst_score'))
        result = conn.execute(
            'update ai_team_watchers set profile_json = ? where team_key = ?',
            (json.dumps(profile), 'botafogo')
        )
        print('rows changed:', result.rowcount)
        conn.commit()

    with db_conn() as conn2:
        conn2.row_factory = sqlite3.Row
        row = conn2.execute('select profile_json from ai_team_watchers where team_key = ?', ('botafogo',)).fetchone()
        p = json.loads(row['profile_json'])
        print('after commit - analyst_score:', p.get('analyst_score'))
        print('after commit - sample_size:', p.get('sample_size'))

except Exception as e:
    traceback.print_exc()
