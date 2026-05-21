# 813Assistant

Личный Telegram AI-штаб на aiogram 3.x + SQLite + APScheduler.

Пользователь пишет обычным текстом → бот понимает → структурирует → предлагает действие → после подтверждения сохраняет, напоминает, синхронизирует.

---

## Что умеет

- **Доступ** ограничен по `ALLOWED_USER_ID` — middleware на все messages и callbacks.
- **Свободный ввод** → AI/fallback parser → intent → Action Preview → Confirm → DB.
- **Голосовые сообщения** → OpenAI Whisper → тот же parser → preview.
- **Intents**: `create_task`, `create_reminder`, `rest_day`, `schedule_override`, `create_problem_block`, `show_today`, `show_tasks`, `do_nothing`.
- **Напоминания** через APScheduler — Telegram push в нужное время с кнопками (Готово / Перенести / Отмена).
- **Check-ins** утром/днём/вечером — автоматически, с антиспамом и quiet mode.
- **Problem blocks** — активные блоки проблем, решение за 3 шага, snooze, архив, ресурсы.
- **Next step** — `/next` анализирует задачи / напоминания / блоки и даёт 1–3 действия.
- **Overload screen** — если пишешь "мне плохо" / "не вывожу" — стабилизация вместо задач.
- **Miro sync** — `/sync_miro` создаёт/обновляет штаб в Miro без дублей. Если не настроен — не падает.
- **Cleanup** — `/cleanup` архивирует только мелкие просроченные задачи, не трогает важные.
- **Health check** — `/health` показывает статус DB / OpenAI / Miro / scheduler прямо в Telegram.

---

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Открыть штаб |
| `/today` | Сводка дня |
| `/tasks` | Активные задачи |
| `/reminders` | Активные напоминания |
| `/problems` `/blocks` | Активные блоки проблем |
| `/next` | Следующий шаг |
| `/schedule` | Ближайшие override-режимы |
| `/archive` | Архивные задачи |
| `/cleanup` | Убрать мелкие просроченные задачи |
| `/sync_miro` | Синхронизировать Miro-штаб |
| `/health` | Статус бота (DB, scheduler, конфиги) |
| `/help` | Краткая помощь |
| `/debug_create_test_data` | DEBUG: создать тестовые task/reminder/block |

---

## Настройка `.env`

```bash
cp .env.example .env
nano .env
```

| Переменная | Описание |
|-----------|----------|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `BOT_ID` | ID бота (число) |
| `ALLOWED_USER_ID` | Telegram user_id единственного пользователя |
| `TIMEZONE` | Таймзона, например `Europe/Moscow` |
| `OPENAI_API_KEY` | Ключ OpenAI (опционально — без него работает fallback parser) |
| `OPENAI_MODEL_FAST` | Модель для простых intents |
| `OPENAI_MODEL_SMART` | Модель для сложного анализа |
| `OPENAI_TRANSCRIPTION_MODEL` | Модель Whisper (по умолчанию `whisper-1`) |
| `MIRO_ACCESS_TOKEN` | Miro OAuth token (опционально) |
| `MIRO_BOARD_ID` | ID доски Miro (опционально) |
| `MIRO_AI_ZONE_START_X` | X-координата начала AI-зоны (по умолчанию 5000) |
| `MIRO_AI_ZONE_START_Y` | Y-координата (по умолчанию 0) |
| `CHECKIN_ENABLED` | `true` / `false` |
| `CHECKIN_MORNING_TIME` | Время утреннего check-in, например `09:30` |
| `CHECKIN_DAY_TIME` | Время дневного check-in |
| `CHECKIN_EVENING_TIME` | Время вечернего check-in |
| `DATABASE_URL` | SQLite URL (по умолчанию `sqlite+aiosqlite:///./assistant.db`) |

> `.env` добавлен в `.gitignore` — не коммитить.

---

## Первый запуск на сервере

```bash
# 1. Получить код
git clone <repo_url> /opt/bot/813Assistant
cd /opt/bot/813Assistant

# 2. Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# 3. Зависимости
pip install -r requirements.txt

# 4. Настройка
cp .env.example .env
nano .env          # заполнить BOT_TOKEN, ALLOWED_USER_ID и т.д.

# 5. Проверка кода
bash scripts_check_no_conflicts.sh
python -m compileall .

# 6. Первый запуск
python -m bot.main
```

При первом запуске `assistant.db` создаётся автоматически со всеми таблицами.

### Если assistant.db уже существует

Миграция применяется **автоматически** при каждом старте:
новые колонки добавляются через `ALTER TABLE` (idempotent — не ломается при повторе).

Если хочешь начать с чистой БД:

```bash
rm assistant.db
python -m bot.main
```

### Проверка после запуска

Написать боту `/health` — должен прийти ответ:

```
813Assistant health

DB: OK
OpenAI: set / missing (fallback parser active)
Miro: set / missing (sync disabled)
Scheduler: running
...
```

---

## Systemd (production)

```bash
sudo cp deploy/813assistant.service.example /etc/systemd/system/813assistant.service
# Отредактировать WorkingDirectory и ExecStart если нужно
sudo systemctl daemon-reload
sudo systemctl enable 813assistant
sudo systemctl start 813assistant
sudo systemctl status 813assistant
```

Логи:

```bash
journalctl -u 813assistant -f
journalctl -u 813assistant --since "1 hour ago"
```

---

## Backup SQLite

```bash
# Настроить cron (бэкап каждый день в 03:00)
chmod +x deploy/backup_sqlite.sh.example
cp deploy/backup_sqlite.sh.example /opt/bot/backup_813assistant.sh

crontab -e
# Добавить строку:
0 3 * * * /opt/bot/backup_813assistant.sh >> /var/log/813assistant_backup.log 2>&1
```

Бэкапы хранятся в `/opt/bot/backups/813Assistant/`, ротация 14 дней.

---

## Голосовые сообщения

Pipeline:

```
voice → скачать файл → Whisper (OPENAI_API_KEY) → текст → intent parser → Action Preview → Confirm → DB
```

Если `OPENAI_API_KEY` не задан:

```
Транскрибация не настроена: отсутствует OPENAI_API_KEY.
```

Голос использует тот же parser, что и текст — отдельной бизнес-логики нет.

---

## OpenAI / Fallback parser

- Если `OPENAI_API_KEY` пустой → rule-based fallback parser, бот не падает.
- Простые intents → `OPENAI_MODEL_FAST`.
- Сложный анализ (планирование, next_step, перегруз) → `OPENAI_MODEL_SMART`.
- Если SMART не задан — fallback в FAST. Если оба не заданы — fallback parser.

---

## Action Preview

Бот **не сохраняет данные без подтверждения**.

Требуют подтверждения:
- `create_task`
- `create_reminder`
- `rest_day` / `schedule_override`
- `create_problem_block`

Не требуют:
- `/today`, `/tasks`, `/reminders`, `/next`, `/help`, `/health`

---

## Напоминания

- Создаются через preview: "завтра вечером напомни проверить оплату Артёма"
- Сохраняются в `reminders` с `status=active`
- APScheduler планирует job `reminder:{id}` при confirm
- При старте бота все активные будущие напоминания перепланируются
- В момент срабатывания Telegram push с кнопками: **Готово / Перенести / Отмена**
- Snooze: +1ч / вечером / завтра утром

---

## Check-ins

Автоматически 3 раза в день через APScheduler (тот же instance, что reminders).

Антиспам:
- `checkin_enabled=false` → не отправляется
- `quiet_until > now` → не отправляется
- Активность пользователя < 45 минут назад → не отправляется
- Уже был check-in < 2 часов → не отправляется
- Максимум 3 check-in в день

Quiet mode: кнопка [Тихий режим] → [На 2 часа / До завтра / Выключить check-ins].

Ручной тест:
1. Поставить `CHECKIN_DAY_TIME` на текущее время + 2 минуты
2. Запустить бота
3. Дождаться check-in

---

## Problem blocks

```
"путаюсь в расчётах заказов" → preview → confirm → /problems
```

Категории: `study`, `exam`, `money`, `orders`, `health`, `training`, `sleep`, `conflict`, `work`, `discipline`, `overload`, `system`, `other`.

Действия: Следующий шаг / Отложить (до завтра / 3д / неделю) / Ресурсы / Развернуть / Закрыть / Архив.

Check-ins показывают максимум один active problem block.

После дедлайна блок → `expired`, не попадает в `/next` и check-ins.

---

## /next — следующий шаг

Анализирует: active tasks, overdue tasks, reminders в ближайшие 2ч, rest_day, problem blocks, overload.

Выдаёт 1–3 действия. Не создаёт данные в БД — только рекомендует.

Если rest_day — не предлагает тяжёлый план. Если overload — сначала стабилизация.

---

## /sync_miro

```
/sync_miro → создаёт/обновляет секции штаба в Miro
```

Секции: ШТАБ / TODAY, ЗАДАЧИ / TASKS, НАПОМИНАНИЯ, ПРОБЛЕМЫ, РАСПИСАНИЕ, CHECK-INS, АРХИВ.

Дубли предотвращаются через `miro_mappings` (entity_type + entity_id + board_id).

Если `MIRO_ACCESS_TOKEN` или `MIRO_BOARD_ID` пустые:

```
Miro не настроен.
Нужно заполнить: MIRO_ACCESS_TOKEN, MIRO_BOARD_ID
```

---

## /cleanup

Архивирует только задачи с:
- `is_minor=True`
- `auto_cleanup_allowed=True`
- просрочены > 3 дней
- нет активного linked reminder
- нет защищённых ключевых слов (оплата, заказ, клиент, ЕГЭ, долг...)

High/urgent задачи не архивируются никогда.

---

## Smoke-test checklist

После деплоя вручную проверить:

```
[ ] /start → штабное меню с кнопками
[ ] /health → DB: OK, Scheduler: running
[ ] "разобраться с Miro" → create_task preview → Подтвердить → /tasks покажет задачу
[ ] "мне плохо, я не вывожу" → Overload screen (не обычная задача)
[ ] "путаюсь в расчётах заказов" → problem_block preview → confirm → /problems
[ ] /next → 1–3 действия
[ ] /sync_miro без MIRO config → "Miro не настроен", без ошибки
[ ] /cleanup → "Убрано в архив: 0 задач" (нет кандидатов)
[ ] /archive → архивные задачи (или "Архив пуст")
[ ] /debug_create_test_data → тестовые данные + напоминание через ~1 мин
[ ] Через 1 мин → Telegram push напоминания, кнопки работают
[ ] Тихий режим → [Тихий режим] → [На 2 часа] → check-ins не приходят
[ ] Другой Telegram user → "Доступ закрыт."
```

---

## Development checks

```bash
bash scripts_check_no_conflicts.sh
python -m compileall .
python -c "import ast, os; [ast.parse(open(os.path.join(r,f)).read()) for r,_,fs in os.walk('bot') for f in fs if f.endswith('.py')]; print('AST OK')"
```

---

## Miro: �����������

### /miro_debug

���������� ������ Miro ��� ������. GET /v2/boards/{id}/items?limit=1. ������ �������� sticky note.

### /sync_miro

���� ����� ��������� �� ���������: 'Miro ������� ��������. �������: N, ������: N'.
������ ������ ���������� � status_code, entity_type, content_preview.
���� ������� item �� ��������� sync ���������.

### ���� �� �������

    journalctl -u 813assistant -n 120 --no-pager

���: Miro POST failed: status=400 entity=task entity_id=5

### ������� 400 Bad Request

- ���������� fillColor ('gray') -> ���������� �� 'light_gray'
- ������ content -> ���������� �� '��� ��������'
- ������� content -> ���������� �� 1500 ��������
- x/y = None -> �������������� � int
- Mapping �� 201 -> mp.item_id ����������� ������ ��� �������� create


---

## Miro ��� ���������� ����

### ���������

**Telegram-��� = ��������**: ��������� ������, ������ ������, ������ next step,
���� reminders/check-ins, �������������� problem blocks.

**Miro = �����������**: ���������� ��������� �������, �� ��������� �������,
�� ������ entities ��� ��.

### Layout � 3-���������� �����

����� /sync_miro �� ����� ����������:

    813ASSISTANT / AI-����   [��������� �����]

    ��� 1:  [����/TODAY]  [��������� ���]  [�����]
    ��� 2:  [������]      [�����������]    [���������� �����]
    ��� 3:  [����������]  [CHECK-INS]      [�����]
    ��� 4:  [������] [������] [�ר��] [����] [���������]

������ ������: ������� ���������-shape + sticky-�������� ������.
Empty state: "��� �������� �����" ���� ������ ���.

### ���������� AI-����

    MIRO_AI_ZONE_START_X=8000  # ������������� ��������
    MIRO_AI_ZONE_START_Y=2000

��, ��� ������ ��� � ������ � ���� ���� �����.
������ ���� ������������ �� �������������.
��� ������ �������� �� .env � �� ���������.

### ���-���� �����

    COL_STRIDE = 2100 px   (������ ������ 1800 + ����� 300)
    ROW_STRIDE = 1700 px   (������ ������ 1400 + ����� 300)
    ��������� �����: ���� row-0 �� 500 px

### ��� ���������� ������ ������

| ������        | ������                                                 |
|---------------|--------------------------------------------------------|
| ����/TODAY    | ����, �����, �����, counts �����/�����������/������    |
| ��������� ��� | 1-3 �������� �� NextStepService                        |
| �����         | ���������, ����� �����, check-ins off                  |
| ������        | ���-7 (high/urgent ����), +N ���� ������               |
| �����������   | ��������� 5, ������������ ����                         |
| ��������      | ���-5 �������� ������, title + next_action             |
| ����������    | schedule_overrides                                     |
| CHECK-INS     | enabled/disabled, quiet_until, ����������              |
| �����         | summary (counts) + ��������� 5 �������� �����          |
| ������-��������� | counts �������� �����/������ �� ����               |

### �������������� ������

- ������ entity ����� ���������� (entity_type, entity_id) � miro_mappings.
- ���������� headers: entity_id �� ��������� 5000-5024 (�������������).
- ���������� ��������: entity_id = id ������ � ��.
- ��������� /sync_miro ������ PATCH (����������), �� ������ ����� ��������.
- mp.item_id ����������� ������ ��� �������� 201 Created.

### �����������

**������� ��������:**

    /miro_debug

���������� Board ID (������ 8 ��������), ������ GET /items?limit=10,
������ �������� sticky "813Assistant debug" (������ ��� �������!).
����� �� ������������ �����.

**������ �������������:**

    /sync_miro

�����: "Miro �������. ������: 14, ��������: 23, �������: 5, ���������: 18"

**���� �� �������:**

    journalctl -u 813assistant -n 120 --no-pager

������: `Miro POST FAIL [status=400 entity=task id=5]`

### Debug notes

- /miro_debug ������ sticky "813Assistant debug" � ��� �������� ������.
- /sync_miro �� ������ debug notes.
- Debug sticky ���������� ��� �������� ����� (���� board header).
