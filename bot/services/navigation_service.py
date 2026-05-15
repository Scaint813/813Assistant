from __future__ import annotations


class NavigationService:
    def __init__(self):
        self._stack: dict[int, list[str]] = {}

    def push(self, user_id: int, screen: str):
        self._stack.setdefault(user_id, []).append(screen)

    def back(self, user_id: int) -> str:
        stack = self._stack.setdefault(user_id, ["main_menu"])
        if len(stack) > 1:
            stack.pop()
        return stack[-1]

    def current(self, user_id: int) -> str:
        return self._stack.get(user_id, ["main_menu"])[-1]

    def reset(self, user_id: int):
        self._stack[user_id] = ["main_menu"]
