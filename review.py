"""Channel Subscription scan, coverage, signals, and recommendation routing."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep as default_sleep
from typing import Any, Callable

from pyrogram import enums
from pyrogram.types import Chat, Message

from catalog import chat_label
from dialogs import list_dialogs
from purge import retry_on_flood
from subs import (
    ANALYSIS_PAUSE,
    POSTS_WINDOW,
    compute_coverage,
    compute_metrics,
    fetch_creation_date,
    fetch_posts,
    format_coverage_short,
    group_media_publications,
    metric_signals,
    refine_channel_history,
)
from verdict import REASON_RU, VerdictStatus, has_text, is_valid_verdict, status_of, text_reasons

_PRIORITY = {"red": 0, "yellow": 1, "gray": 2}


@dataclass
class Candidate:
    category: str
    reasons: list[str]
    voice: str | None
    checked: bool
    chat: Any
    status: VerdictStatus = VerdictStatus.VALID


class Review:
    """One session's scanned subscriptions. Callers do not read row keys."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def candidates(self) -> list[Candidate]:
        items = [item for row in self._rows if (item := _classify(row)) is not None]
        items.sort(key=lambda item: _PRIORITY[item.category])
        return items

    def chats(self) -> list[Chat]:
        return [row["chat"] for row in self._rows]

    def for_overlap(self) -> list[dict[str, Any]]:
        """Opaque input for find_channel_overlaps. Do not read keys."""
        return self._rows

    def refresh(
        self,
        app: Any,
        chat_ids: set[int] | list[int],
        *,
        horizon_days: int,
        limit: int,
        evaluate: Callable[..., Any],
        log: Callable[[str], None],
        sleep: Callable[[float], None] = default_sleep,
        rng: Callable[..., float] = random.uniform,
    ) -> None:
        selected = set(chat_ids)
        for row in self._rows:
            chat = row["chat"]
            if chat.id not in selected:
                continue
            title = getattr(chat, "title", str(chat.id))
            log(f"Уточнение истории для «{title}» (до {horizon_days} дн., лимит {limit})…")
            full = _get_chat(app, chat, log, sleep, rng)
            if full is not None:
                members = getattr(full, "members_count", 0) or 0
                desc = getattr(full, "description", None) or row.get("description") or ""
                row["description"] = desc
            else:
                members = 0
            cov_old = row.get("coverage")
            scan_dt = cov_old.get("scan_started_at") if cov_old else None
            all_msgs, pubs, cov = refine_channel_history(
                app,
                chat.id,
                row.get("posts") or [],
                horizon_days=horizon_days,
                limit=limit,
                scan_started_at=scan_dt,
                sleep=sleep,
                rng=rng,
                log=log,
            )
            row["posts"] = all_msgs
            row["publications"] = pubs
            row["coverage"] = cov
            row["metrics"] = compute_metrics(chat, pubs, members)
            row["signals"] = metric_signals(row["metrics"])
            _attach(row, evaluate, log)
            log(f"Уточнено: {format_coverage_short(cov)}")


def scan(
    app: Any,
    evaluate: Callable[..., Any],
    log: Callable[[str], None],
    *,
    sleep: Callable[[float], None] = default_sleep,
    rng: Callable[..., float] = random.uniform,
) -> Review:
    main, archive, archive_error = list_dialogs(app)
    if archive_error:
        log(f"Архив не прочитан: {archive_error}")
    channels = [
        row.chat
        for row in [*main, *archive]
        if getattr(row, "chat", None) and row.chat.type == enums.ChatType.CHANNEL
    ]
    if not channels:
        log("Каналов в подписках не найдено.")
        return Review([])

    log(f"Найдено каналов: {len(channels)}. Сбор метрик по последним постам…")
    rows: list[dict[str, Any]] = []
    scan_start = datetime.now(timezone.utc)
    for i, chat in enumerate(channels):
        if i:
            sleep(rng(*ANALYSIS_PAUSE))
        row = _sample(app, chat, scan_start, log=log, sleep=sleep, rng=rng)
        _attach(row, evaluate, log)
        rows.append(row)
    return Review(rows)


def _sample(
    app: Any,
    chat: Chat,
    scan_start: datetime,
    *,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    rng: Callable[..., float],
) -> dict[str, Any]:
    full = _get_chat(app, chat, log, sleep, rng)
    posts: list[Message] = []
    created_at = None
    members = 0
    fetch_err = None
    description = ""
    if full is not None and chat.id is not None:
        chat_id = chat.id
        members = getattr(full, "members_count", 0) or 0
        description = getattr(full, "description", None) or ""
        fetched, err = retry_on_flood(
            lambda: fetch_posts(app, chat_id),
            sleep=sleep,
            rng=rng,
            log=log,
            what=f", канал {chat_label(chat)}",
        )
        if err is not None:
            fetch_err = f"{type(err).__name__}: {err}"
            log(f"Ошибка при получении постов {chat_label(chat)}: {err}")
        posts = fetched or []
        created_at = fetch_creation_date(app, chat_id)
    else:
        fetch_err = "чат не прочитан"
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
    log(f"{chat_label(chat)}{growth_str}{signals_str} [{format_coverage_short(coverage)}]")
    return {
        "chat": chat,
        "description": description,
        "metrics": metrics,
        "signals": signals,
        "posts": posts,
        "publications": pubs,
        "coverage": coverage,
    }


def _get_chat(
    app: Any,
    chat: Chat,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    rng: Callable[..., float],
) -> Chat | None:
    if chat.id is None:
        return None
    val, error = retry_on_flood(
        lambda: app.get_chat(chat.id),
        sleep=sleep,
        rng=rng,
        log=log,
        what=f", канал {chat_label(chat)}",
    )
    if error is None:
        return val
    log(f"Ошибка при получении {chat_label(chat)}: {error}")
    return None


def _attach(row: dict[str, Any], evaluate: Callable[..., Any], log: Callable[[str], None]) -> None:
    posts = row.get("publications") or row.get("posts") or []
    if not has_text(posts):
        row["verdict"] = None
        if "coverage" in row:
            row["coverage"]["model_texts_count"] = 0
            row["coverage"]["truncated_texts_count"] = 0
        return
    title = getattr(row["chat"], "title", None) or "без названия"
    log(f"Запрос вердикта LLM: {title}")
    try:
        verdict = evaluate(title, posts, description=row.get("description") or "")
    except Exception:
        logging.exception("Вердикт не удался, канал без вердикта")
        row["verdict"] = {"error": "см. traceback в логе"}
        return
    if not is_valid_verdict(verdict):
        row["verdict"] = {"error": "некорректный ответ модели"}
        return
    row["verdict"] = verdict
    if "coverage" in row and isinstance(verdict, dict):
        row["coverage"]["model_texts_count"] = verdict.get("model_texts_count", 0)
        row["coverage"]["truncated_texts_count"] = verdict.get("truncated_texts_count", 0)


def _classify(row: dict[str, Any]) -> Candidate | None:
    verdict = row.get("verdict")
    signals = row.get("signals") or []
    posts = row.get("publications") or row.get("posts") or []
    chat = row["chat"]
    if has_text(posts) and is_valid_verdict(verdict) and isinstance(verdict, dict):
        reasons = text_reasons(verdict)
        if reasons:
            return Candidate(
                "red",
                [REASON_RU.get(reason, reason) for reason in reasons] + list(signals),
                verdict.get("voice"),
                True,
                chat,
                status=VerdictStatus.VALID,
            )
        if signals:
            return Candidate(
                "yellow",
                list(signals),
                verdict.get("voice"),
                False,
                chat,
                status=VerdictStatus.VALID,
            )
        return None
    status = status_of(verdict)
    status_text = "нет текста" if status is VerdictStatus.NO_TEXT else "ошибка вердикта"
    return Candidate("gray", [status_text, *signals], None, False, chat, status=status)
