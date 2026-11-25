"""Единый шов UI: все вызовы questionary живут здесь, логика получает фейки в тестах."""

import questionary


class StyledTitle(list[tuple[str, str]]):
    """Список токенов prompt_toolkit (style, text), чьё str() возвращает плоский текст."""

    # ponytail: prompt_toolkit принимает список пар (стиль, текст);
    # переопределение __str__ позволяет коду логов/тестов видеть строку без ANSI-магии.
    def __str__(self) -> str:
        return "".join(part[1] for part in self)


def ask_select(message: str, choices: list[tuple[str, str]]) -> str | None:
    q_choices = [
        questionary.Choice(title=c[0], value=c[1]) if isinstance(c, tuple) else c
        for c in choices
    ]
    return questionary.select(message, choices=q_choices).ask()


def ask_text(message: str) -> str | None:
    return questionary.text(message).ask()


def ask_confirm(message: str, default: bool = False) -> bool:
    return questionary.confirm(message, default=default).ask()


def ask_chats(
    choices: list[tuple[str | StyledTitle, str | int, bool] | tuple[str]],
    message: str = "Отметьте чаты для удаления своих сообщений",
) -> list[str | int] | None:
    q_choices = [
        questionary.Separator(c[0])
        if len(c) == 1
        else questionary.Choice(c[0], value=c[1], checked=c[2])
        for c in choices
    ]
    return questionary.checkbox(
        message,
        choices=q_choices,
        instruction="Пробел — отметить, Enter — дальше",
    ).ask()
