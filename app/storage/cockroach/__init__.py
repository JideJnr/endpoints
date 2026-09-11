"""
CockroachDB pilot code -- NOT wired into the live app yet.

This package exists purely to test whether predictx can talk to CockroachDB,
starting with just the `users` table (login/accounts). Nothing here runs
automatically; app.storage.users (SQLite) is still what the real app uses.

Run `python scripts/pilot_test_cockroach_users.py` to try it out once
COCKROACH_DATABASE_URL is set.
"""
