"""Message purge module: human-paced message deletion with FloodWait backoff."""

import random

from pyrogram.errors import FloodWait, RPCError

BATCH_LIMIT = 100
BATCH_PAUSE = (8.0, 25.0)
CHAT_PAUSE = (25.0, 75.0)
FLOOD_JITTER = (1.0, 3.0)


def _wait(e: FloodWait) -> int:
    return int(getattr(e, "value", getattr(e, "x", 0)))


def batch_pause(rng=random.uniform) -> float:
    return rng(*BATCH_PAUSE)


def chat_pause(rng=random.uniform) -> float:
    return rng(*CHAT_PAUSE)


def flood_pause(wait_seconds: int, rng=random.uniform) -> float:
    return float(wait_seconds) + rng(*FLOOD_JITTER)


def purge_chat(app, chat_id, *, sleep, rng=random.uniform, log=print) -> None:
    previous = None
    while True:
        try:
            messages = list(app.search_messages(chat_id, from_user="me", limit=BATCH_LIMIT))
        except FloodWait as e:
            delay = flood_pause(_wait(e), rng)
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
                delay = flood_pause(_wait(e), rng)
                log(f"FloodWait {delay:.0f} с, чат {chat_id}")
                sleep(delay)
            except RPCError as e:
                log(f"Ошибка удаления в чате {chat_id}: {e}")
                return

        previous = ids
        log(f"Удалено {len(ids)} сообщений из чата {chat_id}.")
        if len(messages) < BATCH_LIMIT:
            return

        delay = batch_pause(rng)
        log(f"Пауза {delay:.1f} с в чате {chat_id}")
        sleep(delay)


def purge_chats(app, chats, *, sleep, rng=random.uniform, log=print) -> None:
    # ponytail: sequential chats, pool if multi-account needed
    for i, chat in enumerate(chats):
        if i:
            delay = chat_pause(rng)
            log(f"Пауза {delay:.1f} с перед чатом {chat.title}")
            sleep(delay)
        log(f"Удаление сообщений из чата: {chat.title} (ID: {chat.id})")
        purge_chat(app, chat.id, sleep=sleep, rng=rng, log=log)
