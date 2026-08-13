import sys

files = [
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\utils\match_helpers.py',
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\league_memory\crud.py',
]

out = []
for path in files:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    out.append(f'\n=== {path.split(chr(92))[-1]} ===')
    # Show first 50 lines (imports)
    for i, line in enumerate(lines[:50], 1):
        out.append(f'{i}: {line.rstrip()}')
    # Show lines with _to_float
    out.append('--- _to_float lines ---')
    for i, line in enumerate(lines, 1):
        if '_to_float' in line:
            out.append(f'{i}: {line.rstrip()}')

with open('debug_imports_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
