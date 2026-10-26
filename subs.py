"""Channel subscriptions: local quality metrics, indirect review signals, mass unsubscribe."""

import random
import re
from datetime import datetime, timezone
from typing import Any

from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError

from purge import flood_delay

POSTS_WINDOW = 30
ANALYSIS_PAUSE = (1.0, 3.0)
CHAT_PAUSE = (25.0, 75.0)
MIN_POSTS_FOR_CV = 2  # минимум постов для статистики (CV, частота)

# ponytail: пороги ресёрча (Statzen/TGuard/ppc.world, ADR 0007) в одном dict.
# Метрики служат косвенными сигналами для проверки, а не доказательством накрутки.
THRESHOLDS = {
    "err_good": 15.0,  # % views/subs: >=15 живой
    "err_bad": 5.0,  # <5% — низкая вовлечённость
    "err_inflated": 100.0,  # views>subs — аномально высокий охват
    "er_good": 3.0,  # % взаимодействий/views: >=3 отлично
    "er_bad": 0.5,  # <0.5 при живых views — низкий отклик
    "ad_share_good": 0.05,  # доля рекламных постов: <=5% чисто
    "ad_share_bad": 0.25,  # >25% — высокая доля рекламы
    "cv_good": 0.25,  # коэффициент вариации views: живой диапазон 0.2-0.5
    "cv_bad": 0.15,  # подозрительно ровный охват
    "freq_good": 40.0,  # постов/мес: 20-40 оптимум
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


def channel_subscriptions(app: Any) -> list[Any]:
    """Все каналы, на которые подписан аккаунт (каналы уже есть в get_dialogs)."""
    return [
        d.chat
        for d in app.get_dialogs()
        if getattr(d, "chat", None) and d.chat.type == enums.ChatType.CHANNEL
    ]


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


def _reaction_total(message: Any) -> int:
    """Сумма реакций поста: Message.reactions — объект MessageReactions со списком .reactions."""
    wrapper = getattr(message, "reactions", None)
    if wrapper is None:
        return 0
    items = getattr(wrapper, "reactions", None)
    if not items:
        return 0
    try:
        return sum(int(r.count) for r in items)
    except (TypeError, ValueError):
        return 0


def compute_metrics(
    _chat: Any,
    posts: list[Any],
    members: int,
    *,
    created_at: Any = None,
) -> dict[str, Any]:
    """Quality Metrics из дампа постов (ADR 0007)."""
    texts = [(getattr(m, "text", None) or getattr(m, "caption", None) or "") for m in posts]
    raw_views = [getattr(m, "views", None) for m in posts]
    valid_views = [float(v) for v in raw_views if v is not None and float(v) > 0]
    reactions = [_reaction_total(m) for m in posts]
    raw_forwards = [getattr(m, "forwards", None) for m in posts]
    dates = [m.date for m in posts if getattr(m, "date", None)]

    # ponytail: отсутствие данных/знаменателя не симулирует низкий отклик
    err: float | None = None
    if valid_views and members > 0:
        err = round((sum(valid_views) / len(valid_views)) / members * 100, 2)

    er_values: list[float] = []
    for m, r, f in zip(posts, reactions, raw_forwards):
        v = getattr(m, "views", None)
        if v is not None and float(v) > 0:
            f_val = float(f) if f is not None else 0.0
            er_values.append((r + f_val) / float(v) * 100)
    er: float | None = round(sum(er_values) / len(er_values), 2) if er_values else None

    cv: float | None = None
    if len(valid_views) >= MIN_POSTS_FOR_CV:
        cv = round(_coefficient_of_variation(valid_views), 3)

    ad_share = (
        round(sum(bool(AD_MARKERS.search(t)) for t in texts) / len(posts), 3)
        if posts
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
        valid_dates = [_to_utc_dt(m.date) for m in posts if getattr(m, "date", None)]
        ref_dt = max((d for d in valid_dates if d), default=None) or datetime.now(timezone.utc)
        diff_sec = max((ref_dt - c_dt).total_seconds(), 0.0)
        age_days = round(diff_sec / 86400.0, 1)
        age_months = max(age_days / 30.44, 0.1)
        growth_per_month = round(members / age_months, 1)
    elif created_at is not None and hasattr(created_at, "__sub__"):
        try:
            valid_dates = [m.date for m in posts if getattr(m, "date", None)]
            ref_dt = max(valid_dates, default=None)
            if ref_dt is not None:
                diff = ref_dt - created_at
                sec = (
                    diff.total_seconds()
                    if hasattr(diff, "total_seconds")
                    else float(diff) * 86400
                )
                age_days = round(max(sec / 86400.0, 0.0), 1)
                age_months = max(age_days / 30.44, 0.1)
                growth_per_month = round(members / age_months, 1)
        except Exception:
            age_days = None
            growth_per_month = None

    return {
        "err": err,
        "er": er,
        "ad_share": ad_share,
        "cv_views": cv,
        "posts_per_month": posts_per_month,
        "members": members,
        "posts": len(posts),
        "age_days": age_days,
        "growth_per_month": growth_per_month,
    }


def _coefficient_of_variation(values: list[float]) -> float:
    mean = sum(values) / len(values)
    if mean == 0:
        return 1.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return (variance ** 0.5) / mean


def metric_signals(m: dict[str, Any], t: dict[str, Any] | None = None) -> list[str]:
    """Косвенные сигналы метрик (ADR 0007, ADR 0009). Не являются доказательством накрутки."""
    t = t or THRESHOLDS
    signals: list[str] = []

    freq = m.get("posts_per_month")
    if freq is not None and freq > t["freq_bad"]:
        signals.append(f"частое обновление ({freq:.0f} постов/мес)")

    cv = m.get("cv_views")
    if cv is not None and cv < t["cv_bad"]:
        signals.append("подозрительно ровный охват")

    err = m.get("err")
    if err is not None and err > t["err_inflated"]:
        signals.append(f"аномально высокий охват (ERR {err:.0f}%)")

    if err is not None and err < t["err_bad"]:
        signals.append(f"низкая вовлечённость (ERR {err:.1f}%)")

    ad = m.get("ad_share")
    if ad is not None and ad > t["ad_share_bad"]:
        signals.append(f"высокая доля рекламы ({round(ad * 100)}%)")

    age = m.get("age_days")
    if (
        age is not None
        and age >= t["abandoned_years"] * 365.25
        and freq is not None
        and freq < t["abandoned_freq"]
        and err is not None
        and err < t["err_bad"]
    ):
        years = age / 365.25
        signals.append(f"заброшенный канал ({years:.1f} г.)")

    members = m.get("members", 0)
    if (
        age is not None
        and age <= t["bot_farm_days"]
        and members >= t["bot_farm_subs"]
        and err is not None
        and err < t["err_bad"]
    ):
        signals.append(f"резкий прирост аудитории ({members} суб за {int(age)} дн)")

    return signals


def leave_chat(app: Any, chat_id: int, *, sleep, rng=random.uniform, log=print) -> bool:
    while True:
        try:
            app.leave_chat(chat_id)
            return True
        except FloodWait as e:
            delay = flood_delay(e, rng)
            log(f"FloodWait {delay:.0f} с, чат {chat_id}")
            sleep(delay)
        except RPCError as e:
            log(f"Ошибка отписки от чата {chat_id}: {e}")
            return False


def leave_chats(app: Any, chats: list[Any], *, sleep, rng=random.uniform, log=print) -> list[Any]:
    done: list[Any] = []
    for i, chat in enumerate(chats):
        if i:
            delay = rng(*CHAT_PAUSE)
            log(f"Пауза {delay:.1f} с перед чатом {chat.title}")
            sleep(delay)
        if leave_chat(app, chat.id, sleep=sleep, rng=rng, log=log):
            done.append(chat)
            log(f"Отписка выполнена: {chat.title}")
    return done
