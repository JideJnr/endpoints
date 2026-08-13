import ast, os

cwd = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'

fixes = {
    r'app\ai\ai_betbuilder.py': {
        'anchor': 'def _odds_dimension(',
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

    r'app\competition\competition_special.py': {
        'anchor': 'def apply_known_competition_context(',
        'code': '''
def _name_quality_score(name: str) -> float:
    """Rough quality proxy for a team name — longer names score higher."""
    n = str(name or '').strip()
    if not n:
        return 0.5
    return min(1.0, 0.4 + len(n) / 40)


''',
    },

    r'app\monitoring\system_supervisor.py': {
        'anchor': 'def run_system_supervisor(',
        'code': '''
def _age_seconds(timestamp) -> float:
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

    r'app\routers\agent.py': {
        'anchor': 'def get_memory_leagues(',
        'code': '''
def _is_finished_doc(doc: dict) -> bool:
    """Return True when the match document represents a finished match."""
    period = str(doc.get('period') or '').lower()
    status = doc.get('status') or {}
    status_type = str((status.get('type') if isinstance(status, dict) else status) or '').lower()
    return period in {'ft', 'finished', 'ended', 'aet', 'ap'} or status_type in {'finished', 'ended'}


''',
    },

    r'app\storage\mongo_store.py': {
        'anchor': 'def _get_settings(',
        'code': '''
class _RowCountStub:
    """Stub returned when pruning is disabled — behaves like a zero-row result."""
    rowcount = 0


''',
    },

    r'app\team_watcher\team_watcher_engine.py': {
        'anchor': 'def init_tw_tables(',
        'code': '''
def _compute_profile_stats(rows: list) -> dict:
    """Compute win/draw/loss/goal stats from a list of match rows."""
    wins = draws = losses = 0
    goals_for = goals_against = 0
    for row in rows or []:
        try:
            own = int(row['own_goals'] or 0)
            opp = int(row['opp_goals'] or 0)
        except Exception:
            continue
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
    side_label = str(side or 'team').capitalize()
    if result == 'win':
        return f'{side_label} won {score}'
    if result == 'loss':
        return f'{side_label} lost {score}'
    if result == 'draw':
        return f'Draw {score}'
    return None


''',
    },

    r'app\utils\portfolio.py': {
        'anchor': 'def _annotate(',
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

with open('fix_remaining_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(results))
print('done')
