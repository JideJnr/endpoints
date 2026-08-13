"""Remove duplicate function definitions from non-canonical files."""
from __future__ import annotations
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FIXES: dict[str, tuple[str, list[str]]] = {
    "_context_source": (
        "app/models/poisson.py",
        ["app/models/dixon_coles.py"],
    ),
    "_is_live_doc": (
        "app/enrichment/match_enrichment.py",
        ["app/routers/frontend.py"],
    ),
    "_is_finished_doc": (
        "app/enrichment/match_enrichment.py",
        ["app/routers/frontend.py"],
    ),
    "_is_not_started_period": (
        "app/storage/buffer.py",
        ["app/enrichment/enriched_prediction.py"],
    ),
    "_date_from_start_time": (
        "app/storage/buffer.py",
        ["app/utils/mobile_bridge.py"],
    ),
    "_safe_call": (
        "app/monitoring/prediction_monitor.py",
        ["app/monitoring/system_supervisor.py"],
    ),
    "_band": (
        "app/competition/competition_special.py",
        ["app/team_watcher/team_watcher.py"],
    ),
    "_impact": (
        "app/enrichment/contextual_intelligence.py",
        ["app/monitoring/prediction_audit.py"],
    ),
    "_fetch_web": (
        "app/storage/buffer.py",
        ["app/enrichment/enrichment.py"],
    ),
    "_ensure_column": (
        "app/storage/db.py",
        ["app/competition/competition_special.py"],
    ),
    "_ensure_signal_outcomes_table": (
        "app/storage/league_memory/schema.py",
        ["app/storage/league_memory/crud.py"],
    ),
    "_ensure_signal_combination_outcomes_table": (
        "app/storage/db.py",
        ["app/storage/league_memory/schema.py"],
    ),
    "_side_from_selection_and_match": (
        "app/storage/league_memory/_helpers.py",
        ["app/market/market_intent.py"],
    ),
    "_side_from_team_selection": (
        "app/utils/match_helpers.py",
        ["app/routers/frontend.py"],
    ),
    "_match_sides": (
        "app/utils/match_helpers.py",
        ["app/routers/frontend.py"],
    ),
    "_extract_1x2": (
        "app/storage/buffer.py",
        [
            "app/enrichment/match_enrichment.py",
            "app/models/odds_predictor.py",
            "app/monitoring/prediction_audit.py",
            "app/routers/frontend.py",
        ],
    ),
    "_data_sources": (
        "app/storage/buffer.py",
        ["app/enrichment/enrichment.py", "app/enrichment/match_enrichment.py"],
    ),
    "_hf_token": (
        "app/config/config.py",
        ["app/ai/ai_brain.py", "app/enrichment/enrichment.py"],
    ),
    "_parse_datetime": (
        "app/competition/competition_special.py",
        ["app/enrichment/contextual_intelligence.py", "app/scheduling/scheduler.py"],
    ),
}


def _find_func_spans(src: str, func_name: str) -> list[tuple[int, int]]:
    """Return list of (start_line, end_line) 1-indexed for each def of func_name."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    n = len(lines)
    spans = []
    # Collect all function nodes at any level
    all_funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_funcs.append(node)
    all_funcs.sort(key=lambda x: x.lineno)

    for i, node in enumerate(all_funcs):
        if node.name != func_name:
            continue
        # Start: include decorators
        start = node.lineno
        if node.decorator_list:
            start = node.decorator_list[0].lineno
        # End: next sibling at same or lower indent, or EOF
        # Use end_lineno if available (Python 3.8+)
        if hasattr(node, "end_lineno") and node.end_lineno:
            end = node.end_lineno
        else:
            # Fallback: find next def/class at same col_offset
            end = n
            for other in all_funcs[i + 1:]:
                if other.col_offset <= node.col_offset:
                    end = other.lineno - 1
                    break
        spans.append((start, end))
    return spans


def remove_func(path: Path, func_name: str) -> bool:
    src = path.read_text(encoding="utf-8", errors="replace")
    spans = _find_func_spans(src, func_name)
    if not spans:
        print(f"  NOT FOUND: {func_name} in {path.relative_to(ROOT)}")
        return False

    lines = src.splitlines(keepends=True)
    # Remove from bottom to top
    for start, end in sorted(spans, reverse=True):
        # Also eat blank lines immediately before the function
        pre = start - 2  # 0-indexed line before start
        while pre >= 0 and lines[pre].strip() == "":
            pre -= 1
        del_from = pre + 1  # 0-indexed
        del_to = end  # end is 1-indexed inclusive → 0-indexed exclusive = end
        del lines[del_from:del_to]

    path.write_text("".join(lines), encoding="utf-8")
    print(f"  REMOVED: {func_name} from {path.relative_to(ROOT)}")
    return True


def main() -> None:
    for func_name, (canonical, non_canonicals) in FIXES.items():
        print(f"\n{func_name} (canonical: {canonical})")
        for nc in non_canonicals:
            p = ROOT / nc
            if not p.exists():
                print(f"  MISSING FILE: {nc}")
                continue
            remove_func(p, func_name)
    print("\nDONE")


if __name__ == "__main__":
    main()
