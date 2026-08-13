import sys, traceback
sys.path.insert(0, r'c:\Users\Victor\Documents\Personal Workstation\football\predictx')

out = []

# Step 1: check all modules that prediction_agent imports for _to_float usage
import ast

files_to_check = [
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\ai\prediction_agent.py',
    r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\enrichment\enriched_prediction.py',
]

for path in files_to_check:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        src = f.read()
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(ast.dump(node)[:120])
    has_to_float_import = '_to_float' in src[:3000]  # check top of file
    out.append(f"\n=== {path.split(chr(92))[-1]} ===")
    out.append(f"  _to_float in imports section: {has_to_float_import}")

# Step 2: try calling predict_sofascore_event
try:
    from app.ai.prediction_agent import predict_sofascore_event
    out.append("\npredict_sofascore_event imported OK")
    result = predict_sofascore_event({}, [], [])
    out.append(f"call result keys: {list(result.keys()) if isinstance(result, dict) else result}")
except NameError as e:
    out.append(f"\nNameError: {e}")
    out.append(traceback.format_exc())
except Exception as e:
    out.append(f"\n{type(e).__name__}: {e}")

# Step 3: check research_filter for _to_float
try:
    from app.research.research_filter import evaluate_pick
    out.append("\nresearch_filter imported OK")
except NameError as e:
    out.append(f"\nresearch_filter NameError: {e}")
    out.append(traceback.format_exc())
except Exception as e:
    out.append(f"\nresearch_filter {type(e).__name__}: {e}")

with open('debug_to_float_out.txt', 'w') as f:
    f.write('\n'.join(out))

print("done")
