import os

targets = ['grade_predictions_for_date', '_sofascore_ids_for_predictions']

for root, dirs, files in os.walk('app'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for fname in files:
        if not fname.endswith('.py'):
            continue
        path = os.path.join(root, fname)
        try:
            content = open(path, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        for t in targets:
            if t in content:
                print(f'{t}  ->  {path}')
