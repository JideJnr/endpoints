"""
Reset deferred predictions and trigger re-match / re-enrich / re-predict.

Usage:
    python -m tools.reset_deferred_predictions
    python tools/reset_deferred_predictions.py
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear deferred predictions (insufficient data) from the match buffer, "
        "reset enrichment state, and re-run the unified upcoming pipeline to rematch, "
        "renrich, and repredict affected fixtures.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report how many matches would be reset; do not modify any data.",
    )
    args = parser.parse_args(argv)

    from app.storage.db import _conn
    from app.storage.league_memory import _init_db
    from app.storage.buffer import _init_buffer_table

    _init_db()

    with _conn() as conn:
        _init_buffer_table(conn)
        rows = conn.execute(
            """
            select match_id, match_date, name,
                   json_extract(raw_enriched, '$.prediction_error') as prediction_error,
                   json_extract(raw_enriched, '$.sofascore_match_status') as sofascore_match_status
            from match_buffer
            where is_finished = 0
              and raw_enriched is not null
              and (
                json_extract(raw_enriched, '$.prediction_error') is not null
                or json_extract(raw_enriched, '$.sofascore_match_status') in ('no_match', 'srl_skip')
              )
            """
        ).fetchall()

    matches = [
        {
            "match_id": r["match_id"],
            "match_date": r["match_date"],
            "name": r["name"],
            "prediction_error": r["prediction_error"],
            "sofascore_match_status": r["sofascore_match_status"],
        }
        for r in rows
    ]

    print(f"Found {len(matches)} match(es) with deferred predictions or no_match/srl_skip status:")
    for m in matches:
        print(f"  - {m['match_id']}  {m['match_date']}  {m['name']}")
        print(f"    prediction_error: {m['prediction_error']}")
        print(f"    sofascore_match_status: {m['sofascore_match_status']}")

    if args.dry_run:
        print("\n[dry-run] No changes made. Re-run without --dry-run to reset and repredict.")
        return 0

    if not matches:
        print("\nNo matches to reset. Exiting.")
        return 0

    print(f"\nResetting {len(matches)} match(es) and running job_unified_upcoming...")
    from app.scheduling.scheduler import reset_deferred_predictions_and_repredict

    result = reset_deferred_predictions_and_repredict()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
