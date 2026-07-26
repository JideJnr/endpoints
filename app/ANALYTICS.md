# Analytics & Monitoring

These modules give visibility into what the prediction engine is doing and how well it's performing. They are read-heavy — they query the database, produce summaries, and trigger corrections, but they don't modify prediction logic directly.

---

## `prediction_monitor.py`

The hourly learning and performance review loop.

`run_prediction_monitor(auto_correct=True)` — runs every hour and:
1. Grades any pending predictions that have results
2. Detects mismatch patterns (what signals were consistently present in losses)
3. Computes trend windows (last 24h, 7d, previous 7d)
4. Applies corrections if the monitor holds the `"learning"` authority lease
5. Returns a full report including `{metrics, trend, mismatches, corrections}`

**Win rate data (from last grading):**
- Overall: 52.3%
- Double chance: 58.6%
- Goals: 60.0%
- Match result (1X2): 33.3% (needs improvement)

The mismatch analysis shows which signals appeared in losses — currently `market_signal_misread` and `side_market_miss` dominate.

---

## `system_supervisor.py`

Operational health audit with safe auto-corrections.

`run_system_supervisor(auto_correct=True)` — runs every 3 minutes and checks:
- Duplicate buffer rows
- Pending predictions that have been waiting too long
- Stuck jobs (holding guards too long)
- Stale core jobs (haven't run recently)
- Ghost matches (not-started but kickoff passed)

When `auto_correct=True`, it releases stuck guards, purges ghosts, and re-runs stale jobs. Only holds `"operational"` authority — never touches learning/weights.

---

## `system_audit.py`

Structured audit report across all pipeline components.

`prediction_system_audit(limit)` → comprehensive report covering:
- Ingest status (last ingested, counts per date)
- Enrichment coverage (matched vs unmatched, SofaScore hit rate)
- Prediction coverage (predicted vs deferred, no-bet rate)
- Grading status (pending vs graded, win rates by market type)
- Job health (last run times, fail counts)

Accessible via `GET /system/audit`.

---

## `desk_analytics.py`

Performance analytics for the prediction desk.

Returns detailed breakdowns of:
- Win rate by pick type, league, country, confidence band
- ROI calculations (entry-odds basis and break-even proxy)
- CLV quality summary

Used by the Analytics page in the frontend.

---

## `match_intelligence.py`

Builds a rich intelligence document for the frontend match detail view.

`build_match_intelligence(doc, prediction, readiness)` → combines:
- Prediction engine picks + signals
- Market movement data
- SofaScore grades (if available)
- Regime tier and confidence adjustments
- AI analysis (if fetched)
- Similar matches context

This is what powers the match detail page's "deep dive" section.

---

## `current_predictions.py`

Loads and formats the latest predictions for the dashboard and today's picks list.

`list_recent_dashboard_predictions(hours, limit)` → queries `prediction_history` for recent ungraded predictions, attaches role learning data, filters out no_bet picks, and returns formatted rows ready for the frontend.

The `/predictions/today` endpoint uses this as its data source.
