import sqlite3
conn = sqlite3.connect('data/predictx_memory.sqlite3')
cols = [r[1] for r in conn.execute('pragma table_info(matches)').fetchall()]
print('matches columns:', cols)
conn.close()
