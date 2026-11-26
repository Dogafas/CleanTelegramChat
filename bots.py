"""Bot dialogs: scan, fixed signals, RED/YELLOW/GRAY routing (ADR 0011)."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import sleep as default_sleep
from typing import Any

from pyrogram import Client, enums
from pyrogram.types import Chat, Dialog, Message

from dialogs import is_bot_dialog, list_dialogs
from purge import retry_on_flood
from review import Candidate
from session import sync_list
from subs import AD_MARKERS, ANALYSIS_PAUSE
from verdict import REASON_RU, evaluate, has_text, is_valid_verdict, text_reasons

SILENCE_DAYS = 90          # молчит ≥ 3 мес.
UNUSED_OUTGOING = 1        # ≤ 1 исходящего за всю историю (только /start или ничего)
SPAM_AFTER_OUTGOING = 3    # ≥ 3 входящих после последнего исходящего
AD_SHARE_BAD = 0.25        # ≥ 25% входящих бота несут рекламу

AD_ENTITY_TYPES = (enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK)


@dataclass
class BotInfo:
    """Скан бот-диалога: профиль, полная история, сигналы, вердикт."""

    dialog: Dialog
    chat_id: int
    archived: bool
    bio: str = ""
    deleted: bool = False
    outgoing: int = 0
    probe: str | None = None  # None | "replied" | "silent" | "error"
    signals: list[str] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    messages: list[Message] = field(default_factory=list)
    error: str | None = None


def _display_name(chat: Chat) -> str:
    name = " ".join(
        p for p in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if p
    )
    return name or getattr(chat, "username", None) or str(getattr(chat, "id", "?"))


def bot_label(chat: Chat) -> str:
    """Имя бот-диалога: у User нет .title, берём имя/username."""
    username = getattr(chat, "username", None)
    name = _display_name(chat)
    return f"{name} (@{username}) — {chat.id}" if username else f"{name} — {chat.id}"


def bot_name(chat: Chat) -> str:
    """Короткое имя для Jev (channel в state)."""
    return _display_name(chat)


def _to_utc(val: datetime) -> datetime:
    return val if val.tzinfo else val.replace(tzinfo=UTC)


def _is_ad(m: Message) -> bool:
    text = str(getattr(m, "text", None) or getattr(m, "caption", None) or "")
    if AD_MARKERS.search(text):
        return True
    entities = list(getattr(m, "entities", None) or [])
    entities += list(getattr(m, "caption_entities", None) or [])
    return any(getattr(e, "type", None) in AD_ENTITY_TYPES for e in entities)


def ad_share(messages: list[Message]) -> float:
    """Доля входящих сообщений бота с рекламными маркерами или URL-entities."""
    incoming = [
        m
        for m in messages
        if not getattr(m, "service", False) and not getattr(m, "outgoing", False)
    ]
    if not incoming:
        return 0.0
    return sum(1 for m in incoming if _is_ad(m)) / len(incoming)


def bot_signals(messages: list[Message], *, now: datetime | None = None) -> list[str]:
    """Фиксированные сигналы ADR 0011 §3 по полной истории (новейшие первыми)."""
    now = now or datetime.now(UTC)
    signals: list[str] = []

    last_date = getattr(messages[0], "date", None) if messages else None
    if last_date is None or (now - _to_utc(last_date)) >= timedelta(days=SILENCE_DAYS):
        signals.append("молчит ≥ 3 мес.")

    outgoing = sum(1 for m in messages if getattr(m, "outgoing", False))
    if outgoing <= UNUSED_OUTGOING:
        signals.append("не использован")

    # «новейшие первыми»: до первого исходящего — входящие после нашего последнего ответа
    after_last_outgoing = 0
    for m in messages:
        if getattr(m, "outgoing", False):
            break
        if not getattr(m, "service", False):
            after_last_outgoing += 1
    if after_last_outgoing >= SPAM_AFTER_OUTGOING:
        signals.append("спам без участия")

    if ad_share(messages) >= AD_SHARE_BAD:
        signals.append("рекламная витрина")
    return signals


def _is_deleted(
    app: Client,
    chat_id: int,
    *,
    sleep: Callable[[float], None],
    rng: Callable[..., float],
    log: Callable[[str], None],
) -> bool:
    val, error = retry_on_flood(
        lambda: app.get_users(chat_id), sleep=sleep, rng=rng, log=log, what=", бот"
    )
    return error is not None or bool(getattr(val, "is_deleted", False))


def _get_bio(
    app: Client,
    chat_id: int,
    *,
    sleep: Callable[[float], None],
    rng: Callable[..., float],
    log: Callable[[str], None],
) -> str:
    val, _ = retry_on_flood(
        lambda: app.get_chat(chat_id), sleep=sleep, rng=rng, log=log, what=", бот"
    )
    return str(getattr(val, "bio", None) or "") if val is not None else ""


def _fetch_history(
    app: Client,
    chat_id: int,
    *,
    sleep: Callable[[float], None],
    rng: Callable[..., float],
    log: Callable[[str], None],
) -> tuple[list[Message], str | None]:
    # ADR 0011: полная история без лимита — бот-диалоги короткие
    val, error = retry_on_flood(
        lambda: sync_list(app.get_chat_history(chat_id)),
        sleep=sleep, rng=rng, log=log, what=", бот"
    )
    if error is not None:
        return [], f"{type(error).__name__}: {error}"
    return val or [], None


def scan_bots(
    app: Client,
    log: Callable[[str], None] = print,
    *,
    sleep: Callable[[float], None] = default_sleep,
    rng: Callable[..., float] = random.uniform,
    evaluate: Callable[..., Any] = evaluate,
) -> list[BotInfo]:
    """Все бот-диалоги аккаунта (main + архив): профиль, полная история, сигналы."""
    main, archive, archive_error = list_dialogs(app)
    if archive_error:
        log(f"Архив не прочитан: {archive_error}")
    dialogs = [(row, False) for row in main if is_bot_dialog(row)]
    dialogs += [(row, True) for row in archive if is_bot_dialog(row)]
    if not dialogs:
        return []

    log(f"Найдено ботов: {len(dialogs)}. Читаю полную историю диалогов…")
    infos: list[BotInfo] = []
    for i, (dialog, archived) in enumerate(dialogs):
        if i:
            sleep(rng(*ANALYSIS_PAUSE))
        chat = dialog.chat
        chat_id = chat.id
        if chat_id is None:
            log(f"{bot_label(chat)}: диалог без id — пропускаю")
            continue
        info = BotInfo(dialog=dialog, chat_id=chat_id, archived=archived)
        label = bot_label(chat)
        if _is_deleted(app, chat_id, sleep=sleep, rng=rng, log=log):
            info.deleted = True
            log(f"{label}: удалён или заблокирован — кандидат RED")
            infos.append(info)
            continue
        info.bio = _get_bio(app, chat_id, sleep=sleep, rng=rng, log=log)
        info.messages, info.error = _fetch_history(app, chat_id, sleep=sleep, rng=rng, log=log)
        if info.error is None:
            info.outgoing = sum(1 for m in info.messages if getattr(m, "outgoing", False))
            info.signals = bot_signals(info.messages)
            _attach_verdict(info, evaluate, log)
            signals_str = f" | сигналы: {'; '.join(info.signals)}" if info.signals else ""
            log(f"{label}: исходящих {info.outgoing}, сообщений {len(info.messages)}{signals_str}")
        else:
            log(f"{label}: история не прочитана ({info.error})")
        infos.append(info)
    return infos


def _classify_by_texts(info: BotInfo, chat: Chat) -> Candidate | None:
    """Маршрутизация по сигналам и Jev-вердикту (ADR 0011 §5-6)."""
    if info.verdict is not None and not is_valid_verdict(info.verdict):
        return Candidate("gray", ["вердикт не удался"], None, False, chat)

    vreasons = text_reasons(info.verdict) if is_valid_verdict(info.verdict) else []
    voice = info.verdict.get("voice") if info.verdict and vreasons else None
    if "ads" in vreasons and "не использован" in info.signals:
        return Candidate("red", ["рекламная витрина", "не использован"], voice, True, chat)

    reasons = list(
        dict.fromkeys(
            info.signals
            + [REASON_RU.get(r, r) for r in vreasons]
        )
    )
    if reasons:
        return Candidate("yellow", reasons, voice, False, chat)
    if not has_text(info.messages):
        return Candidate("gray", ["нет текстов для оценки"], None, False, chat)
    return None


def classify(info: BotInfo) -> Candidate | None:
    """Маршрутизация ADR 0011 §6. None — бот без признаков, не кандидат."""
    chat = info.dialog.chat
    if info.deleted:
        return Candidate("red", ["удалён / забанен"], None, True, chat)
    if info.probe == "silent":
        return Candidate("red", ["не отвечает на /start"], None, True, chat)
    if info.error:
        return Candidate("gray", [f"история не прочитана ({info.error})"], None, False, chat)
    return _classify_by_texts(info, chat)


def _attach_verdict(
    info: BotInfo,
    evaluate: Callable[..., Any],
    log: Callable[[str], None],
) -> None:
    """Jev-вердикт: имя бота → channel, bio (≤500) → description, тексты → posts (ADR 0011 §5)."""
    if not has_text(info.messages):
        info.verdict = None
        return
    name = bot_name(info.dialog.chat)
    log(f"Запрос вердикта LLM: {name}")
    try:
        verdict = evaluate(name, info.messages, description=info.bio[:500])
    except Exception:
        logging.exception("Вердикт не удался, бот без вердикта")
        info.verdict = {"error": "см. traceback в логе"}
        return
    info.verdict = verdict if is_valid_verdict(verdict) else {"error": "некорректный ответ модели"}


def probe_bot(
    app: Client,
    chat_id: int,
    *,
    wait_s: int = 30,
    poll_s: int = 5,
    sleep: Callable[[float], None] = default_sleep,
    rng: Callable[..., float] = random.uniform,
    log: Callable[[str], None] = print,
) -> str:
    """/start-проба (ADR 0011 §4): replied | silent | error."""
    sent, error = retry_on_flood(
        lambda: app.send_message(chat_id, "/start"), sleep=sleep, rng=rng, log=log, what=", /start"
    )
    if error is not None or sent is None:
        log(f"Не удалось отправить /start боту {chat_id}: {error}")
        return "error"
    waited = 0
    while waited < wait_s:
        sleep(poll_s)
        waited += poll_s
        hist, hist_error = retry_on_flood(
            lambda: sync_list(app.get_chat_history(chat_id, limit=1)),
            sleep=sleep, rng=rng, log=log, what=", бот",
        )
        if hist_error is None and hist:
            last = hist[0]
            last_id = getattr(last, "id", 0)
            if not getattr(last, "outgoing", False) and last_id != getattr(sent, "id", -1):
                return "replied"
    return "silent"
