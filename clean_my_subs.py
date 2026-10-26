"""Clean my subscriptions: оценка каналов, список кандидатов, массовая отписка."""

import logging
import random
from collections.abc import Callable
from functools import partial
from time import sleep
from typing import Any

from pyrogram.errors import FloodWait

from catalog import chat_label
from purge import flood_delay
from session import prompt_sessions, restore_client_loop, telegram_client
from subs import (
    ANALYSIS_PAUSE,
    channel_subscriptions,
    compute_metrics,
    fetch_creation_date,
    fetch_posts,
    leave_chats,
    metric_signals,
)
from ui import StyledTitle, ask_chats, ask_confirm
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
    """Метрики по каждой подписке с постами."""
    channels = channel_subscriptions(app)
    if not channels:
        log("Каналов в подписках не найдено.")
        return []
    log(f"Найдено каналов: {len(channels)}. Сбор метрик по последним постам…")
    results: list[dict[str, Any]] = []
    for i, chat in enumerate(channels):
        if i:
            sleep(rng(*ANALYSIS_PAUSE))
        full_chat = None
        posts = []
        created_at = None
        members = 0
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
            except Exception as e:  # один канал не срывает обход
                log(f"Пропуск {chat_label(chat)}: {e}")
                break
        if full_chat is None:
            continue
        if not posts:
            log(f"Пропуск {chat_label(chat)}: нет постов в истории")
            continue
        metrics = compute_metrics(chat, posts, members, created_at=created_at)
        signals = metric_signals(metrics)
        growth_str = ""
        if metrics.get("growth_per_month") is not None:
            growth_str = (
                f" | возраст: {metrics['age_days']:.0f}д"
                f" (~{metrics['growth_per_month']:.0f} суб/мес)"
            )
        signals_str = f" | сигналы: {'; '.join(signals)}" if signals else " | сигналы: в норме"
        log(f"{chat_label(chat)}{growth_str}{signals_str}")
        results.append({
            "chat": chat,
            "description": getattr(full_chat, "description", None) or "",
            "metrics": metrics,
            "signals": signals,
            "posts": posts,
        })
    return results


def classify_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    """Маршрутизация подписки: RED [Отписаться], YELLOW [Проверить], GRAY [Недостаточно данных]."""
    verdict = row.get("verdict")
    signals = row.get("signals") or []
    posts = row.get("posts") or []

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
    # Приоритет над цветом метрик: серая категория с метрическими сигналами во вторую очередь
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
    classified: list[dict[str, Any]] = []
    for item in candidates:
        if "category" in item and "row" in item:
            classified.append(item)
        else:
            c = classify_candidate(item)
            if c is not None:
                classified.append(c)
    priority = {"red": 0, "yellow": 1, "gray": 2}
    classified.sort(key=lambda x: priority[x["category"]])
    return [_build_candidate_row(c) for c in classified]


def _channel_description(row: dict[str, Any]) -> str:
    chat = row["chat"]
    return row.get("description") or getattr(chat, "description", None) or ""


def _ask_verdict(evaluate_fn: Callable[..., Any], row: dict[str, Any]) -> Any:
    title = getattr(row["chat"], "title", None) or "без названия"
    return evaluate_fn(
        title,
        row["posts"],
        description=_channel_description(row),
    )

def _attach_verdicts(
    scanned: list[dict[str, Any]],
    evaluate_fn: Callable[..., Any],
    log: Callable[[str], None],
) -> None:
    for row in scanned:
        if not has_text(row["posts"]):
            row["verdict"] = None
            continue
        log(f"Запрос вердикта LLM: {row['chat'].title}")
        try:
            verdict = _ask_verdict(evaluate_fn, row)
            if not is_valid_verdict(verdict):
                row["verdict"] = {"error": "некорректный ответ модели"}
            else:
                row["verdict"] = verdict
        except Exception:
            logging.exception("Вердикт не удался, канал без вердикта")
            row["verdict"] = {"error": "см. traceback в логе"}


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
                scanned = scan(app, log)
                if not scanned:
                    continue
                _attach_verdicts(scanned, evaluate_fn, log)
                candidates: list[dict[str, Any]] = []
                for row in scanned:
                    item = classify_candidate(row)
                    if item is not None:
                        candidates.append(item)
                priority = {"red": 0, "yellow": 1, "gray": 2}
                candidates.sort(key=lambda x: priority[x["category"]])
                log(f"Кандидатов на отписку: {len(candidates)} из {len(scanned)}.")
                if not candidates:
                    continue
                choices = _choices(candidates)
                selected = ask(choices) or []
                chats = [c["row"]["chat"] for c in candidates if c["row"]["chat"] in selected]
                restore(app)
                if not chats:
                    log("Кандидаты не выбраны — ничего не отписано.")
                    continue
                log("Будут отписаны:")
                for chat in chats:
                    log(chat_label(chat))
                ok = confirm(f"Отписаться от {len(chats)} каналов?") is True
                # любой questionary-промпт ломает loop клиента — восстанавливаем
                # после всех промптов, до любых вызовов Pyrogram (включая stop в __exit__)
                restore(app)
                if ok:
                    leave(app, chats, sleep=sleep, log=log)
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
