path = r'c:\Users\Victor\Documents\Personal Workstation\football\football_frontend\src\services\apis\footballApi.ts'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines, 1):
    if 'getModelExplorer' in line or 'model-explorer' in line:
        print(f"L{i:4d}: {repr(line)}")
