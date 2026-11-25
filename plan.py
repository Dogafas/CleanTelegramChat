"""Чтение-режимы и план действий (Section 3): применение под журнал отката.

Обратимые альтернативы отписке: без уведомлений (mute), в архив (archive),
читать отдельно (folder), и отписка (leave). Хранение записей и undo —
в journal.py (GLOSSARY: «Журнал отката»).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pyrogram import raw

from chat_folder import add_peer
from journal import ARCHIVE_FOLDER_ID, FOREVER_MUTE, bot_blocked, save_rollback_journal
from purge import retry_on_flood
from subs import leave_chat


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


def _apply_mute(
    app: Any,
    chat_id: int,
) -> tuple[str, dict[str, Any] | None]:
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
                if bot_blocked(app, bot.chat_id):
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
