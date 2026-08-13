path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\scheduling\scheduler.py'
with open(path, 'r', encoding='utf-8', errors='replace') as f:
    lines = f.readlines()

out = []

# Find _safe_call
for i, line in enumerate(lines):
    if '_safe_call(' in line:
        start = max(0, i-2)
        end = min(len(lines), i+3)
        out.append(f'\n--- _safe_call line {i+1} ---')
        for j in range(start, end):
            marker = '>>>' if j == i else '   '
            out.append(f'{marker} {j+1}: {lines[j].rstrip()}')

# Show job_system_supervisor body
out.append('\n\n--- job_system_supervisor ---')
in_fn = False
for i, line in enumerate(lines):
    if 'def job_system_supervisor(' in line:
        in_fn = True
    if in_fn:
        out.append(f'{i+1}: {lines[i].rstrip()}')
        if i > 0 and in_fn and line.strip() == '' and i > 1381 + 3:
            # check if next non-empty line starts a new def
            for j in range(i+1, min(i+5, len(lines))):
                if lines[j].strip().startswith('def ') or lines[j].strip().startswith('class '):
                    in_fn = False
                    break
        if not in_fn:
            break
        if i > 1410:
            break

# Show job_autopilot_guardian body
out.append('\n\n--- job_autopilot_guardian ---')
in_fn = False
for i, line in enumerate(lines):
    if 'def job_autopilot_guardian(' in line:
        in_fn = True
    if in_fn:
        out.append(f'{i+1}: {lines[i].rstrip()}')
        if i > 1450:
            break

with open('debug_sched2_out.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('done')
