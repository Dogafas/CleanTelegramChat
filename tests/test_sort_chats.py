"""Tests for sort_chats.py: categories, caching, sorting, tabs creation, edge cases."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Chat
from typing import Any
from unittest import mock

from pyrogram import enums, raw
from pyrogram.errors import RPCError

from sort_chats import (
    categories_path,
    load_cache,
    load_categories,
    pick_peers,
    render_sort_file,
    save_cache,
    sort_account,
    sortable_dialogs,
    split_assignment,
)


def _main_only(app):
    return list(app.get_dialogs()), [], None



class CategoriesTests(unittest.TestCase):
    tmp_path: Path

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_load_categories_success(self):
        cat_file = self.tmp_path / "cats.txt"
        cat_file.write_text("  ИТ  \n# новости\nИТ\n\n  Крипта\n", encoding="utf-8")  # noqa: RUF001
        cats = load_categories(cat_file)
        self.assertEqual(cats, ["ИТ", "# новости", "Крипта"])

    def test_load_categories_not_found(self):
        missing = self.tmp_path / "none.txt"
        with self.assertRaises(ValueError) as cm:
            load_categories(missing)
        self.assertIn("Файл категорий не найден", str(cm.exception))

    def test_load_categories_empty(self):
        empty = self.tmp_path / "empty.txt"
        empty.write_text("   \n\n", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_categories(empty)
        self.assertIn("Файл категорий пуст", str(cm.exception))

    def test_load_categories_reserved(self):
        reserved = self.tmp_path / "res.txt"
        reserved.write_text("Новости\nбез категории\n", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_categories(reserved)
        self.assertIn("Зарезервированное имя категории", str(cm.exception))

        reserved2 = self.tmp_path / "res2.txt"
        reserved2.write_text("не оценено\n", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_categories(reserved2)
        self.assertIn("Зарезервированное имя категории", str(cm.exception))

    def test_categories_path_resolution(self):
        root = self.tmp_path / "app_root"
        root.mkdir()
        env_file = root / ".env"

        # 1. default: categories.txt in root
        self.assertEqual(categories_path(environ={}, root=root), root / "categories.txt")

        # 2. from .env
        env_file.write_text("CATEGORIES_FILE=my_cats.txt\n", encoding="utf-8")
        self.assertEqual(
            categories_path(env_file=env_file, environ={}, root=root),
            root / "my_cats.txt",
        )

        # 3. from environ relative
        self.assertEqual(
            categories_path(environ={"CATEGORIES_FILE": "env_cats.txt"}, root=root),
            root / "env_cats.txt",
        )

        # 4. from environ absolute
        abs_p = (self.tmp_path / "abs_cats.txt").resolve()
        self.assertEqual(
            categories_path(environ={"CATEGORIES_FILE": str(abs_p)}, root=root),
            abs_p,
        )


class SplitAndPickTests(unittest.TestCase):
    def test_split_assignment_thresholds(self):
        cats = ["A", "B", "C"]
        scores = {"A": 0.5, "B": 0.49, "C": 0.9}
        assigned = split_assignment(scores, cats)
        self.assertEqual(assigned, ["A", "C"])

    def test_split_assignment_invalid_or_missing(self):
        cats = ["A", "B"]
        with self.assertRaises(KeyError):
            split_assignment({"A": 0.8}, cats)

        with self.assertRaises(ValueError):
            split_assignment({"A": 0.8, "B": "invalid"}, cats)

    def test_pick_peers_sorting_and_limits(self):
        c1 = Chat(id=1, title="Первый")
        c2 = Chat(id=2, title="Второй")
        c3 = Chat(id=3, title="Третий")
        c4 = Chat(id=4, title="Архивный")

        # rows: (noul, index, chat)
        rows = [
            (0.8, 1, c2),
            (0.9, 0, c1),
            (0.8, 0, c3),
            (0.95, 3, c4),
        ]
        # Expected sort order:
        # (-0.95, 3) -> c4
        # (-0.9, 0) -> c1
        # (-0.8, 0) -> c3
        # (-0.8, 1) -> c2
        kept, rest = pick_peers(rows, limit=2)
        self.assertEqual(kept, [c4, c1])
        self.assertEqual(rest, [c3, c2])

        # limit <= 0 -> all in rest
        kept0, rest0 = pick_peers(rows, limit=0)
        self.assertEqual(kept0, [])
        self.assertEqual(rest0, [c4, c1, c3, c2])


class RenderSortFileTests(unittest.TestCase):
    def test_render_sort_file_formatting(self):
        categories = ["Tech", "News", "Empty"]
        c1 = Chat(id=101, title="Habr")
        c2 = Chat(id=102, title="TechCrunch")
        c3 = Chat(id=103, title=None)  # без названия
        c_uncat = Chat(id=201, title="Memes")
        c_uneval = Chat(id=301, title="Broken")

        by_cat = {
            "Tech": [(0.8, 0, c1), (0.9, 1, c2)],
            "News": [(0.7, 2, c3)],
            "Empty": [],
        }
        uncat = [(0.0, 3, c_uncat)]
        uneval = [(0.0, 4, c_uneval)]

        res = render_sort_file(categories, by_cat, uncat, uneval)

        expected = (
            "## Tech\n"
            "Habr \u2014 101\n"
            "TechCrunch \u2014 102\n\n"
            "## News\n"
            "без названия \u2014 103\n\n"
            "## Empty\n\n"
            "## без категории\n"
            "Memes \u2014 201\n\n"
            "## не оценено\n"
            "Broken \u2014 301\n"
        )
        self.assertEqual(res, expected)

    def test_render_sort_file_omits_empty_uncat_and_uneval(self):
        categories = ["Main"]
        c1 = Chat(id=1, title="Chat1")
        by_cat = {"Main": [(0.8, 0, c1)]}
        res = render_sort_file(categories, by_cat, [], [])
        self.assertEqual(res, "## Main\nChat1 \u2014 1\n")


class CacheTests(unittest.TestCase):
    tmp_path: Path

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_cache_save_and_load(self):
        p = self.tmp_path / "test.cache.json"
        save_cache(p, "hash123", {"10": {"texts_hash": "t1", "scores": {"A": 0.8}}})
        loaded = load_cache(p)
        self.assertEqual(loaded["categories_hash"], "hash123")
        self.assertEqual(loaded["entries"]["10"]["scores"]["A"], 0.8)

    def test_corrupted_cache_returns_empty(self):
        p = self.tmp_path / "bad.cache.json"
        p.write_text("not json", encoding="utf-8")
        self.assertEqual(load_cache(p), {})

        p.write_text("[]", encoding="utf-8")
        self.assertEqual(load_cache(p), {})


class SortableDialogsTests(unittest.TestCase):
    def test_rpc_error_on_main_dialogs_fails(self):
        class App:
            def get_dialogs(self):
                raise RPCError("Connection reset")

        logs = []
        dialogs = sortable_dialogs(App(), log=logs.append)
        self.assertIsNone(dialogs)
        self.assertTrue(any("Список диалогов не прочитан" in msg for msg in logs))

    def test_archive_rpc_error_continues_with_main(self):
        main_c = Chat(id=1, type=enums.ChatType.CHANNEL, title="MainChan")

        class App:
            def get_dialogs(self):
                return [Chat(chat=main_c)]

            def invoke(self, *a, **k):
                raise RPCError("Archive failure")

        logs = []
        dialogs = sortable_dialogs(App(), log=logs.append)
        self.assertEqual(dialogs, [main_c])
        self.assertTrue(any("Архив не прочитан" in msg for msg in logs))

    def test_filtering_and_deduplication(self):
        c_chan = Chat(id=1, type=enums.ChatType.CHANNEL, title="Chan")
        c_sg = Chat(id=2, type=enums.ChatType.SUPERGROUP, title="SG")
        c_g = Chat(id=3, type=enums.ChatType.GROUP, title="Group")
        c_bot = Chat(id=4, type=enums.ChatType.BOT, title="Bot")
        c_private = Chat(id=5, type=enums.ChatType.PRIVATE, title="User")
        c_dup = Chat(id=1, type=enums.ChatType.CHANNEL, title="ChanArchived")

        class App:
            def get_dialogs(self):
                return [
                    Chat(chat=c_chan),
                    Chat(chat=c_sg),
                    Chat(chat=c_bot),
                    Chat(chat=c_private),
                ]

        # Directly simulate dialogs iteration
        with mock.patch(
            "sort_chats.list_dialogs",
            return_value=(
                [Chat(chat=c_chan), Chat(chat=c_sg), Chat(chat=c_bot), Chat(chat=c_private)],
                [Chat(chat=c_dup), Chat(chat=c_g)],
                None,
            ),
        ):
            dialogs = sortable_dialogs(App(), log=lambda *_: None)
            self.assertEqual(dialogs, [c_chan, c_sg, c_g])


class SortAccountEndToEndTests(unittest.TestCase):
    tmp_path: Path

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    @staticmethod
    def _client(app):
        def factory(session_name):
            class _Ctx:
                def __enter__(self):
                    return app

                def __exit__(self, *a):
                    return False

            return _Ctx()

        return factory

    def test_no_typesafe_key_stops_before_client(self):
        client_called = False

        def fake_client(session_name):
            nonlocal client_called
            client_called = True
            raise AssertionError("Client should not be called")

        logs = []
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}, clear=False):
            sort_account(
                "test_s",
                categories=["News"],
                client=fake_client,
                evaluate_fn=None,
                log=logs.append,
                root=self.tmp_path,
            )

        self.assertFalse(client_called)
        self.assertFalse((self.tmp_path / "sort-test_s.md").exists())
        self.assertTrue(any("TYPESAFE_API_KEY не задан" in m for m in logs))

    def test_caching_and_history_none(self):
        c1 = Chat(id=10, title="Chan1", username="c1", type=enums.ChatType.CHANNEL)
        c2 = Chat(id=20, title="Chan2", username="c2", type=enums.ChatType.CHANNEL)

        invoked_calls: list[str] = []

        def fake_evaluate(state, questions):
            invoked_calls.append(state["title"])
            return Chat(
                answers={
                    "c0": Chat(noul=0.8),
                    "c1": Chat(noul=0.2),
                }
            )

        class FakeApp:
            def get_dialogs(self):
                return [Chat(chat=c1), Chat(chat=c2)]

            def get_chat(self, chat_id):
                return Chat(description="desc")

            def resolve_peer(self, chat_id):
                return Chat(id=chat_id)

            def invoke(self, query, **kw):
                if isinstance(query, raw.functions.help.GetAppConfig):
                    return raw.types.JsonObject(value=[])
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return []
                return None

        # Pass 1: c1 has posts ["p1"], c2 history is None (fails fetch)
        with mock.patch("sort_chats.fetch_posts") as mock_fetch, \
             mock.patch("sort_chats.list_dialogs", side_effect=_main_only):
            def fetch_side_effect(app, chat_id, limit):
                if chat_id == 10:
                    return [Chat(text="p1", caption=None)]
                raise RPCError("Cannot read history")

            mock_fetch.side_effect = fetch_side_effect

            sort_account(
                "s1",
                categories=["Tech", "News"],
                client=self._client(FakeApp()),
                evaluate_fn=fake_evaluate,
                ask=lambda *a, **k: [],  # no tabs selected
                confirm=lambda msg: True,
                sleep=lambda *_: None,
                rng=lambda a, b: a,
                log=lambda *_: None,
                root=self.tmp_path,
            )

        self.assertEqual(invoked_calls, ["Chan1", "Chan2"])

        # Check cache: c1 is in cache, c2 is NOT in cache
        cache = load_cache(self.tmp_path / "sort-s1.cache.json")
        self.assertIn("10", cache["entries"])
        self.assertNotIn("20", cache["entries"])

        # Pass 2: Run again with same categories. c1 should hit cache, c2 will evaluate again!
        invoked_calls.clear()
        with mock.patch("sort_chats.fetch_posts") as mock_fetch, \
             mock.patch("sort_chats.list_dialogs", side_effect=_main_only):
            mock_fetch.side_effect = fetch_side_effect
            sort_account(
                "s1",
                categories=["Tech", "News"],
                client=self._client(FakeApp()),
                evaluate_fn=fake_evaluate,
                ask=lambda *a, **k: [],
                confirm=lambda msg: True,
                sleep=lambda *_: None,
                rng=lambda a, b: a,
                log=lambda *_: None,
                root=self.tmp_path,
            )

        self.assertEqual(invoked_calls, ["Chan2"])  # Chan1 was cached!

    def test_one_dialog_fails_evaluation_goes_to_unevaluated(self):
        c1 = Chat(id=1, title="GoodChan", username="g", type=enums.ChatType.CHANNEL)
        c2 = Chat(id=2, title="BadChan", username="b", type=enums.ChatType.CHANNEL)

        def fake_evaluate(state, questions):
            if state["title"] == "BadChan":
                raise RuntimeError("LLM failure")
            return Chat(answers={"c0": Chat(noul=0.9)})

        class FakeApp:
            def get_dialogs(self):
                return [Chat(chat=c1), Chat(chat=c2)]

            def get_chat(self, chat_id):
                return Chat(description="")

            def invoke(self, query, **kw):
                if isinstance(query, raw.functions.help.GetAppConfig):
                    return raw.types.JsonObject(value=[])
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return []
                return None

        with mock.patch("sort_chats.fetch_posts", return_value=[]), \
             mock.patch("sort_chats.list_dialogs", side_effect=_main_only):
            sort_account(
                "s2",
                categories=["Coding"],
                client=self._client(FakeApp()),
                evaluate_fn=fake_evaluate,
                ask=lambda *a, **k: [],
                sleep=lambda *_: None,
                rng=lambda a, b: a,
                log=lambda *_: None,
                root=self.tmp_path,
            )

        md = (self.tmp_path / "sort-s2.md").read_text(encoding="utf-8")
        self.assertIn("## Coding\nGoodChan \u2014 1\n", md)
        self.assertIn("## не оценено\nBadChan \u2014 2\n", md)

    def test_tab_creation_and_filters_rules(self):
        # 1. Existing folder with title "Existing"
        # 2. Category > 12 chars: "VeryLongCategoryName"
        # 3. Case sensitivity: "работа" vs "Работа"
        # 4. Folder slots limitation and prompt re-ask
        c1 = Chat(id=11, title="C1", username="u1", type=enums.ChatType.CHANNEL)
        c2 = Chat(id=12, title="C2", username="u2", type=enums.ChatType.CHANNEL)

        existing_tab = Chat(
            id=2, title=raw.types.TextWithEntities(text="Existing", entities=[])
        )
        work_tab = Chat(id=3, title="Работа")

        invoked_updates: list[Any] = []

        class FakeApp:
            def get_me(self):
                return Chat(is_premium=False)

            def get_dialogs(self):
                return [Chat(chat=c1), Chat(chat=c2)]

            def get_chat(self, chat_id):
                return Chat(description="")
            def resolve_peer(self, chat_id):
                return raw.types.InputPeerChannel(channel_id=chat_id, access_hash=0)

            def invoke(self, query, **kw):
                if isinstance(query, raw.functions.help.GetAppConfig):
                    # Folder limit default = 4 folders, so free = 4 - 2 = 2
                    item_val: Any = raw.types.JsonObjectValue(
                        key="dialog_filters_limit_default",
                        value=raw.types.JsonNumber(value=4.0),  # type: ignore[reportArgumentType]
                    )
                    return raw.types.JsonObject(value=[item_val])  # type: ignore[reportArgumentType]
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return [existing_tab, work_tab]
                if isinstance(query, raw.functions.messages.UpdateDialogFilter):
                    invoked_updates.append(query)
                    return True
                return None

        # Categories:
        # "Existing" -> already exists, only file
        # "VeryLongCategoryName" -> >12 chars, only file
        # "работа" -> lowercase, not blocked by "Работа"!
        # "Short" -> valid
        categories = ["Existing", "VeryLongCategoryName", "работа", "Short"]

        def fake_evaluate(state, questions):
            # assign everything to all categories
            return Chat(answers={f"c{i}": Chat(noul=0.9) for i in range(len(categories))})

        seen_messages: list[str] = []
        ask_count = 0

        def fake_ask(choices, message=""):
            nonlocal ask_count
            ask_count += 1
            seen_messages.append(message)
            # choices should only contain "работа" and "Short"
            labels = [c[1] for c in choices]
            self.assertNotIn("Existing", labels)
            self.assertNotIn("VeryLongCategoryName", labels)
            self.assertIn("работа", labels)
            self.assertIn("Short", labels)

            # First time: select 3 items (more than free slots = 2) -> will re-prompt
            if ask_count == 1:
                return ["работа", "Short", "Extra"]
            # Second time: select 1 item ("работа")
            return ["работа"]

        with mock.patch("sort_chats.fetch_posts", return_value=[]), \
             mock.patch("sort_chats.list_dialogs", side_effect=_main_only):
            sort_account(
                "s3",
                categories=categories,
                client=self._client(FakeApp()),
                evaluate_fn=fake_evaluate,
                ask=fake_ask,
                confirm=lambda msg: True,
                sleep=lambda *_: None,
                rng=lambda a, b: a,
                log=lambda *_: None,
                root=self.tmp_path,
            )

        self.assertEqual(ask_count, 2)
        self.assertIn("Свободных слотов: 2", seen_messages[0])
        self.assertIn("Свободных слотов 2, отмечено 3", seen_messages[1])

        # Verify update dialog filter was called for "работа" with id >= 2 (not 2 or 3 because used)
        self.assertEqual(len(invoked_updates), 1)

    def test_empty_choice_or_confirm_declined_leaves_file_and_no_tabs(self):
        c1 = Chat(id=1, title="C1", username="u1", type=enums.ChatType.CHANNEL)
        invoked_updates: list[Any] = []

        class FakeApp:
            def get_dialogs(self):
                return [Chat(chat=c1)]

            def get_chat(self, chat_id):
                return Chat(description="")

            def invoke(self, query, **kw):
                if isinstance(query, raw.functions.help.GetAppConfig):
                    return raw.types.JsonObject(value=[])
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return []
                if isinstance(query, raw.functions.messages.UpdateDialogFilter):
                    invoked_updates.append(query)
                    return True
                return None

        # confirm returns False
        with mock.patch("sort_chats.fetch_posts", return_value=[]), \
             mock.patch("sort_chats.list_dialogs", side_effect=_main_only):
            sort_account(
                "s4",
                categories=["News"],
                client=self._client(FakeApp()),
                evaluate_fn=lambda s, q: Chat(answers={"c0": Chat(noul=0.8)}),
                ask=lambda *a, **k: ["News"],
                confirm=lambda msg: False,
                sleep=lambda *_: None,
                rng=lambda a, b: a,
                log=lambda *_: None,
                root=self.tmp_path,
            )

        self.assertEqual(len(invoked_updates), 0)
        # File must exist
        self.assertTrue((self.tmp_path / "sort-s4.md").exists())


if __name__ == "__main__":
    unittest.main()
