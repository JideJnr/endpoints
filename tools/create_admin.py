#!/usr/bin/env python3
"""
Promote an existing user to admin, or create one as admin outright. This is
the only way an account becomes admin — there's no self-service path, and
the API never lets a user set their own role.

Usage:
    python tools/create_admin.py user@example.com
        Promote an existing account to admin.

    python tools/create_admin.py newadmin@example.com --create --password "correct horse battery staple"
        Create a brand-new account that starts as admin.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.users import UserExistsError, create_user, get_user_by_email, set_user_role  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--password", help="Required with --create")
    parser.add_argument("--create", action="store_true", help="Create the account if it doesn't exist yet")
    args = parser.parse_args()

    existing = get_user_by_email(args.email)
    if not existing:
        if not args.create:
            print(f"No account with email {args.email}.")
            print("Re-run with --create --password '...' to create one as admin.")
            raise SystemExit(1)
        if not args.password:
            print("--password is required with --create")
            raise SystemExit(1)
        try:
            user = create_user(args.email, args.password, role="admin")
        except UserExistsError as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        print(f"Created admin account {user['email']} (id={user['id']})")
        return

    if existing["role"] == "admin":
        print(f"{args.email} is already an admin.")
        return

    if set_user_role(args.email, "admin"):
        print(f"{args.email} is now an admin.")
    else:
        print(f"Failed to update role for {args.email}.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
