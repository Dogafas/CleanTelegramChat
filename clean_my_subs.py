"""Clean my subscriptions: меню оценки, режимов чтения и отписки."""

import logging
from collections.abc import Callable
from typing import Any

from pyrogram.types import Chat

import ui
from catalog import chat_label
from journal import rollback_session
from overlap import check_duplicate_group_unsubscribes, find_channel_overlaps
from plan import ActionPlan, apply_action_plan
from review import Candidate, Review, category_counts, scan
from session import prompt_sessions, telegram_client
from ui import StyledTitle
from verdict import evaluate, verdict_label

_LABELS = {
    "red": ("fg:ansired", "[Отписаться]"),
    "yellow": ("fg:ansiyellow", "[Проверить]"),
    "gray": ("fg:ansibrightblack", "[Недостаточно данных]"),
}

def _action_note(plan: ActionPlan, chat_id: int) -> str | None:
    act = plan.actions.get(chat_id)
    if not act:
        return None
    if act.leave:
        return "👉 в плане: отписка"
    parts = []
    if act.mute:
        parts.append("без звука")
    if act.archive:
        parts.append("в архив")
    if act.folder_title:
        parts.append(f"папка '{act.folder_title}'")
    return f"👉 в плане: {', '.join(parts)}" if parts else None


def choice_row(
    candidate: Candidate,
    *,
    checked: bool | None = None,
    action_note: str | None = None,
) -> tuple[StyledTitle, Chat, bool]:
    style, label = _LABELS[candidate.category]
    parts = [chat_label(candidate.chat)]
    if candidate.voice:
        parts.append(verdict_label(candidate.voice))
    if candidate.reasons:
        parts.append(", ".join(candidate.reasons))
    if action_note:
        parts.append(action_note)
    title = StyledTitle([(style, label), ("", f" {' | '.join(parts)}")])
    is_checked = candidate.checked if checked is None else checked
    return (title, candidate.chat, is_checked)

def _is_candidate_checked(
    candidate: Candidate,
    act_type: str,
    plan: ActionPlan,
    leave_initialized: bool,
) -> bool:
    cid = candidate.chat.id
    act = plan.actions.get(cid)
    if act_type == "leave":
        return bool(act and act.leave) if leave_initialized else candidate.checked
    if not act:
        return False
    mapping = {
        "mute": act.mute,
        "archive": act.archive,
        "folder": bool(act.folder_title),
        "clear": bool(act.leave or act.mute or act.archive or act.folder_title),
    }
    return mapping.get(act_type, False)


def _apply_candidate_action(
    candidate: Candidate,
    act_type: str,
    is_selected: bool,
    plan: ActionPlan,
    folder_title: str | None,
) -> None:
    cid = candidate.chat.id
    title = getattr(candidate.chat, "title", str(cid))
    act = plan.actions.get(cid)
    if act_type == "leave":
        if is_selected:
            plan.set_leave(cid, title)
        elif act and act.leave:
            plan.remove(cid)
        return
    if not is_selected and act and act.leave:
        return  # неподтверждённый режим не отменяет запланированную отписку
    if act_type == "mute":
        plan.set_mute(cid, title, enable=is_selected)
    elif act_type == "archive":
        plan.set_archive(cid, title, enable=is_selected)
    elif act_type == "folder":
        plan.set_folder(cid, title, folder_title if is_selected and folder_title else None)


def _build_action_plan_desc(summary: dict[str, int]) -> str:
    parts = []
    if summary["leave"]:
        parts.append(f"отписка: {summary['leave']}")
    if summary["mute"]:
        parts.append(f"без звука: {summary['mute']}")
    if summary["archive"]:
        parts.append(f"в архив: {summary['archive']}")
    if summary["folder"]:
        parts.append(f"папка: {summary['folder']}")
    return f" [{', '.join(parts)}]" if parts else " [план пуст]"


def _apply_selected_action_group(
    act_type: str,
    selected_ids: set[int],
    candidates: list[Candidate],
    plan: ActionPlan,
    *,
    state: dict[str, Any],
    ui: Any,
    app: Any,
    log: Callable[[str], None],
) -> None:
    if act_type == "clear":
        for cid in selected_ids:
            plan.remove(cid)
        log(f"Сброшены действия для {len(selected_ids)} каналов.")
        return
    if act_type == "leave":
        state["leave_initialized"] = True
    folder_title = None
    if act_type == "folder" and selected_ids:
        folder_title = ui.ask_text(app, "Введите название папки чтения:") or "Чтение"
    for candidate in candidates:
        _apply_candidate_action(
            candidate,
            act_type,
            candidate.chat.id in selected_ids,
            plan,
            folder_title,
        )


def _manage_action_plan(
    candidates: list[Candidate],
    plan: ActionPlan,
    state: dict[str, Any] | None = None,
    *,
    ui: Any,
    app: Any,
    log: Callable[[str], None],
) -> None:
    if state is None:
        state = {}

    while True:
        plan_desc = _build_action_plan_desc(plan.summary())
        actions_menu = [
            ("❌ Отписаться от каналов", "leave"),
            ("🔕 Выключить звук (без уведомлений)", "mute"),
            ("📦 Убрать в архив", "archive"),
            ("📁 Сложить в папку для чтения", "folder"),
            ("🧹 Сбросить запланированные действия для каналов", "clear"),
            ("⬅️ Назад в главное меню", "back"),
        ]
        act_type = ui.ask_select(
            app, f"Разбор каналов{plan_desc} — выберите действие:", actions_menu
        )
        if not act_type or act_type == "back":
            break

        leave_init = bool(state.get("leave_initialized"))
        choices = [
            choice_row(
                c,
                checked=_is_candidate_checked(c, act_type, plan, leave_init),
                action_note=_action_note(plan, c.chat.id),
            )
            for c in candidates
        ]

        selected = ui.ask_chats(app, choices, message="Отметьте каналы для отписки")
        if selected is None:
            continue

        _apply_selected_action_group(
            act_type,
            {chat.id for chat in selected},
            candidates,
            plan,
            state=state,
            ui=ui,
            app=app,
            log=log,
        )
        log(f"План обновлён: {plan.summary()}")
def _inspect_overlaps_menu(
    overlaps: list[dict[str, Any]],
    ui: Any,
    app: Any,
    log: Callable[[str], None],
) -> None:
    if not overlaps:
        log("Пересечений материалов не обнаружено.")
        return

    compared = [item for item in overlaps if item.get("has_comparable_window")]
    if not compared:
        log("Пересечения обнаружены, но сопоставимые временные окна отсутствуют.")
        return

    choices: list[tuple[str, int]] = []
    for idx, item in enumerate(compared):
        title_a = getattr(item["chat_a"], "title", "канал A")
        title_b = getattr(item["chat_b"], "title", "канал B")
        rate_a = int(item["overlap_rate_a"] * 100)
        rate_b = int(item["overlap_rate_b"] * 100)
        label = f"«{title_a}» ({rate_a}% дублей) <-> «{title_b}» ({rate_b}% дублей)"
        choices.append((label, idx))
    choices.append(("⬅️ Назад", -1))

    choice_idx = ui.ask_select(app, "Выберите пару каналов для просмотра совпадений:", choices)
    if choice_idx is None or choice_idx == -1:
        return

    item = compared[choice_idx]
    title_a = getattr(item["chat_a"], "title", "канал A")
    title_b = getattr(item["chat_b"], "title", "канал B")
    log(f"=== Перекрытие: «{title_a}» и «{title_b}» ===")
    log(
        f"Период: {item['window_start'].strftime('%Y-%m-%d')} .. "
        f"{item['window_end'].strftime('%Y-%m-%d')}"
    )
    rate_a_pct = int(item["overlap_rate_a"] * 100)
    rate_b_pct = int(item["overlap_rate_b"] * 100)
    log(
        f"В канале «{title_a}»: {item['dup_count_a']} из {item['count_a_in_window']} "
        f"дублей ({rate_a_pct}%)"
    )
    log(
        f"В канале «{title_b}»: {item['dup_count_b']} из {item['count_b_in_window']} "
        f"дублей ({rate_b_pct}%)"
    )
    if item.get("text_matches"):
        log("Примеры текстовых дублей:")
        for match in item["text_matches"][:3]:
            log(f" - {match['ref_a']} <-> {match['ref_b']}: {match['preview']}")
    if item.get("pure_forwards"):
        log(f"Чистых пересылок одного поста: {len(item['pure_forwards'])}")
    if item.get("shared_origins"):
        log(f"Общий первоисточник (разные комментарии): {len(item['shared_origins'])}")
    if item.get("shared_urls"):
        log(f"Общие URL первоисточников: {len(item['shared_urls'])}")


def _drilldown_menu(
    app: Any,
    reviewed: Review,
    overlaps: list[dict[str, Any]],
    *,
    ui: Any,
    evaluate_fn: Callable[..., Any],
    log: Callable[[str], None],
) -> None:
    channel_choices = [(chat_label(chat), chat, False) for chat in reviewed.chats()]
    selected = ui.ask_chats(app, channel_choices) or []
    if not selected:
        log("Каналы для уточнения не выбраны.")
        return

    days_str = ui.ask_text(app, "Горизонт сбора в днях (по умолчанию 14):") or "14"
    limit_str = ui.ask_text(app, "Предел сообщений на канал (по умолчанию 200):") or "200"
    try:
        horizon_days = max(1, int(days_str))
        limit = max(10, int(limit_str))
    except ValueError:
        horizon_days = 14
        limit = 200

    reviewed.refresh(
        app,
        {chat.id for chat in selected},
        horizon_days=horizon_days,
        limit=limit,
        evaluate=evaluate_fn,
        log=log,
    )
    overlaps.clear()
    overlaps.extend(find_channel_overlaps(reviewed.for_overlap()))
    log("Межканальные пересечения пересчитаны.")


def _review_and_apply_menu(
    app: Any,
    plan: ActionPlan,
    overlaps: list[dict[str, Any]],
    *,
    ui: Any,
    session_name: str,
    log: Callable[[str], None],
) -> None:
    if plan.is_empty():
        log("План действий пуст. Выберите действия для каналов.")
        return

    summary = plan.summary()
    log(
        f"План действий: отписка: {summary['leave']}, mute: {summary['mute']}, "
        f"архив: {summary['archive']}, папка: {summary['folder']}."
    )
    leave_ids = {cid for cid, action in plan.actions.items() if action.leave}
    if leave_ids:
        for warning in check_duplicate_group_unsubscribes(leave_ids, overlaps):
            log(f"⚠️ {warning}")

    if not ui.ask_confirm(app, "Применить запланированные действия в Telegram?"):
        log("Применение плана отменено пользователем.")
        return

    reports = apply_action_plan(app, plan, session_name=session_name, log=log)
    applied_count = sum(1 for report in reports if report["status"] == "applied")
    log(f"Применение завершено: {applied_count} действий выполнено.")


def _rollback_menu(
    app: Any,
    session_name: str,
    *,
    ui: Any,
    log: Callable[[str], None],
) -> None:
    if not ui.ask_confirm(app, f"Откатить обратимые изменения сессии {session_name}?"):
        return
    results = rollback_session(app, session_name, log=log)
    restored_count = sum(1 for result in results if result["status"] == "restored")
    log(f"Откат завершён: {restored_count} параметров восстановлено.")


def interactive_session_loop(
    app: Any,
    reviewed: Review,
    session_name: str,
    *,
    evaluate_fn: Callable[..., Any],
    ui: Any,
    log: Callable[[str], None],
) -> None:
    plan = ActionPlan()
    overlaps = find_channel_overlaps(reviewed.for_overlap())
    session_state: dict[str, Any] = {}

    while True:
        candidates = reviewed.candidates()
        summary = plan.summary()
        counts = category_counts(candidates)
        stats_str = (
            f" (🔴: {counts['red']}, 🟡: {counts['yellow']}, ⚪: {counts['gray']})"
        )
        plan_str = f" [в плане: {summary['total_channels']}]" if summary["total_channels"] else ""
        menu = [
            (f"📋 Разбор каналов{stats_str}{plan_str}", "candidates"),
            (f"👯 Поиск каналов-дублей ({len(overlaps)} пар)", "overlaps"),
            ("🔍 Проверить каналы глубже (расширенная история)", "drilldown"),
            ("🚀 Проверить корзину и применить в Telegram", "apply"),
            ("↩️ Вернуть как было (отменить прошлые действия)", "rollback"),
            ("🚪 Завершить работу с сессией", "exit"),
        ]
        choice = ui.ask_select(app, f"Сессия {session_name} — выберите раздел:", menu)
        if not choice or choice == "exit":
            log("Завершение работы с сессией.")
            break
        if choice == "candidates":
            _manage_action_plan(candidates, plan, session_state, ui=ui, app=app, log=log)
        elif choice == "overlaps":
            _inspect_overlaps_menu(overlaps, ui, app, log)
        elif choice == "drilldown":
            _drilldown_menu(
                app,
                reviewed,
                overlaps,
                ui=ui,
                evaluate_fn=evaluate_fn,
                log=log,
            )
        elif choice == "apply":
            _review_and_apply_menu(app, plan, overlaps, ui=ui, session_name=session_name, log=log)
        elif choice == "rollback":
            _rollback_menu(app, session_name, ui=ui, log=log)


def clean_subscriptions(
    sessions: list[str],
    *,
    client: Callable[..., Any] = telegram_client,
    ui: Any = ui,
    evaluate_fn: Callable[..., Any] = evaluate,
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
                reviewed = scan(app, evaluate_fn, log)
                if not reviewed.chats():
                    continue
                interactive_session_loop(
                    app,
                    reviewed,
                    session_name,
                    evaluate_fn=evaluate_fn,
                    ui=ui,
                    log=log,
                )
        except Exception:
            logging.exception(f"Сессия {session_name} прервана")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("subs.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    clean_subscriptions(prompt_sessions())


if __name__ == "__main__":
    main()
