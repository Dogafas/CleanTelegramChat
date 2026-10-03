"""Clean my subscriptions: оценка каналов, список кандидатов, массовая отписка."""

import logging
import random
from collections.abc import Callable
from functools import partial
from time import sleep
from typing import Any

from catalog import chat_label
from session import prompt_sessions, restore_client_loop, telegram_client
from subs import (
    ANALYSIS_PAUSE,
    CANDIDATE_THRESHOLD,
    channel_subscriptions,
    compute_metrics,
    fetch_creation_date,
    fetch_posts,
    leave_chats,
    score_channel,
)
from ui import ask_chats, ask_confirm
from verdict import choice_suffix, evaluate, has_text, text_reasons

_ask_unsubscribe = partial(ask_chats, message="Отметьте каналы для отписки")


def scan_channels(app: Any, log: Callable[[str], None], rng=random.uniform) -> list[dict[str, Any]]:
    """Метрики + скор по каждой подписке с постами."""
    channels = channel_subscriptions(app)
    if not channels:
        log("Каналов в подписках не найдено.")
        return []
    log(f"Найдено каналов: {len(channels)}. Сбор метрик по последним постам…")
    results: list[dict[str, Any]] = []
    for i, chat in enumerate(channels):
        if i:
            sleep(rng(*ANALYSIS_PAUSE))
        try:
            full_chat = app.get_chat(chat.id)
            members = getattr(full_chat, "members_count", 0) or 0
            posts = fetch_posts(app, chat.id)
            created_at = fetch_creation_date(app, chat.id)
        except Exception as e:  # один канал не срывает обход
            log(f"Пропуск {chat_label(chat)}: {e}")
            continue
        if not posts:
            log(f"Пропуск {chat_label(chat)}: нет постов в истории")
            continue
        metrics = compute_metrics(chat, posts, members, created_at=created_at)
        result = score_channel(metrics)
        growth_str = ""
        if metrics.get("growth_per_month") is not None:
            growth_str = f" | возраст: {metrics['age_days']:.0f}д (~{metrics['growth_per_month']:.0f} суб/мес)"
        flags_note = f" | ФЛАГИ: {'; '.join(result['flags'])}" if result["flags"] else ""
        log(
            f"{chat_label(chat)} — скор {result['score']}/100{growth_str}"
            f" | по метрикам: {result['scores']}{flags_note}"
        )
        result["chat"] = chat
        result["description"] = getattr(full_chat, "description", None) or ""
        result["metrics"] = metrics
        result["posts"] = posts
        results.append(result)
    return results


def _choices(candidates: list[dict[str, Any]]) -> list[Any]:
    # ponytail: тот же формат кортежей, что и catalog._build_choices
    rows = []
    for c in candidates:
        suffix, checked = choice_suffix(
            c.get("verdict"), c["score"] < CANDIDATE_THRESHOLD, c["flags"]
        )
        title = f"{chat_label(c['chat'])} — скор {c['score']}/100{suffix}"
        rows.append((title, c["chat"], checked))
    return rows


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
                for c in scanned:
                    if has_text(c["posts"]):
                        log(f"Запрос вердикта LLM: {c['chat'].title}")
                        try:
                            desc = c.get("description") or getattr(c["chat"], "description", None) or ""
                            try:
                                c["verdict"] = evaluate_fn(
                                    c["chat"].title, c["posts"], description=desc
                                )
                            except TypeError:
                                c["verdict"] = evaluate_fn(c["chat"].title, c["posts"])
                        except Exception:
                            logging.exception("Вердикт не удался, канал без вердикта")
                            c["verdict"] = {"error": "см. traceback в логе"}
                    else:
                        c["verdict"] = None
                candidates = [
                    c
                    for c in scanned
                    if c["score"] < CANDIDATE_THRESHOLD
                    or c["flags"]
                    or text_reasons(c["verdict"])
                ]
                log(f"Кандидатов на отписку: {len(candidates)} из {len(scanned)}.")
                if not candidates:
                    continue
                selected = ask(_choices(candidates)) or []
                chats = [c["chat"] for c in candidates if c["chat"] in selected]
                log("Будут отписаны:")
                for chat in chats:
                    log(chat_label(chat))
                ok = bool(chats) and confirm(f"Отписаться от {len(chats)} каналов?") is True
                # любой questionary-промпт ломает loop клиента — восстанавливаем
                # после всех промптов, до любых вызовов Pyrogram (включая stop в __exit__)
                restore(app)
                if not chats:
                    log("Кандидаты не выбраны — ничего не отписано.")
                elif ok:
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
