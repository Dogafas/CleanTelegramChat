"""Единый шов UI: все вызовы questionary живут здесь, логика получает фейки в тестах.

Каждый адаптер сам восстанавливает цикл клиента после промпта: prompt_toolkit
(questionary) подменяет thread loop через asyncio.run(), а синхронные вызовы
Pyrogram требуют loop клиента. Парность ask→restore — деталь реализации шва,
вызывающие её не повторяют. app=None (меню до подключения клиента) — no-op.
"""

import asyncio
from typing import Any

import questionary


class StyledTitle(list[tuple[str, str]]):
    """Список токенов prompt_toolkit (style, text), чьё str() возвращает плоский текст."""

    # ponytail: prompt_toolkit принимает список пар (стиль, текст);
    # переопределение __str__ позволяет коду логов/тестов видеть строку без ANSI-магии.
    def __str__(self) -> str:
        return "".join(part[1] for part in self)


def restore_client_loop(app: Any) -> None:
    """prompt_toolkit (questionary) подменяет thread loop через asyncio.run().

    Синхронные вызовы Pyrogram и client.stop() требуют loop клиента.
    """
    loop = getattr(app, "loop", None)
    if loop is not None and not loop.is_closed():
        asyncio.set_event_loop(loop)


def ask_select(app: Any, message: str, choices: list[tuple[str, str]]) -> str | None:
    q_choices = [
        questionary.Choice(title=c[0], value=c[1]) if isinstance(c, tuple) else c
        for c in choices
    ]
    value = questionary.select(message, choices=q_choices).ask()
    restore_client_loop(app)
    return value


def ask_text(app: Any, message: str) -> str | None:
    value = questionary.text(message).ask()
    restore_client_loop(app)
    return value


def ask_confirm(app: Any, message: str, default: bool = False) -> bool:
    value = questionary.confirm(message, default=default).ask()
    restore_client_loop(app)
    return value


def ask_chats(
    app: Any,
    choices: list[tuple[str | StyledTitle, str | int, bool] | tuple[str]],
    message: str = "Отметьте чаты для удаления своих сообщений",
) -> list[str | int] | None:
    q_choices = [
        questionary.Separator(c[0])
        if len(c) == 1
        else questionary.Choice(c[0], value=c[1], checked=c[2])
        for c in choices
    ]
    value = questionary.checkbox(
        message,
        choices=q_choices,
        instruction="Пробел — отметить, Enter — дальше",
    ).ask()
    restore_client_loop(app)
    return value
