import json

data = json.load(open('predictions.json'))
preds = data.get('predictions', [])

# Collect high-confidence result/win picks
# These are the ones most likely to be available at odds > 2.0
candidates = []

for p in preds:
    match_name = p.get('match_name', '?')
    league = p.get('league_name') or 'Unknown'
    picks = p.get('picks') or []

    for pick in picks:
        ptype = pick.get('type', '')
        sel = pick.get('selection', '')
        conf = int(pick.get('confidence') or 0)
        reason = pick.get('reason', '')

        if ptype == 'no_bet':
            continue

        # We want high-confidence directional picks (not double chance / draw protection)
        # that imply a clear winner — these are the ones bookmakers price > 2.0 for underdogs
        is_result = ptype in ('match_result', 'ensemble_1x2', 'market_value', 'value_bet')
        is_goals  = ptype == 'goals' and 'over' in sel.lower()

        if conf >= 70 and (is_result or is_goals):
            candidates.append({
                'match':      match_name,
                'league':     league,
                'pick':       sel,
                'type':       ptype,
                'confidence': conf,
                'reason':     reason,
            })

# Deduplicate by match — keep highest confidence pick per match
seen = {}
for c in sorted(candidates, key=lambda x: x['confidence'], reverse=True):
    key = c['match']
    if key not in seen:
        seen[key] = c

top10 = list(seen.values())[:10]

print("=" * 70)
print(f"TOP {len(top10)} HIGH-CONFIDENCE PICKS (target odds > 2.0)")
print("=" * 70)
for i, r in enumerate(top10, 1):
    print(f"\n{i}. {r['match']}")
    print(f"   League    : {r['league']}")
    print(f"   Pick      : {r['pick']}")
    print(f"   Type      : {r['type']}")
    print(f"   Confidence: {r['confidence']}%")
    print(f"   Reason    : {r['reason'][:100]}")
print("\n" + "=" * 70)
print("NOTE: Verify odds on SportyBet before placing — target selections at 2.0+")
