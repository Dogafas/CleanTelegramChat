"""Message purge module: human-paced message deletion with FloodWait backoff."""

import random

from pyrogram.errors import FloodWait, RPCError

BATCH_LIMIT = 100
BATCH_PAUSE = (8.0, 25.0)
CHAT_PAUSE = (25.0, 75.0)
FLOOD_JITTER = (1.0, 3.0)


def purge_chat(app, chat_id, *, sleep, rng=random.uniform, log=print) -> None:
    previous = None
    while True:
        try:
            messages = list(app.search_messages(chat_id, from_user="me", limit=BATCH_LIMIT))
        except FloodWait as e:
            delay = int(e.value) + rng(*FLOOD_JITTER)
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
                delay = int(e.value) + rng(*FLOOD_JITTER)
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


def purge_chats(app, chats, *, sleep, rng=random.uniform, log=print) -> None:
    # ponytail: sequential chats, pool if multi-account needed
    for i, chat in enumerate(chats):
        if i:
            delay = rng(*CHAT_PAUSE)
            log(f"Пауза {delay:.1f} с перед чатом {chat.title}")
            sleep(delay)
        log(f"Удаление сообщений из чата: {chat.title} (ID: {chat.id})")
        purge_chat(app, chat.id, sleep=sleep, rng=rng, log=log)
