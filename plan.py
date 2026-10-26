"""Reading modes, action plan, and safe rollback (Section 3).

Обратимые альтернативы отписке: без уведомлений (mute), в архив (archive),
читать отдельно (folder), и отписка (leave) с ведением журнала отката.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sort_chats import _filter_title, existing_filters, folder_limits
from subs import leave_chat

FOREVER_MUTE = 2147483647
ARCHIVE_FOLDER_ID = 1
MIN_FOLDER_ID = 2


@dataclass
class ChannelAction:
    """Запланированные действия для одного канала."""

    chat_id: int
    title: str
    leave: bool = False
    mute: bool = False
    archive: bool = False
    folder_id: int | None = None
    folder_title: str | None = None


class ActionPlan:
    """План действий по каналам текущего аккаунта (Section 3)."""

    def __init__(self) -> None:
        self.actions: dict[int, ChannelAction] = {}

    def _get_or_create(self, chat_id: int, title: str) -> ChannelAction:
        if chat_id not in self.actions:
            self.actions[chat_id] = ChannelAction(chat_id=chat_id, title=title)
        return self.actions[chat_id]

    def set_leave(self, chat_id: int, title: str) -> None:
        """Отписка несовместима с другими режимами этого канала."""
        # ponytail: отписка сбрасывает mute/archive/folder
        act = self._get_or_create(chat_id, title)
        act.leave = True
        act.mute = False
        act.archive = False
        act.folder_id = None
        act.folder_title = None

    def set_mute(self, chat_id: int, title: str, enable: bool = True) -> None:
        """Режим 'без уведомлений'."""
        act = self._get_or_create(chat_id, title)
        act.mute = enable
        act.leave = False

    def set_archive(self, chat_id: int, title: str, enable: bool = True) -> None:
        """Режим 'в архив' (mute выбирается отдельно)."""
        act = self._get_or_create(chat_id, title)
        act.archive = enable
        act.leave = False

    def set_folder(
        self, chat_id: int, title: str, folder_id: int, folder_title: str
    ) -> None:
        """Режим 'читать отдельно' (добавление в папку)."""
        act = self._get_or_create(chat_id, title)
        act.folder_id = folder_id
        act.folder_title = folder_title
        act.leave = False

    def remove(self, chat_id: int) -> None:
        _ = self.actions.pop(chat_id, None)

    def is_empty(self) -> bool:
        return not any(
            a.leave or a.mute or a.archive or a.folder_id is not None
            for a in self.actions.values()
        )

    def summary(self) -> dict[str, int]:
        """Сводка запланированных изменений."""
        leave_count = sum(1 for a in self.actions.values() if a.leave)
        mute_count = sum(1 for a in self.actions.values() if a.mute)
        archive_count = sum(1 for a in self.actions.values() if a.archive)
        folder_count = sum(1 for a in self.actions.values() if a.folder_id is not None)
        return {
            "leave": leave_count,
            "mute": mute_count,
            "archive": archive_count,
            "folder": folder_count,
            "total_channels": len(self.actions),
        }


def _get_peer_id(peer: Any) -> int | None:
    for attr in ("channel_id", "chat_id", "user_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return int(val)
    return None


def _journal_path(session_name: str, root_dir: Path | None = None) -> Path:
    base = root_dir or Path("sessions")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"rollback_{session_name}.json"


def save_rollback_journal(
    session_name: str,
    records: list[dict[str, Any]],
    root_dir: Path | None = None,
) -> Path:
    """Сохранение журнала отката без секретов, токенов и текстов (Section 3)."""
    # ponytail: локальный json в sessions/, исключённый из git
    path = _journal_path(session_name, root_dir)
    data = {
        "session_name": session_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "records": records,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_rollback_journal(
    session_name: str, root_dir: Path | None = None
) -> list[dict[str, Any]]:
    path = _journal_path(session_name, root_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data.get("records")
        return list(records) if isinstance(records, list) else []
    except Exception:
        return []


def _apply_mute(
    app: Any,
    chat_id: int,
) -> tuple[str, dict[str, Any] | None]:
    from pyrogram import raw  # noqa: PLC0415

    peer = app.resolve_peer(chat_id)
    settings = app.invoke(
        raw.functions.account.GetNotifySettings(
            peer=raw.types.InputNotifyPeer(peer=peer)
        )
    )
    prev_mute = getattr(settings, "mute_until", 0) or 0
    now_ts = int(time.time())

    if prev_mute > now_ts:
        return "already_set", None

    app.invoke(
        raw.functions.account.UpdateNotifySettings(
            peer=raw.types.InputNotifyPeer(peer=peer),
            settings=raw.types.InputPeerNotifySettings(mute_until=FOREVER_MUTE),
        )
    )
    journal_record = {
        "action": "mute",
        "chat_id": chat_id,
        "prev_state": {"mute_until": prev_mute},
        "applied_state": {"mute_until": FOREVER_MUTE},
    }
    return "applied", journal_record


def _apply_archive(
    app: Any,
    chat_id: int,
) -> tuple[str, dict[str, Any] | None]:
    try:
        chat_info = app.get_chat(chat_id)
        if getattr(chat_info, "folder_id", None) == ARCHIVE_FOLDER_ID:
            return "already_set", None
    except Exception as e:
        _ = e

    app.archive_chats([chat_id])
    journal_record = {
        "action": "archive",
        "chat_id": chat_id,
        "prev_state": {"folder_id": 0},
        "applied_state": {"folder_id": ARCHIVE_FOLDER_ID},
    }
    return "applied", journal_record


def _apply_folder(
    app: Any,
    chat_id: int,
    folder_id: int,
    folder_title: str,
) -> tuple[str, dict[str, Any] | None]:
    from pyrogram import raw  # noqa: PLC0415

    filters = existing_filters(app) or []
    folder_limit, _ = folder_limits(app)
    target_filter = next(
        (
            f
            for f in filters
            if getattr(f, "id", None) == folder_id
            or (folder_title and _filter_title(f) == folder_title)
        ),
        None,
    )

    peer = app.resolve_peer(chat_id)
    peer_id = _get_peer_id(peer)

    if target_filter is not None:
        include_peers = list(getattr(target_filter, "include_peers", []))
        if any(_get_peer_id(p) == peer_id for p in include_peers):
            return "already_set", None

        updated_include = [*include_peers, peer]
        updated_filter = raw.types.DialogFilter(
            id=target_filter.id,
            title=target_filter.title,
            pinned_peers=list(getattr(target_filter, "pinned_peers", [])),
            include_peers=updated_include,
            exclude_peers=list(getattr(target_filter, "exclude_peers", [])),
            contacts=getattr(target_filter, "contacts", None),
            non_contacts=getattr(target_filter, "non_contacts", None),
            groups=getattr(target_filter, "groups", None),
            broadcasts=getattr(target_filter, "broadcasts", None),
            bots=getattr(target_filter, "bots", None),
            exclude_muted=getattr(target_filter, "exclude_muted", None),
            exclude_read=getattr(target_filter, "exclude_read", None),
            exclude_archived=getattr(target_filter, "exclude_archived", None),
            title_noanimate=getattr(target_filter, "title_noanimate", None),
            emoticon=getattr(target_filter, "emoticon", None),
            color=getattr(target_filter, "color", None),
        )
        app.invoke(
            raw.functions.messages.UpdateDialogFilter(
                id=target_filter.id, filter=updated_filter
            )
        )
        record = {
            "action": "folder",
            "chat_id": chat_id,
            "folder_id": target_filter.id,
            "was_new_folder": False,
        }
        return "applied", record

    # Создание новой папки
    if len(filters) >= folder_limit:
        return "error", None

    used_ids = {getattr(f, "id", None) for f in filters}
    new_id = MIN_FOLDER_ID
    while new_id in used_ids:
        new_id += 1

    new_filter = raw.types.DialogFilter(
        id=new_id,
        title=raw.types.TextWithEntities(text=folder_title, entities=[]),
        pinned_peers=[],
        include_peers=[peer],
        exclude_peers=[],
    )
    app.invoke(raw.functions.messages.UpdateDialogFilter(id=new_id, filter=new_filter))
    record = {
        "action": "folder",
        "chat_id": chat_id,
        "folder_id": new_id,
        "was_new_folder": True,
    }
    return "applied", record


def apply_action_plan(
    app: Any,
    plan: ActionPlan,
    *,
    session_name: str = "session",
    journal_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[..., float] = random.uniform,
    leave_fn: Callable[..., bool] = leave_chat,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Применение плана действий с ведением журнала отката (Section 3)."""
    reports: list[dict[str, Any]] = []
    journal_records: list[dict[str, Any]] = []

    for chat_id, act in plan.actions.items():
        title = act.title

        if act.leave:
            ok = leave_fn(app, chat_id, sleep=sleep, rng=rng, log=log)
            status = "applied" if ok else "error"
            reports.append({
                "chat_id": chat_id,
                "title": title,
                "action": "leave",
                "status": status,
                "detail": "отписка выполнена" if ok else "ошибка отписки",
            })
            continue

        if act.mute:
            try:
                status, rec = _apply_mute(app, chat_id)
                if rec:
                    journal_records.append(rec)
                reports.append({
                    "chat_id": chat_id,
                    "title": title,
                    "action": "mute",
                    "status": status,
                    "detail": "уведомления отключены" if status == "applied" else "уже без звука",
                })
            except Exception as e:
                reports.append({
                    "chat_id": chat_id, "title": title, "action": "mute",
                    "status": "error", "detail": f"ошибка mute: {e}",
                })

        if act.archive:
            try:
                status, rec = _apply_archive(app, chat_id)
                if rec:
                    journal_records.append(rec)
                reports.append({
                    "chat_id": chat_id,
                    "title": title,
                    "action": "archive",
                    "status": status,
                    "detail": "канал архивирован" if status == "applied" else "уже в архиве",
                })
            except Exception as e:
                reports.append({
                    "chat_id": chat_id, "title": title, "action": "archive",
                    "status": "error", "detail": f"ошибка архивации: {e}",
                })

        if act.folder_id is not None and act.folder_title:
            try:
                status, rec = _apply_folder(app, chat_id, act.folder_id, act.folder_title)
                if rec:
                    journal_records.append(rec)
                detail_text = (
                    f"добавлен в папку «{act.folder_title}»"
                    if status == "applied"
                    else "уже в папке"
                )
                reports.append({
                    "chat_id": chat_id,
                    "title": title,
                    "action": "folder",
                    "status": status,
                    "detail": detail_text,
                })
            except Exception as e:
                reports.append({
                    "chat_id": chat_id, "title": title, "action": "folder",
                    "status": "error", "detail": f"ошибка папки: {e}",
                })

    if journal_records:
        save_rollback_journal(session_name, journal_records, root_dir=journal_dir)
        log(f"Журнал отката сохранён: {len(journal_records)} записей.")

    return reports


def rollback_session(
    app: Any,
    session_name: str,
    *,
    journal_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Безопасный откат применённых изменений (Section 3)."""
    # ponytail: восстанавливаем только то, что всё ещё соответствует применённому нами
    from pyrogram import raw  # noqa: PLC0415

    records = load_rollback_journal(session_name, root_dir=journal_dir)
    if not records:
        log(f"Для сессии {session_name} нет записей в журнале отката.")
        return []

    results: list[dict[str, Any]] = []

    for rec in reversed(records):
        action = rec.get("action")
        chat_id = rec.get("chat_id")
        if not chat_id:
            continue

        if action == "mute":
            try:
                peer = app.resolve_peer(chat_id)
                cur_settings = app.invoke(
                    raw.functions.account.GetNotifySettings(
                        peer=raw.types.InputNotifyPeer(peer=peer)
                    )
                )
                cur_mute = getattr(cur_settings, "mute_until", 0) or 0
                applied_mute = rec.get("applied_state", {}).get("mute_until", FOREVER_MUTE)

                # Если пользователь вручную поменял настройку, не трогаем её
                if cur_mute != applied_mute:
                    results.append({
                        "chat_id": chat_id, "action": "mute", "status": "skipped",
                        "detail": "настройка была изменена пользователем вручную",
                    })
                    continue

                prev_mute = rec.get("prev_state", {}).get("mute_until", 0)
                app.invoke(
                    raw.functions.account.UpdateNotifySettings(
                        peer=raw.types.InputNotifyPeer(peer=peer),
                        settings=raw.types.InputPeerNotifySettings(mute_until=prev_mute),
                    )
                )
                results.append({
                    "chat_id": chat_id, "action": "mute", "status": "restored",
                    "detail": "прежнее состояние уведомлений восстановлено",
                })
            except Exception as e:
                results.append({
                    "chat_id": chat_id, "action": "mute", "status": "error", "detail": str(e),
                })

        elif action == "archive":
            try:
                app.unarchive_chats([chat_id])
                results.append({
                    "chat_id": chat_id, "action": "archive", "status": "restored",
                    "detail": "канал возвращён из архива",
                })
            except Exception as e:
                results.append({
                    "chat_id": chat_id, "action": "archive", "status": "error", "detail": str(e),
                })

        elif action == "folder":
            try:
                target_folder_id = rec.get("folder_id")
                filters = existing_filters(app) or []
                t_filter = next(
                    (f for f in filters if getattr(f, "id", None) == target_folder_id),
                    None,
                )
                if t_filter is None:
                    results.append({
                        "chat_id": chat_id, "action": "folder", "status": "skipped",
                        "detail": "папка уже не существует",
                    })
                    continue

                peer = app.resolve_peer(chat_id)
                peer_id = _get_peer_id(peer)
                include_peers = [
                    p for p in getattr(t_filter, "include_peers", [])
                    if _get_peer_id(p) != peer_id
                ]

                updated_filter = raw.types.DialogFilter(
                    id=t_filter.id,
                    title=t_filter.title,
                    pinned_peers=list(getattr(t_filter, "pinned_peers", [])),
                    include_peers=include_peers,
                    exclude_peers=list(getattr(t_filter, "exclude_peers", [])),
                    contacts=getattr(t_filter, "contacts", None),
                    non_contacts=getattr(t_filter, "non_contacts", None),
                    groups=getattr(t_filter, "groups", None),
                    broadcasts=getattr(t_filter, "broadcasts", None),
                    bots=getattr(t_filter, "bots", None),
                    exclude_muted=getattr(t_filter, "exclude_muted", None),
                    exclude_read=getattr(t_filter, "exclude_read", None),
                    exclude_archived=getattr(t_filter, "exclude_archived", None),
                    title_noanimate=getattr(t_filter, "title_noanimate", None),
                    emoticon=getattr(t_filter, "emoticon", None),
                    color=getattr(t_filter, "color", None),
                )
                app.invoke(
                    raw.functions.messages.UpdateDialogFilter(
                        id=t_filter.id, filter=updated_filter
                    )
                )
                results.append({
                    "chat_id": chat_id, "action": "folder", "status": "restored",
                    "detail": "канал удалён из папки чтения",
                })
            except Exception as e:
                results.append({
                    "chat_id": chat_id, "action": "folder", "status": "error", "detail": str(e),
                })

    return results
