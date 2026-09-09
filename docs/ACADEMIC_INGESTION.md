# Academic schedule ingestion

The bot treats screenshots and free-form AI output as optional evidence, not as
the source of truth. Academic data enters through source adapters, is normalized,
and only then reaches planning:

```text
HSE iCal / iPhone calendar HSE / future LMS and mail adapters
                         │
                         ▼
                normalized records
                         │
                         ▼
                  CalendarEvent DB
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
       conflict detector       day planner
              └──────────┬──────────┘
                         ▼
                daily brief / review
```

## Available now

- Send an HSE-exported `.ics` file to the bot. The import is owner-isolated,
  idempotent, limited to 5 MB, and replaces the previous HSE snapshot.
- The HSE App iPhone path mirrors its system calendar named exactly `HSE`
  through a private Shortcuts automation. The server rejects other calendar
  names, keeps the stable identifier from event notes, and never reads the
  user's other calendars.
- Optionally set `HSE_ICAL_URL`, `HSE_ICAL_SYNC_ENABLED=true`, and the polling
  interval. Only HTTPS links on `hse.ru` subdomains are accepted, redirects are
  checked before following them, and the private URL is never logged.
- Normalized events keep source ID, title, type, description, teacher, building,
  room, location, start, end, and busy status.
- “Сводка дня”, “Итоги дня”, and “Покажи конфликты” are read-only queries.
  Generating a brief never writes generated timeboxes back to tasks.
- `/home Дубки` stores the departure origin. `/route_fact Дубки | ВШЭ на
  Покровке | 75` stores a known one-way route duration. When no fact exists,
  the detector labels its transfer buffer as approximate.

The existing authenticated Calendar bridge accepts the same optional normalized
fields in every event:

```json
{
  "source": "calendar_shortcut",
  "events": [{
    "id": "stable-source-id",
    "title": "Семинар",
    "event_type": "seminar",
    "description": "...",
    "teacher": "...",
    "building": "...",
    "room": "519",
    "location": "Б. Трехсвятительский пер., 3",
    "start": "2026-09-08T14:40:00+03:00",
    "end": "2026-09-08T16:00:00+03:00",
    "is_busy": true
  }]
}
```

## Adapter boundary for the next sources

Smart LMS and corporate email are not scraped with a password or simulated from
screenshots. Their adapters need an approved authentication flow and must return
one of these contracts before data can affect a plan:

```json
{
  "source": "smart_lms",
  "external_id": "stable-id",
  "course": "Course name",
  "title": "Assignment title",
  "deadline": "2026-09-10T23:59:00+03:00",
  "url": "https://...",
  "confidence": 1.0,
  "observed_at": "2026-09-08T08:00:00+03:00"
}
```

```json
{
  "source": "corporate_email",
  "external_id": "message-id",
  "kind": "deadline_candidate",
  "title": "Submit application",
  "deadline": "2026-09-11T18:00:00+03:00",
  "source_url": "https://...",
  "confidence": 0.91,
  "requires_confirmation": true
}
```

An image/attachment parser must also return structured candidates with source
coordinates and confidence. Low-confidence candidates require user confirmation;
they must never silently create a deadline or calendar event.

## Conflict semantics

The detector reports either a direct overlap or insufficient transfer time.
Exact saved route facts include the configured arrival buffer. Unknown routes use
`DEFAULT_TRANSFER_BUFFER_MINUTES` and carry an approximation marker. Identical
events mirrored by two sources are suppressed as duplicates.
