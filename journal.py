"""Журнал отката: хранение записей и undo применённых изменений (Section 3, ADR 0010).

Файл sessions/rollback_<сессия>.json — без секретов, токенов и текстов.
Применение (plan) дописывает записи; откат потребляет журнал: restored/skipped
уходят из файла, error остаются для повторного запуска.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyrogram import raw

from chat_folder import remove_peer

FOREVER_MUTE = 2147483647
ARCHIVE_FOLDER_ID = 1


def bot_blocked(app: Any, chat_id: int) -> bool:
    """Текущее состояние блокировки бота (state-guard применения и отката)."""
    peer = app.resolve_peer(chat_id)
    full = app.invoke(
        raw.functions.users.GetFullUser(
            id=raw.types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
        )
    )
    return bool(getattr(full.full_user, "blocked", False))


def _journal_path(session_name: str, root_dir: Path | None = None) -> Path:
    base = root_dir or Path("sessions")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"rollback_{session_name}.json"


def _write_journal(path: Path, session_name: str, records: list[dict[str, Any]]) -> Path:
    """Атомарная замена файла журнала: tmp + replace, краш не оставляет битый json."""
    data = {
        "session_name": session_name,
        "timestamp": datetime.now(UTC).isoformat(),
        "records": records,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def save_rollback_journal(
    session_name: str,
    records: list[dict[str, Any]],
    root_dir: Path | None = None,
) -> Path:
    """Дописывает записи к существующему журналу (Section 3).

    Повторный apply без отката не должен лишать отката прежние изменения.
    Журнал без секретов, токенов и текстов.
    """
    path = _journal_path(session_name, root_dir)
    merged = load_rollback_journal(session_name, root_dir=root_dir) + list(records)
    return _write_journal(path, session_name, merged)


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


def _rollback_mute(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    chat_id = rec.get("chat_id")
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
            return {
                "chat_id": chat_id, "action": "mute", "status": "skipped",
                "detail": "настройка была изменена пользователем вручную",
            }

        prev_mute = rec.get("prev_state", {}).get("mute_until", 0)
        app.invoke(
            raw.functions.account.UpdateNotifySettings(
                peer=raw.types.InputNotifyPeer(peer=peer),
                settings=raw.types.InputPeerNotifySettings(mute_until=prev_mute),
            )
        )
        return {
            "chat_id": chat_id, "action": "mute", "status": "restored",
            "detail": "прежнее состояние уведомлений восстановлено",
        }
    except Exception as e:
        return {"chat_id": chat_id, "action": "mute", "status": "error", "detail": str(e)}


def _rollback_archive(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    chat_id = rec.get("chat_id")
    try:
        cur_folder = getattr(app.get_chat(chat_id), "folder_id", None)
        # Ручная архивация/разархивация после apply — не затираем, как у mute
        if cur_folder != ARCHIVE_FOLDER_ID:
            return {
                "chat_id": chat_id, "action": "archive", "status": "skipped",
                "detail": "архив изменён после применения (вручную?) — не трогаем",
            }
        app.unarchive_chats([chat_id])
        return {
            "chat_id": chat_id, "action": "archive", "status": "restored",
            "detail": "канал возвращён из архива",
        }
    except Exception as e:
        return {"chat_id": chat_id, "action": "archive", "status": "error", "detail": str(e)}


def _rollback_folder(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    chat_id = rec.get("chat_id")
    target_folder_id = rec.get("folder_id")
    if not isinstance(chat_id, int) or not isinstance(target_folder_id, int):
        return {
            "chat_id": chat_id, "action": "folder", "status": "error",
            "detail": "битая запись журнала: нет chat_id или folder_id",
        }
    try:
        status = remove_peer(app, chat_id, target_folder_id)
        if status == "skipped":
            return {
                "chat_id": chat_id, "action": "folder", "status": "skipped",
                "detail": "папка уже не существует",
            }
        return {
            "chat_id": chat_id, "action": "folder", "status": "restored",
            "detail": "канал удалён из папки чтения",
        }
    except Exception as e:
        return {"chat_id": chat_id, "action": "folder", "status": "error", "detail": str(e)}


def _rollback_stop(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    chat_id = rec.get("chat_id")
    if not isinstance(chat_id, int):
        return {
            "chat_id": chat_id, "action": "stop", "status": "error",
            "detail": "битая запись журнала: нет chat_id",
        }
    try:
        # State-guard: пользователь сам разблокировал бота — не блокируем повторно
        if not bot_blocked(app, chat_id):
            return {
                "chat_id": chat_id, "action": "stop", "status": "skipped",
                "detail": "бот был разблокирован вручную — не трогаем",
            }
        app.unblock_user(chat_id)
        return {
            "chat_id": chat_id, "action": "stop", "status": "restored",
            "detail": "бот снова работает (блокировка снята)",
        }
    except Exception as e:
        return {"chat_id": chat_id, "action": "stop", "status": "error", "detail": str(e)}


def _rollback_delete_chat(_app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_id": rec.get("chat_id"), "action": "delete_chat", "status": "skipped",
        "detail": "удаление диалога необратимо",
    }


_ROLLBACK_HANDLERS = {
    "mute": _rollback_mute,
    "archive": _rollback_archive,
    "folder": _rollback_folder,
    "stop": _rollback_stop,
    "delete_chat": _rollback_delete_chat,
}


def _rollback_record(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """Откат одной записи журнала: restored | skipped | error."""
    handler = _ROLLBACK_HANDLERS.get(str(rec.get("action")))
    if handler is None:
        return {
            "chat_id": rec.get("chat_id"), "action": str(rec.get("action")),
            "status": "skipped", "detail": "неизвестное действие в записи журнала",
        }
    return handler(app, rec)


def rollback_session(
    app: Any,
    session_name: str,
    *,
    journal_dir: Path | None = None,
    actions: set[str] | None = None,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Безопасный откат применённых изменений (Section 3).

    Откат потребляет журнал: restored/skipped записи уходят из файла,
    error остаются — их можно откатить повторным запуском.
    actions — откатить только записи перечисленных действий; прочие
    остаются в журнале нетронутыми (None — весь журнал).
    """
    # ponytail: восстанавливаем только то, что всё ещё соответствует применённому нами
    records = load_rollback_journal(session_name, root_dir=journal_dir)
    if not records:
        log(f"Для сессии {session_name} нет записей в журнале отката.")
        return []
    selected = [r for r in records if actions is None or r.get("action") in actions]
    kept = [r for r in records if not (actions is None or r.get("action") in actions)]
    if not selected:
        log(f"Для сессии {session_name} нет записей выбранных действий: {sorted(actions or [])}.")
        return []

    results: list[dict[str, Any]] = []
    failed_records: list[dict[str, Any]] = []

    for rec in reversed(selected):
        if not rec.get("chat_id"):
            continue
        result = _rollback_record(app, rec)
        results.append(result)
        if result["status"] == "error":
            failed_records.append(rec)

    final_journal = kept + list(reversed(failed_records))  # прежний порядок журнала
    if final_journal:
        _write_journal(_journal_path(session_name, journal_dir), session_name, final_journal)
        log(f"В журнале осталось {len(final_journal)} записей (ошибки отката или вне области).")
    else:
        _journal_path(session_name, journal_dir).unlink(missing_ok=True)
        log("Журнал отката очищен.")

    return results
