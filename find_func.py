f = open('app/league_memory.py', encoding='utf-8', errors='replace').read()
idx = f.find('grade_predictions_for_date')
print('grade_predictions_for_date at:', idx)
print(f[idx:idx+3000])
print('---')
idx2 = f.find('_sofascore_ids_for_predictions')
print('_sofascore_ids_for_predictions at:', idx2)
if idx2 >= 0:
    print(f[max(0,idx2-100):idx2+500])
