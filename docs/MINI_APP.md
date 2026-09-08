# Telegram Mini App

Mini App — основной визуальный интерфейс 813Assistant. Постоянная кнопка
`Управление` настраивается через Telegram Bot API при запуске процесса, а нижняя
навигация внутри приложения всегда показывает четыре раздела: день, планнер,
задачи и профиль.

Вход в защищённый интерфейс выполняется именно нативной menu button возле поля
ввода. Reply-клавиатура остаётся для текстовых фраз: Telegram не гарантирует
авторизационный `initData` для Mini App, запущенного из `KeyboardButton`.

## Конфигурация

```dotenv
MINI_APP_ENABLED=true
MINI_APP_PUBLIC_URL=https://assistant.example.com/assistant/
MINI_APP_HOST=127.0.0.1
MINI_APP_PORT=8782
```

Публичный URL обязан использовать HTTPS и завершаться `/`: frontend обращается
к API относительными путями. Локальный порт не нужно публиковать напрямую.

Пример location для nginx:

```nginx
location = /assistant {
    return 301 /assistant/;
}

location /assistant/ {
    proxy_pass http://127.0.0.1:8782/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Telegram Web открывает Mini App во frame. Не наследуем серверный
    # X-Frame-Options: SAMEORIGIN; допустимые origins заданы приложением в CSP.
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
}
```

После изменения проверяются конфигурация и публичный endpoint:

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://assistant.example.com/assistant/healthz
```

## Модель безопасности

- Данные API недоступны без заголовка `Authorization: tma <initData>`.
- Сервер проверяет HMAC-подпись Telegram, `auth_date` и allowlist пользователя.
- `initDataUnsafe` не используется как источник доверенных данных.
- Ответы API имеют `Cache-Control: no-store`; статические ответы получают CSP,
  `nosniff`, запрет внешних форм и ограничение Telegram origins для frame.
- Импорт календаря ограничен 5 МБ и меняет только события источника `hse_ical`
  конкретного пользователя.

`X-Debug-User` работает только у экземпляра сервера, созданного с явным
`dev_mode=True`; production-процесс этот режим не включает.

## Поведение задач

Завершение задачи — смена статуса, а не удаление. Она остаётся в разделе
`Завершено сегодня`, где доступно действие `Вернуть`. Временной блок завершённой
задачи больше не показывается как предстоящее дело. Изменить задачу другого
пользователя невозможно: все запросы фильтруются одновременно по `task_id` и
проверенному `user_id`.
