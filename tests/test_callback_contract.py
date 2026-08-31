from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _value_pattern(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("*")
        return "".join(parts)
    return None


def _button_callbacks() -> dict[str, str]:
    callbacks = {}
    for path in (ROOT / "bot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node.func) != "InlineKeyboardButton":
                continue
            for keyword in node.keywords:
                if keyword.arg != "callback_data":
                    continue
                pattern = _value_pattern(keyword.value)
                if pattern:
                    callbacks[pattern] = f"{path.relative_to(ROOT)}:{node.lineno}"
    return callbacks


def _handler_contracts() -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    prefixes: set[str] = set()
    for path in (ROOT / "bot" / "handlers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
        ):
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call) or _call_name(decorator.func) != "callback_query":
                    continue
                for expression in decorator.args:
                    if isinstance(expression, ast.Compare):
                        for comparator in expression.comparators:
                            value = _value_pattern(comparator)
                            if value:
                                exact.add(value)
                    elif isinstance(expression, ast.Call):
                        operation = _call_name(expression.func)
                        if operation == "startswith" and expression.args:
                            value = _value_pattern(expression.args[0])
                            if value:
                                prefixes.add(value)
                        elif operation == "in_" and expression.args:
                            container = expression.args[0]
                            for item in getattr(container, "elts", []):
                                value = _value_pattern(item)
                                if value:
                                    exact.add(value)
    return exact, prefixes


class CallbackContractTests(unittest.TestCase):
    def test_every_rendered_callback_has_a_handler(self):
        exact, prefixes = _handler_contracts()
        missing = []
        for pattern, location in sorted(_button_callbacks().items()):
            static_prefix = pattern.split("*", 1)[0]
            handled = (
                pattern in exact
                or any(pattern.startswith(prefix) for prefix in prefixes)
                or any(static_prefix.startswith(prefix) for prefix in prefixes)
            )
            if not handled:
                missing.append(f"{pattern} at {location}")
        self.assertEqual([], missing, "Buttons without callback handlers:\n" + "\n".join(missing))

    def test_button_labels_do_not_expose_internal_identifiers(self):
        violations = []
        for path in (ROOT / "bot").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _call_name(node.func) != "InlineKeyboardButton":
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "text":
                        continue
                    label = _value_pattern(keyword.value)
                    if label and ("_" in label or "callback" in label.casefold()):
                        violations.append(f"{label!r} at {path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual([], violations, "Technical button labels:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
