"""Chat Folder seam: snapshot, create-only, add peer, remove peer."""

import unittest
from types import SimpleNamespace as Chat

from pyrogram import raw
from pyrogram.errors import RPCError

from chat_folder import add_peer, create_only, remove_peer, snapshot


class SnapshotTests(unittest.TestCase):
    def test_limits_fall_back_when_config_fails(self):
        class App:
            def get_me(self):
                raise RPCError("get_me failed")

            def invoke(self, query, **k):
                return []

        logs = []
        titles, folders, chats, used, ok = snapshot(App(), log=logs.append)
        self.assertTrue(ok)
        self.assertEqual((folders, chats, used), (10, 100, 0))
        self.assertEqual(titles, frozenset())
        self.assertTrue(any("Лимиты вкладок не прочитаны" in msg for msg in logs))

    def test_limits_from_config(self):
        item_folders = raw.types.JsonObjectValue(
            key="dialog_filters_limit_default",
            value=raw.types.JsonNumber(value=15.0),
        )
        item_chats = raw.types.JsonObjectValue(
            key="dialog_filters_chats_limit_default",
            value=raw.types.JsonNumber(value=150.0),
        )

        class App:
            def get_me(self):
                return Chat(is_premium=False)

            def invoke(self, query, **k):
                if isinstance(query, raw.functions.help.GetAppConfig):
                    return raw.types.JsonObject(value=[item_folders, item_chats])
                return []

        logs = []
        _titles, folders, chats, _used, ok = snapshot(App(), log=logs.append)
        self.assertTrue(ok)
        self.assertEqual((folders, chats), (15, 150))
        self.assertTrue(any("Лимиты вкладок: 15 папок, 150 чатов" in msg for msg in logs))

    def test_unreadable_filters_are_not_ok(self):
        class App:
            def invoke(self, query, **k):
                raise RPCError("no filters")

        logs = []
        _titles, _folders, _chats, _used, ok = snapshot(App(), log=logs.append)
        self.assertFalse(ok)
        self.assertTrue(any("Список вкладок не прочитан" in msg for msg in logs))

    def test_titles_come_from_entities(self):
        existing = Chat(id=2, title=raw.types.TextWithEntities(text="Чтение", entities=[]))

        class App:
            def get_me(self):
                return Chat(is_premium=False)

            def invoke(self, query, **k):
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return Chat(filters=[existing])
                return raw.types.JsonObject(value=[])

        titles, *_rest = snapshot(App(), log=lambda *_: None)
        self.assertIn("Чтение", titles)


class FolderMutationTests(unittest.TestCase):
    def test_create_only_refuses_existing_title_and_skips_flags(self):
        existing = Chat(id=2, title=Chat(text="работа"))
        updates = []

        class App:
            def invoke(self, query, **k):
                if isinstance(query, raw.functions.messages.GetDialogFilters):
                    return [existing]
                updates.append(query)
                return True

            def resolve_peer(self, chat_id):
                return raw.types.InputPeerChannel(channel_id=chat_id, access_hash=0)

        status, unresolved, created = create_only(App(), "работа", [1], log=lambda *_: None)
        self.assertEqual(status, "exists")
        self.assertEqual(unresolved, [])
        self.assertEqual(created, 0)
        self.assertEqual(updates, [])

        status, _unresolved, created = create_only(App(), "Новая", [7], log=lambda *_: None)
        self.assertEqual(status, "created")
        self.assertEqual(created, 1)
        filt = updates[0].filter
        self.assertEqual(filt.title.text, "Новая")
        self.assertFalse(getattr(filt, "groups", None))
        self.assertFalse(getattr(filt, "broadcasts", None))

    def test_add_peer_matches_title_with_entities_and_keeps_fields(self):
        existing = Chat(
            id=7,
            title=raw.types.TextWithEntities(text="Чтение", entities=[]),
            include_peers=[],
            pinned_peers=[Chat(channel_id=1)],
            exclude_peers=[],
            groups=True,
        )
        updates = []

        class App:
            def get_me(self):
                return Chat(is_premium=False)

            def invoke(self, query, **k):
                name = type(query).__name__
                if "GetDialogFilters" in name:
                    return Chat(filters=[existing])
                if "GetAppConfig" in name:
                    return raw.types.JsonObject(value=[])
                updates.append(query)
                return True

            def resolve_peer(self, chat_id):
                return Chat(channel_id=chat_id)

        status, folder_id, was_new = add_peer(App(), 555, "Чтение")
        self.assertEqual(status, "applied")
        self.assertEqual(folder_id, 7)
        self.assertFalse(was_new)
        updated = updates[0].filter
        self.assertEqual(updated.groups, True)
        self.assertEqual(len(updated.pinned_peers), 1)
        self.assertEqual(updated.include_peers[0].channel_id, 555)

    def test_remove_peer_keeps_the_folder(self):
        existing = Chat(
            id=7,
            title=Chat(text="Чтение"),
            include_peers=[Chat(channel_id=555), Chat(channel_id=1)],
            pinned_peers=[],
            exclude_peers=[],
        )
        updates = []

        class App:
            def invoke(self, query, **k):
                if "GetDialogFilters" in type(query).__name__:
                    return [existing]
                updates.append(query)
                return True

            def resolve_peer(self, chat_id):
                return Chat(channel_id=chat_id)

        self.assertEqual(remove_peer(App(), 555, 7), "restored")
        self.assertEqual(updates[0].id, 7)
        left = [peer.channel_id for peer in updates[0].filter.include_peers]
        self.assertEqual(left, [1])
        missing = Chat(invoke=lambda *a, **k: [], resolve_peer=lambda i: i)
        self.assertEqual(remove_peer(missing, 1, 9), "skipped")
