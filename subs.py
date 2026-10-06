"""Channel subscriptions: local quality scoring, candidates, mass unsubscribe."""

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
CANDIDATE_THRESHOLD = 60  # суммарный скор ниже — кандидат на отписку
MIN_POSTS_FOR_CV = 2  # минимум постов для статистики (CV, частота)

# ponytail: пороги ресёрча (Statzen/TGuard/ppc.world, ADR 0007) в одном dict,
# крути здесь — в конфиг выносить, только если попросят пользователи
THRESHOLDS = {
    "err_good": 15.0,  # % views/subs: >=15 живой (Statzen: 15-30 новости, 30-55 лайфстайл)
    "err_bad": 5.0,  # <5% — мёртвая/накрученная база (Statzen fake-check)
    "err_inflated": 100.0,  # views>subs — докрутка (TGuard), жёсткий флаг
    "er_good": 3.0,  # % взаимодействий/views: >=3 отлично (TGuard: 0.5-3 норма)
    "er_bad": 0.5,  # <0.5 при живых views — накрутка
    "ad_share_good": 0.05,  # доля рекламных постов: <=5% чисто
    "ad_share_bad": 0.25,  # >25% — витрина (Statzen: реклама чаще 1:4 убивает канал)
    "link_share_good": 0.10,  # доля постов с внешними ссылками
    "link_share_bad": 0.40,
    "cv_good": 0.25,  # коэффициент вариации views: живой диапазон 0.2-0.5
    "cv_bad": 0.15,  # подозрительно ровный охват — жёсткий флаг накрутки
    "fwd_zero_good": 0.20,  # доля постов без пересылок
    "fwd_zero_bad": 0.80,  # константа-ноль пересылок — мёртвая аудитория
    "freq_good": 40.0,  # постов/мес: 20-40 оптимум (Statzen)
    "freq_bad": 60.0,  # >60 — шумовой поток
    "bot_farm_subs": 100_000,  # >= 100k подписчиков
    "bot_farm_days": 60.0,  # <= 2 месяцев (60 дней)
    "abandoned_years": 7.0,  # >= 7 лет
    "abandoned_freq": 2.0,  # постов/мес < 2 (мало постов)
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
        for d in app.get_dialogs(limit=1000)
        if d.chat is not None and d.chat.type == enums.ChatType.CHANNEL
    ]


def fetch_posts(app: Any, chat_id: int, limit: int = POSTS_WINDOW) -> list[Any]:
    return list(app.get_chat_history(chat_id, limit=limit))

def fetch_creation_date(app: Any, chat_id: int) -> Any:
    """Дата создания канала: первое сообщение (id=1)."""
    try:
        msg = app.get_messages(chat_id, 1)
    except Exception:
        return None
    if msg and getattr(msg, "date", None) and not getattr(msg, "empty", False):
        return msg.date
    return None


def _to_utc_dt(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    return None

def _num(value: Any) -> float:
    return float(value) if value else 0.0


def _reaction_total(message: Any) -> int:
    """Сумма реакций поста: Message.reactions — объект MessageReactions со списком .reactions."""
    wrapper = getattr(message, "reactions", None)
    if wrapper is None:
        return 0
    items = getattr(wrapper, "reactions", wrapper)
    try:
        return sum(int(r.count) for r in items)
    except TypeError:
        return 0


def compute_metrics(
    chat: Any,
    posts: list[Any],
    members: int,
    *,
    created_at: Any = None,
) -> dict[str, Any]:
    """Quality Metrics из дампа постов (ADR 0007)."""
    texts = [(getattr(m, "text", None) or getattr(m, "caption", None) or "") for m in posts]
    views = [_num(getattr(m, "views", None)) for m in posts]
    reactions = [_reaction_total(m) for m in posts]
    forwards = [_num(getattr(m, "forwards", None)) for m in posts]
    dates = [m.date for m in posts if getattr(m, "date", None)]

    with_views = [v for v in views if v > 0]
    err = (sum(views) / len(views)) / members * 100 if with_views and members else 0.0
    er_values = [
        (r + f) / v * 100 for v, r, f in zip(views, reactions, forwards) if v > 0
    ]
    er = sum(er_values) / len(er_values) if er_values else 0.0
    cv = _coefficient_of_variation(with_views) if len(with_views) >= MIN_POSTS_FOR_CV else 1.0
    ad_share = sum(bool(AD_MARKERS.search(t)) for t in texts) / len(posts) if posts else 0.0
    link_share = (
        sum(_has_outlink(m) for m in posts) / len(posts) if posts else 0.0
    )
    fwd_zero_share = sum(f == 0 for f in forwards) / len(posts) if posts else 1.0
    posts_per_month = 0.0
    if len(dates) >= MIN_POSTS_FOR_CV:
        span_days = max((max(dates) - min(dates)).total_seconds() / 86400, 0.5)
        posts_per_month = len(dates) / span_days * 30
    title = getattr(chat, "title", "") or ""
    title_dots = bool(re.search(r"[•·]", title))

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
        "err": round(err, 2),
        "er": round(er, 2),
        "ad_share": round(ad_share, 3),
        "link_share": round(link_share, 3),
        "cv_views": round(cv, 3),
        "fwd_zero_share": round(fwd_zero_share, 3),
        "posts_per_month": round(posts_per_month, 1),
        "title_dots": title_dots,
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


def _has_outlink(message: Any) -> bool:
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    entities = (getattr(message, "entities", None) or []) + (
        getattr(message, "caption_entities", None) or []
    )
    from pyrogram import enums  # noqa: PLC0415

    for e in entities:
        if e.type in (enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK):
            return True
    return "http" in text or "t.me/" in text


def _lerp_score(value: float, good: float, bad: float) -> int:
    """100 при значении good, 0 при bad, линейно между (good может быть меньше bad)."""
    if good == bad:
        return 100 if value == good else 0
    t = (value - good) / (bad - good)
    return round(max(0.0, min(1.0, 1.0 - t)) * 100)


def score_channel(m: dict[str, Any], t: dict[str, Any] | None = None) -> dict[str, Any]:
    """Quality Score 0-100: взвешенная сумма скоров метрик + жёсткие флаги накрутки."""
    t = t or THRESHOLDS
    scores = {
        "err": _lerp_score(m["err"], t["err_good"], t["err_bad"]),
        "er": _lerp_score(m["er"], t["er_good"], t["er_bad"]),
        "ad_share": _lerp_score(m["ad_share"], t["ad_share_good"], t["ad_share_bad"]),
        "link_share": _lerp_score(m["link_share"], t["link_share_good"], t["link_share_bad"]),
        "cv_views": _lerp_score(m["cv_views"], t["cv_good"], t["cv_bad"]),
        "fwd_zero_share": _lerp_score(m["fwd_zero_share"], t["fwd_zero_good"], t["fwd_zero_bad"]),
        "posts_per_month": _lerp_score(m["posts_per_month"], t["freq_good"], t["freq_bad"]),
        "title_dots": 0 if m["title_dots"] else 100,
    }
    weights = {
        "err": 20,
        "er": 20,
        "ad_share": 15,
        "cv_views": 15,
        "link_share": 10,
        "fwd_zero_share": 10,
        "posts_per_month": 5,
        "title_dots": 5,
    }
    score = sum(scores[k] * w for k, w in weights.items()) // sum(weights.values())
    flags = []
    if m["err"] > t["err_inflated"]:
        flags.append(f"ERR>{t['err_inflated']:.0f}%: докрутка просмотров")
    if m["cv_views"] < t["cv_bad"]:
        flags.append(f"CV views<{t['cv_bad']}: подозрительно ровный охват")
    age = m.get("age_days")
    if (
        age is not None
        and age <= t["bot_farm_days"]
        and m["members"] >= t["bot_farm_subs"]
        and m["err"] < t["err_bad"]
    ):
        flags.append(
            f"накрутка ботами ({m['members']} суб за {int(age)} дн при ERR < {t['err_bad']}%)"
        )
    if (
        age is not None
        and age >= t["abandoned_years"] * 365.25
        and m["posts_per_month"] < t["abandoned_freq"]
        and m["err"] < t["err_bad"]
    ):
        years = age / 365.25
        flags.append(
            "заброшенный канал "
            f"(возраст {years:.1f} г., {m['posts_per_month']:.1f} постов/мес "
            f"при ERR < {t['err_bad']}%)"
        )
    if flags:
        score = min(score, 30)
    return {"score": score, "flags": flags, "scores": scores}


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
