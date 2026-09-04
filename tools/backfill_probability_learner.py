"""
Backfill the probability learner from already-graded prediction history.

Context
-------
The live grading path (storage/league_memory/queries.py, grade_predictions_for_date)
calls app.models.probability_learner.learn_probabilities() once per graded match, so
the learner should get smarter as results come in. It never did: that block accessed
an sqlite3.Row with `.get(...)`, which sqlite3.Row does not support, so it raised
AttributeError on the very first line every single time -- silently, because the
surrounding except swallowed it without logging. Confirmed live: prediction_history
had 844 graded rows while probability_patterns / signal_outcome_map (the two tables
that call was supposed to fill) had zero.

That bug is now fixed (Row-safe access, failures logged instead of swallowed), so
going forward each newly graded match will feed the learner correctly. This script
is the one-time catch-up: it replays every already-graded match through the same
learning logic (ProbabilityLearner.run_learning_cycle, which reads prediction_history
directly with sqlite3.Row-safe access -- this method itself was never buggy, it was
just never called from anywhere) so the historical 844 matches aren't lost, instead
of waiting for new matches to trickle in one at a time.

Safe to re-run: record_outcome() is an upsert keyed on (pattern/league/pick_type), so
running this twice just re-adds the same rows on top of themselves. Run --dry-run
first if you want to see the count before writing anything.

Usage:
    python tools/backfill_probability_learner.py --dry-run
    python tools/backfill_probability_learner.py
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report how many graded matches would be replayed; do not write anything.",
    )
    args = parser.parse_args(argv)

    from app.storage.db import db_conn
    from app.storage.league_memory import _init_db

    _init_db()

    with db_conn(timeout=10) as conn:
        graded_count = conn.execute(
            """
            select count(*) as n
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss', 'draw')
              and pick_type != 'no_bet'
              and signals_json is not null
              and signals_json != '[]'
            """
        ).fetchone()["n"]

        def table_count(name: str) -> int:
            try:
                return conn.execute(f"select count(*) as n from {name}").fetchone()["n"]
            except Exception:
                return 0

        before = {
            "probability_patterns": table_count("probability_patterns"),
            "signal_outcome_map": table_count("signal_outcome_map"),
        }

    print(f"Graded matches eligible to replay: {graded_count}")
    print(f"Before: probability_patterns={before['probability_patterns']}, "
          f"signal_outcome_map={before['signal_outcome_map']}")

    if args.dry_run:
        print("\n[dry-run] No changes made. Re-run without --dry-run to backfill.")
        return 0

    if not graded_count:
        print("\nNothing to backfill. Exiting.")
        return 0

    from app.models.probability_learner import ProbabilityLearner

    print("\nReplaying graded history through the probability learner...")
    result = ProbabilityLearner().run_learning_cycle()
    print(f"  status: {result.get('status')}")
    print(f"  patterns_learned (rows successfully replayed): {result.get('patterns_learned')}")
    print(f"  total_graded (rows considered): {result.get('total_graded')}")

    with db_conn(timeout=10) as conn:
        after = {
            "probability_patterns": conn.execute("select count(*) as n from probability_patterns").fetchone()["n"],
            "signal_outcome_map": conn.execute("select count(*) as n from signal_outcome_map").fetchone()["n"],
        }

    print(f"\nAfter:  probability_patterns={after['probability_patterns']}, "
          f"signal_outcome_map={after['signal_outcome_map']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
