import os, ast, sys

root = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app'

def get_defined_and_imported(tree, src):
    defined = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                imported.add(name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imported.add(name)
    return defined, imported

def get_called_names(tree):
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                # e.g. obj.method() - skip, only care about bare names
                pass
    return called

# Builtins and common globals to ignore
BUILTINS = set(dir(__builtins__)) if isinstance(__builtins__, dict) else set(dir(__builtins__))
BUILTINS.update({
    'print', 'len', 'range', 'int', 'str', 'float', 'bool', 'list', 'dict',
    'set', 'tuple', 'type', 'isinstance', 'issubclass', 'hasattr', 'getattr',
    'setattr', 'delattr', 'callable', 'iter', 'next', 'enumerate', 'zip',
    'map', 'filter', 'sorted', 'reversed', 'min', 'max', 'sum', 'abs', 'round',
    'open', 'super', 'object', 'property', 'staticmethod', 'classmethod',
    'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError',
    'AttributeError', 'RuntimeError', 'StopIteration', 'NotImplementedError',
    'NameError', 'ImportError', 'OSError', 'IOError', 'FileNotFoundError',
    'any', 'all', 'vars', 'dir', 'id', 'hash', 'repr', 'format', 'chr', 'ord',
    'hex', 'oct', 'bin', 'pow', 'divmod', 'eval', 'exec', 'compile',
    'globals', 'locals', 'breakpoint', 'input', 'exit', 'quit',
    'True', 'False', 'None', 'NotImplemented', 'Ellipsis',
    'BaseException', 'GeneratorExit', 'SystemExit', 'KeyboardInterrupt',
    'ArithmeticError', 'ZeroDivisionError', 'OverflowError',
    'LookupError', 'MemoryError', 'RecursionError', 'UnicodeError',
    'UnicodeDecodeError', 'UnicodeEncodeError', 'PermissionError',
    'TimeoutError', 'ConnectionError', 'BrokenPipeError',
    'frozenset', 'bytes', 'bytearray', 'memoryview', 'complex',
    'slice', 'zip', 'reversed', 'enumerate',
    # common decorators / typing
    'dataclass', 'field', 'Optional', 'Union', 'List', 'Dict', 'Tuple',
    'Any', 'Set', 'Callable', 'Type', 'ClassVar', 'Final',
    'overload', 'abstractmethod', 'cached_property',
    # pytest / test
    'pytest', 'mock', 'patch',
    # common third-party
    'logger', 'logging',
})

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
            tree = ast.parse(src)
        except Exception as e:
            results.append(f"PARSE ERROR {fpath}: {e}")
            continue

        defined, imported = get_defined_and_imported(tree, src)
        called = get_called_names(tree)
        available = defined | imported | BUILTINS

        missing = sorted(
            name for name in called
            if name not in available
            and not name.startswith('__')
            and name[0].islower()  # focus on private/helper functions
        )
        if missing:
            rel = fpath.replace(root, '').lstrip('\\')
            results.append(f"\n{rel}:")
            for name in missing:
                results.append(f"  {name}()")

with open('debug_undefined_fns_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(results) if results else 'No undefined function calls found.')

print('done')
