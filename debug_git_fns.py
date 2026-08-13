import subprocess, re

commit = '2f47dd3'
fpath = 'app/enrichment/enriched_prediction.py'

result = subprocess.run(
    ['git', 'show', f'{commit}:{fpath}'],
    capture_output=True, text=True,
    cwd=r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'
)
src = result.stdout

out = []
for fn in ['_regime_info', '_detail_country', '_probability_unit']:
    # Find function start
    pattern = rf'\ndef {fn}\('
    match = re.search(pattern, src)
    if not match:
        out.append(f'\n=== {fn} NOT FOUND ===')
        continue
    start = match.start() + 1  # skip leading \n
    # Find next top-level def or class after this one
    rest = src[start:]
    next_def = re.search(r'\ndef [a-zA-Z_]|\nclass [a-zA-Z_]', rest[1:])
    if next_def:
        body = rest[:next_def.start() + 1]
    else:
        body = rest[:3000]
    out.append(f'\n=== {fn} ===')
    out.append(body.rstrip())

with open('debug_git_fns_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
