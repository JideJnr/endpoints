import ast

path = r'c:\Users\Victor\Documents\Personal Workstation\football\predictx\app\enrichment\enriched_prediction.py'

with open(path, 'rb') as f:
    raw = f.read()

# Detect line ending
eol = b'\r\n' if b'\r\n' in raw else b'\n'
src = raw.decode('utf-8')

INSERT_BEFORE = 'class EnrichedPrediction:'

functions = '''
def _regime_info(doc: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.market.regime import get_regime_for_doc
        r = get_regime_for_doc(doc)
        return {
            "tier":           r.tier,
            "name":           r.name,
            "min_confidence": r.min_confidence,
            "edge_threshold": r.edge_threshold,
            "stake_cap":      r.stake_cap,
            "description":    r.description,
        }
    except Exception:
        return {}


def _detail_country(detail: dict[str, Any]) -> str | None:
    tournament = detail.get("tournament") or {}
    if isinstance(tournament, dict):
        category = tournament.get("category") or {}
        if isinstance(category, dict) and category.get("name"):
            return str(category.get("name"))
    return None


def _probability_unit(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    probability = number / 100 if number > 1 else number
    return max(0.0, min(1.0, probability))


'''

if INSERT_BEFORE not in src:
    print(f'ANCHOR NOT FOUND: {INSERT_BEFORE}')
else:
    new_src = src.replace(INSERT_BEFORE, functions + INSERT_BEFORE, 1)
    # Verify syntax
    try:
        ast.parse(new_src)
        print('syntax OK')
    except SyntaxError as e:
        print(f'SyntaxError: {e}')
        exit(1)
    # Write back preserving original line endings
    if eol == b'\r\n':
        new_src = new_src.replace('\r\n', '\n').replace('\n', '\r\n')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(new_src)
    print('written')

    # Verify all three present
    for fn in ['_regime_info', '_detail_country', '_probability_unit']:
        print(f'{fn}: {f"def {fn}(" in new_src}')
