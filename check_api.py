import os

path = r'football_frontend\src\services\apis\footballApi.ts'
# walk up to find it
for root, dirs, files in os.walk(r'..'):
    for f in files:
        if f == 'footballApi.ts':
            full = os.path.join(root, f)
            print('Found:', full)
            with open(full, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines, 1):
                s = line.strip()
                if any(x in s for x in ['export', 'analytics', 'model', 'specialist', 'brain', 'grade', 'clv']):
                    print(f"L{i:4d}: {s[:120]}")
