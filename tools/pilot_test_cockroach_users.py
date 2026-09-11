"""
CockroachDB pilot test -- run this on YOUR PC, not in any sandbox.

What this does, in plain terms:
  1. Connects to your CockroachDB cluster using the connection string in
     the COCKROACH_DATABASE_URL environment variable.
  2. Creates a `users` table there if it doesn't already exist.
  3. Creates (or reuses) one test account, then checks that looking it up,
     logging in with the right password, rejecting the wrong password, and
     promoting its role all work -- exactly the same operations the real
     app's login system needs.
  4. Prints PASS or FAIL for each step in plain language.

This does NOT touch your live SQLite database or your live app at all.
It only proves CockroachDB itself works for this one piece before we
move anything real onto it.

How to run it (Windows PowerShell, from the predictx folder):

    pip install psycopg2-binary
    $env:COCKROACH_DATABASE_URL = "postgresql://ajoke:...@sixth-faerie-33467.j77.aws-eu-central-1.cockroachlabs.cloud:26257/defaultdb?sslmode=verify-full&sslrootcert=$env:appdata\postgresql\root.crt"
    python tools\pilot_test_cockroach_users.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_EMAIL = "predictx-pilot-test@example.com"
TEST_PASSWORD = "pilot-test-password-123"


def _pass(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    try:
        from app.storage.cockroach import users as pg_users
        from app.storage.cockroach.db import init_users_schema
    except ImportError as exc:
        print(f"Could not import the pilot code: {exc}")
        print("Make sure you're running this from inside the predictx folder.")
        return 1

    print("Step 1: connect + create the users table if needed...")
    try:
        init_users_schema()
        _pass("Connected to CockroachDB and the users table is ready.")
    except Exception as exc:
        _fail(f"Could not connect / create schema: {exc}")
        print("\nCheck: is COCKROACH_DATABASE_URL set correctly? Is the cert path right?")
        return 1

    print("\nStep 2: create (or reuse) a test account...")
    existing = pg_users.get_user_by_email(TEST_EMAIL)
    if existing:
        user = existing
        _pass(f"Test account already exists (id={user['id']}) -- reusing it.")
    else:
        try:
            user = pg_users.create_user(TEST_EMAIL, TEST_PASSWORD, display_name="Pilot Test", role="user")
            _pass(f"Created test account (id={user['id']}).")
        except Exception as exc:
            _fail(f"Could not create test account: {exc}")
            return 1

    print("\nStep 3: look the account up by id and by email...")
    by_id = pg_users.get_user_by_id(user["id"])
    by_email = pg_users.get_user_by_email(TEST_EMAIL)
    if by_id and by_email and by_id["email"] == TEST_EMAIL:
        _pass("Lookup by id and by email both work.")
    else:
        _fail("Lookup by id/email did not return the expected account.")
        return 1

    print("\nStep 4: log in with the correct password...")
    logged_in = pg_users.verify_credentials(TEST_EMAIL, TEST_PASSWORD)
    if logged_in:
        _pass(f"Correct password accepted (last_login_at={logged_in['last_login_at']}).")
    else:
        _fail("Correct password was rejected -- this should not happen.")
        return 1

    print("\nStep 5: reject a wrong password...")
    rejected = pg_users.verify_credentials(TEST_EMAIL, "definitely-wrong-password")
    if rejected is None:
        _pass("Wrong password was correctly rejected.")
    else:
        _fail("Wrong password was accepted -- this is a real problem.")
        return 1

    print("\nStep 6: promote the test account to admin and confirm it stuck...")
    changed = pg_users.set_user_role(TEST_EMAIL, "admin")
    refreshed = pg_users.get_user_by_email(TEST_EMAIL)
    if changed and refreshed and refreshed["role"] == "admin":
        _pass("Role change applied and confirmed.")
    else:
        _fail("Role change did not apply as expected.")
        return 1

    # Reset role back to 'user' so re-running this script doesn't leave the
    # test account escalated.
    pg_users.set_user_role(TEST_EMAIL, "user")

    print("\nAll checks passed. CockroachDB works for the users-table pilot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
