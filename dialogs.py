"""Main dialogs plus archive. Domain filters stay with callers."""

from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import Dialog

from session import sync_list


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
