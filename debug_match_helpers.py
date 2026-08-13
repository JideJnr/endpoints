path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\utils\match_helpers.py'
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

out = []
for i, line in enumerate(lines[115:140], 116):
    out.append(f'{i}: {line.rstrip()}')

with open('debug_match_helpers_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
