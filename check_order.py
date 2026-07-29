with open('app/ai_prediction_pipeline.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = ['H2H_FALLBACK', 'COMMON_FALLBACK', 'FORM_FALLBACK', 'ODDS_FALLBACK', 'SIMILAR_FALLBACK']
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    for t in targets:
        if t in stripped:
            print(f"L{i:4d}: {stripped[:100]}")
            break
