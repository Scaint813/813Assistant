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

with open('bot/services/subject_normalizer.py', encoding='utf-8') as f:
    sn = f.read()
with open('bot/services/study_problem_templates.py', encoding='utf-8') as f:
    st = f.read()
with open('bot/database/queries.py', encoding='utf-8') as f:
    q = f.read()
with open('bot/services/ai_service.py', encoding='utf-8') as f:
    ai = f.read()
with open('bot/services/action_preview.py', encoding='utf-8') as f:
    ap = f.read()
with open('bot/services/problem_block_service.py', encoding='utf-8') as f:
    pb = f.read()
with open('bot/handlers/domains.py', encoding='utf-8') as f:
    dom = f.read()
with open('bot/handlers/health.py', encoding='utf-8') as f:
    health = f.read()
with open('bot/handlers/menu.py', encoding='utf-8') as f:
    menu = f.read()
with open('bot/services/miro_service.py', encoding='utf-8') as f:
    miro = f.read()

CHECKS = {
    # subject_normalizer
    'subject_normalizer: normalize_subject': 'def normalize_subject' in sn,
    'subject_normalizer: canonical_key': 'def canonical_key' in sn,
    'subject_normalizer: find_best_exam_match': 'def find_best_exam_match' in sn,
    'subject_normalizer: find_duplicate_exam_pairs': 'def find_duplicate_exam_pairs' in sn,
    'subject_normalizer: Базовая математика alias': '"Базовая математика"' in sn,
    # study_problem_templates
    'templates: build_study_problem_plan': 'def build_study_problem_plan' in st,
    'templates: russian task 21': '("russian", 21)' in st,
    'templates: social task 24': '("social", 24)' in st,
    'templates: english': '("english", None)' in st,
    'templates: math': '("math", None)' in st,
    # queries
    'queries: upsert_exam_date_smart': 'upsert_exam_date_smart' in q,
    'queries: archive_exam_date': 'archive_exam_date' in q,
    'queries: find_duplicate_exams': 'find_duplicate_exams' in q,
    'queries: archive_study_schedule_item': 'archive_study_schedule_item' in q,
    'queries: get_exam_date_by_id': 'get_exam_date_by_id' in q,
    # ai_service
    'ai: update_exam_date intent': 'update_exam_date' in ai,
    'ai: delete_exam_date intent': 'delete_exam_date' in ai,
    'ai: delete_study_schedule_item intent': 'delete_study_schedule_item' in ai,
    'ai: Базовая математика in _SUBJECT_NORM': '"Базовая математика"' in ai,
    'ai: target_subject field': 'target_subject' in ai,
    'ai: new_date field': 'new_date' in ai,
    'ai: update/delete fallback detection': 'update_kws' in ai,
    # action_preview
    'preview: _render_edit_package': '_render_edit_package' in ap,
    'preview: update_exam_date group': 'update_exam_date' in ap,
    'preview: delete_exam_date group': 'delete_exam_date' in ap,
    # problem_block_service
    'pb_service: study_problem_templates import': 'build_study_problem_plan' in pb,
    'pb_service: exam/study enrichment': 'category in {"exam", "study"}' in pb,
    'pb_service: miro_plan in resources': '"study_plan"' in pb,
    # domains
    'domains: upsert_exam_date_smart import': 'upsert_exam_date_smart' in dom,
    'domains: archive_exam_date import': 'archive_exam_date' in dom,
    'domains: normalize_subject import': 'normalize_subject' in dom,
    'domains: update_exam_date handler': '"update_exam_date"' in dom,
    'domains: delete_exam_date handler': '"delete_exam_date"' in dom,
    'domains: delete_study_schedule_item handler': '"delete_study_schedule_item"' in dom,
    'domains: study_created_exams counter': 'study_created_exams' in dom,
    'domains: total_active shown': 'Активных экзаменов' in dom,
    'domains: dedupe_archive callback': 'dedupe_archive_callback' in dom,
    # health
    'health: find_duplicate_exams import': 'find_duplicate_exams' in health,
    'health: dedupe_study_cmd': 'dedupe_study_cmd' in health,
    'health: dedupe inline keyboard': 'dedupe_archive:' in health,
    # menu compact study
    'menu: _short_subj helper': '_short_subj' in menu,
    'menu: ближайшие занятия sort': '_days_until' in menu,
    'menu: показать только 4 занятия': 'show_sched = sorted_sched[:4]' in menu,
    'menu: /next hint': 'Следующий шаг: /next' in menu,
    # miro deep plan
    'miro: 4-sticky structure ПРОБЛЕМА': '"ПРОБЛЕМА"' in miro,
    'miro: ТЕМЫ sticky': '"ТЕМЫ"' in miro,
    'miro: ПРАКТИКА sticky': '"ПРАКТИКА"' in miro,
    'miro: СЛЕДУЮЩИЙ ШАГ sticky': '"СЛЕДУЮЩИЙ ШАГ"' in miro,
    'miro: study_block_sub entity_type': 'study_block_sub' in miro,
    'miro: _extract_theory resources_json': '_extract_theory' in miro,
    'miro: _extract_practice': '_extract_practice' in miro,
}

all_ok = True
for name, ok in CHECKS.items():
    status = 'OK  ' if ok else 'FAIL'
    print(status + '  ' + name)
    if not ok:
        all_ok = False

print()
print('All checks passed!' if all_ok else f'SOME CHECKS FAILED! ({sum(1 for v in CHECKS.values() if not v)} failures)')
sys.exit(0 if all_ok else 1)
