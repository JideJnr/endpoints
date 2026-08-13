import os

files_and_fns = {
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\team_watcher\team_watcher.py': [
        '_analysis_summary', '_build_overview', '_league_name_for_doc',
        '_league_name_from_rows', '_matchup_context', '_merge_aliases',
        '_position_value', '_resolve_watcher_key', '_resolve_watcher_row',
        '_should_refresh_web_context', '_slug', '_table_from_rows',
        '_table_gap', '_table_lookup', '_team_name', '_team_position',
        '_team_web_context', '_unique_tournament_id_for_doc',
    ],
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\mongo_store.py': ['_latest_prediction'],
}

out = []
for fpath, fns in files_and_fns.items():
    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    fname = fpath.split('\\')[-1]
    out.append(f'\n{"="*50}\n{fname}')
    for fn in fns:
        call_lines = [(i, line) for i, line in enumerate(lines) if fn + '(' in line]
        if not call_lines:
            out.append(f'\n  {fn}: no call sites found')
            continue
        out.append(f'\n  --- {fn} ({len(call_lines)} calls) ---')
        # Show first 2 call sites with context
        for i, line in call_lines[:2]:
            start = max(0, i-1)
            end = min(len(lines), i+3)
            for j in range(start, end):
                marker = '>>>' if j == i else '   '
                out.append(f'  {marker} {j+1}: {lines[j].rstrip()}')

with open('debug_tw_callsites_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
