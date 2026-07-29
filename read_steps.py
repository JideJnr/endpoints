with open('app/ai_prediction_pipeline.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = [
    '_apply_recency_decay',
    '_build_h2h_statement',
    '_step_h2h',
    '_step_team_history',
    '_previous_matches_for_team',
    '_team_history_summary',
]

in_block = False
block_name = ''
for i, line in enumerate(lines, 1):
    stripped = line.rstrip()
    for t in targets:
        if f'def {t}' in stripped:
            in_block = True
            block_name = t
            break
    if in_block:
        print(f"L{i:4d}: {stripped}")
        # stop after blank line following a def block (rough heuristic)
        if i > 1 and stripped == '' and block_name:
            # check if next non-empty line starts a new def
            for j in range(i, min(i+3, len(lines))):
                nxt = lines[j].strip()
                if nxt.startswith('def ') or nxt.startswith('class '):
                    in_block = False
                    break
