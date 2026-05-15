# 813Assistant

Личный Telegram AI-ассистент (MVP) на aiogram 3.x.

## Что умеет MVP
- Ограничение доступа по `ALLOWED_USER_ID`.
- Текст/voice -> AI intent parser -> Action Preview -> Confirm -> запись в SQLite.
- Intents: `create_task`, `create_reminder`, `schedule_override`, `rest_day`, `do_nothing`.
- Команды чтения: `/today`, `/tasks`, `/reminders`, `/schedule`, `/help`.
- Автоархив: `/cleanup` (учитывает активные связанные напоминания).
- Синхронизация Miro: `/sync_miro`.
- Планировщик напоминаний APScheduler с отправкой в Telegram.

## Напоминания
- Создаются естественным текстом: `завтра вечером напомни проверить оплату Артёма`.
- Могут создаваться из voice после расшифровки.
- После подтверждения preview напоминание сохраняется в БД и, если время в будущем, ставится в APScheduler.
- При старте бота все активные будущие напоминания заново планируются.
- В момент срабатывания бот отправляет сообщение в личку:
  - `[Готово]` -> закрыть напоминание
  - `[Перенести]` -> меню переноса
  - `[Отмена]` -> отменить напоминание
- Список: `/reminders` (показывает время, статус и пометку просрочки).

## Настройка `.env`
1. Скопируйте `.env.example` в `.env`.
2. Заполните переменные.
3. `.env` не коммитить.


## Голосовые сообщения
- Бот принимает Telegram voice-сообщения.
- Скачивает audio во временный файл.
- Отправляет файл в OpenAI transcription (`OPENAI_TRANSCRIPTION_MODEL`, по умолчанию `whisper-1`).
- Передаёт расшифровку в тот же intent parser, что и текст.
- Перед любой записью в БД показывает Action Preview.
- Запись выполняется только после подтверждения.

## Подключение OpenAI
1. Создайте API key в OpenAI Platform.
2. Укажите `OPENAI_API_KEY` в `.env`.
3. Проверьте `OPENAI_MODEL` и `OPENAI_TRANSCRIPTION_MODEL`.

## Подключение Miro
1. Создайте Miro app и получите token.
2. Укажите `MIRO_ACCESS_TOKEN`.
3. Укажите `MIRO_BOARD_ID`.
4. Укажите `MIRO_AI_ZONE_START_X/Y` (AI-зона правее ручной зоны).

## Запуск
```bash
python -m bot.main
```

## Команды
`/start /today /tasks /reminders /schedule /help /cleanup /sync_miro`

## Development checks
```bash
bash scripts_check_no_conflicts.sh
python -m compileall .
```
