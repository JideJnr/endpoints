"""
Script to update local function definitions to import from consolidated modules.
"""
import re
import os
from pathlib import Path

BASE = Path(".")  # running from predictx/ directory

# Files and their local functions to replace with imports
UPDATES = {
    # primitives.py replacements
    "app/utils/match_state.py": {
        "functions": ["_first_present", "_to_int", "_optional_int"],
        "import": "from app.utils.primitives import _first_present, _to_int, _optional_int",
    },
    "app/utils/desk_analytics.py": {
        "functions": ["_safe_json", "_to_float"],
        "import": "from app.utils.primitives import _safe_json, _to_float",
    },
    "app/utils/current_predictions.py": {
        "functions": ["_loads"],
        "import": "from app.utils.primitives import _loads",
    },
    "app/team_watcher/team_watcher_engine.py": {
        "functions": ["_to_int", "_to_float"],
        "import": "from app.utils.primitives import _to_int, _to_float",
    },
    "app/team_watcher/team_watcher.py": {
        "functions": ["_loads", "_to_int"],
        "import": "from app.utils.primitives import _loads, _to_int",
    },
    "app/storage/mongo_store.py": {
        "functions": ["_to_int", "_to_float", "_tournament_name", "_team_name"],
        "import": "from app.utils.primitives import _to_int, _to_float\nfrom app.utils.match_helpers import _tournament_name, _team_name",
    },
    "app/signal_combinations.py": {
        "functions": ["_to_int"],
        "import": "from app.utils.primitives import _to_int",
    },
    "app/routers/platform.py": {
        "functions": ["_to_int", "_to_float"],
        "import": "from app.utils.primitives import _to_int, _to_float",
    },
    "app/routers/agent.py": {
        "functions": ["_to_int", "_to_float"],
        "import": "from app.utils.primitives import _to_int, _to_float",
    },
    "app/risk/validation_gate.py": {
        "functions": ["_to_float"],
        "import": "from app.utils.primitives import _to_float",
    },
    "app/risk/risk_manager.py": {
        "functions": ["_to_float"],
        "import": "from app.utils.primitives import _to_float",
    },
    "app/monitoring/system_supervisor.py": {
        "functions": ["_loads"],
        "import": "from app.utils.primitives import _loads",
    },
    "app/monitoring/self_learner.py": {
        "functions": ["_safe_json"],
        "import": "from app.utils.primitives import _safe_json",
    },
    "app/monitoring/prediction_monitor.py": {
        "functions": ["_loads"],
        "import": "from app.utils.primitives import _loads",
    },
    "app/models/poisson.py": {
        "functions": ["_to_int"],
        "import": "from app.utils.primitives import _to_int",
    },
    "app/match_facts.py": {
        "functions": ["_first_present", "_optional_int", "_to_int"],
        "import": "from app.utils.primitives import _first_present_key as _first_present, _optional_int, _to_int",
    },
    "app/market/season_stage.py": {
        "functions": ["_safe_num"],
        "import": "from app.utils.primitives import _safe_num",
    },
    "app/market/market.py": {
        "functions": ["_to_float"],
        "import": "from app.utils.primitives import _to_float",
    },
    "app/enrichment/enriched_prediction.py": {
        "functions": ["_to_float", "_to_int", "_team_name", "_played_seconds"],
        "import": "from app.utils.primitives import _to_float, _to_int\nfrom app.utils.match_helpers import _team_name, _played_seconds",
    },
    "app/enrichment/contextual_intelligence.py": {
        "functions": ["_to_float"],
        "import": "from app.utils.primitives import _to_float",
    },
    "app/data_clients/sportybet_client.py": {
        "functions": ["_first_present"],
        "import": "from app.utils.primitives import _first_present_key as _first_present",
    },
    "app/competition/sos.py": {
        "functions": ["_to_int"],
        "import": "from app.utils.primitives import _to_int",
    },
    "app/competition/competition_special.py": {
        "functions": ["_optional_int", "_safe_num"],
        "import": "from app.utils.primitives import _optional_int, _safe_num",
    },
    "app/bet_builder/core.py": {
        "functions": ["_to_float", "_to_int", "_normalise_selection"],
        "import": "from app.utils.primitives import _to_float, _to_int\nfrom app.utils.match_helpers import _normalise_selection",
    },
    "app/ai/prediction_agent.py": {
        "functions": ["_to_int", "_to_float", "_fraction_to_probability", "_tournament_name", "_team_name", "_norm"],
        "import": "from app.utils.primitives import _to_int, _to_float\nfrom app.utils.match_helpers import _fraction_to_probability, _tournament_name, _team_name, _norm",
    },
    "app/ai/chat_agent.py": {
        "functions": ["_to_int", "_to_float"],
        "import": "from app.utils.primitives import _to_int, _to_float",
    },
    "app/ai/ai_brain.py": {
        "functions": ["_to_int", "_to_float"],
        "import": "from app.utils.primitives import _to_int, _to_float",
    },
    "app/ai/ai_betbuilder.py": {
        "functions": ["_team_name"],
        "import": "from app.utils.match_helpers import _team_name",
    },
    "app/ai/llm_pipeline.py": {
        "functions": ["_safe_float", "_safe_int"],
        "import": "from app.utils.primitives import _safe_float",
    },
    "app/models/odds_predictor.py": {
        "functions": ["_tournament_name"],
        "import": "from app.utils.match_helpers import _tournament_name",
    },
    "app/storage/buffer.py": {
        "functions": ["_played_seconds_local"],
        "import": "from app.utils.match_helpers import _played_seconds as _played_seconds_local",
    },
    "app/utils/time_context.py": {
        "functions": ["_to_datetime_utc"],
        "import": "from app.utils.match_helpers import _to_datetime_utc",
    },
    "app/utils/portfolio.py": {
        "functions": ["_normalise_selection"],
        "import": "from app.utils.match_helpers import _normalise_selection",
    },
    "app/routers/frontend.py": {
        "functions": ["_normalise_selection"],
        "import": "from app.utils.match_helpers import _normalise_selection",
    },
}


def remove_function(content: str, func_name: str) -> str:
    """Remove a function definition from content."""
    # Match the function definition and its body
    pattern = rf'(?m)^def {re.escape(func_name)}\([^)]*\)[^:]*:\n(?:\s+.*\n)*'
    return re.sub(pattern, '', content)


def add_import(content: str, import_line: str) -> str:
    """Add an import line after the last existing import, or at the top."""
    lines = content.split('\n')
    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            last_import_idx = i
    
    # Find a good place to insert - after the last import
    insert_idx = last_import_idx + 1
    # Skip any blank lines after imports
    while insert_idx < len(lines) and lines[insert_idx].strip() == '':
        insert_idx += 1
    
    lines.insert(insert_idx, import_line)
    lines.insert(insert_idx + 1, '')
    return '\n'.join(lines)


def update_file(filepath: str, funcs: list[str], import_line: str):
    """Update a file by removing local functions and adding imports."""
    full_path = BASE / filepath
    if not full_path.exists():
        print(f"SKIP: {filepath} (not found)")
        return
    
    content = full_path.read_text(encoding='utf-8')
    original = content
    
    # Remove functions
    for func in funcs:
        content = remove_function(content, func)
    
    # Add import if functions were removed
    if content != original:
        content = add_import(content, import_line)
        full_path.write_text(content, encoding='utf-8')
        print(f"UPDATED: {filepath}")
    else:
        print(f"SKIP: {filepath} (no changes)")


def main():
    for filepath, config in UPDATES.items():
        update_file(filepath, config["functions"], config["import"])


if __name__ == "__main__":
    main()
