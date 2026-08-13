path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\scheduling\scheduler.py'
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

out = []
targets = ['_safe_call(', '_parse_datetime(', 'with _conn(']
for i, line in enumerate(lines):
    for t in targets:
        if t in line:
            start = max(0, i-2)
            end = min(len(lines), i+3)
            out.append(f'\n--- {t} line {i+1} ---')
            for j in range(start, end):
                marker = '>>>' if j == i else '   '
                out.append(f'{marker} {j+1}: {lines[j].rstrip()}')
            break

with open('debug_sched_missing_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
