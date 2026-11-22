"""Reading modes, action plan, and safe rollback (Section 3).

Обратимые альтернативы отписке: без уведомлений (mute), в архив (archive),
читать отдельно (folder), и отписка (leave) с ведением журнала отката.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from chat_folder import add_peer, remove_peer
from purge import retry_on_flood
from subs import leave_chat

FOREVER_MUTE = 2147483647
ARCHIVE_FOLDER_ID = 1


@dataclass(frozen=True)
class Leave:
    """Отписка: необратима, несовместима с режимами чтения."""


@dataclass(frozen=True)
class Adjust:
    """Режимы чтения: совместимы друг с другом, отменяют отписку."""

    mute: bool = False
    archive: bool = False
    folder_title: str | None = None

    def is_empty(self) -> bool:
        return not (self.mute or self.archive or self.folder_title)


class ChannelAction:
    """Запланированное действие над каналом: либо отписка, либо набор режимов.

    Инвариант «отписка не сочетается с режимами» держит тип: состояние —
    Leave | Adjust, невалидные комбинации неотображаемы. Чтение (leave/mute/
    archive/folder_title) — свойства; мутации — только через методы ActionPlan.
    """

    def __init__(self, chat_id: int, title: str) -> None:
        self.chat_id = chat_id
        self.title = title
        self._mode: Leave | Adjust = Adjust()

    @property
    def mode(self) -> Leave | Adjust:
        return self._mode

    def set_mode(self, mode: Leave | Adjust) -> None:
        self._mode = mode

    @property
    def leave(self) -> bool:
        return isinstance(self._mode, Leave)

    @property
    def mute(self) -> bool:
        return isinstance(self._mode, Adjust) and self._mode.mute

    @property
    def archive(self) -> bool:
        return isinstance(self._mode, Adjust) and self._mode.archive

    @property
    def folder_title(self) -> str | None:
        return self._mode.folder_title if isinstance(self._mode, Adjust) else None


class ActionPlan:
    """План действий по каналам текущего аккаунта (Section 3)."""

    def __init__(self) -> None:
        self.actions: dict[int, ChannelAction] = {}

    def _get_or_create(self, chat_id: int, title: str) -> ChannelAction:
        if chat_id not in self.actions:
            self.actions[chat_id] = ChannelAction(chat_id=chat_id, title=title)
        return self.actions[chat_id]

    def _drop_if_empty(self, chat_id: int) -> None:
        act = self.actions.get(chat_id)
        if act is not None and isinstance(act.mode, Adjust) and act.mode.is_empty():
            del self.actions[chat_id]

    def set_leave(self, chat_id: int, title: str) -> None:
        """Отписка несовместима с другими режимами этого канала."""
        self._get_or_create(chat_id, title).set_mode(Leave())

    def set_mute(self, chat_id: int, title: str, enable: bool = True) -> None:
        """Режим 'без уведомлений'; любой режим отменяет отписку."""
        self._adjust(chat_id, title, mute=enable)

    def set_archive(self, chat_id: int, title: str, enable: bool = True) -> None:
        """Режим 'в архив' (mute выбирается отдельно)."""
        self._adjust(chat_id, title, archive=enable)

    def set_folder(self, chat_id: int, title: str, folder_title: str | None) -> None:
        """Режим 'читать отдельно'; folder_title=None снимает папку."""
        self._adjust(chat_id, title, folder_title=folder_title)

    def _adjust(self, chat_id: int, title: str, **changes: Any) -> None:
        """Единственный писатель режима: заменяет mode, пустой Adjust удаляется."""
        act = self._get_or_create(chat_id, title)
        base = act.mode if isinstance(act.mode, Adjust) else Adjust()
        act.set_mode(replace(base, **changes))
        self._drop_if_empty(chat_id)

    def remove(self, chat_id: int) -> None:
        _ = self.actions.pop(chat_id, None)

    def is_empty(self) -> bool:
        return not self.actions

    def summary(self) -> dict[str, int]:
        """Сводка запланированных изменений."""
        leave_count = sum(1 for a in self.actions.values() if a.leave)
        mute_count = sum(1 for a in self.actions.values() if a.mute)
        archive_count = sum(1 for a in self.actions.values() if a.archive)
        folder_count = sum(1 for a in self.actions.values() if a.folder_title)
        return {
            "leave": leave_count,
            "mute": mute_count,
            "archive": archive_count,
            "folder": folder_count,
            "total_channels": len(self.actions),
        }



def _journal_path(session_name: str, root_dir: Path | None = None) -> Path:
    base = root_dir or Path("sessions")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"rollback_{session_name}.json"


def _write_journal(path: Path, session_name: str, records: list[dict[str, Any]]) -> Path:
    """Атомарная замена файла журнала: tmp + replace, краш не оставляет битый json."""
    data = {
        "session_name": session_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    # ponytail: локальный json в sessions/, исключённый из git
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

        if act.folder_title:
            try:
                status, folder_id, was_new = add_peer(app, chat_id, act.folder_title)
                if status == "applied" and folder_id is not None:
                    journal_records.append({
                        "action": "folder",
                        "chat_id": chat_id,
                        "folder_id": folder_id,
                        "was_new_folder": was_new,
                    })
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


@dataclass(frozen=True)
class BotAction:
    """Запланированное действие над ботом (ADR 0011): стоп и/или удаление диалога."""

    chat_id: int
    title: str
    stop: bool = False
    delete: bool = False


def _bot_blocked(app: Any, chat_id: int) -> bool:
    """Текущее состояние блокировки бота (state-guard отката)."""
    from pyrogram import raw  # noqa: PLC0415

    peer = app.resolve_peer(chat_id)
    full = app.invoke(
        raw.functions.users.GetFullUser(
            id=raw.types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
        )
    )
    return bool(getattr(full.full_user, "blocked", False))


def apply_bot_actions(
    app: Any,
    bots: list[BotAction],
    *,
    session_name: str = "session",
    journal_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[..., float] = random.uniform,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Остановка (block_user) и удаление диалога (delete_chat_history) ботов (ADR 0011).

    Стоп precedes delete. В журнал попадает только реально применённый стоп
    (уже заблокированный бот — already_set, записи нет); удаление необратимо
    и фиксируется с restorable: False.
    """
    reports: list[dict[str, Any]] = []
    journal_records: list[dict[str, Any]] = []

    for bot in bots:
        if bot.stop:
            try:
                if _bot_blocked(app, bot.chat_id):
                    reports.append({
                        "chat_id": bot.chat_id, "title": bot.title,
                        "action": "stop", "status": "already_set",
                        "detail": "бот уже остановлен",
                    })
                else:
                    _, error = retry_on_flood(
                        lambda bot=bot: app.block_user(bot.chat_id),
                        sleep=sleep, rng=rng, log=log,
                        what=f", бот {bot.title}",
                    )
                    if error is not None:
                        raise error
                    journal_records.append({
                        "action": "stop",
                        "chat_id": bot.chat_id,
                        "prev_state": {"blocked": False},
                        "applied_state": {"blocked": True},
                    })
                    reports.append({
                        "chat_id": bot.chat_id, "title": bot.title,
                        "action": "stop", "status": "applied",
                        "detail": "бот остановлен",
                    })
            except Exception as e:
                reports.append({
                    "chat_id": bot.chat_id, "title": bot.title,
                    "action": "stop", "status": "error",
                    "detail": f"ошибка остановки: {e}",
                })
        if bot.delete:
            try:
                _, error = retry_on_flood(
                    lambda bot=bot: app.delete_chat_history(bot.chat_id),
                    sleep=sleep, rng=rng, log=log,
                    what=f", бот {bot.title}",
                )
                if error is not None:
                    raise error
                journal_records.append({
                    "action": "delete_chat",
                    "chat_id": bot.chat_id,
                    "restorable": False,
                })
                reports.append({
                    "chat_id": bot.chat_id, "title": bot.title,
                    "action": "delete_chat", "status": "applied",
                    "detail": "диалог удалён",
                })
            except Exception as e:
                reports.append({
                    "chat_id": bot.chat_id, "title": bot.title,
                    "action": "delete_chat", "status": "error",
                    "detail": f"ошибка удаления диалога: {e}",
                })

    if journal_records:
        save_rollback_journal(session_name, journal_records, root_dir=journal_dir)
        log(f"Журнал отката сохранён: {len(journal_records)} записей.")

    return reports


def _rollback_mute(app: Any, rec: dict[str, Any]) -> dict[str, Any]:
    from pyrogram import raw  # noqa: PLC0415

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
    try:
        target_folder_id = rec.get("folder_id")
        status = remove_peer(app, chat_id, int(target_folder_id))
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
    try:
        # State-guard: пользователь сам разблокировал бота — не блокируем повторно
        if not _bot_blocked(app, chat_id):
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
    handler = _ROLLBACK_HANDLERS.get(rec.get("action"))
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
