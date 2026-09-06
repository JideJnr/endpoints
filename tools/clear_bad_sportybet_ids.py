"""One-shot script: null out sportybet_id values that are sofascore/competition
prefixed IDs across prediction_history, prediction_candidate_history, and
prediction_decision_log.

Rows are preserved — only the bad sportybet_id is cleared to NULL.
Graded rows keep their result/graded_at data intact.
"""
import sys
from app.storage.db import db_conn

TABLES = [
    "prediction_history",
    "prediction_candidate_history",
    "prediction_decision_log",
]

def run() -> None:
    with db_conn(timeout=60) as conn:
        total_cleared = 0
        for table in TABLES:
            try:
                r_sofa = conn.execute(
                    f"update {table} set sportybet_id = null "
                    "where sportybet_id like 'sofascore:%'",
                )
                r_comp = conn.execute(
                    f"update {table} set sportybet_id = null "
                    "where sportybet_id like 'competition:%'",
                )
                cleared = r_sofa.rowcount + r_comp.rowcount
                total_cleared += cleared
                print(f"  {table}: cleared {cleared} rows")
            except Exception as exc:
                print(f"  {table}: ERROR - {exc}", file=sys.stderr)

        conn.commit()
        print(f"Committed. Total rows cleared: {total_cleared}")

        # Verify
        print("\nVerification (should all be 0):")
        for table in TABLES:
            try:
                remaining = conn.execute(
                    f"select count(*) from {table} "
                    "where sportybet_id like 'sofascore:%' "
                    "   or sportybet_id like 'competition:%'"
                ).fetchone()[0]
                status = "OK" if remaining == 0 else f"STILL HAS {remaining} BAD ROWS"
                print(f"  {table}: {status}")
            except Exception as exc:
                print(f"  {table}: ERROR - {exc}", file=sys.stderr)

if __name__ == "__main__":
    run()
