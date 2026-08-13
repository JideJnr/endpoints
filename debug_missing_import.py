import os, ast, sys

root = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app'
results = []

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != '__pycache__']
    for fname in filenames:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(dirpath, fname)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                src = f.read()
        except Exception:
            continue
        if '_to_float' not in src:
            continue
        # Check if it's imported or defined
        has_import = (
            'from app.utils.primitives import' in src and '_to_float' in src.split('from app.utils.primitives import')[1].split('\n')[0]
        ) or (
            'from app.storage.league_memory._helpers import' in src and '_to_float' in src.split('from app.storage.league_memory._helpers import')[1].split('\n')[0]
        ) or (
            'def _to_float' in src
        ) or (
            'import _to_float' in src
        )
        if not has_import:
            # Count usages
            count = src.count('_to_float(')
            rel = fpath.replace(root, '').lstrip('\\')
            results.append(f"{rel}: {count} usages, NO import found")

with open('debug_missing_import_out.txt', 'w') as f:
    f.write('\n'.join(results) if results else 'All files with _to_float have an import.')

print("done")
