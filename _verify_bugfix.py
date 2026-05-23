import sys

checkin = open('bot/services/checkin_service.py', encoding='utf-8').read()
nss = open('bot/services/next_step_service.py', encoding='utf-8').read()
miro = open('bot/services/miro_service.py', encoding='utf-8').read()
norm = open('bot/services/subject_normalizer.py', encoding='utf-8').read()
health = open('bot/handlers/health.py', encoding='utf-8').read()
domains = open('bot/handlers/domains.py', encoding='utf-8').read()
du = open('bot/services/datetime_utils.py', encoding='utf-8').read()

CHECKS = {
    # datetime_utils
    'datetime_utils: ensure_aware': 'def ensure_aware' in du,
    'datetime_utils: ensure_utc_aware': 'def ensure_utc_aware' in du,
    # Timezone fixes in checkin
    'checkin: ensure_aware imported': 'from bot.services.datetime_utils import ensure_aware' in checkin,
    'checkin: quiet_until aware': 'ensure_aware(state.quiet_until' in checkin,
    'checkin: last_activity aware': 'ensure_aware(state.last_user_activity_at' in checkin,
    'checkin: last_checkin aware': 'ensure_aware(state.last_checkin_at' in checkin,
    'checkin: skip reason logging': 'checkin skip' in checkin,
    # Timezone fixes in next_step_service
    'nss: ensure_aware imported': 'from bot.services.datetime_utils import ensure_aware' in nss,
    'nss: remind_at None guard': 'if not r.remind_at:' in nss,
    'nss: remind_at aware': 'ensure_aware(r.remind_at' in nss,
    'nss: deadline aware': 'ensure_aware(t.deadline' in nss,
    # Miro shape fix
    'miro: format field removed': '"format": "plain"' not in miro,
    'miro: shape fallback to sticky': 'Fallback: shape API failed' in miro,
    'miro: _hex_to_sticky helper': 'def _hex_to_sticky' in miro,
    'miro: _render_next_section_safe': '_render_next_section_safe' in miro,
    'miro: NextStepService error caught': 'NextStepService.build_next_step failed' in miro,
    'miro: failed_sections': 'failed_sections = []' in miro,
    'miro: _safe_section wrapper': '_safe_section' in miro,
    'miro: no self._col_width': 'self._col_width' not in miro,
    'miro: no self._card_height': 'self._card_height' not in miro,
    # Subject normalizer
    'normalizer: fixed _is_generic_alias_of': 'normalize_subject(generic)' in norm,
    'normalizer: correct primary/dup comment': 'keep a, archive b' in norm,
    'normalizer: fuzzy match step 4': 'Fuzzy: generic target matches specific' in norm,
    # sync_miro report
    'domains: failed_sections': 'failed_sections' in domains,
    'domains: section list in report': 'Проблемные секции:' in domains,
    # debug_jobs
    'health: debug_jobs_cmd defined': 'debug_jobs_cmd' in health,
    'health: scheduler.get_jobs': 'checkin_service.scheduler.get_jobs()' in health,
    'health: study_session prefix': 'study_session:' in health,
}

all_ok = True
for name, ok in CHECKS.items():
    status = 'OK  ' if ok else 'FAIL'
    print(status + '  ' + name)
    if not ok:
        all_ok = False

print()
if all_ok:
    print('All checks passed!')
else:
    print('FAILED:', sum(1 for v in CHECKS.values() if not v), 'failures')
    sys.exit(1)
