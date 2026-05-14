# 813Assistant

AI Telegram assistant bot scaffold on aiogram 3.x with SQLite, APScheduler, OpenAI-compatible LLM/STT and Miro sync.

## Minimal .env

```env
BOT_TOKEN=your_bot_token
BOT_ID=8501494840
ALLOWED_USER_ID=999166390
TIMEZONE=Europe/Moscow
```

`ALLOWED_USER_ID` ограничивает работу бота только одним пользователем.

## Dev check

```bash
./scripts_check_no_conflicts.sh
```
