"""Dialog walk: dedupe, empty archive, archive failure, pagination."""

import unittest
from types import SimpleNamespace as Chat
from unittest import mock

from pyrogram import raw
from pyrogram.errors import RPCError

from dialogs import list_dialogs


class DialogWalkTests(unittest.TestCase):
    def test_main_failure_propagates(self):
        class App:
            def get_dialogs(self):
                raise RPCError("Connection reset")

        with self.assertRaises(RPCError):
            list_dialogs(App())

    def test_empty_archive_is_not_an_error(self):
        with mock.patch("dialogs._iter_archive", return_value=[]):
            main, archive, err = list_dialogs(Chat(get_dialogs=lambda: [Chat(chat=Chat(id=1))]))
        self.assertIsNone(err)
        self.assertEqual(archive, [])
        self.assertEqual(main[0].chat.id, 1)

    def test_archive_rpc_error_keeps_main(self):
        class App:
            def get_dialogs(self):
                return [Chat(chat=Chat(id=1))]

            def invoke(self, *a, **k):
                raise RPCError("Archive failure")

        main, archive, err = list_dialogs(App())
        self.assertEqual(main[0].chat.id, 1)
        self.assertEqual(archive, [])
        self.assertIn("Archive failure", err)

    def test_main_id_is_not_repeated_in_archive(self):
        main_row = Chat(chat=Chat(id=1))
        dup = Chat(chat=Chat(id=1))
        extra = Chat(chat=Chat(id=2))
        with mock.patch("dialogs._iter_archive", return_value=[dup, extra]):
            _main, archive, err = list_dialogs(Chat(get_dialogs=lambda: [main_row]))
        self.assertIsNone(err)
        self.assertEqual([row.chat.id for row in archive], [2])

    def test_archive_reads_a_second_page(self):
        pages = [
            _page(1, 20, 200),
            _page(2, 10, 100),
            _page_empty(),
        ]

        class App:
            def get_dialogs(self):
                return []

            def invoke(self, query, **k):
                return pages.pop(0)

            def resolve_peer(self, chat_id):
                return raw.types.InputPeerChannel(channel_id=chat_id, access_hash=0)

        def parse(_client, peer, _users, _chats):
            return Chat(id=peer.channel_id)

        with mock.patch("pyrogram.types.Chat._parse_dialog", parse):
            _main, archive, err = list_dialogs(App())
        self.assertIsNone(err)
        self.assertEqual([row.chat.id for row in archive], [1, 2])


def _dialog(channel_id: int, top: int):
    return raw.types.Dialog(
        peer=raw.types.PeerChannel(channel_id=channel_id),
        top_message=top,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=raw.types.PeerNotifySettings(),
    )


def _page(channel_id: int, top: int, date: int):
    return Chat(
        users=[],
        chats=[],
        messages=[Chat(id=top, date=date)],
        dialogs=[_dialog(channel_id, top)],
    )


def _page_empty():
    return Chat(users=[], chats=[], messages=[], dialogs=[])
