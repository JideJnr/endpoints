import os

files = {
    r'app\ai\ai_betbuilder.py': ['class ', 'def run_', 'def build_'],
    r'app\competition\competition_special.py': ['def history_', 'def league_strength', 'def _history'],
    r'app\monitoring\system_supervisor.py': ['def supervise', 'def check_', 'def run_'],
    r'app\routers\agent.py': ['def _estimate', 'def _pick', 'def _format'],
    r'app\storage\mongo_store.py': ['class Mongo', 'class _Row', 'def _prune'],
    r'app\team_watcher\team_watcher_engine.py': ['def rebuild_', 'def _rebuild', 'def update_'],
    r'app\utils\portfolio.py': ['def _pick_', 'def _normalise', 'def build_'],
}

cwd = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'
out = []

for rel, patterns in files.items():
    fpath = os.path.join(cwd, rel)
    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
        src = f.read()
    out.append(f'\n=== {rel.split(chr(92))[-1]} ===')
    # Show first 5 top-level def/class names
    import re
    defs = re.findall(r'^(?:def|class) \w+', src, re.MULTILINE)
    out.append('  Top-level defs: ' + ', '.join(defs[:15]))

with open('debug_anchors_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
