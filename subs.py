"""Channel subscriptions: local quality metrics, indirect review signals, mass unsubscribe."""

import random
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from purge import retry_on_flood

POSTS_WINDOW = 30
ANALYSIS_PAUSE = (1.0, 3.0)
MIN_POSTS_FOR_CV = 2  # минимум постов для статистики (CV, частота)

# ponytail: пороги ресёрча (Statzen/TGuard/ppc.world, ADR 0007) в одном dict.
# Метрики служат косвенными сигналами для проверки, а не доказательством накрутки.
THRESHOLDS = {
    "err_bad": 5.0,  # <5% — низкая вовлечённость
    "err_inflated": 100.0,  # views>subs — аномально высокий охват
    "ad_share_bad": 0.25,  # >25% — высокая доля рекламы
    "cv_bad": 0.15,  # подозрительно ровный охват
    "freq_bad": 60.0,  # >60 — частое обновление / нагрузка
    "bot_farm_subs": 100_000,  # >= 100k подписчиков
    "bot_farm_days": 60.0,  # <= 2 месяцев (60 дней)
    "abandoned_years": 7.0,  # >= 7 лет
    "abandoned_freq": 2.0,  # постов/мес < 2
}

AD_MARKERS = re.compile(
    r"(подпишис|переходи|промокод|реклам[ае]|заказать|купить|курс[ао]?|"
    r"инвестици|заработ|t\.me/\w)",
    re.IGNORECASE,
)




def fetch_posts(app: Any, chat_id: int, limit: int = POSTS_WINDOW) -> list[Any]:
    return list(app.get_chat_history(chat_id, limit=limit))


def fetch_creation_date(app: Any, chat_id: int) -> Any:
    """Дата создания канала: первое сообщение (id=1)."""
    try:
        msg = app.get_messages(chat_id, 1)
        if msg and not getattr(msg, "empty", False):
            return msg.date
    except Exception:
        return None
    return None


def _to_utc_dt(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return None


class Publication:
    """Публикация канала: одиночное сообщение или объединённый медиальбом."""

    def __init__(
        self,
        id: int,
        date: Any,
        text: str = "",
        *,
        views: int | None = None,
        media_group_id: str | None = None,
        forward_from_chat_id: int | None = None,
        forward_from_message_id: int | None = None,
        urls: list[str] | None = None,
    ) -> None:
        self.id = id
        self.date = date
        self.text = text
        self.caption = text
        self.views = views
        self.media_group_id = media_group_id
        self.forward_from_chat_id = forward_from_chat_id
        self.forward_from_message_id = forward_from_message_id
        self.urls = urls or []


def _extract_text(m: Any) -> str:
    raw = getattr(m, "text", None) or getattr(m, "caption", None) or ""
    return str(raw)


def _extract_forward_info(m: Any) -> tuple[int | None, int | None]:
    """(origin_chat_id, origin_message_id) из forward_origin."""
    origin = getattr(m, "forward_origin", None)
    if origin is None:
        return None, None
    origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if origin_chat is not None:
        fwd_chat_id = getattr(origin_chat, "id", None)
    else:
        sender_user = getattr(origin, "sender_user", None)
        fwd_chat_id = getattr(sender_user, "id", None) if sender_user is not None else None
    return fwd_chat_id, getattr(origin, "message_id", None)


def group_media_publications(messages: list[Any]) -> list[Publication]:
    """Объединение сообщений медиагрупп в единые публикации (Section 1)."""
    # ponytail: группируем сообщения с одинаковым media_group_id в одну публикацию
    if not messages:
        return []

    grouped: list[Publication] = []
    by_media_group: dict[str, Publication] = {}

    for m in messages:
        if isinstance(m, Publication):
            grouped.append(m)
            continue

        mg_id = getattr(m, "media_group_id", None)
        mg_key = str(mg_id) if mg_id is not None else None

        if mg_key is not None and mg_key in by_media_group:
            pub = by_media_group[mg_key]
            m_text = _extract_text(m)
            if m_text.strip() and not pub.text.strip():
                pub.text = m_text
                pub.caption = m_text
            m_views = getattr(m, "views", None)
            if m_views is not None:
                pub.views = max(pub.views or 0, int(m_views))
            f_chat, f_msg = _extract_forward_info(m)
            if f_chat is not None and pub.forward_from_chat_id is None:
                pub.forward_from_chat_id = f_chat
                pub.forward_from_message_id = f_msg
            continue

        text = _extract_text(m)
        views = getattr(m, "views", None)
        views_val = int(views) if views is not None else None
        msg_id = getattr(m, "id", 0)
        date = getattr(m, "date", None)
        f_chat, f_msg = _extract_forward_info(m)

        pub = Publication(
            id=msg_id,
            date=date,
            text=text,
            views=views_val,
            media_group_id=mg_key,
            forward_from_chat_id=f_chat,
            forward_from_message_id=f_msg,
        )
        grouped.append(pub)
        if mg_key is not None:
            by_media_group[mg_key] = pub

    return grouped


def compute_coverage(
    messages: list[Any],
    publications: list[Publication] | None = None,
    *,
    scan_started_at: datetime | None = None,
    limit: int = POSTS_WINDOW,
    horizon_days: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Сведения о покрытии выборки канала (Section 1)."""
    scan_dt = _to_utc_dt(scan_started_at) or datetime.now(timezone.utc)
    pubs = publications if publications is not None else group_media_publications(messages)

    dates_raw = [getattr(m, "date", None) for m in messages if getattr(m, "date", None)]
    valid_dates = [d for d in (_to_utc_dt(d) for d in dates_raw) if d is not None]
    oldest_dt = min(valid_dates) if valid_dates else None
    newest_dt = max(valid_dates) if valid_dates else None

    raw_count = len(messages)
    pubs_count = len(pubs)
    text_count = sum(bool(getattr(p, "text", "").strip()) for p in pubs)

    span_days: float | None = None
    span_hours: float | None = None
    if oldest_dt and newest_dt:
        sec = max((newest_dt - oldest_dt).total_seconds(), 0.0)
        span_days = round(sec / 86400.0, 2)
        span_hours = round(sec / 3600.0, 1)

    if error:
        status = "unavailable" if raw_count == 0 else "partial"
    elif raw_count == 0:
        status = "unavailable"
        error = "нет доступных сообщений"
    else:
        limit_reached = raw_count >= limit
        horizon_reached = bool(
            horizon_days and oldest_dt and oldest_dt <= scan_dt - timedelta(days=horizon_days)
        )
        status = "partial" if (limit_reached and not horizon_reached) else "success"

    return {
        "scan_started_at": scan_dt,
        "raw_messages_count": raw_count,
        "publications_count": pubs_count,
        "text_publications_count": text_count,
        "model_texts_count": 0,
        "truncated_texts_count": 0,
        "status": status,
        "error": error,
        "span_days": span_days,
        "span_hours": span_hours,
    }


def format_coverage_short(cov: dict[str, Any]) -> str:
    """Короткая сводка покрытия для компактной строки кандидата."""
    if cov.get("status") == "unavailable":
        err = cov.get("error") or "нет сообщений"
        return f"история недоступна: {err}"

    raw = cov.get("raw_messages_count", 0)
    pubs = cov.get("publications_count", 0)
    texts = cov.get("text_publications_count", 0)
    span_days = cov.get("span_days")
    span_hours = cov.get("span_hours")

    if span_days is not None and span_days >= 1.0:
        span_str = f"{span_days:.1f} дн."
    elif span_hours is not None:
        span_str = f"{max(span_hours, 0.1):.0f} ч."
    else:
        span_str = "период неизвестен"

    pub_part = f"{raw} сообщ." if raw == pubs else f"{raw} сообщ. ({pubs} публ.)"
    text_part = f"текст в {texts}" if texts > 0 else "только медиа без текста"
    parts = [f"{pub_part} за {span_str}", text_part]

    trunc = cov.get("truncated_texts_count", 0)
    if trunc > 0:
        parts.append(f"{trunc} обрезано")

    if cov.get("status") == "partial":
        parts.append("частично")

    return "; ".join(parts)


def compute_metrics(
    _chat: Any,
    posts: list[Any],
    members: int,
    *,
    created_at: Any = None,
) -> dict[str, Any]:
    """Quality Metrics из дампа постов (ADR 0007)."""
    pubs = group_media_publications(posts)
    texts = [(getattr(m, "text", None) or getattr(m, "caption", None) or "") for m in pubs]
    raw_views = [getattr(m, "views", None) for m in pubs]
    valid_views = [float(v) for v in raw_views if v is not None and float(v) > 0]
    dates = [m.date for m in pubs if getattr(m, "date", None)]

    # ponytail: отсутствие данных/знаменателя не симулирует низкий отклик
    err: float | None = None
    if valid_views and members > 0:
        err = round((sum(valid_views) / len(valid_views)) / members * 100, 2)

    cv: float | None = None
    if len(valid_views) >= MIN_POSTS_FOR_CV:
        cv = round(_coefficient_of_variation(valid_views), 3)

    ad_share = (
        round(sum(bool(AD_MARKERS.search(t)) for t in texts) / len(pubs), 3)
        if pubs
        else 0.0
    )

    posts_per_month: float | None = None
    if len(dates) >= MIN_POSTS_FOR_CV:
        span_days = max((max(dates) - min(dates)).total_seconds() / 86400, 0.5)
        posts_per_month = round(len(dates) / span_days * 30, 1)

    age_days: float | None = None
    growth_per_month: float | None = None
    c_dt = _to_utc_dt(created_at)
    if c_dt:
        valid_dates = [_to_utc_dt(m.date) for m in pubs if getattr(m, "date", None)]
        ref_dt = max((d for d in valid_dates if d), default=None) or datetime.now(timezone.utc)
        diff_sec = max((ref_dt - c_dt).total_seconds(), 0.0)
        age_days = round(diff_sec / 86400.0, 1)
        age_months = max(age_days / 30.44, 0.1)
        growth_per_month = round(members / age_months, 1)

    return {
        "err": err,
        "ad_share": ad_share,
        "cv_views": cv,
        "posts_per_month": posts_per_month,
        "members": members,
        "posts": len(pubs),
        "raw_messages": len(posts),
        "age_days": age_days,
        "growth_per_month": growth_per_month,
    }


def _coefficient_of_variation(values: list[float]) -> float:
    mean = statistics.mean(values)
    return (statistics.pstdev(values) / mean) if mean else 1.0

def metric_signals(m: dict[str, Any]) -> list[str]:
    """Косвенные сигналы метрик (ADR 0007, ADR 0009). Не являются доказательством накрутки."""
    signals: list[str] = []

    freq = m.get("posts_per_month")
    if freq is not None and freq > THRESHOLDS["freq_bad"]:
        signals.append(f"частое обновление ({freq:.0f} постов/мес)")

    cv = m.get("cv_views")
    if cv is not None and cv < THRESHOLDS["cv_bad"]:
        signals.append("подозрительно ровный охват")

    err = m.get("err")
    if err is not None and err > THRESHOLDS["err_inflated"]:
        signals.append(f"аномально высокий охват (ERR {err:.0f}%)")

    if err is not None and err < THRESHOLDS["err_bad"]:
        signals.append(f"низкая вовлечённость (ERR {err:.1f}%)")

    ad = m.get("ad_share")
    if ad is not None and ad > THRESHOLDS["ad_share_bad"]:
        signals.append(f"высокая доля рекламы ({round(ad * 100)}%)")

    age = m.get("age_days")
    if (
        age is not None
        and age >= THRESHOLDS["abandoned_years"] * 365.25
        and freq is not None
        and freq < THRESHOLDS["abandoned_freq"]
        and err is not None
        and err < THRESHOLDS["err_bad"]
    ):
        years = age / 365.25
        signals.append(f"заброшенный канал ({years:.1f} г.)")

    members = m.get("members", 0)
    if (
        age is not None
        and age <= THRESHOLDS["bot_farm_days"]
        and members >= THRESHOLDS["bot_farm_subs"]
        and err is not None
        and err < THRESHOLDS["err_bad"]
    ):
        signals.append(f"резкий прирост аудитории ({members} суб за {int(age)} дн)")

    return signals


def leave_chat(app: Any, chat_id: int, *, sleep, rng=random.uniform, log=print) -> bool:
    _val, error = retry_on_flood(
        lambda: app.leave_chat(chat_id),
        sleep=sleep,
        rng=rng,
        log=log,
        what=f", чат {chat_id}",
    )
    if error is None:
        return True
    log(f"Ошибка отписки от чата {chat_id}: {error}")
    return False


def _process_history_batch(
    batch: list[Any],
    by_id: dict[int, Any],
    newest_id: int | None,
    horizon_dt: datetime,
    limit: int,
) -> tuple[bool, bool]:
    """Обработка одной порции пагинации: возвращает (horizon_reached, limit_reached)."""
    for m in batch:
        mid = getattr(m, "id", None)
        if mid is None or (newest_id is not None and mid > newest_id) or mid in by_id:
            continue
        by_id[mid] = m
        m_dt = _to_utc_dt(getattr(m, "date", None))
        if m_dt and m_dt <= horizon_dt:
            return True, len(by_id) >= limit
        if len(by_id) >= limit:
            return False, True
    return False, len(by_id) >= limit


def refine_channel_history(
    app: Any,
    chat_id: int,
    existing_messages: list[Any],
    *,
    horizon_days: int = 14,
    limit: int = 200,
    scan_started_at: datetime | None = None,
    batch_size: int = 100,
    sleep: Any = None,
    rng: Any = random.uniform,
    log: Callable[[str], None] = print,
) -> tuple[list[Any], list[Publication], dict[str, Any]]:
    """Уточняющий сбор расширенной истории по выбранному каналу (Section 4)."""
    # ponytail: добираем сообщения вглубь истории без сдвига снимка новыми постами
    scan_dt = _to_utc_dt(scan_started_at) or datetime.now(timezone.utc)
    horizon_dt = scan_dt - timedelta(days=horizon_days)

    by_id: dict[int, Any] = {
        m.id: m
        for m in existing_messages
        if getattr(m, "id", None) is not None
    }
    oldest_id = min(by_id.keys()) if by_id else None
    newest_id = max(by_id.keys()) if by_id else None

    horizon_reached = False
    if oldest_id is not None:
        oldest_d = _to_utc_dt(getattr(by_id[oldest_id], "date", None))
        horizon_reached = bool(oldest_d and oldest_d <= horizon_dt)

    limit_reached = len(by_id) >= limit
    history_exhausted = False
    fetch_error: str | None = None

    while not (horizon_reached or limit_reached or history_exhausted):
        to_fetch = min(batch_size, limit - len(by_id))
        batch, err = retry_on_flood(
            lambda tf=to_fetch, oid=oldest_id: list(
                app.get_chat_history(chat_id, limit=tf, offset_id=oid)
            ),
            sleep=sleep,
            rng=rng,
            log=log,
            what=f" при уточнении {chat_id}",
        )
        if err is not None:
            fetch_error = f"{type(err).__name__}: {err}"
            break

        if not batch:
            history_exhausted = True
            break

        h_done, l_done = _process_history_batch(batch, by_id, newest_id, horizon_dt, limit)
        horizon_reached = horizon_reached or h_done
        limit_reached = limit_reached or l_done
        oldest_id = min(by_id.keys()) if by_id else None
        if len(batch) < to_fetch:
            history_exhausted = True

    all_messages = sorted(by_id.values(), key=lambda m: getattr(m, "id", 0), reverse=True)
    pubs = group_media_publications(all_messages)
    coverage = compute_coverage(
        all_messages,
        pubs,
        scan_started_at=scan_dt,
        limit=limit,
        horizon_days=horizon_days,
        error=fetch_error,
    )
    return all_messages, pubs, coverage
