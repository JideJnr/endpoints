"""
Consolidate all DB connection/init primitives from league_memory.py into app/db.py.
Run once: python tools/consolidate_db.py
"""
import re
import sys
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

LM_PATH = os.path.join(ROOT, "app", "league_memory.py")
DB_PATH_FILE = os.path.join(ROOT, "app", "db.py")
TW_PATH = os.path.join(ROOT, "app", "team_watcher.py")
LM_DB_PATH = os.path.join(ROOT, "app", "league_memory", "db.py")

# ── 1. Read league_memory.py ──────────────────────────────────────────────────
with open(LM_PATH, encoding="utf-8", errors="ignore") as f:
    lm_lines = f.readlines()

def extract_top_level_block(lines, start_idx):
    """Extract a top-level def/assignment block from start_idx until next top-level symbol."""
    block = [lines[start_idx]]
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if re.match(r"^(def |class |[A-Z_][A-Z_0-9]* =)", line) and not line.startswith(" "):
            break
        block.append(line)
    return "".join(block)

# Find line indices
idx = {}
for i, line in enumerate(lm_lines):
    if re.match(r"^_DB_SCHEMA_READY\s*=", line):   idx["schema_ready"] = i
    if re.match(r"^_DB_SCHEMA_LOCK\s*=", line):    idx["schema_lock"] = i
    if re.match(r"^def _init_db\(\)", line):        idx["init_db"] = i
    if re.match(r"^def _init_db_unlocked\(\)", line): idx["init_db_unlocked"] = i
    if re.match(r"^def _ensure_column\(", line):   idx["ensure_column"] = i
    if re.match(r"^def _is_sqlite_lock\(", line):  idx["is_sqlite_lock"] = i

print("Found markers:", {k: v+1 for k, v in idx.items()})

schema_ready_line  = lm_lines[idx["schema_ready"]].rstrip()
schema_lock_line   = lm_lines[idx["schema_lock"]].rstrip()
init_db_block      = extract_top_level_block(lm_lines, idx["init_db"])
init_db_u_block    = extract_top_level_block(lm_lines, idx["init_db_unlocked"])
ensure_col_block   = extract_top_level_block(lm_lines, idx["ensure_column"])
is_lock_block      = extract_top_level_block(lm_lines, idx["is_sqlite_lock"])

# ── 2. Append to app/db.py ────────────────────────────────────────────────────
with open(DB_PATH_FILE, encoding="utf-8") as f:
    db_txt = f.read()

# Only add if not already present
additions = []
if "_DB_SCHEMA_READY" not in db_txt:
    additions.append("\nimport threading\n")
    additions.append(f"\n{schema_ready_line}\n")
    additions.append(f"{schema_lock_line}\n")

if "_ensure_column" not in db_txt:
    additions.append(f"\n\n{ensure_col_block}")

if "_is_sqlite_lock" not in db_txt:
    # Already exists as is_sqlite_lock — add underscore alias
    additions.append("\n\n# Backward-compatible underscore alias\n_is_sqlite_lock = is_sqlite_lock\n")

if "_init_db_unlocked" not in db_txt:
    additions.append(f"\n\n{init_db_u_block}")

if "_init_db" not in db_txt:
    additions.append(f"\n\n{init_db_block}")

if additions:
    with open(DB_PATH_FILE, "a", encoding="utf-8") as f:
        f.writelines(additions)
    print(f"app/db.py: appended {len(additions)} blocks")
else:
    print("app/db.py: nothing to add (already present)")

# ── 3. Patch league_memory.py ─────────────────────────────────────────────────
# Replace the 4 definitions with a single import line
# Mark lines to remove (replace with empty)
lines_to_blank = set()

for key in ("schema_ready", "schema_lock"):
    lines_to_blank.add(idx[key])

for key in ("init_db", "init_db_unlocked", "ensure_column", "is_sqlite_lock"):
    start = idx[key]
    lines_to_blank.add(start)
    for i in range(start + 1, len(lm_lines)):
        line = lm_lines[i]
        if re.match(r"^(def |class |[A-Z_][A-Z_0-9]* =)", line) and not line.startswith(" "):
            break
        lines_to_blank.add(i)

# Replace the top import line to add db primitives
new_import = (
    "from app.storage.db import ("
    "DB_PATH, _conn, close_db, connect_readonly_db, db_conn, get_db, "
    "_init_db, _init_db_unlocked, _ensure_column, _is_sqlite_lock, "
    "_DB_SCHEMA_READY, _DB_SCHEMA_LOCK"
    ")\n"
)

# Find the existing app.db import line and replace it
new_lm_lines = []
replaced_import = False
for i, line in enumerate(lm_lines):
    if i in lines_to_blank:
        continue  # drop the old definition
    if not replaced_import and re.match(r"^from app\.db import", line):
        new_lm_lines.append(new_import)
        replaced_import = True
        continue
    new_lm_lines.append(line)

# Remove threading import if no longer needed (schema lock now in db.py)
new_lm_lines = [
    line for line in new_lm_lines
    if not re.match(r"^import threading\s*$", line)
]

with open(LM_PATH, "w", encoding="utf-8") as f:
    f.writelines(new_lm_lines)
print(f"league_memory.py: patched ({len(lm_lines)} -> {len(new_lm_lines)} lines)")

# ── 4. Update league_memory/db.py re-exports ─────────────────────────────────
lm_db_new = """from __future__ import annotations

from app.storage.db import (
    DB_PATH,
    _conn,
    close_db,
    connect_db,
    connect_readonly_db,
    configure_connection,
    db_conn,
    get_db,
    is_sqlite_lock,
    _init_db,
    _init_db_unlocked,
    _ensure_column,
    _is_sqlite_lock,
    _DB_SCHEMA_READY,
    _DB_SCHEMA_LOCK,
)

__all__ = [
    "DB_PATH",
    "_conn",
    "close_db",
    "connect_db",
    "connect_readonly_db",
    "configure_connection",
    "db_conn",
    "get_db",
    "is_sqlite_lock",
    "_init_db",
    "_init_db_unlocked",
    "_ensure_column",
    "_is_sqlite_lock",
    "_DB_SCHEMA_READY",
    "_DB_SCHEMA_LOCK",
]
"""
with open(LM_DB_PATH, "w", encoding="utf-8") as f:
    f.write(lm_db_new)
print("league_memory/db.py: updated re-exports")

# ── 5. Update team_watcher.py ─────────────────────────────────────────────────
with open(TW_PATH, encoding="utf-8") as f:
    tw_txt = f.read()

tw_new = tw_txt.replace(
    "from app.storage.league_memory import _ensure_column, _init_db",
    "from app.storage.db import _ensure_column, _init_db",
)
if tw_new != tw_txt:
    with open(TW_PATH, "w", encoding="utf-8") as f:
        f.write(tw_new)
    print("team_watcher.py: updated import")
else:
    print("team_watcher.py: no change needed")

print("\nDone. Verify with: python -c \"from app.storage.db import _init_db, _ensure_column; print('ok')\"")
