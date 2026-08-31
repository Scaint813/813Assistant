# Apple Health → 813Assistant через Shortcuts

Telegram не имеет прямого доступа к HealthKit. В этой интеграции iPhone сам читает только разрешённые пользователем дневные агрегаты и отправляет их на сервер 813Assistant. Бот не запрашивает и не хранит отдельные измерения, диагнозы или историю медицинских записей.

## 1. Сервер

В `.env`:

```dotenv
HEALTH_BRIDGE_ENABLED=true
HEALTH_BRIDGE_HOST=127.0.0.1
HEALTH_BRIDGE_PORT=8781
HEALTH_BRIDGE_TOKEN=replace_with_at_least_32_random_characters
HEALTH_NUDGE_TIME=18:00
DEFAULT_STEP_GOAL=10000
HEALTH_MIN_STEP_GAP=1000
```

Сгенерировать секрет можно командой `openssl rand -hex 32`. Не коммить его и не передавать в query string.

После рестарта локальная проверка:

```bash
curl -H "Authorization: Bearer $HEALTH_BRIDGE_TOKEN" \
  http://127.0.0.1:8781/api/health/status
```

Endpoint слушает localhost по умолчанию. Для iPhone опубликуй его только через HTTPS reverse proxy или доверенную VPN/Tailscale; не открывай порт 8781 напрямую в интернет.

## 2. Shortcut на iPhone

Создай автоматизацию, например ежедневно в 17:55:

1. `Find Health Samples` → тип `Steps`, период `Today`; посчитать сумму.
2. При необходимости так же получить `Active Energy`, `Workouts`, `Sleep`, `Resting Heart Rate` и `Heart Rate Variability`; передавай только итог за день.
3. `Current Date` → формат `yyyy-MM-dd`.
4. Собрать Dictionary с полями ниже.
5. `Get Contents of URL`:
   - URL: `https://your-host.example/api/health/snapshot`;
   - Method: `POST`;
   - Request Body: `JSON`;
   - Header `Authorization`: `Bearer <HEALTH_BRIDGE_TOKEN>`.

Первый запуск Shortcut попросит системное разрешение на выбранные Health categories. Разрешай только те агрегаты, которые действительно хочешь использовать.

Payload:

```json
{
  "date": "2026-08-01",
  "steps": 6400,
  "step_goal": 10000,
  "active_energy_kcal": 410,
  "workout_minutes": 0,
  "sleep_minutes": 455,
  "resting_heart_rate": 58,
  "hrv_ms": 54,
  "source": "shortcut"
}
```

Ответ при успехе:

```json
{"status":"saved","id":1,"date":"2026-08-01"}
```

Повторная отправка за ту же дату обновляет существующий snapshot, а не создаёт дубль.

## 3. Проверка сценария

1. Запусти Shortcut вручную.
2. В Telegram вызови `/steps` и сравни числа с Health.
3. Для теста поставь `HEALTH_NUDGE_TIME` на ближайшие 2–3 минуты и перезапусти бота.
4. При дефиците ≥ `HEALTH_MIN_STEP_GAP` должно прийти одно сообщение с фактическим остатком.
5. Нажми `Прогулка через 30 минут`: появятся задача и reminder с кнопкой undo.
6. Повтори Shortcut: snapshot обновится, но второй nudge в тот же день не придёт.

Если snapshot за сегодня отсутствует, уже набрана цель, записано ≥30 минут тренировки или включён тихий режим, сообщение не отправляется.

## 4. Apple Calendar для реалистичного плана

В отдельной автоматизации Shortcut:

1. `Find Calendar Events` → ближайшие 14 дней.
2. Для каждого события собрать `id`, `title`, `start`, `end`, `calendar`, `is_busy=true`.
3. Отправить массив JSON на `POST https://your-host.example/api/calendar/events` с тем же Bearer token.
4. В Telegram вызвать `/calendar`, затем `/today`.

Calendar endpoint только принимает зеркало занятых окон. Он не создаёт и не редактирует события Apple Calendar.

## Ограничения и приватность

- Bearer token защищает endpoint, но не заменяет HTTPS.
- Bridge привязан к `ALLOWED_USER_ID`; произвольный Telegram user id в payload не принимается.
- Планировщик использует агрегаты как сигнал для бытового напоминания, а не как медицинскую рекомендацию.
- Чтобы отключить интеграцию, установи `HEALTH_BRIDGE_ENABLED=false`, перезапусти бота и удали автоматизацию Shortcut.
