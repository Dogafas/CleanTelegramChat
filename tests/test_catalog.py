"""Checks for catalog logic: chat_label and select_from_dialogs. No pytest, no network."""

import unittest

from pyrogram import enums

from catalog import (
    ALL,
    ALL_BASIC,
    ALL_PRIVATE,
    ALL_PUBLIC,
    chat_label,
    select_from_dialogs,
)


class FakeChat:
    def __init__(self, id, title, username=None, type=enums.ChatType.SUPERGROUP):
        self.id = id
        self.title = title
        self.username = username
        self.type = type


class FakeDialog:
    def __init__(self, chat):
        self.chat = chat


class SelectCatalogClassificationTests(unittest.TestCase):
    def test_chat_label(self):
        c1 = FakeChat(1, "Chat One", username="user1")
        self.assertEqual(chat_label(c1), "Chat One (@user1) — 1")
        c2 = FakeChat(2, "Chat Two", username=None)
        self.assertEqual(chat_label(c2), "Chat Two — 2")
        c3 = FakeChat(3, None, username="")
        self.assertEqual(chat_label(c3), "без названия — 3")

    def test_classification_sections(self):
        c_pub = FakeChat(1, "Pub", username="pub_user", type=enums.ChatType.SUPERGROUP)
        c_priv_none = FakeChat(2, "Priv1", username=None, type=enums.ChatType.SUPERGROUP)
        c_priv_empty = FakeChat(3, "Priv2", username="", type=enums.ChatType.SUPERGROUP)
        c_basic = FakeChat(4, "Basic", username=None, type=enums.ChatType.GROUP)
        c_channel = FakeChat(5, "Chan", username="channel_user", type=enums.ChatType.CHANNEL)
        c_private_chat = FakeChat(6, "Direct", username="direct_user", type=enums.ChatType.PRIVATE)
        c_bot = FakeChat(7, "Bot", username="bot_user", type=enums.ChatType.BOT)
        c_basic_first = FakeChat(10, "First Basic", type=enums.ChatType.GROUP)
        c_pub_second = FakeChat(10, "Second Pub", username="pub", type=enums.ChatType.SUPERGROUP)

        dialogs = [
            FakeDialog(c_pub),
            FakeDialog(c_priv_none),
            FakeDialog(c_priv_empty),
            FakeDialog(c_basic),
            FakeDialog(c_channel),
            FakeDialog(c_private_chat),
            FakeDialog(c_bot),
            FakeDialog(c_basic_first),
            FakeDialog(c_pub_second),
        ]

        captured_choices = []

        def fake_ask(choices):
            captured_choices.append(choices)
            return [ALL]

        res = select_from_dialogs(dialogs, ask=fake_ask, confirm=lambda _: True, log=lambda _: None)
        self.assertEqual(res, [c_pub, c_priv_none, c_priv_empty, c_basic, c_basic_first])

        choices = captured_choices[0]
        sentinel_values = {c[1] for c in choices if len(c) == 3}
        self.assertTrue({ALL, ALL_PUBLIC, ALL_PRIVATE, ALL_BASIC}.issubset(sentinel_values))
        separators = {c[0] for c in choices if len(c) == 1}
        self.assertEqual(
            separators, {"Публичные супергруппы", "Закрытые супергруппы", "Обычные группы"}
        )

    def test_empty_sections_hidden(self):
        c_pub = FakeChat(1, "Pub", username="pub_user", type=enums.ChatType.SUPERGROUP)

        # Empty sections hidden
        captured_single = []
        select_from_dialogs(
            [FakeDialog(c_pub)],
            ask=lambda c: captured_single.append(c) or [c_pub.id],
            confirm=lambda _: True,
            log=lambda _: None,
        )
        single_choices = captured_single[0]
        single_values = {c[1] for c in single_choices if len(c) == 3}
        self.assertIn(ALL_PUBLIC, single_values)
        self.assertNotIn(ALL_PRIVATE, single_values)
        self.assertNotIn(ALL_BASIC, single_values)
        single_separators = {c[0] for c in single_choices if len(c) == 1}
        self.assertIn("Публичные супергруппы", single_separators)
        self.assertNotIn("Закрытые супергруппы", single_separators)
        self.assertNotIn("Обычные группы", single_separators)


class SelectResolutionTests(unittest.TestCase):
    def test_resolution(self):
        pub_1 = FakeChat(101, "P1", username="p1", type=enums.ChatType.SUPERGROUP)
        pub_2 = FakeChat(102, "P2", username="p2", type=enums.ChatType.SUPERGROUP)
        priv_1 = FakeChat(103, "Pr1", username=None, type=enums.ChatType.SUPERGROUP)
        b_1 = FakeChat(104, "B1", type=enums.ChatType.GROUP)
        b_2 = FakeChat(105, "B2", type=enums.ChatType.GROUP)

        dialogs = [
            FakeDialog(pub_1),
            FakeDialog(pub_2),
            FakeDialog(priv_1),
            FakeDialog(b_1),
            FakeDialog(b_2),
        ]

        r1 = select_from_dialogs(
            dialogs, ask=lambda _: [ALL_PUBLIC, b_1.id], confirm=lambda _: True, log=lambda _: None
        )
        self.assertEqual(r1, [pub_1, pub_2, b_1])

        r_all = select_from_dialogs(
            dialogs, ask=lambda _: [ALL], confirm=lambda _: True, log=lambda _: None
        )
        self.assertEqual(r_all, [pub_1, pub_2, priv_1, b_1, b_2])

        sub_dialogs = [FakeDialog(pub_1), FakeDialog(b_1)]
        r_priv = select_from_dialogs(
            sub_dialogs, ask=lambda _: [ALL_PRIVATE], confirm=lambda _: True, log=lambda _: None
        )
        self.assertEqual(r_priv, [])

        r_basic = select_from_dialogs(
            [FakeDialog(b_1)], ask=lambda _: [ALL_BASIC], confirm=lambda _: True, log=lambda _: None
        )
        self.assertEqual(r_basic, [b_1])

        r_unk = select_from_dialogs(
            dialogs, ask=lambda _: [999, "unknown_str"], confirm=lambda _: True, log=lambda _: None
        )
        self.assertEqual(r_unk, [])

        r_dedup = select_from_dialogs(
            dialogs,
            ask=lambda _: [ALL_PUBLIC, pub_1.id],
            confirm=lambda _: True,
            log=lambda _: None,
        )
        self.assertEqual(r_dedup, [pub_1, pub_2])


class SelectFromDialogsTests(unittest.TestCase):
    def setUp(self):
        self.pub_1 = FakeChat(101, "P1", username="p1", type=enums.ChatType.SUPERGROUP)
        self.priv_1 = FakeChat(103, "Pr1", username=None, type=enums.ChatType.SUPERGROUP)
        self.b_1 = FakeChat(104, "B1", type=enums.ChatType.GROUP)
        self.c_channel = FakeChat(5, "Chan", username="channel_user", type=enums.ChatType.CHANNEL)
        self.all_dialogs = [
            FakeDialog(self.pub_1),
            FakeDialog(self.priv_1),
            FakeDialog(self.b_1),
        ]

    def test_reask_preserves_checked_state(self):
        pub_1, b_1 = self.pub_1, self.b_1
        captured_choices = []
        ask_answers = [[pub_1.id], [pub_1.id, b_1.id]]
        confirm_answers = [False, True]
        logs = []

        def fake_ask(choices):
            captured_choices.append(choices)
            return ask_answers.pop(0)

        def fake_confirm(msg):
            return confirm_answers.pop(0)

        result = select_from_dialogs(
            self.all_dialogs,
            ask=fake_ask,
            confirm=fake_confirm,
            log=logs.append,
        )
        self.assertEqual(result, [pub_1, b_1])
        self.assertEqual(len(captured_choices), 2)

        first_chat_choices = [
            c for c in captured_choices[0] if len(c) == 3 and isinstance(c[1], int)
        ]
        self.assertTrue(all(not c[2] for c in first_chat_choices))

        second_chat_choices = [
            c for c in captured_choices[1] if len(c) == 3 and isinstance(c[1], int)
        ]
        second_checked_map = {c[1]: c[2] for c in second_chat_choices}
        self.assertIs(second_checked_map.get(pub_1.id), True)
        self.assertIs(second_checked_map.get(b_1.id), False)

    def test_empty_selection(self):
        confirm_called = []
        log_empty = []
        res_empty = select_from_dialogs(
            self.all_dialogs,
            ask=lambda choices: [],
            confirm=lambda msg: confirm_called.append(msg) or True,
            log=log_empty.append,
        )
        self.assertEqual(res_empty, [])
        self.assertEqual(confirm_called, [])
        self.assertIn("Ничего не выбрано.", log_empty)

    def test_cancel(self):
        log_cancel = []
        res_cancel = select_from_dialogs(
            self.all_dialogs,
            ask=lambda choices: None,
            confirm=lambda msg: True,
            log=log_cancel.append,
        )
        self.assertEqual(res_cancel, [])
        self.assertIn("Выбор отменён.", log_cancel)

    def test_no_dialogs(self):
        log_none = []
        res_none = select_from_dialogs(
            [FakeDialog(self.c_channel)],
            ask=lambda choices: (_ for _ in ()).throw(AssertionError("ask should not be called")),
            confirm=lambda msg: (_ for _ in ()).throw(
                AssertionError("confirm should not be called")
            ),
            log=log_none.append,
        )
        self.assertEqual(res_none, [])
        self.assertIn("Нет чатов для выбора.", log_none)


if __name__ == "__main__":
    unittest.main()
