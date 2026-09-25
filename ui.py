"""Единый шов UI: все вызовы questionary живут здесь, логика получает фейки в тестах."""

from typing import Any


def ask_select(message: str, choices: list[Any]) -> Any:
    import questionary  # noqa: PLC0415

    q_choices = [
        questionary.Choice(title=c[0], value=c[1]) if isinstance(c, tuple) else c
        for c in choices
    ]
    return questionary.select(message, choices=q_choices).ask()


def ask_text(message: str) -> str | None:
    import questionary  # noqa: PLC0415

    return questionary.text(message).ask()


def ask_confirm(message: str, default: bool = False) -> bool:
    import questionary  # noqa: PLC0415

    return questionary.confirm(message, default=default).ask()


def ask_chats(choices: list[Any]) -> Any:
    import questionary  # noqa: PLC0415

    q_choices = [
        questionary.Separator(c[0])
        if len(c) == 1
        else questionary.Choice(c[0], value=c[1], checked=c[2])
        for c in choices
    ]
    return questionary.checkbox(
        "Отметьте чаты для удаления своих сообщений",
        choices=q_choices,
        instruction="Пробел — отметить, Enter — дальше",
    ).ask()
