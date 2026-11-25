import logging
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from pyrogram import enums
from pyrogram.errors import RPCError
from pyrogram.types import Chat, Dialog

from dialogs import list_dialogs
from ui import StyledTitle, ask_chats, ask_confirm

ALL = "all"
ALL_PUBLIC = "public"
ALL_PRIVATE = "private"
ALL_BASIC = "basic"

UiRows = list[tuple[str | StyledTitle, str | int, bool] | tuple[str]]


def chat_label(chat: Chat) -> str:
    title = getattr(chat, "title", None) or "без названия"
    username = getattr(chat, "username", None)
    if username:
        return f"{title} (@{username}) — {chat.id}"
    return f"{title} — {chat.id}"


def _membership_catalog(
    dialogs: Iterable[Dialog],
) -> tuple[list[Chat], list[Chat], list[Chat]]:
    public: list[Chat] = []
    private: list[Chat] = []
    basic: list[Chat] = []
    seen_ids: set[int] = set()

    for item in dialogs:
        chat = item.chat
        if chat is None or chat.type is None or chat.id is None:
            continue
        if chat.id in seen_ids:
            continue
        seen_ids.add(chat.id)

        if chat.type == enums.ChatType.SUPERGROUP:
            if getattr(chat, "username", None):
                public.append(chat)
            else:
                private.append(chat)
        elif chat.type == enums.ChatType.GROUP:
            basic.append(chat)

    return public, private, basic


def _sections(
    public: list[Chat], private: list[Chat], basic: list[Chat]
) -> tuple[tuple[list[Chat], str, str, str], ...]:
    return (
        (public, ALL_PUBLIC, "Все публичные", "Публичные супергруппы"),
        (private, ALL_PRIVATE, "Все закрытые", "Закрытые супергруппы"),
        (basic, ALL_BASIC, "Все обычные", "Обычные группы"),
    )


def _resolve_selection(
    public: list[Chat],
    private: list[Chat],
    basic: list[Chat],
    values: Iterable[str | int],
) -> list[Chat]:
    val_set = set(values)
    selected_ids = {v for v in val_set if isinstance(v, int) and not isinstance(v, bool)}
    result: list[Chat] = []
    seen: set[int] = set()
    for chats, flag, _bulk, _label in _sections(public, private, basic):
        include = ALL in val_set or flag in val_set
        for chat in chats:
            if chat.id is None:
                continue
            if chat.id not in seen and (include or chat.id in selected_ids):
                seen.add(chat.id)
                result.append(chat)
    return result



def _build_choices(
    public: list[Chat],
    private: list[Chat],
    basic: list[Chat],
    previous_selected: set[str | int],
) -> UiRows:
    # ponytail: plain tuples (title, value, checked) / (label,) avoid leaking
    # questionary into catalog logic
    sections = _sections(public, private, basic)
    choices: UiRows = [
        ("Все показанные", ALL, ALL in previous_selected),
    ]
    for chats, value, bulk, _label in sections:
        if chats:
            choices.append((bulk, value, value in previous_selected))
    for chats, _value, _bulk, label in sections:
        if not chats:
            continue
        choices.append((label,))
        for chat in chats:
            if chat.id is None:
                continue
            choices.append((chat_label(chat), chat.id, chat.id in previous_selected))
    return choices


def select_from_dialogs(
    dialogs: Iterable[Dialog],
    *,
    ask: Callable[[UiRows], Any],
    confirm: Callable[[str], Any],
    log: Callable[[str], None] = logging.info,
) -> list[Chat]:
    public, private, basic = _membership_catalog(dialogs)
    if not public and not private and not basic:
        log("Нет чатов для выбора.")
        return []

    previous_selected: set[str | int] = set()

    while True:
        choices = _build_choices(public, private, basic, previous_selected)
        selected_values = ask(choices)
        if selected_values is None:
            log("Выбор отменён.")
            return []
        if not selected_values:
            log("Ничего не выбрано.")
            return []

        selected_chats = _resolve_selection(public, private, basic, selected_values)
        if not selected_chats:
            log("Ничего не выбрано.")
            return []

        log(f"Будет очищено {len(selected_chats)} чатов:")
        for chat in selected_chats:
            log(chat_label(chat))

        confirmed = confirm("Удалить свои сообщения в этих чатах?")
        if confirmed is True:
            titles = [getattr(chat, "title", None) or "без названия" for chat in selected_chats]
            log(f"Вы выбрали для удаления сообщений в: {', '.join(titles)}")
            return selected_chats

        previous_selected = set(selected_values)


def prompt_selection(
    app: Any,
    *,
    ask: Callable[[UiRows], Any] | None = None,
    confirm: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = logging.info,
) -> list[Chat]:
    if ask is None:
        ask = partial(ask_chats, app)
    if confirm is None:
        confirm = partial(ask_confirm, app)

    try:
        main_dialogs, archive_dialogs, archive_error = list_dialogs(app)
    except RPCError as e:
        log(f"Список диалогов не прочитан: {e}")
        return []
    if archive_error:
        log(f"Архив диалогов не прочитан: {archive_error}")
        return []
    all_dialogs = main_dialogs + archive_dialogs
    log(f"Итого у Вас доступ к {len(all_dialogs)} чатам, группам, каналам, ботам...")

    return select_from_dialogs(all_dialogs, ask=ask, confirm=confirm, log=log)
