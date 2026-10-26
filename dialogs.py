"""Main dialogs plus archive. Domain filters stay with callers."""

from typing import Any

from pyrogram.errors import RPCError


def _chat_id(row: Any) -> int | None:
    chat = getattr(row, "chat", None)
    cid = getattr(chat, "id", None)
    return int(cid) if cid is not None else None


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


def list_dialogs(app: Any) -> tuple[list[Any], list[Any], str | None]:
    """(main, archive, archive_error). Main failure propagates. Main ids win."""
    main = list(app.get_dialogs())
    archive_error = None
    archive: list[Any] = []
    try:
        archive = list(_iter_archive(app))
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
