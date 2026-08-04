import pathlib
content = open('tests/test_team_watcher_engine_props_template.txt', encoding='utf-8').read()
pathlib.Path('tests/test_team_watcher_engine_props.py').write_text(content, encoding='utf-8')
print('OK')
