import sqlite3, sys

DB = "data/predictx_memory.sqlite3"
conn = sqlite3.connect(DB, timeout=30)
conn.execute("pragma wal_checkpoint(TRUNCATE)")
conn.execute("""
    create table if not exists specialist_performance (
        specialist_name  text not null,
        league_key       text not null default '__global__',
        pick_type        text not null default '__all__',
        samples          integer not null default 0,
        wins             integer not null default 0,
        losses           integer not null default 0,
        win_rate         real,
        weight           real not null default 1.0,
        last_updated     text not null default current_timestamp,
        primary key (specialist_name, league_key, pick_type)
    )
""")
conn.commit()
tables = [t[0] for t in conn.execute("select name from sqlite_master where type='table' order by name").fetchall()]
conn.close()
print("Tables:", tables)
print("specialist_performance present:", "specialist_performance" in tables)
