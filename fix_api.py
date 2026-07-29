path = r'c:\Users\Victor\Documents\Personal Workstation\football\football_frontend\src\services\apis\footballApi.ts'

with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find and remove the bad backtick-n literal block, restore clean getModelExplorer
# The bad block starts with the mangled getBrainSpecialists and ends before getModelExplorer
import re

# Remove everything from the bad getBrainSpecialists up to (but not including) getModelExplorer
bad_pattern = re.compile(
    r'export const getBrainSpecialists.*?(?=export const getModelExplorer)',
    re.DOTALL
)

good_block = (
    "export const getBrainSpecialists = (league = '', pickType = '') =>\n"
    "  api.get('/analytics/brain/specialists', { params: { league, pick_type: pickType } }).then(r => r.data);\n"
    "\n"
    "export const getBrainSummary = () =>\n"
    "  api.get('/analytics/brain/summary').then(r => r.data);\n"
    "\n"
    "export const getBrainModelWeights = () =>\n"
    "  api.get('/analytics/brain/model-weights').then(r => r.data);\n"
    "\n"
    "export const getBrainSignalWeights = (league = '') =>\n"
    "  api.get('/analytics/brain/signals', { params: { league } }).then(r => r.data);\n"
    "\n"
    "export const triggerBrainLearn = () =>\n"
    "  api.post('/analytics/brain/learn').then(r => r.data);\n"
    "\n"
)

new_content, count = bad_pattern.subn(good_block, content)
print(f"Replacements made: {count}")

if count:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("File written OK")
else:
    print("Pattern not found — printing surrounding context:")
    idx = content.find('getBrainSpecialists')
    if idx >= 0:
        print(repr(content[idx:idx+300]))
    else:
        print("getBrainSpecialists not found at all")
