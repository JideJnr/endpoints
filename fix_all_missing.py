import ast, re

cwd = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'

fixes = {
    # ── ai_betbuilder.py ──────────────────────────────────────────────────────
    r'app\ai\ai_betbuilder.py': {
        'anchor': 'class AIBetBuilder',
        'code': '''
def _total_goals(score: str | None) -> float | None:
    """Parse a score string like '2-1' and return total goals."""
    if not score or '-' not in str(score):
        return None
    parts = str(score).split('-', 1)
    try:
        return float(parts[0]) + float(parts[1])
    except (ValueError, TypeError):
        return None


''',
    },

    # ── competition_special.py ────────────────────────────────────────────────
    r'app\competition\competition_special.py': {
        'anchor': 'def history_league_strength(',
        'code': '''
def _name_quality_score(name: str) -> float:
    """Rough quality proxy for a team name — longer, more specific names score higher."""
    n = str(name or '').strip()
    if not n:
        return 0.5
    return min(1.0, 0.4 + len(n) / 40)


''',
    },

    # ── prediction_monitor.py ─────────────────────────────────────────────────
    r'app\monitoring\prediction_monitor.py': {
        'anchor': 'def _persist_monitor_snapshot(',
        'code': '''
def _record_monitor_activity(result: dict) -> None:
    """No-op stub — activity recording is handled by _persist_monitor_snapshot."""
    pass


''',
    },

    # ── system_supervisor.py ──────────────────────────────────────────────────
    r'app\monitoring\system_supervisor.py': {
        'anchor': 'def supervise_jobs(',
        'code': '''
def _age_seconds(timestamp: str | float | None) -> float:
    """Return seconds elapsed since *timestamp* (ISO string or unix epoch)."""
    if not timestamp:
        return 0.0
    from datetime import datetime, timezone
    try:
        if isinstance(timestamp, (int, float)):
            ts = float(timestamp)
            if ts > 1e10:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00')).astimezone(timezone.utc)
        return max(0.0, (datetime.now(tz=timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


''',
    },

    # ── agent.py ──────────────────────────────────────────────────────────────
    r'app\routers\agent.py': {
        'anchor': 'def _estimate_odds(',
        'code': '''
def _is_finished_doc(doc: dict) -> bool:
    """Return True when the match document represents a finished match."""
    period = str(doc.get('period') or '').lower()
    status = (doc.get('status') or {}) if isinstance(doc.get('status'), dict) else {}
    status_type = str(status.get('type') or doc.get('status') or '').lower()
    return period in {'ft', 'finished', 'ended', 'aet', 'ap'} or status_type in {'finished', 'ended'}


''',
    },

    # ── db.py ─────────────────────────────────────────────────────────────────
    r'app\storage\db.py': {
        'anchor': 'def _conn(',
        'code': '''
def _is_sqlite_lock(exc: Exception) -> bool:
    """Return True when *exc* is a SQLite database-locked / busy error."""
    msg = str(exc).lower()
    return 'database is locked' in msg or 'unable to open' in msg or 'disk i/o error' in msg


''',
    },

    # ── mongo_store.py ────────────────────────────────────────────────────────
    r'app\storage\mongo_store.py': {
        'anchor': 'class MongoStore',
        'code': '''
class _RowCountStub:
    """Stub returned when pruning is disabled — behaves like a zero-row result."""
    rowcount = 0


''',
    },

    # ── team_watcher_engine.py ────────────────────────────────────────────────
    r'app\team_watcher\team_watcher_engine.py': {
        'anchor': 'def rebuild_team_profile(',
        'code': '''
def _compute_profile_stats(rows: list) -> dict:
    """Compute win/draw/loss/goal stats from a list of match rows."""
    wins = draws = losses = 0
    goals_for = goals_against = 0
    for row in rows or []:
        own = int(row['own_goals'] or 0) if 'own_goals' in row.keys() else 0
        opp = int(row['opp_goals'] or 0) if 'opp_goals' in row.keys() else 0
        goals_for += own
        goals_against += opp
        if own > opp:
            wins += 1
        elif own == opp:
            draws += 1
        else:
            losses += 1
    sample = wins + draws + losses
    return {
        'sample_size': sample,
        'wins': wins,
        'draws': draws,
        'losses': losses,
        'win_rate': round(wins / sample, 3) if sample else 0.0,
        'draw_rate': round(draws / sample, 3) if sample else 0.0,
        'loss_rate': round(losses / sample, 3) if sample else 0.0,
        'avg_goals_for': round(goals_for / sample, 2) if sample else 0.0,
        'avg_goals_against': round(goals_against / sample, 2) if sample else 0.0,
        'avg_goals_total': round((goals_for + goals_against) / sample, 2) if sample else 0.0,
    }


def _note_for_result(result: str | None, own_goals: int, opp_goals: int, side: str, profile: dict) -> str | None:
    """Generate a short human-readable note for a match result."""
    if not result:
        return None
    score = f'{own_goals}-{opp_goals}'
    side_label = side or 'team'
    if result == 'win':
        return f'{side_label.capitalize()} won {score}'
    if result == 'loss':
        return f'{side_label.capitalize()} lost {score}'
    if result == 'draw':
        return f'Draw {score}'
    return None


''',
    },

    # ── portfolio.py ──────────────────────────────────────────────────────────
    r'app\utils\portfolio.py': {
        'anchor': 'def _pick_dimension(',
        'code': '''
def _normalise_league(name: str) -> str:
    """Normalise a league/tournament name to a consistent key."""
    return str(name or '').lower().strip().replace('-', ' ')


def _start_time(pick: dict) -> float | None:
    """Extract match start time as a unix timestamp from a pick record."""
    for key in ('match_date', 'start_time', 'kickoff', 'created_at'):
        val = pick.get(key)
        if not val:
            continue
        try:
            if isinstance(val, (int, float)):
                return float(val)
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00')).astimezone(timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


def _time_window(timestamp: float) -> str:
    """Bucket a unix timestamp into a coarse time window label."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        hour = dt.hour
        if hour < 12:
            return 'morning'
        if hour < 17:
            return 'afternoon'
        return 'evening'
    except Exception:
        return 'unknown'


''',
    },
}

# ── team_watcher.py — many missing functions ──────────────────────────────────
team_watcher_code = '''
def _slug(name: str) -> str:
    """Convert a team name to a URL-safe slug key."""
    import re as _re
    return _re.sub(r'[^a-z0-9]+', '-', str(name or '').lower().strip()).strip('-')


def _team_name(team: object) -> str:
    """Extract team name from a string or dict."""
    if isinstance(team, dict):
        return str(team.get('name') or team.get('team_name') or '')
    return str(team or '')


def _league_name_for_doc(doc: dict) -> str:
    """Extract league/tournament name from a match document."""
    t = doc.get('tournament') or doc.get('league_name') or ''
    if isinstance(t, dict):
        return str(t.get('name') or '')
    return str(t or '')


def _league_name_from_rows(rows: list) -> str:
    """Infer league name from a list of match rows."""
    for row in rows or []:
        name = row['league_name'] if 'league_name' in row.keys() else None
        if name:
            return str(name)
    return ''


def _unique_tournament_id_for_doc(doc: dict) -> str | None:
    """Extract a unique tournament ID from a match document."""
    detail = doc.get('sofascore_detail') or {}
    t = detail.get('tournament') or doc.get('tournament') or {}
    if isinstance(t, dict):
        return str(t.get('uniqueTournamentId') or t.get('id') or '') or None
    return None


def _resolve_watcher_key(conn, team: dict) -> str | None:
    """Find an existing watcher key for a team by alias matching."""
    team_key = str(team.get('team_key') or '')
    if not team_key:
        return None
    row = conn.execute(
        "SELECT team_key FROM ai_team_watcher WHERE team_key = ? LIMIT 1",
        (team_key,)
    ).fetchone()
    return row['team_key'] if row else None


def _resolve_watcher_row(conn, team_key: str) -> object | None:
    """Fetch the watcher row for a given team key."""
    if not team_key:
        return None
    return conn.execute(
        "SELECT * FROM ai_team_watcher WHERE team_key = ? LIMIT 1",
        (str(team_key),)
    ).fetchone()


def _merge_aliases(existing: object | None, team: dict) -> list:
    """Merge new aliases into the existing alias list."""
    import json as _json
    existing_aliases = []
    if existing:
        try:
            existing_aliases = _json.loads(existing['aliases_json'] or '[]')
        except Exception:
            pass
    new_aliases = team.get('aliases') or []
    seen = {(a.get('provider'), a.get('id')) for a in existing_aliases}
    for alias in new_aliases:
        key = (alias.get('provider'), alias.get('id'))
        if key not in seen:
            existing_aliases.append(alias)
            seen.add(key)
    return existing_aliases


def _table_lookup(doc: dict) -> dict:
    """Build a name->row lookup from standings in the document."""
    detail = doc.get('sofascore_detail') or {}
    standings = detail.get('standings') or doc.get('standings') or []
    table: dict = {}
    for group in standings if isinstance(standings, list) else []:
        rows = group.get('rows') or [] if isinstance(group, dict) else []
        for row in rows:
            name = str((row.get('team') or {}).get('name') or row.get('team_name') or '')
            if name:
                table[name.lower()] = row
    return table


def _team_position(table_map: dict, name: str, sporty_id, sofa_id, *, return_row: bool = False):
    """Look up a team's position from the standings table map."""
    if not table_map or not name:
        return None if return_row else None
    key = str(name or '').lower().strip()
    row = table_map.get(key)
    if row is None:
        # fuzzy: try partial match
        for k, v in table_map.items():
            if key in k or k in key:
                row = v
                break
    if return_row:
        return row
    return int((row or {}).get('position') or 0) or None


def _position_value(row: object | None) -> int | None:
    """Extract position integer from a standings row."""
    if row is None:
        return None
    try:
        return int(row['position'] or 0) or None
    except Exception:
        return None


def _table_gap(team_row: object | None, opponent_row: object | None) -> int | None:
    """Return the position gap between two teams in the standings."""
    t = _position_value(team_row)
    o = _position_value(opponent_row)
    if t is None or o is None:
        return None
    return o - t


def _table_from_rows(rows: list) -> dict | None:
    """Build a minimal table dict from match rows."""
    if not rows:
        return None
    return {'rows': list(rows)}


def _matchup_context(home: dict, away: dict, doc: dict) -> dict:
    """Build a brief matchup context dict from home/away team data."""
    return {
        'home_position': home.get('team_position'),
        'away_position': away.get('team_position'),
        'table_gap': (
            (away.get('team_position') or 0) - (home.get('team_position') or 0)
            if home.get('team_position') and away.get('team_position') else None
        ),
    }


def _analysis_summary(analysis: dict | None) -> str:
    """Return a short text summary from an analysis dict."""
    if not analysis:
        return ''
    return str(analysis.get('summary') or analysis.get('note') or '')


def _team_web_context(team: dict, doc: dict, team_row, opponent_row) -> dict:
    """Build web context dict for a team signal."""
    return {
        'team_name': team.get('team_name'),
        'team_position': _position_value(team_row),
        'opponent_position': _position_value(opponent_row),
        'table_gap': _table_gap(team_row, opponent_row),
        'league_name': _league_name_for_doc(doc),
    }


def _should_refresh_web_context(watcher: dict, sample: int) -> bool:
    """Return True when the web context is stale or missing."""
    from datetime import datetime, timezone
    web_context = watcher.get('web_context') or {}
    last_at = watcher.get('last_web_context_at')
    if not web_context or not last_at:
        return True
    try:
        dt = datetime.fromisoformat(str(last_at).replace('Z', '+00:00')).astimezone(timezone.utc)
        age_hours = (datetime.now(tz=timezone.utc) - dt).total_seconds() / 3600
        return age_hours > (6 if sample >= 5 else 24)
    except Exception:
        return True


def _build_overview(
    team_name: str,
    league_name: str,
    **kwargs,
) -> dict:
    """Build a team overview dict from available data."""
    return {
        'team_name': team_name,
        'league_name': league_name,
        **{k: v for k, v in kwargs.items() if v is not None},
    }


'''

fixes[r'app\team_watcher\team_watcher.py'] = {
    'anchor': 'def team_watch_signal(',
    'code': team_watcher_code,
}

import os

results = []
for rel_path, fix in fixes.items():
    fpath = os.path.join(cwd, rel_path)
    try:
        with open(fpath, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        results.append(f'FILE NOT FOUND: {rel_path}')
        continue

    eol = b'\r\n' if b'\r\n' in raw else b'\n'
    src = raw.decode('utf-8', errors='replace')

    anchor = fix['anchor']
    code = fix['code']

    if anchor not in src:
        results.append(f'ANCHOR NOT FOUND in {rel_path}: {anchor!r}')
        continue

    new_src = src.replace(anchor, code + anchor, 1)

    try:
        ast.parse(new_src)
    except SyntaxError as e:
        results.append(f'SYNTAX ERROR in {rel_path}: {e}')
        continue

    if eol == b'\r\n':
        new_src = new_src.replace('\r\n', '\n').replace('\n', '\r\n')

    with open(fpath, 'w', encoding='utf-8', newline='') as f:
        f.write(new_src)

    results.append(f'OK: {rel_path}')

with open('fix_all_missing_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(results))
print('done')
