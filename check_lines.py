with open('app/ai_prediction_pipeline.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines[148:168], 149):
    print(f"L{i:4d}: {line.rstrip()}")
