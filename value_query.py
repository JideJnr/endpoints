import sqlite3, json, sys
from app.config import get_settings

db = str(get_settings().database_path)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

rows = conn.execute('''
    select ph.match_id, ph.match_name, ph.picks_json, ph.signals_json,
           mb.period, mb.is_live, mb.is_finished, mb.score_home, mb.score_away,
           mb.raw_enriched, mb.raw_sporty
    from prediction_history ph
    left join match_buffer mb on mb.match_id = ph.match_id
    where date(ph.created_at) = date("now")
      and ph.picks_json is not null and ph.picks_json != "[]"
    group by ph.match_id
    having ph.created_at = max(ph.created_at)
''').fetchall()

sys.stdout.write(f"Total predictions today: {len(rows)}\n")

results = []
for r in rows:
    picks = json.loads(r['picks_json'] or '[]')
    vb = next((p for p in picks if p.get('type') == 'value_bet'), None)
    if not vb:
        continue

    odds_1x2 = {}
    for raw_key in ('raw_enriched', 'raw_sporty'):
        raw = r[raw_key]
        if not raw:
            continue
        doc = json.loads(raw)
        markets = doc.get('sportybet_markets') or doc.get('markets') or []
        for mkt in markets:
            name = (mkt.get('name') or '').lower()
            if mkt.get('id') == '1' or '1x2' in name or name == 'match result':
                for sel in mkt.get('selections', []):
                    odds_1x2[sel.get('name')] = sel.get('odds')
                break
        if odds_1x2:
            break

    sel = vb['selection']
    odds_val = (odds_1x2.get('Home') or odds_1x2.get('1')) if sel in ('Home','1') else \
               (odds_1x2.get('Away') or odds_1x2.get('2')) if sel in ('Away','2') else \
               (odds_1x2.get('Draw') or odds_1x2.get('X'))
    try:
        odds_float = float(odds_val) if odds_val else 0.0
    except:
        odds_float = 0.0

    if odds_float < 2.0:
        continue

    period = r['period'] or 'Not start'
    not_started = period in ('Not start', 'Not started', '', None)
    sh = r['score_home']
    sa = r['score_away']
    score = f"{sh}-{sa}" if sh is not None and sa is not None else '-'

    snaps = conn.execute('select count(*) from odds_snapshots where match_id=?',(r['match_id'],)).fetchone()[0]
    mov = conn.execute('select home_odds,draw_odds,away_odds from odds_snapshots where match_id=? order by snapshot_time asc limit 1',(r['match_id'],)).fetchone()
    cur = conn.execute('select home_odds,draw_odds,away_odds from odds_snapshots where match_id=? order by snapshot_time desc limit 1',(r['match_id'],)).fetchone()

    movement = 'no data'
    open_odds = None
    if mov and cur and snaps > 1:
        o = mov['home_odds'] if sel in ('Home','1') else mov['away_odds'] if sel in ('Away','2') else mov['draw_odds']
        c = cur['home_odds'] if sel in ('Home','1') else cur['away_odds'] if sel in ('Away','2') else cur['draw_odds']
        open_odds = o
        if o and c:
            diff = round(c - o, 3)
            movement = f'shortened {abs(diff)}' if diff < -0.05 else f'drifted +{diff}' if diff > 0.05 else f'stable ({diff:+.3f})'

    signals = json.loads(r['signals_json'] or '[]')
    key_sigs = [s['name'] for s in signals if abs(s.get('impact') or 0) >= 5][:3]

    results.append({
        'match': r['match_name'],
        'selection': sel,
        'confidence': vb['confidence'],
        'reason': vb['reason'],
        'odds': odds_float,
        'open_odds': open_odds,
        'movement': movement,
        'snaps': snaps,
        'not_started': not_started,
        'period': period,
        'score': score,
        'H': odds_1x2.get('Home') or odds_1x2.get('1'),
        'D': odds_1x2.get('Draw') or odds_1x2.get('X'),
        'A': odds_1x2.get('Away') or odds_1x2.get('2'),
        'signals': key_sigs,
    })

results.sort(key=lambda x: x['confidence'], reverse=True)
not_started_list = [x for x in results if x['not_started']]
sys.stdout.write(f"Value bets odds>=2.0 NOT STARTED: {len(not_started_list)}\n")
sys.stdout.write(f"Value bets odds>=2.0 ALL: {len(results)}\n\n")

target = not_started_list[:10] if not_started_list else results[:10]
label = 'NOT STARTED' if not_started_list else 'ALL (predicted before kickoff, now live)'
sys.stdout.write(f"=== TOP 10 VALUE BETS ({label}) ===\n\n")
for i, x in enumerate(target, 1):
    status = 'NOT STARTED' if x['not_started'] else f"{x['period']}  score: {x['score']}"
    sys.stdout.write(f"{i}. {x['match']}\n")
    sys.stdout.write(f"   Status  : {status}\n")
    sys.stdout.write(f"   Pick    : {x['selection']} @ {x['odds']}  open: {x['open_odds']}  movement: {x['movement']}  [{x['snaps']} snaps]\n")
    sys.stdout.write(f"   1X2     : H={x['H']}  D={x['D']}  A={x['A']}\n")
    sys.stdout.write(f"   Conf    : {x['confidence']}%  |  signals: {x['signals']}\n")
    sys.stdout.write(f"   Reason  : {x['reason']}\n\n")

conn.close()
sys.stdout.flush()
