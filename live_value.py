import sqlite3, json, sys
from app.config import get_settings
from app.enriched_prediction import predict_enriched_match
from app.market import get_movement

db = str(get_settings().database_path)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# Step 1: get all live matches from buffer
live_rows = conn.execute('''
    select match_id, period, score_home, score_away, raw_enriched, raw_sporty
    from match_buffer
    where is_live = 1 and is_finished = 0
    order by start_time asc
''').fetchall()

sys.stdout.write(f"Live matches in buffer: {len(live_rows)}\n\n")

results = []

for r in live_rows:
    raw = r['raw_enriched'] or r['raw_sporty']
    if not raw:
        continue
    doc = json.loads(raw)

    # Run fresh prediction
    try:
        pred = predict_enriched_match(doc)
    except Exception as e:
        continue

    picks = pred.get('picks') or []
    signals = pred.get('signals') or []

    # Get all markets for odds > 5
    markets = doc.get('sportybet_markets') or doc.get('markets') or []
    high_odds_picks = []

    for mkt in markets:
        mkt_name = (mkt.get('name') or '').lower()
        for sel in mkt.get('selections') or []:
            try:
                odds_val = float(sel.get('odds') or 0)
            except:
                odds_val = 0
            if odds_val >= 5.0:
                high_odds_picks.append({
                    'market': mkt.get('name') or mkt.get('desc') or 'Unknown',
                    'selection': sel.get('name') or sel.get('desc'),
                    'odds': odds_val,
                })

    if not high_odds_picks:
        continue

    # Sort by odds desc
    high_odds_picks.sort(key=lambda x: x['odds'], reverse=True)

    # Get best prediction pick
    best_pick = picks[0] if picks else {}
    score = f"{r['score_home']}-{r['score_away']}" if r['score_home'] is not None else '-'

    # Get odds movement
    movement = get_movement(str(r['match_id']))
    sharp = movement.get('sharp_signal') or 'none'
    snaps = movement.get('snapshots', 0)

    # Key signals
    key_sigs = [(s['name'], round(s.get('impact') or 0, 1)) for s in signals if abs(s.get('impact') or 0) >= 4][:5]

    # Models
    models = pred.get('models') or {}
    ensemble = models.get('ensemble') or {}
    poisson = models.get('poisson') or {}
    dixon = models.get('dixon_coles') or {}
    elo = models.get('elo') or {}

    results.append({
        'match': pred.get('name') or r['match_id'],
        'match_id': r['match_id'],
        'period': r['period'],
        'score': score,
        'best_pick': best_pick,
        'high_odds': high_odds_picks[:5],
        'sharp': sharp,
        'snaps': snaps,
        'signals': key_sigs,
        'ensemble': ensemble,
        'poisson': poisson.get('probabilities') or {},
        'dixon': dixon.get('probabilities') or {},
        'elo': elo,
        'time_decay': pred.get('time_decay_multiplier', 1.0),
        'minute': pred.get('rules', {}).get('minute') or 0,
    })

# Sort by best_pick confidence desc
results.sort(key=lambda x: x['best_pick'].get('confidence', 0), reverse=True)

sys.stdout.write(f"Live matches with odds >= 5.0: {len(results)}\n")
sys.stdout.write("=" * 70 + "\n\n")

for i, x in enumerate(results[:5], 1):
    ep = x['ensemble'].get('probabilities') or {}
    sys.stdout.write(f"{i}. {x['match']}\n")
    sys.stdout.write(f"   Status   : {x['period']}  |  Score: {x['score']}  |  Minute: {x['minute']}'\n")
    sys.stdout.write(f"   Best Pick: [{x['best_pick'].get('type','?')}] {x['best_pick'].get('selection','?')} — {x['best_pick'].get('confidence','?')}% conf\n")
    sys.stdout.write(f"   Reason   : {x['best_pick'].get('reason','')}\n")
    sys.stdout.write(f"   Ensemble : H={ep.get('home_win','?')}%  D={ep.get('draw','?')}%  A={ep.get('away_win','?')}%  → {x['ensemble'].get('prediction','?')}\n")
    sys.stdout.write(f"   Poisson  : H={x['poisson'].get('home_win','?')}%  D={x['poisson'].get('draw','?')}%  A={x['poisson'].get('away_win','?')}%\n")
    sys.stdout.write(f"   Dixon    : H={x['dixon'].get('home_win','?')}%  D={x['dixon'].get('draw','?')}%  A={x['dixon'].get('away_win','?')}%\n")
    elo_d = x['elo']
    if elo_d and not elo_d.get('error'):
        sys.stdout.write(f"   ELO      : H={elo_d.get('home_win_probability','?')}%  A={elo_d.get('away_win_probability','?')}%  diff={elo_d.get('elo_diff','?')}\n")
    sys.stdout.write(f"   Movement : {x['sharp']}  ({x['snaps']} snapshots)\n")
    sys.stdout.write(f"   Decay    : x{x['time_decay']}\n")
    sys.stdout.write(f"   Signals  : {x['signals']}\n")
    sys.stdout.write(f"   Odds >=5 :\n")
    for o in x['high_odds']:
        sys.stdout.write(f"     • [{o['market']}] {o['selection']} @ {o['odds']}\n")
    sys.stdout.write("\n")

conn.close()
sys.stdout.flush()
