"""Main dialogs plus archive, and domain dialog-type predicates (GLOSSARY)."""

from pyrogram import Client, enums
from pyrogram.errors import RPCError
from pyrogram.types import Chat, Dialog

from session import sync_list


def is_channel_subscription(row: Dialog) -> bool:
    """Channel Subscription (GLOSSARY): канал в подписках аккаунта."""
    chat = getattr(row, "chat", None)
    return chat is not None and getattr(chat, "type", None) == enums.ChatType.CHANNEL


def is_sortable_dialog(chat: Chat) -> bool:
    """Sortable Dialog (GLOSSARY): канал, супергруппа или обычная группа."""
    return chat.type in (
        enums.ChatType.CHANNEL,
        enums.ChatType.SUPERGROUP,
        enums.ChatType.GROUP,
    )


def is_bot_dialog(row: Dialog) -> bool:
    """Бот-диалог (ADR 0011): личный чат с ботом."""
    chat = getattr(row, "chat", None)
    return chat is not None and getattr(chat, "type", None) == enums.ChatType.BOT


def _chat_id(row: Dialog) -> int | None:
    chat = getattr(row, "chat", None)
    cid = getattr(chat, "id", None)
    return int(cid) if cid is not None else None


def list_dialogs(app: Client) -> tuple[list[Dialog], list[Dialog], str | None]:
    """(main, archive, archive_error). Main failure propagates. Main ids win."""
    main = sync_list(app.get_dialogs(from_archive=False))
    archive_error = None
    archive: list[Dialog] = []
    try:
        archive = sync_list(app.get_dialogs(from_archive=True))
    except RPCError as e:
        archive_error = str(e)
        archive = []
    seen = {cid for row in main if (cid := _chat_id(row)) is not None}
    archive = [
        row
        for row in archive
        if (cid := _chat_id(row)) is None or cid not in seen
    ]
    return main, archive, archive_error
