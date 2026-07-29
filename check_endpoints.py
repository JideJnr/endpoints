import re

with open('app/routers/frontend.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    if any(x in line for x in ['model_explorer', 'model-explorer', 'specialist', 'analytics', 'grade', 'brain']):
        print(f"L{i:4d}: {line.rstrip()}")
