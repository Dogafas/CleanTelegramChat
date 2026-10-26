"""Clean my subscriptions: оценка каналов, список кандидатов, массовая отписка."""

import logging
import random
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from time import sleep
from typing import Any

from pyrogram.errors import FloodWait

from catalog import chat_label
from overlap import (
    check_duplicate_group_unsubscribes,
    find_channel_overlaps,
)
from plan import (
    ActionPlan,
    apply_action_plan,
    rollback_session,
)
from purge import flood_delay
from session import prompt_sessions, restore_client_loop, telegram_client
from sort_chats import _filter_title, existing_filters
from subs import (
    ANALYSIS_PAUSE,
    POSTS_WINDOW,
    channel_subscriptions,
    compute_coverage,
    compute_metrics,
    fetch_creation_date,
    fetch_posts,
    format_coverage_short,
    group_media_publications,
    leave_chats,
    metric_signals,
    refine_channel_history,
)
from ui import StyledTitle, ask_chats, ask_confirm, ask_select, ask_text
from verdict import (
    REASON_RU,
    evaluate,
    has_text,
    is_valid_verdict,
    text_reasons,
    verdict_label,
)

_ask_unsubscribe = partial(ask_chats, message="Отметьте каналы для отписки")


def scan_channels(
    app: Any, log: Callable[[str], None], rng=random.uniform
) -> list[dict[str, Any]]:
    """Метрики и сведения о покрытии по каждой подписке (Section 1)."""
    channels = channel_subscriptions(app)
    if not channels:
        log("Каналов в подписках не найдено.")
        return []
    log(f"Найдено каналов: {len(channels)}. Сбор метрик по последним постам…")
    results: list[dict[str, Any]] = []
    scan_start = datetime.now(timezone.utc)

    for i, chat in enumerate(channels):
        if i:
            sleep(rng(*ANALYSIS_PAUSE))
        full_chat = None
        posts: list[Any] = []
        created_at = None
        members = 0
        fetch_err: str | None = None

        while True:
            try:
                full_chat = app.get_chat(chat.id)
                members = getattr(full_chat, "members_count", 0) or 0
                posts = fetch_posts(app, chat.id)
                created_at = fetch_creation_date(app, chat.id)
                break
            except FloodWait as e:
                delay = flood_delay(e, rng)
                log(f"FloodWait {delay:.0f} с, канал {chat_label(chat)}")
                sleep(delay)
            except Exception as e:
                fetch_err = f"{type(e).__name__}: {e}"
                log(f"Ошибка при получении {chat_label(chat)}: {e}")
                break

        pubs = group_media_publications(posts)
        coverage = compute_coverage(
            posts,
            pubs,
            scan_started_at=scan_start,
            limit=POSTS_WINDOW,
            error=fetch_err,
        )
        metrics = compute_metrics(chat, pubs, members, created_at=created_at)
        signals = metric_signals(metrics)

        growth_str = ""
        if metrics.get("growth_per_month") is not None:
            growth_str = (
                f" | возраст: {metrics['age_days']:.0f}д"
                f" (~{metrics['growth_per_month']:.0f} суб/мес)"
            )
        signals_str = f" | сигналы: {'; '.join(signals)}" if signals else " | сигналы: в норме"
        cov_summary = format_coverage_short(coverage)
        log(f"{chat_label(chat)}{growth_str}{signals_str} [{cov_summary}]")

        results.append({
            "chat": chat,
            "description": getattr(full_chat, "description", None) or "",
            "metrics": metrics,
            "signals": signals,
            "posts": posts,
            "publications": pubs,
            "coverage": coverage,
        })
    return results


def classify_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    """Маршрутизация подписки: RED [Отписаться], YELLOW [Проверить], GRAY [Недостаточно данных]."""
    verdict = row.get("verdict")
    signals = row.get("signals") or []
    posts = row.get("publications") or row.get("posts") or []

    # Текстовая оценка доступна и корректна
    if has_text(posts) and is_valid_verdict(verdict) and isinstance(verdict, dict):
        reasons = text_reasons(verdict)
        if reasons:
            text_labels = [REASON_RU.get(r, r) for r in reasons]
            # ponytail: все сработавшие причины (текст + метрические предупреждения) в одной строке
            return {
                "category": "red",
                "label": "[Отписаться]",
                "style": "fg:ansired",
                "checked": True,
                "voice": verdict.get("voice"),
                "reasons": [*text_labels, *signals],
                "row": row,
            }
        if signals:
            return {
                "category": "yellow",
                "label": "[Проверить]",
                "style": "fg:ansiyellow",
                "checked": False,
                "voice": verdict.get("voice"),
                "reasons": signals,
                "row": row,
            }
        return None

    # Вердикт недоступен (нет текста, сбой API или некорректная форма ответа)
    status_reason = "нет текста" if not has_text(posts) else "ошибка вердикта"
    return {
        "category": "gray",
        "label": "[Недостаточно данных]",
        "style": "fg:ansibrightblack",
        "checked": False,
        "voice": None,
        "reasons": [status_reason, *signals],
        "row": row,
    }


def _build_candidate_row(c: dict[str, Any]) -> tuple[StyledTitle, Any, bool]:
    chat = c["row"]["chat"]
    parts = [chat_label(chat)]
    if c.get("voice"):
        parts.append(verdict_label(c["voice"]))
    if c.get("reasons"):
        parts.append(", ".join(c["reasons"]))
    tail = f" {' | '.join(parts)}"
    styled_title = StyledTitle([(c["style"], c["label"]), ("", tail)])
    return (styled_title, chat, c["checked"])


def _choices(candidates: list[dict[str, Any]]) -> list[tuple[StyledTitle, Any, bool]]:
    """Построение списка вариантов чекбокса для кандидатов на отписку."""
    classified = [
        c if ("category" in c and "row" in c) else classify_candidate(c) for c in candidates
    ]
    priority = {"red": 0, "yellow": 1, "gray": 2}
    items = [c for c in classified if c is not None]
    items.sort(key=lambda x: priority[x["category"]])
    return [_build_candidate_row(c) for c in items]


def _channel_description(row: dict[str, Any]) -> str:
    chat = row["chat"]
    return row.get("description") or getattr(chat, "description", None) or ""


def _ask_verdict(evaluate_fn: Callable[..., Any], row: dict[str, Any]) -> Any:
    title = getattr(row["chat"], "title", None) or "без названия"
    posts = row.get("publications") or row.get("posts") or []
    return evaluate_fn(
        title,
        posts,
        description=_channel_description(row),
    )


def _attach_verdicts(
    scanned: list[dict[str, Any]],
    evaluate_fn: Callable[..., Any],
    log: Callable[[str], None],
) -> None:
    for row in scanned:
        posts = row.get("publications") or row.get("posts") or []
        if not has_text(posts):
            row["verdict"] = None
            if "coverage" in row:
                row["coverage"]["model_texts_count"] = 0
                row["coverage"]["truncated_texts_count"] = 0
            continue
        log(f"Запрос вердикта LLM: {getattr(row['chat'], 'title', 'канал')}")
        try:
            verdict = _ask_verdict(evaluate_fn, row)
            if not is_valid_verdict(verdict):
                row["verdict"] = {"error": "некорректный ответ модели"}
            else:
                row["verdict"] = verdict
                if "coverage" in row and isinstance(verdict, dict):
                    row["coverage"]["model_texts_count"] = verdict.get("model_texts_count", 0)
                    row["coverage"]["truncated_texts_count"] = verdict.get(
                        "truncated_texts_count", 0
                    )
        except Exception:
            logging.exception("Вердикт не удался, канал без вердикта")
            row["verdict"] = {"error": "см. traceback в логе"}


def _manage_action_plan(
    candidates: list[dict[str, Any]],
    plan: ActionPlan,
    *,
    ask_chats_fn: Callable[..., Any],
    select_fn: Callable[..., Any],
    text_fn: Callable[..., Any],
    app: Any,
    restore_fn: Callable[[Any], None],
    log: Callable[[str], None],
) -> None:
    """Интерфейс настройки режимов чтения и отписки (Section 3)."""
    actions_menu = [
        ("❌ Запланировать отписку (leave)", "leave"),
        ("🔕 Запланировать 'без звука' (mute)", "mute"),
        ("📦 Запланировать 'в архив' (archive)", "archive"),
        ("📁 Запланировать 'в папку чтения' (folder)", "folder"),
        ("🧹 Очистить запланированное действие для каналов", "clear"),
        ("⬅️ Назад в главное меню", "back"),
    ]
    act_type = select_fn("Выберите действие для применения к каналам:", actions_menu)
    restore_fn(app)
    if not act_type or act_type == "back":
        return

    choices = _choices(candidates)
    # Сбрасываем checked-по умолчанию для режима настроек кроме отписки
    if act_type != "leave":
        choices = [(c[0], c[1], False) for c in choices]

    selected = ask_chats_fn(choices) or []
    restore_fn(app)
    if not selected:
        log("Каналы не выбраны.")
        return

    folder_id: int | None = None
    folder_title: str | None = None
    if act_type == "folder":
        folder_title = text_fn("Введите название папки чтения:") or "Чтение"
        restore_fn(app)
        filters = existing_filters(app) or []
        target = next((f for f in filters if _filter_title(f) == folder_title), None)
        folder_id = target.id if target else -1

    for chat in selected:
        cid = chat.id
        title = getattr(chat, "title", str(cid))
        if act_type == "leave":
            plan.set_leave(cid, title)
        elif act_type == "mute":
            plan.set_mute(cid, title)
        elif act_type == "archive":
            plan.set_archive(cid, title)
        elif act_type == "folder" and folder_id is not None and folder_title:
            plan.set_folder(cid, title, folder_id, folder_title)
        elif act_type == "clear":
            plan.remove(cid)

    log(f"План обновлён: {plan.summary()}")


def _inspect_overlaps_menu(
    overlaps: list[dict[str, Any]],
    select_fn: Callable[..., Any],
    log: Callable[[str], None],
) -> None:
    """Обзор обнаруженных межканальных пересечений (Section 2)."""
    if not overlaps:
        log("Пересечений материалов не обнаружено.")
        return

    compared = [o for o in overlaps if o.get("has_comparable_window")]
    if not compared:
        log("Пересечения обнаружены, но сопоставимые временные окна отсутствуют.")
        return

    choices: list[tuple[str, Any]] = []
    for idx, o in enumerate(compared):
        t_a = getattr(o["chat_a"], "title", "канал A")
        t_b = getattr(o["chat_b"], "title", "канал B")
        r_a = int(o["overlap_rate_a"] * 100)
        r_b = int(o["overlap_rate_b"] * 100)
        label = f"«{t_a}» ({r_a}% дублей) <-> «{t_b}» ({r_b}% дублей)"
        choices.append((label, idx))
    choices.append(("⬅️ Назад", -1))

    choice_idx = select_fn("Выберите пару каналов для просмотра совпадений:", choices)
    if choice_idx is None or choice_idx == -1:
        return

    item = compared[choice_idx]
    t_a = getattr(item["chat_a"], "title", "канал A")
    t_b = getattr(item["chat_b"], "title", "канал B")
    log(f"=== Перекрытие: «{t_a}» и «{t_b}» ===")
    log(
        f"Период: {item['window_start'].strftime('%Y-%m-%d')} .. "
        f"{item['window_end'].strftime('%Y-%m-%d')}"
    )
    rate_a_pct = int(item["overlap_rate_a"] * 100)
    rate_b_pct = int(item["overlap_rate_b"] * 100)
    log(
        f"В канале «{t_a}»: {item['dup_count_a']} из {item['count_a_in_window']} "
        f"дублей ({rate_a_pct}%)"
    )
    log(
        f"В канале «{t_b}»: {item['dup_count_b']} из {item['count_b_in_window']} "
        f"дублей ({rate_b_pct}%)"
    )

    if item.get("text_matches"):
        log("Примеры текстовых дублей:")
        for tm in item["text_matches"][:3]:
            log(f" - {tm['ref_a']} <-> {tm['ref_b']}: {tm['preview']}")
    if item.get("pure_forwards"):
        log(f"Чистых пересылок одного поста: {len(item['pure_forwards'])}")
    if item.get("shared_origins"):
        log(f"Общий первоисточник (разные комментарии): {len(item['shared_origins'])}")
    if item.get("shared_urls"):
        log(f"Общие URL первоисточников: {len(item['shared_urls'])}")


def _drilldown_menu(
    app: Any,
    scanned: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    *,
    ask_chats_fn: Callable[..., Any],
    text_fn: Callable[..., Any],
    evaluate_fn: Callable[..., Any],
    restore_fn: Callable[[Any], None],
    log: Callable[[str], None],
) -> None:
    """Уточняющий сбор по выбранным каналам (Section 4)."""
    channel_choices = [
        (chat_label(r["chat"]), r["chat"], False)
        for r in scanned
    ]
    selected = ask_chats_fn(channel_choices) or []
    restore_fn(app)
    if not selected:
        log("Каналы для уточнения не выбраны.")
        return

    days_str = text_fn("Горизонт сбора в днях (по умолчанию 14):") or "14"
    restore_fn(app)
    limit_str = text_fn("Предел сообщений на канал (по умолчанию 200):") or "200"
    restore_fn(app)

    try:
        horizon_days = max(1, int(days_str))
        limit = max(10, int(limit_str))
    except ValueError:
        horizon_days = 14
        limit = 200

    selected_ids = {c.id for c in selected}
    for row in scanned:
        if row["chat"].id not in selected_ids:
            continue
        title = getattr(row["chat"], "title", str(row["chat"].id))
        log(f"Уточнение истории для «{title}» (до {horizon_days} дн., лимит {limit})…")

        cov_old = row.get("coverage")
        scan_dt = cov_old.get("scan_started_at") if cov_old else None

        all_msgs, pubs, cov = refine_channel_history(
            app,
            row["chat"].id,
            row.get("posts") or [],
            horizon_days=horizon_days,
            limit=limit,
            scan_started_at=scan_dt,
            log=log,
        )
        row["posts"] = all_msgs
        row["publications"] = pubs
        row["coverage"] = cov
        members = getattr(row["chat"], "members_count", 0) or 0
        row["metrics"] = compute_metrics(row["chat"], pubs, members)
        row["signals"] = metric_signals(row["metrics"])
        _attach_verdicts([row], evaluate_fn, log)
        log(f"Уточнено: {format_coverage_short(cov)}")

    # Пересчитываем межканальные пересечения после уточнения
    overlaps.clear()
    overlaps.extend(find_channel_overlaps(scanned))
    log("Межканальные пересечения пересчитаны.")


def _review_and_apply_menu(
    app: Any,
    plan: ActionPlan,
    overlaps: list[dict[str, Any]],
    *,
    confirm_fn: Callable[[str], Any],
    session_name: str,
    restore_fn: Callable[[Any], None],
    log: Callable[[str], None],
) -> None:
    """Проверка и подтверждение плана действий (Section 3)."""
    if plan.is_empty():
        log("План действий пуст. Выберите действия для каналов.")
        return

    summary = plan.summary()
    log(
        f"План действий: отписка: {summary['leave']}, mute: {summary['mute']}, "
        f"архив: {summary['archive']}, папка: {summary['folder']}."
    )

    leave_ids = {cid for cid, a in plan.actions.items() if a.leave}
    if leave_ids:
        warnings = check_duplicate_group_unsubscribes(leave_ids, overlaps)
        for w in warnings:
            log(f"⚠️ {w}")

    if not confirm_fn("Применить запланированные действия в Telegram?"):
        log("Применение плана отменено пользователем.")
        restore_fn(app)
        return

    restore_fn(app)
    reports = apply_action_plan(app, plan, session_name=session_name, log=log)
    restore_fn(app)

    applied_count = sum(1 for r in reports if r["status"] == "applied")
    log(f"Применение завершено: {applied_count} действий выполнено.")


def _rollback_menu(
    app: Any,
    session_name: str,
    *,
    confirm_fn: Callable[[str], Any],
    restore_fn: Callable[[Any], None],
    log: Callable[[str], None],
) -> None:
    """Откат применённых изменений из журнала (Section 3)."""
    if not confirm_fn(f"Откатить обратимые изменения сессии {session_name}?"):
        restore_fn(app)
        return
    restore_fn(app)
    results = rollback_session(app, session_name, log=log)
    restore_fn(app)
    restored_count = sum(1 for r in results if r["status"] == "restored")
    log(f"Откат завершён: {restored_count} параметров восстановлено.")


def interactive_session_loop(
    app: Any,
    scanned: list[dict[str, Any]],
    session_name: str,
    *,
    evaluate_fn: Callable[..., Any],
    ask_chats_fn: Callable[..., Any],
    confirm_fn: Callable[[str], Any],
    select_fn: Callable[..., Any],
    text_fn: Callable[..., Any],
    restore_fn: Callable[[Any], None],
    log: Callable[[str], None],
) -> None:
    """Главный интерактивный цикл управления подписками и диетой (Section 1-4)."""
    plan = ActionPlan()
    overlaps = find_channel_overlaps(scanned)

    while True:
        candidates = [c for r in scanned if (c := classify_candidate(r)) is not None]
        summary = plan.summary()
        plan_str = f" [в плане: {summary['total_channels']}]" if summary["total_channels"] else ""

        menu = [
            (f"📋 Кандидаты и режимы чтения{plan_str}", "candidates"),
            (f"🔗 Обзор пересечений каналов ({len(overlaps)} пар)", "overlaps"),
            ("🔍 Уточнить выбранные каналы (расширенная история)", "drilldown"),
            ("📝 Просмотреть и применить план действий", "apply"),
            ("↩️ Откатить изменения из журнала (Rollback)", "rollback"),
            ("🚪 Завершить работу с сессией", "exit"),
        ]

        choice = select_fn(f"Сессия {session_name} — выберите раздел:", menu)
        restore_fn(app)

        if not choice or choice == "exit":
            log("Завершение работы с сессией.")
            break

        if choice == "candidates":
            _manage_action_plan(
                candidates,
                plan,
                ask_chats_fn=ask_chats_fn,
                select_fn=select_fn,
                text_fn=text_fn,
                app=app,
                restore_fn=restore_fn,
                log=log,
            )
        elif choice == "overlaps":
            _inspect_overlaps_menu(overlaps, select_fn, log)
        elif choice == "drilldown":
            _drilldown_menu(
                app,
                scanned,
                overlaps,
                ask_chats_fn=ask_chats_fn,
                text_fn=text_fn,
                evaluate_fn=evaluate_fn,
                restore_fn=restore_fn,
                log=log,
            )
        elif choice == "apply":
            _review_and_apply_menu(
                app,
                plan,
                overlaps,
                confirm_fn=confirm_fn,
                session_name=session_name,
                restore_fn=restore_fn,
                log=log,
            )
        elif choice == "rollback":
            _rollback_menu(
                app,
                session_name,
                confirm_fn=confirm_fn,
                restore_fn=restore_fn,
                log=log,
            )


def clean_subscriptions(
    sessions: list[str],
    *,
    client: Callable[..., Any] = telegram_client,
    scan: Callable[..., list[dict[str, Any]]] = scan_channels,
    restore: Callable[[Any], None] = restore_client_loop,
    evaluate_fn: Callable[..., Any] = evaluate,
    ask: Callable[[list[Any]], Any] = _ask_unsubscribe,
    confirm: Callable[[str], Any] = ask_confirm,
    leave: Callable[..., list[Any]] = leave_chats,
    log: Callable[[str], None] = logging.info,
    interactive_menu: bool | None = None,
    select_fn: Callable[..., Any] = ask_select,
    text_fn: Callable[..., Any] = ask_text,
) -> None:
    if not sessions:
        log("Сессии не выбраны. Завершение работы.")
        return
    multi = len(sessions) > 1

    # ponytail: если передан прямой mock ask_fn (тесты), сохраняем однопроходный флоу
    is_interactive = (
        interactive_menu
        if interactive_menu is not None
        else (ask == _ask_unsubscribe)
    )

    for session_name in sessions:
        if multi:
            log(f"=== Сессия: {session_name} ===")
        try:
            with client(session_name=session_name) as app:
                scanned = scan(app, log)
                if not scanned:
                    continue
                _attach_verdicts(scanned, evaluate_fn, log)

                if is_interactive:
                    interactive_session_loop(
                        app,
                        scanned,
                        session_name,
                        evaluate_fn=evaluate_fn,
                        ask_chats_fn=ask,
                        confirm_fn=confirm,
                        select_fn=select_fn,
                        text_fn=text_fn,
                        restore_fn=restore,
                        log=log,
                    )
                    continue

                candidates = [c for r in scanned if (c := classify_candidate(r)) is not None]
                candidates.sort(key=lambda x: {"red": 0, "yellow": 1, "gray": 2}[x["category"]])
                log(f"Кандидатов на отписку: {len(candidates)} из {len(scanned)}.")
                if not candidates:
                    continue
                selected = ask(_choices(candidates)) or []
                chats = [c["row"]["chat"] for c in candidates if c["row"]["chat"] in selected]
                restore(app)
                if not chats:
                    log("Кандидаты не выбраны — ничего не отписано.")
                    continue
                log("Будут отписаны:\n" + "\n".join(chat_label(c) for c in chats))
                if confirm(f"Отписаться от {len(chats)} каналов?") is True:
                    leave(app, chats, sleep=sleep, log=log)
                restore(app)
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
