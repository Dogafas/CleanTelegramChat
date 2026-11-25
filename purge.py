"""Message purge module: human-paced message deletion with FloodWait backoff."""

import random
from collections.abc import Callable, Iterable
from typing import Any

from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Chat

from session import sync_list

BATCH_LIMIT = 100
BATCH_PAUSE = (8.0, 25.0)
CHAT_PAUSE = (25.0, 75.0)
FLOOD_JITTER = (1.0, 3.0)
_FLOOD_FALLBACK = 5  # сервер прислал нечисловой value — консервативная пауза


def flood_delay(e: FloodWait, rng: Callable[..., float] = random.uniform) -> float:
    """Задержка FloodWait: секунды сервера + джиттер."""
    value = e.value if isinstance(e.value, (int, str)) else _FLOOD_FALLBACK
    return int(value) + rng(*FLOOD_JITTER)


def retry_on_flood(
    op: Callable[[], T],
    *,
    sleep: Callable[[float], None] | None = None,
    rng: Callable[..., float] = random.uniform,
    max_attempts: int = 5,
    log: Callable[[str], None] = print,
    what: str = "",
) -> tuple[T | None, Exception | None]:
    """Выполнить op() с повтором при FloodWait.

    Возвращает (value, error):
      - успех: (value, None)
      - ошибка / исчерпаны попытки: (None, exc)
    Если sleep is None, FloodWait не ждёт и сразу возвращается как ошибка.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return op(), None
        except FloodWait as e:
            if sleep is None or attempt >= max_attempts:
                return None, e
            delay = flood_delay(e, rng)
            log(f"FloodWait {delay:.0f} с{what}")
            sleep(delay)
        except Exception as e:
            return None, e
    return None, RuntimeError(f"retry_on_flood: нет попыток (max_attempts={max_attempts})")


def purge_chat(
    app: Any,
    chat_id: int,
    *,
    sleep: Callable[[float], None],
    rng: Callable[..., float] = random.uniform,
    log: Callable[[str], None] = print,
) -> None:
    previous: list[int] | None = None
    while True:
        try:
            messages = sync_list(
                app.search_messages(chat_id, from_user="me", limit=BATCH_LIMIT)
            )
        except FloodWait as e:
            delay = flood_delay(e, rng)
            log(f"FloodWait {delay:.0f} с, чат {chat_id}")
            sleep(delay)
            continue
        except RPCError as e:
            log(f"Ошибка поиска в чате {chat_id}: {e}")
            return

        if not messages:
            return

        ids = [m.id for m in messages]
        if ids == previous:
            log(f"Повтор того же батча в чате {chat_id}, стоп")
            return

        while True:
            try:
                app.delete_messages(chat_id, ids, revoke=True)
                break
            except FloodWait as e:
                delay = flood_delay(e, rng)
                log(f"FloodWait {delay:.0f} с, чат {chat_id}")
                sleep(delay)
            except RPCError as e:
                log(f"Ошибка удаления в чате {chat_id}: {e}")
                return

        previous = ids
        log(f"Удалено {len(ids)} сообщений из чата {chat_id}.")
        if len(messages) < BATCH_LIMIT:
            return

        delay = rng(*BATCH_PAUSE)
        log(f"Пауза {delay:.1f} с в чате {chat_id}")
        sleep(delay)


def purge_chats(
    app: Any,
    chats: Iterable[Chat],
    *,
    sleep: Callable[[float], None],
    rng: Callable[..., float] = random.uniform,
    log: Callable[[str], None] = print,
) -> None:
    for i, chat in enumerate(chats):
        if i:
            delay = rng(*CHAT_PAUSE)
            log(f"Пауза {delay:.1f} с перед чатом {chat.title}")
            sleep(delay)
        if chat.id is None:
            log(f"Чат без id, пропускаю: {chat.title}")
            continue
        log(f"Удаление сообщений из чата: {chat.title} (ID: {chat.id})")
        purge_chat(app, chat.id, sleep=sleep, rng=rng, log=log)
