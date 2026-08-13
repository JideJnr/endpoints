import ast, os

cwd = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'

modified = [
    r'app\enrichment\enriched_prediction.py',
    r'app\ai\ai_betbuilder.py',
    r'app\competition\competition_special.py',
    r'app\monitoring\prediction_monitor.py',
    r'app\monitoring\system_supervisor.py',
    r'app\routers\agent.py',
    r'app\storage\db.py',
    r'app\storage\mongo_store.py',
    r'app\team_watcher\team_watcher.py',
    r'app\team_watcher\team_watcher_engine.py',
    r'app\utils\portfolio.py',
    r'app\utils\match_helpers.py',
]

out = []
for rel in modified:
    fpath = os.path.join(cwd, rel)
    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            src = f.read()
        ast.parse(src)
        out.append(f'OK: {rel}')
    except SyntaxError as e:
        out.append(f'SYNTAX ERROR {rel}: {e}')
    except FileNotFoundError:
        out.append(f'NOT FOUND: {rel}')

with open('validate_syntax_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
