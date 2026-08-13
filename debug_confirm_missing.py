import os, ast

files_to_check = {
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\ai\ai_betbuilder.py': ['_total_goals'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\ai\llm_pipeline.py': ['_aggregate_specialists', '_build_model_summary', '_call_llm', '_safe_int'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\competition\competition_special.py': ['_name_quality_score'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\monitoring\prediction_monitor.py': ['_record_monitor_activity'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\monitoring\self_learner.py': ['_calibration_verdict', '_grade_specialists_from_history', '_incorporate_ai_analysis', '_incorporate_user_behavior', '_safe_json_object'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\monitoring\system_supervisor.py': ['_age_seconds'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\routers\agent.py': ['_is_finished_doc'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\scheduling\scheduler.py': ['_ApschedulerShutdownNoiseFilter', '_conn'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\db.py': ['_is_sqlite_lock'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\mongo_store.py': ['_RowCountStub', '_latest_prediction', '_manual_finished_archive_doc', '_scheduled_event_archive_doc'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\team_watcher\team_watcher.py': ['_analysis_summary', '_build_overview', '_league_name_for_doc', '_league_name_from_rows', '_matchup_context', '_merge_aliases', '_position_value', '_resolve_watcher_key', '_resolve_watcher_row', '_should_refresh_web_context', '_slug', '_table_from_rows', '_table_gap', '_table_lookup', '_team_name', '_team_position', '_team_web_context', '_unique_tournament_id_for_doc'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\team_watcher\team_watcher_engine.py': ['_compute_profile_stats', '_note_for_result'],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\utils\portfolio.py': ['_normalise_league', '_start_time', '_time_window'],
}

out = []
for fpath, fns in files_to_check.items():
    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
    except FileNotFoundError:
        out.append(f'FILE NOT FOUND: {fpath}')
        continue

    fname = fpath.split('\\')[-1]
    missing_confirmed = []
    for fn in fns:
        # Check if defined anywhere in the file (including as class method)
        if f'def {fn}(' in src:
            status = 'defined_in_file'
        else:
            status = 'MISSING'
            missing_confirmed.append(fn)

    if missing_confirmed:
        out.append(f'\n{fname}: MISSING -> {missing_confirmed}')
    else:
        out.append(f'{fname}: all present (likely class methods or nested)')

with open('debug_confirm_missing_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
