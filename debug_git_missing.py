import subprocess, sys

out = []

# Check git log for these functions
for fn in ['_regime_info', '_detail_country', '_probability_unit']:
    result = subprocess.run(
        ['git', 'log', '--all', '-S', f'def {fn}', '--oneline'],
        capture_output=True, text=True,
        cwd=r'c:\Users\Victor\Documents\Personal Workstation\football\predictx'
    )
    out.append(f'\n=== git log for def {fn} ===')
    out.append(result.stdout.strip() or '(not found in git history)')

with open('debug_git_missing_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
