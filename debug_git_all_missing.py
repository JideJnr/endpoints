import subprocess, re

cwd = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'

missing_by_file = {
    'app/ai/ai_betbuilder.py': ['_total_goals'],
    'app/competition/competition_special.py': ['_name_quality_score'],
    'app/monitoring/prediction_monitor.py': ['_record_monitor_activity'],
    'app/monitoring/system_supervisor.py': ['_age_seconds'],
    'app/routers/agent.py': ['_is_finished_doc'],
    'app/scheduling/scheduler.py': ['_ApschedulerShutdownNoiseFilter', '_conn'],
    'app/storage/db.py': ['_is_sqlite_lock'],
    'app/storage/mongo_store.py': ['_RowCountStub', '_latest_prediction'],
    'app/team_watcher/team_watcher.py': [
        '_analysis_summary', '_build_overview', '_league_name_for_doc',
        '_league_name_from_rows', '_matchup_context', '_merge_aliases',
        '_position_value', '_resolve_watcher_key', '_resolve_watcher_row',
        '_should_refresh_web_context', '_slug', '_table_from_rows',
        '_table_gap', '_table_lookup', '_team_name', '_team_position',
        '_team_web_context', '_unique_tournament_id_for_doc',
    ],
    'app/team_watcher/team_watcher_engine.py': ['_compute_profile_stats', '_note_for_result'],
    'app/utils/portfolio.py': ['_normalise_league', '_start_time', '_time_window'],
}

out = []

for fpath, fns in missing_by_file.items():
    out.append(f'\n{"="*60}')
    out.append(f'FILE: {fpath}')
    for fn in fns:
        # Find commits that contain this function definition
        r = subprocess.run(
            ['git', 'log', '--all', '-S', f'def {fn}(', '--oneline', '--', fpath],
            capture_output=True, text=True, cwd=cwd
        )
        commits = r.stdout.strip().splitlines()
        if not commits:
            # Try without file filter
            r2 = subprocess.run(
                ['git', 'log', '--all', '-S', f'def {fn}(', '--oneline'],
                capture_output=True, text=True, cwd=cwd
            )
            commits = r2.stdout.strip().splitlines()

        out.append(f'\n  def {fn}:')
        if not commits:
            out.append('    NOT IN GIT HISTORY')
            continue

        # Use most recent commit
        commit = commits[0].split()[0]
        out.append(f'    found in: {commits[0]}')

        # Extract function body
        r3 = subprocess.run(
            ['git', 'show', f'{commit}:{fpath}'],
            capture_output=True, text=True, cwd=cwd
        )
        src = r3.stdout
        if not src:
            out.append('    could not read file from commit')
            continue

        pattern = rf'\ndef {re.escape(fn)}\('
        match = re.search(pattern, src)
        if not match:
            out.append('    function not found in that commit')
            continue

        start = match.start() + 1
        rest = src[start:]
        next_def = re.search(r'\ndef [a-zA-Z_]|\nclass [a-zA-Z_]', rest[1:])
        body = rest[:next_def.start() + 1] if next_def else rest[:2000]
        out.append('    BODY:')
        for line in body.rstrip().splitlines():
            out.append(f'    {line}')

with open('debug_git_all_missing_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
