"""Sort chats: классификация каналов и групп аккаунта по категориям и создание вкладок."""

import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError

from catalog import _iter_archive
from session import prompt_sessions, restore_client_loop, telegram_client
from subs import ANALYSIS_PAUSE, POSTS_WINDOW, fetch_posts
from ui import ask_chats, ask_confirm
from verdict import _post_text, load_typesafe_env

RESERVED = ("без категории", "не оценено")
TITLE_MAX = 12
NOUL_YES = 0.5
FALLBACK_FOLDERS = 10
FALLBACK_CHATS = 100
PREMIUM_FOLDERS = 20
PREMIUM_CHATS = 200


def categories_path(
    env_file: Path | None = None,
    environ: dict[str, str] | None = None,
    root: Path | None = None,
) -> Path:
    base = Path(__file__).resolve().parent if root is None else Path(root)
    env = os.environ if environ is None else environ
    raw = env.get("CATEGORIES_FILE", "").strip()
    if not raw:
        path_env = env_file or (base / ".env")
        if path_env.is_file():
            from session import _parse_env  # noqa: PLC0415

            data = _parse_env(path_env.read_text(encoding="utf-8"))
            raw = data.get("CATEGORIES_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else base / p
    return base / "categories.txt"


def load_categories(path: Path) -> list[str]:
    if not path.is_file():
        raise ValueError(f"Файл категорий не найден: {path}")
    content = path.read_text(encoding="utf-8")
    cats: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        name = line.strip()
        if not name:
            continue
        if name in RESERVED:
            raise ValueError(f"Зарезервированное имя категории: {name}")
        if name not in seen:
            seen.add(name)
            cats.append(name)
    if not cats:
        raise ValueError(f"Файл категорий пуст: {path}")
    return cats


def split_assignment(scores: dict[str, Any], categories: list[str]) -> list[str]:
    return [name for name in categories if float(scores[name]) >= NOUL_YES]


def pick_peers(rows: list[tuple[float, int, Any]], limit: int) -> tuple[list[Any], list[Any]]:
    sorted_rows = sorted(rows, key=lambda r: (-r[0], r[1]))
    k = max(0, limit)
    return [r[2] for r in sorted_rows[:k]], [r[2] for r in sorted_rows[k:]]

def _chat_line(chat: Any) -> str:
    title = getattr(chat, "title", None) or "без названия"
    return f"{title} \u2014 {chat.id}"


def render_sort_file(
    categories: list[str],
    by_category: dict[str, list[Any]],
    uncategorized: list[Any],
    unevaluated: list[Any],
) -> str:
    # порядок — обход диалогов (возрастающий index), сортировка не нужна
    sections: list[str] = []
    for cat in categories:
        lines = [f"## {cat}"]
        for _score, _index, chat in by_category.get(cat, []):
            lines.append(_chat_line(chat))
        sections.append("\n".join(lines))

    if uncategorized:
        lines = ["## без категории"]
        for _score, _index, chat in uncategorized:
            lines.append(_chat_line(chat))
        sections.append("\n".join(lines))

    if unevaluated:
        lines = ["## не оценено"]
        for _score, _index, chat in unevaluated:
            lines.append(_chat_line(chat))
        sections.append("\n".join(lines))

    return "\n\n".join(sections) + "\n"


def load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        if "categories_hash" not in data or "entries" not in data:
            return {}
        if not isinstance(data["entries"], dict):
            return {}
        return data
    except Exception:
        return {}


def save_cache(path: Path, categories_hash: str, entries: dict[str, Any]) -> None:
    data = {
        "categories_hash": categories_hash,
        "entries": entries,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sortable_dialogs(
    app: Any,
    log: Callable[[str], None] = logging.info,
) -> list[Any] | None:
    try:
        main_dialogs = list(app.get_dialogs())
    except RPCError as e:
        log(f"Список диалогов не прочитан: {e}")
        return None

    archive_dialogs: list[Any] = []
    try:
        archive_dialogs = list(_iter_archive(app))
    except RPCError as e:
        log(f"Архив не прочитан: {e}")

    result: list[Any] = []
    seen_ids: set[int] = set()

    for item in main_dialogs + archive_dialogs:
        chat = getattr(item, "chat", item)
        if chat is None or not hasattr(chat, "id") or not hasattr(chat, "type"):
            continue
        if (
            chat.type in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP, enums.ChatType.GROUP)
            and chat.id not in seen_ids
        ):
            seen_ids.add(chat.id)
            result.append(chat)

    return result


def dialog_description(app: Any, chat: Any) -> str:
    desc = getattr(chat, "description", None)
    if desc:
        return str(desc)
    try:
        full = app.get_chat(chat.id)
        return str(getattr(full, "description", "") or "")
    except Exception:
        return ""


def history_texts(
    app: Any,
    chat_id: int,
    *,
    sleep: Callable[[float], Any] = time.sleep,
    rng: Callable[..., float] = random.uniform,
) -> list[str] | None:
    res = None
    try:
        try:
            posts = fetch_posts(app, chat_id, POSTS_WINDOW)
        except FloodWait as e:
            sleep(float(e.value))
            posts = fetch_posts(app, chat_id, POSTS_WINDOW)
        res = [txt for m in posts if (txt := _post_text(m).strip())]
    except Exception:
        res = None
    finally:
        sleep(rng(*ANALYSIS_PAUSE))
    return res


def make_questions(categories: list[str]) -> dict[str, Any]:
    from typesafe_sdk import Noul  # noqa: PLC0415

    return {
        f"c{i}": Noul(
            instructions=(
                f"Этот Telegram-диалог относится к категории «{name}»? "
                "Если history_read false, история недоступна — решай по названию, "
                "username и описанию, не считай пустые posts признаком мёртвого диалога."
            )
        )
        for i, name in enumerate(categories)
    }


def evaluate_dialog(
    chat: Any,
    description: str,
    texts: list[str] | None,
    questions: dict[str, Any],
    categories: list[str],
    *,
    evaluate_fn: Any = None,
) -> dict[str, float] | None:
    state = {
        "title": getattr(chat, "title", None) or "без названия",
        "username": getattr(chat, "username", None) or "",
        "description": description,
        "posts": texts if texts is not None else [],
        "history_read": texts is not None,
    }
    try:
        if evaluate_fn is None:
            from typesafe_sdk import TypeSafeClient  # noqa: PLC0415

            load_typesafe_env()
            with TypeSafeClient() as client:
                response = client.system_one(state=state, questions=questions)
        else:
            response = evaluate_fn(state, questions)

        scores: dict[str, float] = {}
        for i, name in enumerate(categories):
            key = f"c{i}"
            ans = response.answers[key]
            noul_val = getattr(ans, "noul", None)
            if noul_val is None:
                raise ValueError(f"Ответ {key} не содержит noul")
            scores[name] = float(noul_val)
        return scores
    except Exception:
        return None


def classify_dialogs(
    app: Any,
    dialogs: list[Any],
    categories: list[str],
    questions: dict[str, Any],
    cached_entries: dict[str, Any],
    *,
    evaluate_fn: Any,
    sleep: Callable[[float], Any],
    rng: Callable[..., float],
    log: Callable[[str], None],
) -> tuple[
    dict[str, list[tuple[float, int, Any]]],
    list[tuple[float, int, Any]],
    list[tuple[float, int, Any]],
    dict[str, Any],
]:
    by_category: dict[str, list[tuple[float, int, Any]]] = {c: [] for c in categories}
    uncategorized: list[tuple[float, int, Any]] = []
    unevaluated: list[tuple[float, int, Any]] = []
    current_run_entries: dict[str, Any] = {}

    for index, chat in enumerate(dialogs):
        title = getattr(chat, "title", None) or "без названия"
        log(f"Оценка: {title} \u2014 {chat.id}")

        desc = dialog_description(app, chat)
        texts = history_texts(app, chat.id, sleep=sleep, rng=rng)

        chat_id_str = str(chat.id)
        scores: dict[str, float] | None = None
        texts_hash = ""

        if texts is not None:
            texts_hash = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()
            cached = cached_entries.get(chat_id_str)
            if (
                cached
                and isinstance(cached, dict)
                and cached.get("texts_hash") == texts_hash
                and isinstance(cached.get("scores"), dict)
            ):
                try:
                    scores = {cat: float(cached["scores"][cat]) for cat in categories}
                    current_run_entries[chat_id_str] = {
                        "texts_hash": texts_hash,
                        "scores": scores,
                    }
                except (KeyError, ValueError, TypeError):
                    scores = None

        if scores is None:
            scores = evaluate_dialog(
                chat, desc, texts, questions, categories, evaluate_fn=evaluate_fn
            )
            if scores is not None and texts is not None:
                current_run_entries[chat_id_str] = {
                    "texts_hash": texts_hash,
                    "scores": scores,
                }

        if scores is None:
            unevaluated.append((0.0, index, chat))
            continue

        try:
            assigned = split_assignment(scores, categories)
            if assigned:
                for cat in assigned:
                    by_category[cat].append((scores[cat], index, chat))
            else:
                uncategorized.append((0.0, index, chat))
        except Exception:
            unevaluated.append((0.0, index, chat))

    return by_category, uncategorized, unevaluated, current_run_entries


def folder_limits(
    app: Any,
    log: Callable[[str], None] = logging.info,
) -> tuple[int, int]:
    try:
        from pyrogram import raw  # noqa: PLC0415

        me = app.get_me()
        config = app.invoke(raw.functions.help.GetAppConfig(hash=0))
        if not isinstance(config, raw.types.JsonObject):
            log("Лимиты вкладок не прочитаны, берём 10 папок и 100 чатов.")
            return FALLBACK_FOLDERS, FALLBACK_CHATS

        is_prem = bool(getattr(me, "is_premium", False))
        cfg = {
            item.key: int(item.value.value)
            for item in config.value
            if getattr(item, "key", None)
            and isinstance(getattr(item, "value", None), raw.types.JsonNumber)
        }
        chat_key = (
            "dialog_filters_chats_limit_premium"
            if is_prem
            else "dialog_filters_chats_limit_default"
        )
        folders = cfg.get(
            "dialog_filters_limit_premium" if is_prem else "dialog_filters_limit_default",
            PREMIUM_FOLDERS if is_prem else FALLBACK_FOLDERS,
        )
        chats = cfg.get(chat_key, PREMIUM_CHATS if is_prem else FALLBACK_CHATS)
        log(f"Лимиты вкладок: {folders} папок, {chats} чатов.")
        return folders, chats
    except Exception:
        log("Лимиты вкладок не прочитаны, берём 10 папок и 100 чатов.")
        return FALLBACK_FOLDERS, FALLBACK_CHATS


def existing_filters(
    app: Any,
    log: Callable[[str], None] = logging.info,
) -> list[Any] | None:
    try:
        from pyrogram import raw  # noqa: PLC0415

        res = app.invoke(raw.functions.messages.GetDialogFilters())
        if hasattr(res, "filters") and isinstance(res.filters, list):
            return res.filters
        if isinstance(res, list):
            return res
        msg = (
            f"Список вкладок не прочитан: неожиданный тип {type(res).__name__}. "
            "Вкладки не создаём."
        )
        log(msg)
        return None
    except Exception as e:
        log(f"Список вкладок не прочитан: {e}. Вкладки не создаём.")
        return None


def sort_account(
    session_name: str,
    *,
    categories: list[str],
    client: Callable[..., Any] = telegram_client,
    ask: Callable[..., Any] = ask_chats,
    confirm: Callable[..., Any] = ask_confirm,
    evaluate_fn: Any = None,
    restore: Callable[[Any], None] = restore_client_loop,
    sleep: Callable[[float], Any] = time.sleep,
    rng: Callable[..., float] = random.uniform,
    log: Callable[[str], None] = logging.info,
    root: Path | str | None = None,
) -> None:
    root_path = Path(__file__).resolve().parent if root is None else Path(root)

    if evaluate_fn is None:
        load_typesafe_env()
        if not os.environ.get("TYPESAFE_API_KEY"):
            log("TYPESAFE_API_KEY не задан. Классификация остановлена.")
            return

    categories_hash = hashlib.sha256("\n".join(categories).encode("utf-8")).hexdigest()
    cache_path = root_path / f"sort-{session_name}.cache.json"
    loaded_cache = load_cache(cache_path)
    cached_entries: dict[str, Any] = {}
    if loaded_cache.get("categories_hash") == categories_hash:
        cached_entries = loaded_cache.get("entries", {})

    questions = make_questions(categories)

    with client(session_name=session_name) as app:
        dialogs = sortable_dialogs(app, log=log)
        if dialogs is None:
            return

        by_category, uncategorized, unevaluated, current_run_entries = classify_dialogs(
            app,
            dialogs,
            categories,
            questions,
            cached_entries,
            evaluate_fn=evaluate_fn,
            sleep=sleep,
            rng=rng,
            log=log,
        )

        md_path = root_path / f"sort-{session_name}.md"
        md_content = render_sort_file(categories, by_category, uncategorized, unevaluated)
        md_path.write_text(md_content, encoding="utf-8")
        log(f"Классификация записана: {md_path}")

        save_cache(cache_path, categories_hash, current_run_entries)

        folder_limit, max_chats = folder_limits(app, log=log)
        filters = existing_filters(app, log=log)
        if filters is None:
            return

        selected = choose_folders(
            app,
            categories,
            by_category,
            folder_limit,
            filters,
            ask=ask,
            confirm=confirm,
            restore=restore,
            log=log,
        )
        if selected:
            create_folders(app, by_category, max_chats, filters, selected, log=log)


def _filter_title(f: Any) -> str:
    title = getattr(f, "title", None)
    if isinstance(title, str):
        return title.strip()
    if title is not None and hasattr(title, "text") and isinstance(title.text, str):
        return title.text.strip()
    return ""


def choose_folders(
    app: Any,
    categories: list[str],
    by_category: dict[str, list[tuple[float, int, Any]]],
    folder_limit: int,
    filters: list[Any],
    *,
    ask: Callable[..., Any],
    confirm: Callable[..., Any],
    restore: Callable[[Any], None],
    log: Callable[[str], None],
) -> list[str] | None:
    free = max(0, folder_limit - len(filters))
    existing_titles = {_filter_title(f) for f in filters if _filter_title(f)}

    checkbox_items: list[tuple[str, str, bool]] = []
    for name in categories:
        count = len(by_category.get(name, []))
        if len(name) > TITLE_MAX:
            log(f"«{name}» длиннее 12 символов — только файл.")
            continue
        # ponytail: existing folders never updated; delete the tab in Telegram to rebuild
        if name in existing_titles:
            log(f"Вкладка «{name}» уже есть — только файл.")
            continue
        if count > 0:
            checkbox_items.append((f"{name} ({count})", name, False))

    if free == 0:
        log("Свободных слотов 0. Вкладки не создаём.")
        return None

    if not checkbox_items:
        return None

    msg = f"Отметьте категории для вкладок. Свободных слотов: {free}"
    while True:
        try:
            selected = ask(checkbox_items, message=msg)
        except TypeError:
            selected = ask(checkbox_items)

        if not selected:
            restore(app)
            log("Вкладки не выбраны.")
            return None

        if len(selected) > free:
            msg = (
                f"Свободных слотов {free}, отмечено {len(selected)}. "
                f"Отметьте не больше {free}."
            )
            checkbox_items = [
                (label, name, name in selected)
                for (label, name, _) in checkbox_items
            ]
            continue
        break

    ok = confirm(f"Создать {len(selected)} вкладок?")
    restore(app)
    if ok is not True:
        log("Вкладки не созданы.")
        return None

    return selected


def create_folders(
    app: Any,
    by_category: dict[str, list[tuple[float, int, Any]]],
    max_chats: int,
    filters: list[Any],
    selected: list[str],
    *,
    log: Callable[[str], None],
) -> None:
    from pyrogram import raw  # noqa: PLC0415

    used_ids = {f.id for f in filters if hasattr(f, "id") and isinstance(f.id, int)}
    for name in selected:
        folder_id = None
        for cid in range(2, 256):
            if cid not in used_ids:
                folder_id = cid
                used_ids.add(folder_id)
                break
        if folder_id is None:
            log("Нет свободного id вкладки.")
            break

        rows = by_category.get(name, [])
        kept_chats, rest_chats = pick_peers(rows, max_chats)
        resolved_peers = []
        unresolved_count = 0
        for chat in kept_chats:
            try:
                peer = app.resolve_peer(chat.id)
                resolved_peers.append(peer)
            except Exception as e:
                title = getattr(chat, "title", None) or "без названия"
                log(f"Не удалось добавить {title} — {chat.id} во вкладку «{name}»: {e}")
                unresolved_count += 1

        if not resolved_peers:
            log(f"Вкладка «{name}» пустая — не создаём.")
            continue

        try:
            dialog_filter: Any = raw.types.DialogFilter(
                id=folder_id,
                title=raw.types.TextWithEntities(text=name, entities=[]),
                pinned_peers=[],
                include_peers=resolved_peers,
                exclude_peers=[],
            )
            app.invoke(
                raw.functions.messages.UpdateDialogFilter(
                    id=folder_id,
                    filter=dialog_filter,
                ),
                sleep_threshold=60,
            )
            kept_count = len(resolved_peers)
            rest_count = len(rest_chats) + unresolved_count
            log(f"«{name}»: во вкладку {kept_count}, только файл {rest_count}.")
        except RPCError as e:
            log(f"Вкладка «{name}» не создана: {e}")

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("sort.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    try:
        categories = load_categories(categories_path())
    except ValueError as e:
        logging.info(str(e))
        return

    load_typesafe_env()
    if not os.environ.get("TYPESAFE_API_KEY"):
        logging.info("TYPESAFE_API_KEY не задан. Классификация остановлена.")
        return

    sessions = prompt_sessions(allow_all=False)
    if not sessions:
        logging.info("Сессии не выбраны. Завершение работы.")
        return
    if len(sessions) > 1:
        logging.info("Нужна одна сессия.")
        return

    sort_account(sessions[0], categories=categories)


if __name__ == "__main__":
    main()
