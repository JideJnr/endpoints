path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\league_memory\queries.py'
with open(path, 'rb') as f:
    raw = f.read()

eol = b'\r\n' if b'\r\n' in raw else b'\n'
src = raw.decode('utf-8', errors='replace')

# Fix 1: remove _ensure_signal_outcomes_table from crud import
old_crud = 'from .crud import _sofascore_ids_for_predictions, store_local_signal_outcomes, _aggregate_resolved_snapshots, _safe_mark_buffer_finished, _ensure_signal_outcomes_table, _backfill_local_signal_outcomes_from_history'
new_crud = 'from .crud import _sofascore_ids_for_predictions, store_local_signal_outcomes, _aggregate_resolved_snapshots, _safe_mark_buffer_finished, _backfill_local_signal_outcomes_from_history'

# Fix 2: add _ensure_signal_outcomes_table to schema import
old_schema = 'from .schema import _ensure_signal_combination_outcomes_table'
new_schema = 'from .schema import _ensure_signal_combination_outcomes_table, _ensure_signal_outcomes_table'

if old_crud not in src:
    print(f'CRUD anchor not found')
elif old_schema not in src:
    print(f'SCHEMA anchor not found')
else:
    new_src = src.replace(old_crud, new_crud, 1).replace(old_schema, new_schema, 1)
    import ast
    try:
        ast.parse(new_src)
        print('syntax OK')
    except SyntaxError as e:
        print(f'SyntaxError: {e}')
        exit(1)
    if eol == b'\r\n':
        new_src = new_src.replace('\r\n', '\n').replace('\n', '\r\n')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(new_src)
    print('written')
