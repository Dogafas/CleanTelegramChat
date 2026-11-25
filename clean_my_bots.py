"""Clean my bots: остановка и удаление бот-диалогов (ADR 0011)."""

import logging
from collections.abc import Callable
from typing import Any

from pyrogram import Client

from bots import BotInfo, bot_label, classify, probe_bot, scan_bots
from plan import BotAction, apply_bot_actions, rollback_session
from review import Candidate
from session import prompt_sessions, restore_client_loop, telegram_client
from ui import StyledTitle, ask_chats, ask_confirm

_LABELS = {
    "red": ("fg:ansired", "[Остановить и удалить]"),
    "yellow": ("fg:ansiyellow", "[Проверить]"),
    "gray": ("fg:ansibrightblack", "[Недостаточно данных]"),
}
_PRIORITY = {"red": 0, "yellow": 1, "gray": 2}


def bot_row(info: BotInfo, candidate: Candidate | None) -> tuple[StyledTitle, int, bool]:
    """Строка чекбокса: (title, chat_id, checked)."""
    parts = [bot_label(info.dialog.chat)]
    if info.archived:
        parts.append("архив")
    checked = False
    tokens: list[tuple[str, str]] = []
    if candidate is not None:
        style, label = _LABELS[candidate.category]
        if candidate.reasons:
            parts.append(", ".join(candidate.reasons))
        checked = candidate.checked
        tokens.append((style, f"{label} "))
    tokens.append(("", " | ".join(parts)))
    return (StyledTitle(tokens), info.chat_id, checked)


def ordered(infos: list[BotInfo]) -> list[tuple[BotInfo, Candidate | None]]:
    """Кандидаты RED → YELLOW → GRAY, затем боты без признаков."""
    rows = [(info, classify(info)) for info in infos]
    rows.sort(key=lambda row: _PRIORITY.get(row[1].category, 3) if row[1] else 3)
    return rows


def _select_bot_ids(
    rows: list[tuple[BotInfo, Candidate | None]],
    message: str,
    ask: Callable[..., Any],
) -> set[int]:
    choices = [bot_row(info, candidate) for info, candidate in rows]
    return set(ask(choices, message=message) or [])


def _build_actions(
    infos: list[BotInfo],
    stop_ids: set[int],
    delete_ids: set[int],
) -> list[BotAction]:
    actions: list[BotAction] = []
    for info in infos:
        chat = info.dialog.chat
        stop = info.chat_id in stop_ids
        delete = info.chat_id in delete_ids
        if stop or delete:
            actions.append(
                BotAction(chat_id=info.chat_id, title=bot_label(chat), stop=stop, delete=delete)
            )
    return actions


def probe_menu(
    app: Client,
    infos: list[BotInfo],
    *,
    restore: Callable[[Client], None],
    ask: Callable[..., Any],
    confirm: Callable[[str], Any],
    probe_fn: Callable[..., str],
    log: Callable[[str], None],
) -> None:
    """Opt-in глубокая проверка: /start только тем ботам, кого отметили."""
    if not confirm(
        "Глубокая проверка: отправить /start выбранным ботам? Живые боты получат сообщение."
    ):
        return
    rows = [(info, classify(info)) for info in infos]
    choices = [
        (bot_row(info, candidate)[0], info.chat_id, False)
        for info, candidate in rows
    ]
    selected = set(ask(choices, message="Отметьте ботов для пробы /start") or [])
    if not selected:
        return
    restore(app)
    for info in infos:
        chat = info.dialog.chat
        if info.chat_id not in selected:
            continue
        log(f"Проба /start: {bot_label(chat)}…")
        info.probe = probe_fn(app, info.chat_id, log=log)
        if info.probe == "silent":
            log(f"{bot_label(chat)}: не ответил на /start → RED")
        elif info.probe == "replied":
            log(f"{bot_label(chat)}: ответил, бот живой")
        else:
            log(f"{bot_label(chat)}: проба не удалась")


def _bot_session(
    app: Client,
    infos: list[BotInfo],
    session_name: str,
    *,
    restore: Callable[[Client], None],
    ask: Callable[..., Any],
    confirm: Callable[[str], Any],
    probe_fn: Callable[..., str] = probe_bot,
    log: Callable[[str], None],
) -> None:
    probe_menu(app, infos, restore=restore, ask=ask, confirm=confirm, probe_fn=probe_fn, log=log)

    rows = ordered(infos)
    reds = sum(1 for _, c in rows if c is not None and c.category == "red")
    yellows = sum(1 for _, c in rows if c is not None and c.category == "yellow")
    grays = sum(1 for _, c in rows if c is not None and c.category == "gray")
    log(
        f"Кандидаты: 🔴 {reds}, 🟡 {yellows}, ⚪ {grays}"
        f" (без признаков: {len(rows) - reds - yellows - grays})."
    )

    stop_ids = _select_bot_ids(rows, "Отметьте ботов для остановки (обратимо)", ask)
    delete_ids = _select_bot_ids(rows, "Отметьте ботов для удаления диалога (НЕобратимо)", ask)
    if not stop_ids and not delete_ids:
        log("Боты не выбраны.")
        return
    actions = _build_actions(infos, stop_ids, delete_ids)
    stops = sum(1 for a in actions if a.stop)
    deletes = sum(1 for a in actions if a.delete)
    log(f"План: остановить ботов: {stops}, удалить диалогов: {deletes}.")
    if not confirm(f"Выполнить в Telegram? Удаление {deletes} диалогов необратимо."):
        log("Применение отменено пользователем.")
        return
    restore(app)
    reports = apply_bot_actions(app, actions, session_name=session_name, log=log)
    applied = sum(1 for r in reports if r["status"] == "applied")
    log(f"Готово: {applied} действий выполнено.")
    stopped = any(r["action"] == "stop" and r["status"] == "applied" for r in reports)
    if stopped and confirm("Откатить остановку (разблокировать остановленных ботов)?"):
        restore(app)
        # Только остановки: mute/archive/folder прошлых запусков остаются в журнале
        rollback_session(app, session_name, actions={"stop"}, log=log)


def clean_bots(
    sessions: list[str],
    *,
    client: Callable[..., Any] = telegram_client,
    restore: Callable[[Client], None] = restore_client_loop,
    ask: Callable[..., Any] = ask_chats,
    confirm: Callable[[str], Any] = ask_confirm,
    probe_fn: Callable[..., str] = probe_bot,
    log: Callable[[str], None] = logging.info,
) -> None:
    if not sessions:
        log("Сессии не выбраны. Завершение работы.")
        return
    multi = len(sessions) > 1
    for session_name in sessions:
        if multi:
            log(f"=== Сессия: {session_name} ===")
        try:
            with client(session_name=session_name) as app:
                restore(app)
                infos = scan_bots(app, log)
                if not infos:
                    log("Бот-диалогов не найдено.")
                    continue
                _bot_session(
                    app,
                    infos,
                    session_name,
                    restore=restore,
                    ask=ask,
                    confirm=confirm,
                    probe_fn=probe_fn,
                    log=log,
                )
        except Exception:
            logging.exception(f"Сессия {session_name} прервана")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    clean_bots(prompt_sessions())


if __name__ == "__main__":
    main()
