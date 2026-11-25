"""Review seam: routing, archive inclusion, refresh, coverage log."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Chat
from unittest import mock

from pyrogram import enums
from pyrogram.errors import FloodWait

from review import _classify, scan
from verdict import VerdictStatus

_CLEAN = {
    "voice": "deep",
    "slop": 0.1,
    "clickbait": 0.1,
    "ads": 0.1,
    "emotional_pressure": 0.1,
    "repetition": 0.1,
}


def _post(text="мысли о жизни", *, day=1, views=100, media_group_id=None):
    return Chat(
        id=day,
        text=text,
        caption=None,
        date=datetime(2026, 10, day, tzinfo=timezone.utc),
        views=views,
        reactions=None,
        forwards=0,
        media_group_id=media_group_id,
    )


def _channel(cid, title):
    return Chat(id=cid, type=enums.ChatType.CHANNEL, title=title, username="u")


def _app(posts, *, members=1000, description="био"):
    return Chat(
        get_chat=lambda cid: Chat(members_count=members, description=description),
        get_chat_history=lambda cid, limit: posts,
        get_messages=lambda cid, mid: None,
    )


def _run(posts, evaluate, *, main=None, archive=None, app=None, log=None):
    chat = _channel(1, "Канал")
    rows = main if main is not None else [Chat(chat=chat)]
    logs = []
    with mock.patch("review.list_dialogs", return_value=(rows, archive or [], None)):
        reviewed = scan(
            app or _app(posts),
            evaluate,
            logs.append if log is None else log,
            sleep=lambda *_: None,
            rng=lambda a, b: 0,
        )
    return reviewed, logs


class ReviewRoutingTests(unittest.TestCase):
    def test_text_fail_is_checked_and_sees_description(self):
        seen = {}

        def evaluate(title, posts, description=""):
            seen["description"] = description
            return dict(_CLEAN, ads=0.8)

        reviewed, _logs = _run([_post()], evaluate, app=_app([_post()], description="Био канала"))
        cand = reviewed.candidates()[0]
        self.assertEqual(cand.category, "red")
        self.assertTrue(cand.checked)
        self.assertIn("рекламная витрина", cand.reasons)
        self.assertEqual(seen["description"], "Био канала")

    def test_signals_without_text_fail_are_unchecked(self):
        posts = [_post(views=10, day=i) for i in range(1, 21)]
        reviewed, _logs = _run(posts, lambda *a, **k: dict(_CLEAN))
        cand = reviewed.candidates()[0]
        self.assertEqual(cand.category, "yellow")
        self.assertFalse(cand.checked)
        self.assertTrue(cand.reasons)

    def test_missing_text_is_gray_and_skips_the_model(self):
        called = []
        empty = Chat(id=1, text="", caption="", date=None, views=1, reactions=None, forwards=0)
        reviewed, _logs = _run([empty], lambda *a, **k: called.append(1))
        cand = reviewed.candidates()[0]
        self.assertEqual(cand.category, "gray")
        self.assertIn("нет текста", cand.reasons)
        self.assertEqual(called, [])

    def test_gray_reasons_are_typed_not_string_parsing(self):
        # одна категория, разные программные статусы: без парсинга русских строк
        no_text = _classify({"chat": _channel(1, "Пустой"), "verdict": None, "posts": []})
        errored = _classify(
            {"chat": _channel(2, "Сломанный"), "verdict": {"error": "x"}, "posts": [_post()]}
        )
        assert no_text is not None and errored is not None
        self.assertEqual((no_text.category, no_text.status), ("gray", VerdictStatus.NO_TEXT))
        self.assertEqual((errored.category, errored.status), ("gray", VerdictStatus.ERROR))

    def test_clean_channel_is_not_a_candidate(self):
        views = (10, 500, 40, 800, 20)
        posts = [_post(views=view, day=i * 3) for i, view in enumerate(views, start=1)]
        reviewed, _logs = _run(posts, lambda *a, **k: dict(_CLEAN))
        self.assertEqual(reviewed.candidates(), [])
        self.assertEqual([chat.id for chat in reviewed.chats()], [1])

    def test_archived_channel_is_scanned(self):
        archived = Chat(chat=_channel(9, "Архив"))
        reviewed, _logs = _run([], lambda *a, **k: dict(_CLEAN), main=[], archive=[archived])
        self.assertEqual([chat.id for chat in reviewed.chats()], [9])

    def test_candidates_are_ordered_red_then_yellow_then_gray(self):
        red = Chat(chat=_channel(1, "Red"))
        yellow = Chat(chat=_channel(2, "Yellow"))
        gray = Chat(chat=_channel(3, "Gray"))

        def history(cid, limit):
            if cid == 3:
                return [Chat(
                    id=1, text="", caption="", date=None, views=1, reactions=None, forwards=0,
                )]
            return [_post(views=10)]

        def evaluate(title, posts, description=""):
            if title == "Red":
                return dict(_CLEAN, slop=0.9)
            return dict(_CLEAN)

        app = Chat(
            get_chat=lambda cid: Chat(members_count=1000, description=""),
            get_chat_history=history,
            get_messages=lambda cid, mid: None,
        )
        reviewed, _logs = _run([], evaluate, main=[yellow, gray, red], app=app)
        self.assertEqual([c.category for c in reviewed.candidates()], ["red", "yellow", "gray"])

    def test_floodwait_retries_the_chat_read(self):
        calls = {"n": 0}

        def get_chat(cid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWait(value=1)
            return Chat(members_count=10, description="ok")

        sleeps = []
        app = Chat(
            get_chat=get_chat,
            get_chat_history=lambda cid, limit: [_post()],
            get_messages=lambda cid, mid: None,
        )
        listed = ([Chat(chat=_channel(1, "Chan"))], [], None)
        with mock.patch("review.list_dialogs", return_value=listed):
            scan(
                app,
                lambda *a, **k: dict(_CLEAN),
                lambda *_: None,
                sleep=sleeps.append,
                rng=lambda a, b: 1,
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [2.0])

    def test_floodwait_retries_the_post_fetch(self):
        calls = {"n": 0}

        def get_chat_history(cid, limit):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWait(value=1)
            return [_post()]

        sleeps = []
        app = Chat(
            get_chat=lambda cid: Chat(members_count=10, description="ok"),
            get_chat_history=get_chat_history,
            get_messages=lambda cid, mid: None,
        )
        listed = ([Chat(chat=_channel(1, "Chan"))], [], None)
        with mock.patch("review.list_dialogs", return_value=listed):
            reviewed = scan(
                app,
                lambda *a, **k: dict(_CLEAN),
                lambda *_: None,
                sleep=sleeps.append,
                rng=lambda a, b: 1,
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual([chat.id for chat in reviewed.chats()], [1])

    def test_album_is_one_publication_in_the_log(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        posts = [
            Chat(id=i, text="подпись" if i == 1 else "", caption=None, date=now, views=10,
                 reactions=None, forwards=0, media_group_id="mg")
            for i in range(1, 5)
        ]
        _reviewed, logs = _run(posts, lambda *a, **k: dict(_CLEAN))
        self.assertTrue(any("4 сообщ. (1 публ.)" in line for line in logs))

    def test_refresh_uses_full_chat_members_and_logs_coverage(self):
        seen = {}

        def get_chat(cid):
            seen["members"] = 400
            return Chat(members_count=400, description="новое")

        app = Chat(
            get_chat=get_chat,
            get_chat_history=lambda cid, limit=100, offset_id=None: [
                _post(text="старый пост с фактом", day=2)
            ],
            get_messages=lambda cid, mid: None,
        )
        reviewed, _logs = _run([_post()], lambda *a, **k: dict(_CLEAN), app=app)
        logs = []
        reviewed.refresh(
            app,
            [1],
            horizon_days=14,
            limit=200,
            evaluate=lambda *a, **k: dict(_CLEAN),
            log=logs.append,
            sleep=lambda *_: None,
            rng=lambda a, b: 0,
        )
        self.assertEqual(seen["members"], 400)
        self.assertTrue(any(line.startswith("Уточнено:") for line in logs))

    def test_frequency_uses_full_history_not_the_model_sample(self):
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        posts = [
            Chat(
                id=i,
                text=f"пост номер {i} с наблюдением",
                caption=None,
                date=now - timedelta(hours=i),
                views=100 + i,
                reactions=None,
                forwards=0,
                media_group_id=None,
            )
            for i in range(1, 41)
        ]
        seen = {}

        def evaluate(title, texts, description=""):
            seen["n"] = len(texts)
            return dict(_CLEAN)

        reviewed, _logs = _run(posts, evaluate)
        self.assertEqual(seen["n"], 40)
        self.assertTrue(any("част" in reason for reason in reviewed.candidates()[0].reasons))
