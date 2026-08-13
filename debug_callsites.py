import os

files_and_fns = {
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\ai\ai_betbuilder.py': ['_total_goals'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\competition\competition_special.py': ['_name_quality_score'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\monitoring\prediction_monitor.py': ['_record_monitor_activity'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\monitoring\system_supervisor.py': ['_age_seconds'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\routers\agent.py': ['_is_finished_doc'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\scheduling\scheduler.py': ['_ApschedulerShutdownNoiseFilter', '_conn'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\db.py': ['_is_sqlite_lock'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\mongo_store.py': ['_RowCountStub', '_latest_prediction'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\team_watcher\team_watcher_engine.py': ['_compute_profile_stats', '_note_for_result'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\utils\portfolio.py': ['_normalise_league', '_start_time', '_time_window'],
}

out = []

for fpath, fns in files_and_fns.items():
    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    fname = fpath.split('\\')[-1]
    out.append(f'\n{"="*50}\n{fname}')
    for fn in fns:
        out.append(f'\n  --- {fn} call sites ---')
        for i, line in enumerate(lines):
            if fn + '(' in line:
                # Show 2 lines before and 2 after
                start = max(0, i-2)
                end = min(len(lines), i+3)
                for j in range(start, end):
                    marker = '>>>' if j == i else '   '
                    out.append(f'  {marker} {j+1}: {lines[j].rstrip()}')

with open('debug_callsites_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
