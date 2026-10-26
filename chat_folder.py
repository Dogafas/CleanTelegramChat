"""Chat Folder raw protocol. Sort creates; reading mode adds a peer."""

from __future__ import annotations

import logging
from typing import Any

from pyrogram.errors import RPCError

FALLBACK_FOLDERS = 10
FALLBACK_CHATS = 100
PREMIUM_FOLDERS = 20
PREMIUM_CHATS = 200
MIN_FOLDER_ID = 2

_COPY_FIELDS = (
    "pinned_peers",
    "exclude_peers",
    "contacts",
    "non_contacts",
    "groups",
    "broadcasts",
    "bots",
    "exclude_muted",
    "exclude_read",
    "exclude_archived",
    "title_noanimate",
    "emoticon",
    "color",
)


def filter_title(folder: Any) -> str:
    title = getattr(folder, "title", None)
    if isinstance(title, str):
        return title.strip()
    text = getattr(title, "text", None)
    if isinstance(text, str):
        return text.strip()
    return ""


def snapshot(app: Any, log: Any = logging.info) -> tuple[frozenset[str], int, int, int, bool]:
    """(titles, folder_limit, chat_limit, used, ok). ok is false if filters cannot be read."""
    filters = _read_filters(app, log)
    if filters is None:
        return frozenset(), 0, 0, 0, False
    folder_limit, chat_limit = _limits(app, log)
    titles = frozenset(title for folder in filters if (title := filter_title(folder)))
    return titles, folder_limit, chat_limit, len(filters), True


def create_only(
    app: Any,
    title: str,
    chat_ids: list[int],
    *,
    used_ids: set[int] | None = None,
    log: Any = logging.info,
) -> tuple[str, list[tuple[int, str]], int]:
    """Create a missing folder. Never updates an existing title.

    Returns (status, unresolved, created_count). status: created|exists|empty|error|no_id.
    """
    filters = _read_filters(app, log)
    if filters is None:
        return "error", [], 0
    if title in {filter_title(folder) for folder in filters}:
        return "exists", [], 0
    taken = {folder.id for folder in filters if isinstance(getattr(folder, "id", None), int)}
    if used_ids:
        taken |= used_ids
    folder_id = next((cid for cid in range(MIN_FOLDER_ID, 256) if cid not in taken), None)
    if folder_id is None:
        return "no_id", [], 0

    unresolved: list[tuple[int, str]] = []
    peers = []
    for chat_id in chat_ids:
        try:
            peers.append(app.resolve_peer(chat_id))
        except Exception as e:
            unresolved.append((chat_id, str(e)))
    if not peers:
        return "empty", unresolved, 0

    from pyrogram import raw  # noqa: PLC0415

    dialog_filter = raw.types.DialogFilter(
        id=folder_id,
        title=raw.types.TextWithEntities(text=title, entities=[]),
        pinned_peers=[],
        include_peers=peers,
        exclude_peers=[],
    )
    try:
        app.invoke(
            raw.functions.messages.UpdateDialogFilter(id=folder_id, filter=dialog_filter),
            sleep_threshold=60,
        )
    except RPCError as e:
        log(f"Вкладка «{title}» не создана: {e}")
        return "error", unresolved, 0
    if used_ids is not None:
        used_ids.add(folder_id)
    return "created", unresolved, len(peers)


def add_peer(app: Any, chat_id: int, title: str) -> tuple[str, int | None, bool]:
    """Add a chat to a reading folder. Returns (status, folder_id, was_new)."""
    filters = _read_filters(app)
    if filters is None:
        return "error", None, False
    folder_limit, _ = _limits(app)
    target = next((folder for folder in filters if filter_title(folder) == title), None)
    try:
        peer = app.resolve_peer(chat_id)
    except Exception:
        return "error", None, False
    peer_id = _peer_id(peer)

    from pyrogram import raw  # noqa: PLC0415

    if target is not None:
        include = list(getattr(target, "include_peers", []))
        if any(_peer_id(item) == peer_id for item in include):
            return "already_set", getattr(target, "id", None), False
        updated = _copied(target, include_peers=[*include, peer])
        app.invoke(
            raw.functions.messages.UpdateDialogFilter(id=target.id, filter=updated),
            sleep_threshold=60,
        )
        return "applied", target.id, False

    if len(filters) >= folder_limit:
        return "error", None, False
    used = {getattr(folder, "id", None) for folder in filters}
    new_id = MIN_FOLDER_ID
    while new_id in used:
        new_id += 1
    created = raw.types.DialogFilter(
        id=new_id,
        title=raw.types.TextWithEntities(text=title, entities=[]),
        pinned_peers=[],
        include_peers=[peer],
        exclude_peers=[],
    )
    app.invoke(
        raw.functions.messages.UpdateDialogFilter(id=new_id, filter=created),
        sleep_threshold=60,
    )
    return "applied", new_id, True


def remove_peer(app: Any, chat_id: int, folder_id: int) -> str:
    """Drop a peer. The folder itself stays, even if it becomes empty."""
    filters = _read_filters(app) or []
    target = next((folder for folder in filters if getattr(folder, "id", None) == folder_id), None)
    if target is None:
        return "skipped"
    peer = app.resolve_peer(chat_id)
    peer_id = _peer_id(peer)
    include = [item for item in getattr(target, "include_peers", []) if _peer_id(item) != peer_id]
    updated = _copied(target, include_peers=include)

    from pyrogram import raw  # noqa: PLC0415

    app.invoke(
        raw.functions.messages.UpdateDialogFilter(id=target.id, filter=updated),
        sleep_threshold=60,
    )
    return "restored"


def _read_filters(app: Any, log: Any = logging.info) -> list[Any] | None:
    try:
        from pyrogram import raw  # noqa: PLC0415

        res = app.invoke(raw.functions.messages.GetDialogFilters())
        if hasattr(res, "filters") and isinstance(res.filters, list):
            return res.filters
        if isinstance(res, list):
            return res
        log(
            f"Список вкладок не прочитан: неожиданный тип {type(res).__name__}. "
            "Вкладки не создаём."
        )
        return None
    except Exception as e:
        log(f"Список вкладок не прочитан: {e}. Вкладки не создаём.")
        return None


def _limits(app: Any, log: Any = logging.info) -> tuple[int, int]:
    try:
        from pyrogram import raw  # noqa: PLC0415

        me = app.get_me()
        config = app.invoke(raw.functions.help.GetAppConfig(hash=0))
        if not isinstance(config, raw.types.JsonObject):
            log("Лимиты вкладок не прочитаны, берём 10 папок и 100 чатов.")
            return FALLBACK_FOLDERS, FALLBACK_CHATS
        is_prem = bool(getattr(me, "is_premium", False))
        cfg = {
            item.key: int(item.value.value)
            for item in config.value
            if getattr(item, "key", None)
            and isinstance(getattr(item, "value", None), raw.types.JsonNumber)
        }
        chat_key = (
            "dialog_filters_chats_limit_premium"
            if is_prem
            else "dialog_filters_chats_limit_default"
        )
        folders = cfg.get(
            "dialog_filters_limit_premium" if is_prem else "dialog_filters_limit_default",
            PREMIUM_FOLDERS if is_prem else FALLBACK_FOLDERS,
        )
        chats = cfg.get(chat_key, PREMIUM_CHATS if is_prem else FALLBACK_CHATS)
        log(f"Лимиты вкладок: {folders} папок, {chats} чатов.")
        return folders, chats
    except Exception:
        log("Лимиты вкладок не прочитаны, берём 10 папок и 100 чатов.")
        return FALLBACK_FOLDERS, FALLBACK_CHATS


def _peer_id(peer: Any) -> int | None:
    for attr in ("channel_id", "chat_id", "user_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return int(val)
    return None


def _copied(source: Any, *, include_peers: list[Any]) -> Any:
    from pyrogram import raw  # noqa: PLC0415

    kwargs = {
        "id": source.id,
        "title": source.title,
        "include_peers": include_peers,
    }
    for name in _COPY_FIELDS:
        if name in {"pinned_peers", "exclude_peers"}:
            kwargs[name] = list(getattr(source, name, []) or [])
        else:
            kwargs[name] = getattr(source, name, None)
    return raw.types.DialogFilter(**kwargs)
