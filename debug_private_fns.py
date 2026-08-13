import os, ast, sys

root = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app'

BUILTINS = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))

def collect_file_info(src, fpath):
    tree = ast.parse(src)

    defined = set()
    imported = set()
    called_private = {}  # name -> [lineno, ...]

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id.startswith('_'):
                name = func.id
                called_private.setdefault(name, []).append(node.lineno)

    available = defined | imported | BUILTINS
    missing = {}
    for name, lines in called_private.items():
        if name not in available and not name.startswith('__'):
            missing[name] = lines
    return missing

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
            missing = collect_file_info(src, fpath)
        except SyntaxError as e:
            results.append(f"SYNTAX ERROR {fpath}: {e}")
            continue
        except Exception as e:
            results.append(f"ERROR {fpath}: {e}")
            continue

        if missing:
            rel = fpath.replace(root, '').lstrip('\\')
            results.append(f"\n{rel}:")
            for name, lines in sorted(missing.items()):
                results.append(f"  {name}()  [lines: {', '.join(str(l) for l in lines[:5])}]")

with open('debug_private_fns_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(results) if results else 'No undefined private function calls found.')

print('done')
