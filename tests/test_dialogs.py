"""Dialog walk: dedupe, empty archive, archive failure."""

import unittest
from types import SimpleNamespace as Chat

from pyrogram.errors import RPCError

from dialogs import list_dialogs


class _App:
    """get_dialogs dispatches on from_archive; archive raises by default."""

    def __init__(self, main, archive=None, archive_error=None):
        self.main = main
        self.archive = archive or []
        self.archive_error = archive_error

    def get_dialogs(self, from_archive=None):
        if from_archive:
            if self.archive_error:
                raise self.archive_error
            return self.archive
        return self.main


class DialogWalkTests(unittest.TestCase):
    def test_main_failure_propagates(self):
        class App:
            def get_dialogs(self, from_archive=None):
                raise RPCError("Connection reset")

        with self.assertRaises(RPCError):
            list_dialogs(App())

    def test_empty_archive_is_not_an_error(self):
        main, archive, err = list_dialogs(_App([Chat(chat=Chat(id=1))]))
        self.assertIsNone(err)
        self.assertEqual(archive, [])
        self.assertEqual(main[0].chat.id, 1)

    def test_archive_rpc_error_keeps_main(self):
        app = _App([Chat(chat=Chat(id=1))], archive_error=RPCError("Archive failure"))
        main, archive, err = list_dialogs(app)
        self.assertEqual(main[0].chat.id, 1)
        self.assertEqual(archive, [])
        self.assertIn("Archive failure", err)

    def test_main_id_is_not_repeated_in_archive(self):
        main_row = Chat(chat=Chat(id=1))
        dup = Chat(chat=Chat(id=1))
        extra = Chat(chat=Chat(id=2))
        _main, archive, err = list_dialogs(_App([main_row], [dup, extra]))
        self.assertIsNone(err)
        self.assertEqual([row.chat.id for row in archive], [2])
