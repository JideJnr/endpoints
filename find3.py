f = open('app/storage/league_memory/crud.py', encoding='utf-8', errors='replace').read()
idx = f.find('_sofascore_ids_for_predictions')
print('idx:', idx)
if idx >= 0:
    print(f[max(0,idx-50):idx+500])
else:
    # search all occurrences
    import re
    for m in re.finditer('_sofascore_ids_for_predictions', f):
        print('at', m.start(), repr(f[m.start():m.start()+200]))
