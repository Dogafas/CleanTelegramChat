"""Local exact channel overlap analysis (Section 2).

Локальный линейный поиск точных межканальных совпадений по текстам,
пересылкам и адресам первоисточников без внешних сервисов и векторных БД.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Any

from pyrogram.types import Chat, Message, MessageEntity

from subs import Publication, _to_utc_dt

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MIN_DUPLICATE_TEXT_LEN = 25
MIN_DUPLICATE_WORDS = 4
MIN_CHANNELS_TO_COMPARE = 2


@dataclass
class OverlapInput:
    """Вход сравнения: канал и его публикации; строится review.for_overlap()."""

    chat: Chat
    publications: list[Publication]


@dataclass
class _PairContext:
    """Контекст пары каналов внутри общего временного окна."""

    id_a: int
    id_b: int
    chat_a: Chat
    chat_b: Chat
    ids_a: set[int]
    ids_b: set[int]
    map_a: dict[int, Publication]
    map_b: dict[int, Publication]


def normalize_text(text: str) -> str:
    """Консервативная нормализация текста для поиска точных дублей (Section 2)."""
    # ponytail: NFKC и схлопывание пробелов; числа, знаки и отрицания не удаляем
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    norm = re.sub(r"\s+", " ", norm).strip()
    if len(norm) < MIN_DUPLICATE_TEXT_LEN or len(norm.split()) < MIN_DUPLICATE_WORDS:
        return ""
    return norm


def normalize_url(url: str) -> str:
    """Консервативная нормализация URL первоисточника (Section 2)."""
    # ponytail: чистим протокол, порт и хвостовой слэш; query/fragment сохраняем
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return ""
        netloc = parts.netloc.lower()
        if ":" in netloc:
            host, port = netloc.split(":", 1)
            if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
                netloc = host
        path = parts.path
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, parts.fragment))
    except Exception:
        return url.strip()


def extract_urls(text: str, entities: list[MessageEntity] | None = None) -> list[str]:
    """Извлечение и нормализация адресов материалов из текста и Telegram-сущностей."""
    urls: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        norm = normalize_url(match.group(0))
        if norm and norm not in urls:
            urls.append(norm)
    if entities:
        for ent in entities:
            url_prop = getattr(ent, "url", None)
            if url_prop:
                norm = normalize_url(str(url_prop))
                if norm and norm not in urls:
                    urls.append(norm)
    return urls


def post_reference(chat: Chat, item: Message | Publication) -> str:
    """Ссылка на публикацию в Telegram или локальный идентификатор без выдуманных URL."""
    msg_id = getattr(item, "id", None) or 0
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    title = getattr(chat, "title", None) or getattr(chat, "id", "канал")
    return f"[{title} пост #{msg_id}]"


def build_overlap_indices(
    channels_data: list[OverlapInput],
) -> tuple[
    dict[str, set[tuple[int, int]]],
    dict[tuple[int, int], set[tuple[int, int]]],
    dict[str, set[tuple[int, int]]],
]:
    """Линейное построение трёх локальных инвертированных индексов (Section 2)."""
    text_index: dict[str, set[tuple[int, int]]] = {}
    forward_index: dict[tuple[int, int], set[tuple[int, int]]] = {}
    url_index: dict[str, set[tuple[int, int]]] = {}

    for row in channels_data:
        chat_id = row.chat.id
        if chat_id is None:
            continue
        pubs = row.publications

        for pub in pubs:
            pub_id = pub.id
            raw_text = getattr(pub, "text", "")
            norm_t = normalize_text(raw_text)
            if norm_t:
                text_index.setdefault(norm_t, set()).add((chat_id, pub_id))

            fwd_chat = getattr(pub, "forward_from_chat_id", None)
            fwd_msg = getattr(pub, "forward_from_message_id", None)
            if fwd_chat is not None and fwd_msg is not None:
                forward_index.setdefault((fwd_chat, fwd_msg), set()).add((chat_id, pub_id))

            urls = getattr(pub, "urls", None)
            if not urls:
                urls = extract_urls(raw_text)
                pub.urls = urls
            for u in urls:
                url_index.setdefault(u, set()).add((chat_id, pub_id))

    return text_index, forward_index, url_index


def _match_texts(
    ctx: _PairContext,
    text_idx: dict[str, set[tuple[int, int]]],
) -> tuple[set[int], set[int], list[dict[str, Any]]]:
    dup_a: set[int] = set()
    dup_b: set[int] = set()
    matches: list[dict[str, Any]] = []

    for norm_t, entries in text_idx.items():
        a_pubs = [pid for cid, pid in entries if cid == ctx.id_a and pid in ctx.ids_a]
        b_pubs = [pid for cid, pid in entries if cid == ctx.id_b and pid in ctx.ids_b]
        if a_pubs and b_pubs:
            dup_a.update(a_pubs)
            dup_b.update(b_pubs)
            ref_a = post_reference(ctx.chat_a, ctx.map_a[a_pubs[0]])
            ref_b = post_reference(ctx.chat_b, ctx.map_b[b_pubs[0]])
            matches.append({
                "type": "exact_text",
                "ref_a": ref_a,
                "ref_b": ref_b,
                "preview": norm_t[:120],
            })
    return dup_a, dup_b, matches


def _match_forwards(
    ctx: _PairContext,
    fwd_idx: dict[tuple[int, int], set[tuple[int, int]]],
) -> tuple[set[int], set[int], list[dict[str, Any]], list[dict[str, Any]]]:
    dup_a: set[int] = set()
    dup_b: set[int] = set()
    pure_fwd: list[dict[str, Any]] = []
    shared_orig: list[dict[str, Any]] = []

    for fwd_key, entries in fwd_idx.items():
        a_pubs = [pid for cid, pid in entries if cid == ctx.id_a and pid in ctx.ids_a]
        b_pubs = [pid for cid, pid in entries if cid == ctx.id_b and pid in ctx.ids_b]
        if a_pubs and b_pubs:
            p_a = ctx.map_a[a_pubs[0]]
            p_b = ctx.map_b[b_pubs[0]]
            ref_a = post_reference(ctx.chat_a, p_a)
            ref_b = post_reference(ctx.chat_b, p_b)
            t_a = normalize_text(getattr(p_a, "text", ""))
            t_b = normalize_text(getattr(p_b, "text", ""))
            if t_a == t_b:
                dup_a.update(a_pubs)
                dup_b.update(b_pubs)
                pure_fwd.append({
                    "type": "pure_forward",
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                    "origin": fwd_key,
                })
            else:
                shared_orig.append({
                    "type": "shared_origin",
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                    "origin": fwd_key,
                })
    return dup_a, dup_b, pure_fwd, shared_orig


def _match_urls(
    ctx: _PairContext,
    dup_a: set[int],
    dup_b: set[int],
    url_idx: dict[str, set[tuple[int, int]]],
) -> list[dict[str, Any]]:
    shared_urls: list[dict[str, Any]] = []
    for u, entries in url_idx.items():
        a_pubs = [pid for cid, pid in entries if cid == ctx.id_a and pid in ctx.ids_a]
        b_pubs = [pid for cid, pid in entries if cid == ctx.id_b and pid in ctx.ids_b]
        if a_pubs and b_pubs:
            un_dup_a = [pid for pid in a_pubs if pid not in dup_a]
            un_dup_b = [pid for pid in b_pubs if pid not in dup_b]
            if un_dup_a and un_dup_b:
                shared_urls.append({
                    "type": "shared_url",
                    "url": u,
                    "ref_a": post_reference(ctx.chat_a, ctx.map_a[un_dup_a[0]]),
                    "ref_b": post_reference(ctx.chat_b, ctx.map_b[un_dup_b[0]]),
                })
    return shared_urls


def _compare_channel_pair(
    row_a: OverlapInput,
    row_b: OverlapInput,
    text_idx: dict[str, set[tuple[int, int]]],
    fwd_idx: dict[tuple[int, int], set[tuple[int, int]]],
    url_idx: dict[str, set[tuple[int, int]]],
) -> dict[str, Any]:
    """Сравнение пары каналов в границах общего временного окна (Section 2)."""
    chat_a = row_a.chat
    chat_b = row_b.chat
    id_a, id_b = chat_a.id, chat_b.id
    if id_a is None or id_b is None:
        return {
            "chat_a": chat_a, "chat_b": chat_b, "has_comparable_window": False,
            "status": "incomparable_windows", "reason": "канал без id",
            "overlap_rate_a": 0.0, "overlap_rate_b": 0.0,
        }

    dates_a = [_to_utc_dt(p.date) for p in row_a.publications if getattr(p, "date", None)]
    dates_a = [d for d in dates_a if d is not None]
    dates_b = [_to_utc_dt(p.date) for p in row_b.publications if getattr(p, "date", None)]
    dates_b = [d for d in dates_b if d is not None]

    if not dates_a or not dates_b:
        return {
            "chat_a": chat_a,
            "chat_b": chat_b,
            "has_comparable_window": False,
            "status": "incomparable_windows",
            "reason": "нет дат для определения сопоставимого окна",
            "overlap_rate_a": 0.0,
            "overlap_rate_b": 0.0,
        }

    w_start = max(min(dates_a), min(dates_b))
    w_end = min(max(dates_a), max(dates_b))
    if w_start > w_end:
        return {
            "chat_a": chat_a, "chat_b": chat_b, "has_comparable_window": False,
            "status": "incomparable_windows", "reason": "временные окна публикаций не пересекаются",
            "overlap_rate_a": 0.0, "overlap_rate_b": 0.0,
        }

    in_win_a = [
        p for p in row_a.publications
        if (pub_dt := _to_utc_dt(p.date)) and w_start <= pub_dt <= w_end
    ]
    in_win_b = [
        p for p in row_b.publications
        if (pub_dt := _to_utc_dt(p.date)) and w_start <= pub_dt <= w_end
    ]
    count_a, count_b = len(in_win_a), len(in_win_b)

    if count_a == 0 or count_b == 0:
        return {
            "chat_a": chat_a,
            "chat_b": chat_b,
            "has_comparable_window": False,
            "status": "incomparable_windows",
            "reason": "в общем окне нет публикаций одного из каналов",
            "overlap_rate_a": 0.0,
            "overlap_rate_b": 0.0,
        }

    ctx = _PairContext(
        id_a=id_a,
        id_b=id_b,
        chat_a=chat_a,
        chat_b=chat_b,
        ids_a={p.id for p in in_win_a},
        ids_b={p.id for p in in_win_b},
        map_a={p.id: p for p in in_win_a},
        map_b={p.id: p for p in in_win_b},
    )

    dup_ta, dup_tb, text_matches = _match_texts(ctx, text_idx)
    dup_fa, dup_fb, pure_fwd, shared_orig = _match_forwards(ctx, fwd_idx)
    dup_a = dup_ta | dup_fa
    dup_b = dup_tb | dup_fb
    shared_urls = _match_urls(ctx, dup_a, dup_b, url_idx)

    return {
        "chat_a": chat_a,
        "chat_b": chat_b,
        "has_comparable_window": True,
        "status": "compared",
        "window_start": w_start,
        "window_end": w_end,
        "count_a_in_window": count_a,
        "count_b_in_window": count_b,
        "dup_count_a": len(dup_a),
        "dup_count_b": len(dup_b),
        "overlap_rate_a": round(len(dup_a) / count_a, 3),
        "overlap_rate_b": round(len(dup_b) / count_b, 3),
        "unmatched_a": count_a - len(dup_a),
        "unmatched_b": count_b - len(dup_b),
        "text_matches": text_matches,
        "pure_forwards": pure_fwd,
        "shared_origins": shared_orig,
        "shared_urls": shared_urls,
    }


def find_channel_overlaps(
    channels_data: list[OverlapInput],
) -> list[dict[str, Any]]:
    """Поиск пар каналов с пересечениями материалов за сопоставимое окно (Section 2)."""
    if len(channels_data) < MIN_CHANNELS_TO_COMPARE:
        return []

    text_idx, fwd_idx, url_idx = build_overlap_indices(channels_data)
    by_chat_id = {row.chat.id: row for row in channels_data if row.chat.id is not None}

    # Находим пары каналов, имеющие общие записи в индексах
    candidate_pairs: set[tuple[int, int]] = set()

    for idx in (text_idx, fwd_idx, url_idx):
        for entries in idx.values():
            if len(entries) > 1:
                chat_ids = sorted({cid for cid, _ in entries})
                for i in range(len(chat_ids)):
                    for j in range(i + 1, len(chat_ids)):
                        candidate_pairs.add((chat_ids[i], chat_ids[j]))

    overlap_results: list[dict[str, Any]] = []

    for id_a, id_b in sorted(candidate_pairs):
        row_a = by_chat_id[id_a]
        row_b = by_chat_id[id_b]
        res = _compare_channel_pair(row_a, row_b, text_idx, fwd_idx, url_idx)
        overlap_results.append(res)

    return overlap_results


def check_duplicate_group_unsubscribes(
    selected_chat_ids: set[int],
    overlap_results: list[dict[str, Any]],
    threshold: float = 0.5,
) -> list[str]:
    """Предупреждение перед отпиской при выборе всех источников дублирующей темы (Section 2)."""
    warnings: list[str] = []
    seen_pairs: set[tuple[int, int]] = set()

    for item in overlap_results:
        if not item.get("has_comparable_window"):
            continue
        rate_a = item.get("overlap_rate_a", 0.0)
        rate_b = item.get("overlap_rate_b", 0.0)
        # Если взаимное или одностороннее перекрытие существенно
        if rate_a >= threshold and rate_b >= threshold:
            cid_a = item["chat_a"].id
            cid_b = item["chat_b"].id
            pair = (min(cid_a, cid_b), max(cid_a, cid_b))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            if cid_a in selected_chat_ids and cid_b in selected_chat_ids:
                t_a = getattr(item["chat_a"], "title", None) or str(cid_a)
                t_b = getattr(item["chat_b"], "title", None) or str(cid_b)
                pct_a = int(rate_a * 100)
                pct_b = int(rate_b * 100)
                warnings.append(
                    f"Вы отписываетесь от всех источников темы: «{t_a}» ({pct_a}% дублей) "
                    f"и «{t_b}» ({pct_b}% дублей). Наблюдаемые копии будут потеряны."
                )

    return warnings
