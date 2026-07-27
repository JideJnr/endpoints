import sys, json
sys.path.insert(0, 'app')
import sqlite3
from league_memory import DB_PATH

c = sqlite3.connect(str(DB_PATH))
c.row_factory = sqlite3.Row

# 1. Total predictions and grading status
print('=== PREDICTION GRADING STATUS ===')
r = c.execute('''
    select
        count(*) as total,
        sum(case when result is not null then 1 else 0 end) as graded,
        sum(case when result is null then 1 else 0 end) as ungraded,
        sum(case when result is null and datetime(created_at) < datetime('now','-3 hours') then 1 else 0 end) as overdue
    from prediction_history
''').fetchone()
print(f'  total: {r["total"]}, graded: {r["graded"]}, ungraded: {r["ungraded"]}, overdue (>3h): {r["overdue"]}')

# 2. Ungraded predictions - do their matches exist as finished?
print('\n=== UNGRADED WITH FINISHED MATCH IN DB ===')
r2 = c.execute('''
    select count(*) as cnt from prediction_history ph
    where ph.result is null
      and exists (
          select 1 from matches m
          where m.match_id = ph.match_id
            and m.is_finished = 1
            and m.final_home_goals is not null
      )
''').fetchone()
print(f'  ungraded predictions that HAVE a finished match: {r2["cnt"]}')

# 3. Sample of ungraded predictions with finished matches
print('\n=== SAMPLE UNGRADED + FINISHED (up to 5) ===')
rows = c.execute('''
    select ph.id, ph.match_id, ph.match_name, ph.pick_type, ph.selection,
           ph.confidence, ph.created_at,
           m.final_home_goals, m.final_away_goals, m.is_finished
    from prediction_history ph
    join matches m on m.match_id = ph.match_id
    where ph.result is null and m.is_finished = 1 and m.final_home_goals is not null
    limit 5
''').fetchall()
for r in rows:
    print(f'  id={r["id"]} match={r["match_name"]} pick={r["pick_type"]}:{r["selection"]} score={r["final_home_goals"]}-{r["final_away_goals"]} created={r["created_at"]}')

# 4. Ungraded predictions - do their matches exist in match_buffer as finished?
print('\n=== UNGRADED WITH FINISHED IN match_buffer ===')
r3 = c.execute('''
    select count(*) as cnt from prediction_history ph
    where ph.result is null
      and exists (
          select 1 from match_buffer mb
          where mb.match_id = ph.match_id
            and mb.is_finished = 1
      )
''').fetchone()
print(f'  ungraded with finished match_buffer entry: {r3["cnt"]}')

# 5. Check match_id format mismatch
print('\n=== MATCH ID FORMAT SAMPLE ===')
ph_ids = c.execute('select distinct match_id from prediction_history limit 5').fetchall()
m_ids = c.execute('select distinct match_id from matches where is_finished=1 limit 5').fetchall()
mb_ids = c.execute('select distinct match_id from match_buffer limit 5').fetchall()
print('  prediction_history match_ids:', [r['match_id'] for r in ph_ids])
print('  matches (finished) match_ids:', [r['match_id'] for r in m_ids])
print('  match_buffer match_ids:', [r['match_id'] for r in mb_ids])

# 6. Check grade_overdue_predictions scheduler job
print('\n=== JOB RUNS (grading jobs) ===')
jobs = c.execute('''
    select job_id, status, started_at, finished_at, error_message
    from job_runs
    where job_id like '%grad%' or job_id like '%result%' or job_id like '%finish%'
    order by started_at desc limit 10
''').fetchall()
if jobs:
    for j in jobs:
        print(f'  {j["job_id"]} | {j["status"]} | {j["started_at"]} | err={j["error_message"]}')
else:
    print('  no grading jobs found in job_runs')

# 7. All recent job runs
print('\n=== ALL RECENT JOB RUNS (last 20) ===')
all_jobs = c.execute('''
    select job_id, status, started_at, error_message
    from job_runs
    order by started_at desc limit 20
''').fetchall()
for j in all_jobs:
    print(f'  {j["job_id"]} | {j["status"]} | {j["started_at"]} | err={j["error_message"]}')

c.close()
