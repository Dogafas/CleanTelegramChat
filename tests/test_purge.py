"""Checks for purge logic: purge_chat FloodWait/batch behavior, purge_chats pauses."""

import unittest

from pyrogram import enums
from pyrogram.errors import FloodWait

from purge import purge_chat, purge_chats


class FakeChat:
    def __init__(self, id, title, username=None, type=enums.ChatType.SUPERGROUP):
        self.id = id
        self.title = title
        self.username = username
        self.type = type


class FloodOnDeleteApp:
    """search yields 100 ids twice; delete raises FloodWait once then succeeds."""

    def __init__(self):
        self.batches = [
            [type("M", (), {"id": i})() for i in range(100)],
            [type("M", (), {"id": i})() for i in range(100)],
        ]
        self.delete_calls = []
        self.flooded = False

    def search_messages(self, chat_id, from_user, limit):
        return self.batches.pop(0)

    def delete_messages(self, chat_id, ids, revoke):
        self.delete_calls.append(list(ids))
        if not self.flooded:
            self.flooded = True
            raise FloodWait(30)


class FloodWaitRetryTests(unittest.TestCase):
    def test_delete_floodwait_retries_same_batch(self):
        app = FloodOnDeleteApp()
        sleeps = []

        def gen(a, b):
            return 1.0 if b == 3.0 else 0.0  # jitter -> 1.0, pauses -> 0.0

        purge_chat(app, -100, sleep=sleeps.append, rng=gen, log=lambda *a: None)
        self.assertTrue(app.flooded, "FloodWait never raised")
        self.assertEqual(sleeps[0], 31.0, f"expected flood sleep 30+1, got {sleeps[0]}")
        self.assertEqual(sleeps.count(31.0), 1, f"exactly one flood sleep expected, got {sleeps}")
        self.assertEqual(app.delete_calls[0], app.delete_calls[1], "delete retried with other ids")
        self.assertEqual(len(app.delete_calls), 2, app.delete_calls)
        self.assertEqual(len(app.batches), 0, "search refetched after FloodWait")


class ShortBatchApp:
    def __init__(self):
        self.batch = [type("M", (), {"id": i})() for i in (7, 8)]
        self.deleted = []
        self.searches = 0

    def search_messages(self, chat_id, from_user, limit):
        self.searches += 1
        return self.batch

    def delete_messages(self, chat_id, ids, revoke):
        self.deleted.append(list(ids))


class ShortBatchTests(unittest.TestCase):
    def test_short_final_batch_returns_without_sleeping(self):
        app = ShortBatchApp()
        sleeps = []
        purge_chat(app, -100, sleep=sleeps.append, rng=lambda a, b: 0, log=lambda *a: None)
        self.assertEqual(app.deleted, [[7, 8]])
        self.assertEqual(app.searches, 1, "search must not refetch after a partial batch")
        self.assertEqual(sleeps, [], sleeps)


class PurgeChatsTests(unittest.TestCase):
    def test_one_chat_pause_between_chats_none_before_first(self):
        class NoMessagesApp:
            def search_messages(self, chat_id, from_user, limit):
                return []

        chats = [FakeChat(-1001, "one"), FakeChat(-1002, "two")]
        sleeps = []
        logs = []
        purge_chats(
            NoMessagesApp(),
            chats,
            sleep=sleeps.append,
            rng=lambda a, b: a,
            log=logs.append,
        )
        self.assertEqual(
            sleeps, [25.0], f"expected exactly one chat_pause between chats, got {sleeps}"
        )
        self.assertIn("Удаление сообщений из чата: two", " ".join(logs))


if __name__ == "__main__":
    unittest.main()
