import ast, os, sys

errors = []
for root, dirs, files in os.walk('bot'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            try:
                with open(path, encoding='utf-8') as fh:
                    ast.parse(fh.read(), filename=path)
            except SyntaxError as e:
                errors.append(str(e))
if errors:
    for e in errors:
        print('SYNTAX ERROR:', e)
    sys.exit(1)

count = sum(1 for r, d, fs in os.walk('bot') for f in fs if f.endswith('.py'))
print(f'AST OK ({count} files)')

with open('bot/services/miro_service.py', encoding='utf-8') as f:
    miro = f.read()
with open('bot/handlers/menu.py', encoding='utf-8') as f:
    menu = f.read()
with open('bot/handlers/health.py', encoding='utf-8') as f:
    health = f.read()

CHECKS = {
    'menu: Command study decorator': 'Command("study")' in menu,
    'menu: try/except in study': 'except Exception' in menu,
    'menu: full weekday in study': '\u043f\u043e\u043d\u0435\u0434\u0435\u043b\u044c\u043d\u0438\u043a' in menu,
    'menu: study_blocks[:3]': 'study_blocks[:3]' in menu,
    'miro: _render_exams_section': '_render_exams_section' in miro,
    'miro: _render_study_schedule_section': '_render_study_schedule_section' in miro,
    'miro: _render_study_blocks_section': '_render_study_blocks_section' in miro,
    'miro: exams row 4 (0, 4)': '0, 4,' in miro,
    'miro: study_schedule row 4 (1, 4)': '1, 4,' in miro,
    'miro: study_blocks row 4 (2, 4)': '2, 4,' in miro,
    'miro: sync_all -> _render_exams_section': '_render_exams_section(session' in miro,
    'miro: sync_all -> _render_study_schedule': '_render_study_schedule_section(session' in miro,
    'miro: exam_date entity_type used': '"exam_date"' in miro,
    'miro: study_schedule_item entity_type': '"study_schedule_item"' in miro,
    'health: /miro_debug note about /sync_miro': '/sync_miro' in health,
}

all_ok = True
for name, ok in CHECKS.items():
    status = 'OK  ' if ok else 'FAIL'
    print(status + '  ' + name)
    if not ok:
        all_ok = False

print()
print('All checks passed!' if all_ok else 'SOME CHECKS FAILED!')
sys.exit(0 if all_ok else 1)
