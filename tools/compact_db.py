from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _bytes(num: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}B"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("pragma busy_timeout = 60000")
    return conn


def _checkpoint(conn: sqlite3.Connection) -> None:
    try:
        # No-op in DELETE journal mode; harmless otherwise.
        conn.execute("pragma wal_checkpoint(truncate)")
    except Exception:
        pass


def vacuum_into(src: Path, dst: Path) -> None:
    started = time.time()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    conn = _connect(src)
    try:
        _checkpoint(conn)
        conn.execute("pragma optimize")
        conn.execute(f"vacuum into {sql_literal(str(dst))}")
    finally:
        conn.close()

    duration = round(time.time() - started, 2)
    print(f"vacuum_into_ok seconds={duration} src={src} dst={dst}")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Compact an SQLite DB without deleting any rows (VACUUM INTO).")
    parser.add_argument("--src", required=True, help="Path to source sqlite DB file.")
    parser.add_argument("--dst", required=True, help="Path to output compacted sqlite DB file.")
    parser.add_argument("--backup", action="store_true", help="Also create a .bak copy of src before vacuuming.")
    args = parser.parse_args(argv)

    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()
    if not src.exists():
        print(f"error: src not found: {src}", file=sys.stderr)
        return 2

    src_size = src.stat().st_size
    print(f"src_size={_bytes(src_size)} src={src}")

    if args.backup:
        bak = src.with_suffix(src.suffix + ".bak")
        if not bak.exists():
            print(f"creating_backup={bak}")
            shutil.copy2(src, bak)
        else:
            print(f"backup_exists={bak}")

    vacuum_into(src, dst)

    dst_size = dst.stat().st_size if dst.exists() else 0
    print(f"dst_size={_bytes(dst_size)} dst={dst}")
    if dst_size:
        ratio = round(dst_size / max(1, src_size), 4)
        print(f"size_ratio={ratio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

