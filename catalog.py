import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

from pyrogram import enums
from pyrogram.errors import RPCError

ALL = "all"
ALL_PUBLIC = "public"
ALL_PRIVATE = "private"
ALL_BASIC = "basic"


def chat_label(chat: Any) -> str:
    title = getattr(chat, "title", None) or "без названия"
    username = getattr(chat, "username", None)
    if username:
        return f"{title} (@{username}) — {chat.id}"
    return f"{title} — {chat.id}"


def membership_catalog(dialogs: Iterable[Any]) -> tuple[list[Any], list[Any], list[Any]]:
    public: list[Any] = []
    private: list[Any] = []
    basic: list[Any] = []
    seen_ids: set[Any] = set()

    for item in dialogs:
        chat = getattr(item, "chat", item)
        if chat is None or not hasattr(chat, "id") or not hasattr(chat, "type"):
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


def resolve_selection(
    public: list[Any],
    private: list[Any],
    basic: list[Any],
    values: Iterable[Any],
) -> list[Any]:
    val_set = set(values)
    include_all = ALL in val_set
    include_public = include_all or ALL_PUBLIC in val_set
    include_private = include_all or ALL_PRIVATE in val_set
    include_basic = include_all or ALL_BASIC in val_set

    selected_ids = {
        v for v in val_set if isinstance(v, int) and not isinstance(v, bool)
    }

    result: list[Any] = []
    seen: set[Any] = set()

    for chat in public:
        if (include_public or chat.id in selected_ids) and chat.id not in seen:
            seen.add(chat.id)
            result.append(chat)

    for chat in private:
        if (include_private or chat.id in selected_ids) and chat.id not in seen:
            seen.add(chat.id)
            result.append(chat)

    for chat in basic:
        if (include_basic or chat.id in selected_ids) and chat.id not in seen:
            seen.add(chat.id)
            result.append(chat)

    return result


def _iter_archive(app: Any) -> Any:
    from pyrogram import raw, types  # noqa: PLC0415

    offset_date = 0
    offset_id = 0
    offset_peer = raw.types.InputPeerEmpty()
    previous_last = None
    while True:
        fetched = app.invoke(
            raw.functions.messages.GetDialogs(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=100,
                hash=0,
                folder_id=1,
            ),
            sleep_threshold=60,
        )
        users = {item.id: item for item in fetched.users}
        chats = {item.id: item for item in fetched.chats}
        dates = {
            message.id: message.date
            for message in fetched.messages
            if not isinstance(message, raw.types.MessageEmpty)
            and hasattr(message, "id")
            and hasattr(message, "date")
        }
        page = []
        last_id = None
        last_top = None
        last_date = None
        for dialog in fetched.dialogs:
            if not isinstance(dialog, raw.types.Dialog):
                continue
            try:
                chat = types.Chat._parse_dialog(app, dialog.peer, users, chats)
            except (KeyError, AttributeError):
                continue
            page.append(type("Row", (), {"chat": chat})())
            last_id = chat.id
            last_top = dialog.top_message
            last_date = dates.get(dialog.top_message)
        if not page:
            return
        if last_id == previous_last or last_date is None:
            return
        previous_last = last_id
        yield from page
        offset_id = last_top
        offset_date = last_date
        offset_peer = app.resolve_peer(last_id)


def _ask(choices: list[Any]) -> Any:
    import questionary  # noqa: PLC0415

    return questionary.checkbox(
        "Отметьте чаты для удаления своих сообщений",
        choices=choices,
        instruction="Пробел — отметить, Enter — дальше",
    ).ask()


def _confirm(message: str) -> Any:
    import questionary  # noqa: PLC0415

    return questionary.confirm(message, default=False).ask()


def _build_choices(
    public: list[Any],
    private: list[Any],
    basic: list[Any],
    previous_selected: set[Any],
) -> list[Any]:
    import questionary  # noqa: PLC0415

    choices: list[Any] = [
        questionary.Choice("Все показанные", value=ALL, checked=(ALL in previous_selected)),  # noqa: RUF001
    ]
    if public:
        choices.append(
            questionary.Choice(
                "Все публичные",  # noqa: RUF001
                value=ALL_PUBLIC,
                checked=(ALL_PUBLIC in previous_selected),
            )
        )
    if private:
        choices.append(
            questionary.Choice(
                "Все закрытые",  # noqa: RUF001
                value=ALL_PRIVATE,
                checked=(ALL_PRIVATE in previous_selected),
            )
        )
    if basic:
        choices.append(
            questionary.Choice(
                "Все обычные",  # noqa: RUF001
                value=ALL_BASIC,
                checked=(ALL_BASIC in previous_selected),
            )
        )

    sections = [
        (public, "Публичные супергруппы"),
        (private, "Закрытые супергруппы"),
        (basic, "Обычные группы"),
    ]
    for chats, label in sections:
        if not chats:
            continue
        choices.append(questionary.Separator(label))
        for chat in chats:
            choices.append(
                questionary.Choice(
                    chat_label(chat),
                    value=chat.id,
                    checked=(chat.id in previous_selected),
                )
            )
    return choices


def select_from_dialogs(
    dialogs: Iterable[Any],
    *,
    ask: Callable[[list[Any]], Any],
    confirm: Callable[[str], Any],
    log: Callable[[str], None],
) -> list[Any]:
    public, private, basic = membership_catalog(dialogs)
    if not public and not private and not basic:
        log("Нет чатов для выбора.")
        return []

    previous_selected: set[Any] = set()

    while True:
        choices = _build_choices(public, private, basic, previous_selected)
        selected_values = ask(choices)
        if selected_values is None:
            log("Выбор отменён.")
            return []
        if not selected_values:
            log("Ничего не выбрано.")
            return []

        selected_chats = resolve_selection(public, private, basic, selected_values)
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
    ask: Callable[[list[Any]], Any] | None = None,
    confirm: Callable[[str], Any] | None = None,
    log: Callable[[str], None] = logging.info,
) -> list[Any]:
    if ask is None:
        ask = _ask
    if confirm is None:
        confirm = _confirm

    try:
        main_dialogs = list(app.get_dialogs())
    except RPCError as e:
        log(f"Список диалогов не прочитан: {e}")
        return []

    try:
        archive_dialogs = list(_iter_archive(app))
    except RPCError as e:
        log(f"Архив диалогов не прочитан: {e}")
        return []

    all_dialogs = main_dialogs + archive_dialogs
    log(f"Итого у Вас доступ к {len(all_dialogs)} чатам, группам, каналам, ботам...")  # noqa: RUF001

    loop = getattr(app, "loop", None)
    try:
        return select_from_dialogs(all_dialogs, ask=ask, confirm=confirm, log=log)
    finally:
        # prompt_toolkit Application.run() -> asyncio.run() clears the thread loop.
        if loop is not None and not loop.is_closed():
            asyncio.set_event_loop(loop)
