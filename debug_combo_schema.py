path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\storage\league_memory\queries.py'
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

out = []
for i, line in enumerate(lines):
    if 'signal_combination_outcomes' in line:
        start = max(0, i-2)
        end = min(len(lines), i+20)
        out.append(f'\n--- line {i+1} ---')
        for j in range(start, end):
            out.append(f'{j+1}: {lines[j].rstrip()}')

with open('debug_combo_schema_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
