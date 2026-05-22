#!/usr/bin/env python3
"""Patch health.py: add clarification note to /miro_debug."""

path = "bot/handlers/health.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

OLD = '    lines += [\n        "",\n        "Логи: journalctl -u 813assistant -n 120 --no-pager",\n    ]\n    await message.answer("\\n".join(lines))\n    await screen_service.delete_user_input(message)\n'

NEW = '    lines += [\n        "",\n        "/miro_debug только проверяет API-соединение.",\n        "Для отрисовки штаба используй /sync_miro.",\n        "",\n        "Логи: journalctl -u 813assistant -n 120 --no-pager",\n    ]\n    await message.answer("\\n".join(lines))\n    await screen_service.delete_user_input(message)\n'

if OLD in content:
    content = content.replace(OLD, NEW, 1)
    print("OK: added miro_debug clarification note")
else:
    print("WARN: target block not found in health.py")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
