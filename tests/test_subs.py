"""Проверки отписки от каналов: скоринг метрик, batch-отписка, композиция."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as Chat

from pyrogram import enums
from pyrogram.errors import FloodWait

import clean_my_subs
from subs import compute_metrics, fetch_creation_date, score_channel
import subs
import verdict
from verdict import QUESTIONS, choice_suffix, text_reasons, verdict_label

class _Date:
    def __init__(self, day: int) -> None:
        self.day = day

    def __sub__(self, other: "_Date") -> timedelta:
        return timedelta(days=self.day - other.day)

    def __lt__(self, other: "_Date") -> bool:
        return self.day < other.day

    def __gt__(self, other: "_Date") -> bool:
        return self.day > other.day


def _post(text="мысли о жизни", day=1, *, views=100, reactions=5, forwards=2, link=False):
    return Chat(
        views=views,
        reactions=[Chat(count=reactions)],
        forwards=forwards,
        text=text + (" https://example.com" if link else ""),
        caption=None,
        date=_Date(day),
        entities=[],
        caption_entities=[],
    )


class ScoringTests(unittest.TestCase):
    def test_score_healthy_channel_high(self):
        posts = [_post(views=100 + i * 3, day=i) for i in range(30, 0, -1)]
        m = subs.compute_metrics(Chat(title="Хороший канал"), posts, members=1000)
        r = subs.score_channel(m)
        self.assertGreaterEqual(r["score"], 80)
        self.assertEqual(r["flags"], [])

    def test_score_inflated_views_hard_flag(self):
        posts = [_post(views=2000, reactions=30) for _ in range(20)]
        m = subs.compute_metrics(Chat(title="Канал"), posts, members=1000)
        r = subs.score_channel(m)
        self.assertLessEqual(r["score"], 30)
        self.assertTrue(r["flags"])

    def test_score_ad_showcase_low(self):
        posts = [_post(text="переходи в наш курс, промокод СКИДКА") for _ in range(20)]
        m = subs.compute_metrics(Chat(title="Витрина"), posts, members=1000)
        self.assertLess(subs.score_channel(m)["score"], subs.CANDIDATE_THRESHOLD)

    def test_score_dead_channel_low(self):
        posts = [_post(views=10, reactions=0, forwards=0) for _ in range(20)]
        m = subs.compute_metrics(Chat(title="Мёртвый"), posts, members=1000)
        self.assertLess(subs.score_channel(m)["score"], subs.CANDIDATE_THRESHOLD)

    def test_members_recorded(self):
        posts = [_post() for _ in range(5)]
        m = subs.compute_metrics(Chat(title="T"), posts, members=1000)
        self.assertEqual(m["members"], 1000)
        self.assertEqual(m["posts"], 5)

    def test_reactions_as_message_reactions_object(self):
        # Pyrogram: Message.reactions — MessageReactions со списком .reactions, не итерируемый
        wrapper = Chat(reactions=[Chat(count=3), Chat(count=4)])
        post = _post()
        post.reactions = wrapper
        m = subs.compute_metrics(Chat(title="T"), [post], members=100)
        self.assertEqual(m["er"], 9.0)  # (7 реакций + 2 forwards)/100 views * 100

    def test_post_text_survives_pyrogram_str_slicing(self):
        # Pyrogram Str.__getitem__ режет по utf-16 суррогатам и падает —
        # _post_text обязан резать plain str
        class BadStr(str):
            def __getitem__(self, item):
                raise UnicodeDecodeError("utf-16-le", b"x", 0, 1, "boom")

        post = _post()
        post.text = BadStr("обычный текст поста")
        self.assertEqual(verdict._post_text(post), "обычный текст поста")

        def call(state, questions):
            self.assertEqual(state["posts"], ["обычный текст поста"])

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="news"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.1),
                    "ads": A(noul=0.1),
                    "course_seller": A(noul=0.1),
                }
            )

        v = verdict.evaluate("Канал", [post], call=call)
        self.assertEqual(v["voice"], "news")

    def test_growth_and_age_computed_when_created_at_provided(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=60)
        post = _post(views=100)
        post.date = now
        m = compute_metrics(Chat(title="T"), [post], members=6000, created_at=created)
        self.assertIsNotNone(m["age_days"])
        self.assertAlmostEqual(m["age_days"], 60.0, delta=1.0)
        self.assertIsNotNone(m["growth_per_month"])
        self.assertGreater(m["growth_per_month"], 2500)

    def test_score_bot_farm_hard_flag(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=45)
        # 120k subs, 2000 views per post -> ERR ~ 1.67% < 5%
        posts = [_post(views=2000, day=i) for i in range(10)]
        for p in posts:
            p.date = now
        m = compute_metrics(Chat(title="Бот-ферма"), posts, members=120_000, created_at=created)
        r = score_channel(m)
        self.assertLessEqual(r["score"], 30)
        self.assertTrue(any("накрутка ботами" in f for f in r["flags"]))

    def test_score_abandoned_channel_hard_flag(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=8 * 365)
        # 50k subs, 100 views -> ERR = 0.2% < 5%, 2 posts across 180 days -> freq < 1/mo
        p1 = _post(views=100)
        p1.date = now - timedelta(days=180)
        p2 = _post(views=100)
        p2.date = now
        m = compute_metrics(Chat(title="Заброшенный"), [p1, p2], members=50_000, created_at=created)
        r = score_channel(m)
        self.assertLessEqual(r["score"], 30)
        self.assertTrue(any("заброшенный канал" in f for f in r["flags"]))

    def test_fetch_creation_date(self):
        created = datetime(2020, 1, 1, tzinfo=timezone.utc)
        app_ok = Chat(get_messages=lambda cid, mid: Chat(date=created, empty=False))
        self.assertEqual(fetch_creation_date(app_ok, 123), created)

        app_err = Chat(get_messages=lambda cid, mid: (_ for _ in ()).throw(RuntimeError("fail")))
        self.assertIsNone(fetch_creation_date(app_err, 123))

        app_empty = Chat(get_messages=lambda cid, mid: Chat(empty=True, date=None))
        self.assertIsNone(fetch_creation_date(app_empty, 123))


class CatalogTests(unittest.TestCase):
    def test_channel_subscriptions_filters_channels(self):
        app = Chat(
            get_dialogs=lambda limit: [
                Chat(chat=Chat(id=1, type=enums.ChatType.CHANNEL, title="Канал")),
                Chat(chat=Chat(id=2, type=enums.ChatType.SUPERGROUP, title="Группа")),
            ]
        )
        self.assertEqual([c.id for c in subs.channel_subscriptions(app)], [1])
    def test_scan_channels_extracts_description(self):
        full_chat = Chat(members_count=100, description="Био канала")
        app = Chat(
            get_dialogs=lambda limit: [Chat(chat=Chat(id=1, type=enums.ChatType.CHANNEL, title="Канал"))],
            get_chat=lambda cid: full_chat,
            get_chat_history=lambda cid, limit: [_post()],
            get_messages=lambda cid, mid: None,
        )
        results = clean_my_subs.scan_channels(app, log=lambda *_: None)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["description"], "Био канала")


class LeaveTests(unittest.TestCase):
    def test_leave_chats_with_floodwait(self):
        calls = []

        class Flaky:
            def __init__(self) -> None:
                self.n = 0

            def leave_chat(self, chat_id):
                calls.append(chat_id)
                if self.n == 0:
                    self.n += 1
                    raise FloodWait(value=1)
                return True

        sleeps: list[float] = []
        chats = [Chat(id=5, title="Канал")]
        done = subs.leave_chats(
            Flaky(), chats, sleep=sleeps.append, rng=lambda a, b: a, log=lambda *_: None
        )
        self.assertEqual([c.id for c in done], [5])
        self.assertEqual(calls, [5, 5])
        self.assertEqual(sleeps, [2.0])  # int(1) FloodWait + jitter 1.0


class CompositionTests(unittest.TestCase):
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

    def test_clean_subscriptions_end_to_end(self):
        chat = Chat(id=42, title="Рекламный", username="ads")
        candidate = {
            "chat": chat,
            "score": 20,
            "flags": [],
            "metrics": {"err": 3},
            "posts": [_post()],
        }
        evaluated: list[str] = []
        seen_choices: list = []
        left: list[int] = []

        def evaluate_fn(title, posts):
            evaluated.append(title)
            return {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.8,
                "course_seller": 0.1,
            }
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=evaluate_fn,
            ask=lambda choices: seen_choices.extend(choices)
            or [c["chat"] for c in [candidate]],
            confirm=lambda msg: True,
            leave=lambda app, chats, **kw: left.extend(c.id for c in chats) or chats,
            log=lambda *_: None,
        )
        self.assertEqual(evaluated, ["Рекламный"])
        self.assertEqual(left, [42])
        self.assertTrue(any("скор 20/100" in str(c[0]) for c in seen_choices))

    def test_clean_subscriptions_deselected_leaves_nothing(self):
        chat = Chat(id=7, title="Полезный")
        candidate = {"chat": chat, "score": 10, "flags": ["x"], "metrics": {}, "posts": []}
        left: list[int] = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=lambda *a: None,
            ask=lambda choices: [],
            confirm=lambda m: True,
            leave=lambda app, chats, **kw: left.extend(c.id for c in chats) or chats,
            log=lambda *_: None,
        )
        self.assertEqual(left, [])

    def test_text_fail_slop_at_threshold_adds_candidate(self):
        chat = Chat(id=11, title="Слоп-ньюс")
        scanned = {
            "chat": chat,
            "score": 90,
            "flags": [],
            "metrics": {},
            "posts": [_post()],
        }
        v = {
            "voice": "news",
            "slop": 0.5,
            "clickbait": 0.1,
            "ads": 0.1,
            "course_seller": 0.1,
        }
        self.assertIn("slop", text_reasons(v))
        seen_choices: list = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [scanned],
            restore=lambda app: None,
            evaluate_fn=lambda t, p: v,
            ask=lambda choices: seen_choices.extend(choices) or [],
            confirm=lambda m: True,
            leave=lambda *a, **k: [],
            log=lambda *_: None,
        )
        self.assertEqual(len(seen_choices), 1)
        title, _, checked = seen_choices[0]
        self.assertTrue(checked)
        self.assertIn("слоп", title)

    def test_text_fail_below_threshold_healthy_channel_skipped(self):
        chat = Chat(id=12, title="Здоровый")
        scanned = {
            "chat": chat,
            "score": 90,
            "flags": [],
            "metrics": {},
            "posts": [_post()],
        }
        v = {
            "voice": "deep",
            "slop": 0.49,
            "clickbait": 0.1,
            "ads": 0.1,
            "course_seller": 0.1,
        }
        self.assertEqual(text_reasons(v), [])
        asked = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [scanned],
            restore=lambda app: None,
            evaluate_fn=lambda t, p: v,
            ask=lambda choices: asked.append(choices) or [],
            confirm=lambda m: True,
            leave=lambda *a, **k: [],
            log=lambda *_: None,
        )
        self.assertEqual(asked, [])

    def test_authorial_fun_low_score_unchecked_without_flags(self):
        chat = Chat(id=13, title="Мемы")
        scanned = {
            "chat": chat,
            "score": 20,
            "flags": [],
            "metrics": {},
            "posts": [_post()],
        }
        v = {
            "voice": "authorial_fun",
            "slop": 0.1,
            "clickbait": 0.1,
            "ads": 0.1,
            "course_seller": 0.1,
        }
        seen_choices: list = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [scanned],
            restore=lambda app: None,
            evaluate_fn=lambda t, p: v,
            ask=lambda choices: seen_choices.extend(choices) or [],
            confirm=lambda m: True,
            leave=lambda *a, **k: [],
            log=lambda *_: None,
        )
        self.assertEqual(len(seen_choices), 1)
        _, _, checked = seen_choices[0]
        self.assertFalse(checked)

    def test_authorial_fun_checked_with_fraud_flags(self):
        chat = Chat(id=14, title="Мемы с накруткой")
        scanned = {
            "chat": chat,
            "score": 20,
            "flags": ["накрутка_просмотров"],
            "metrics": {},
            "posts": [_post()],
        }
        v = {
            "voice": "authorial_fun",
            "slop": 0.1,
            "clickbait": 0.1,
            "ads": 0.1,
            "course_seller": 0.1,
        }
        seen_choices: list = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [scanned],
            restore=lambda app: None,
            evaluate_fn=lambda t, p: v,
            ask=lambda choices: seen_choices.extend(choices) or [],
            confirm=lambda m: True,
            leave=lambda *a, **k: [],
            log=lambda *_: None,
        )
        self.assertEqual(len(seen_choices), 1)
        _, _, checked = seen_choices[0]
        self.assertTrue(checked)

    def test_ads_fail_healthy_channel_added_checked(self):
        chat = Chat(id=15, title="Витрина")
        scanned = {
            "chat": chat,
            "score": 90,
            "flags": [],
            "metrics": {},
            "posts": [_post()],
        }
        v = {
            "voice": "deep",
            "slop": 0.1,
            "clickbait": 0.1,
            "ads": 0.5,
            "course_seller": 0.1,
        }
        seen_choices: list = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [scanned],
            restore=lambda app: None,
            evaluate_fn=lambda t, p: v,
            ask=lambda choices: seen_choices.extend(choices) or [],
            confirm=lambda m: True,
            leave=lambda *a, **k: [],
            log=lambda *_: None,
        )
        self.assertEqual(len(seen_choices), 1)
        title, _, checked = seen_choices[0]
        self.assertTrue(checked)
        self.assertIn("рекл 50%", title)
    def test_clean_subscriptions_passes_description_to_evaluate(self):
        chat = Chat(id=42, title="Канал с описанием", username="desc")
        candidate = {
            "chat": chat,
            "description": "Описание канала про крипту",
            "score": 50,
            "flags": [],
            "metrics": {"err": 3},
            "posts": [_post()],
        }
        passed_desc: list[str] = []

        def evaluate_fn(title, posts, description=""):
            passed_desc.append(description)
            return {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "course_seller": 0.1,
            }

        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=evaluate_fn,
            ask=lambda choices: [],
            confirm=lambda msg: True,
            leave=lambda *a, **kw: [],
            log=lambda *_: None,
        )
        self.assertEqual(passed_desc, ["Описание канала про крипту"])

class VerdictTests(unittest.TestCase):
    def test_jev_questions_shape(self):
        for required in ("voice", "slop", "clickbait", "ads", "course_seller"):
            self.assertIn(required, QUESTIONS)
        for removed in ("usefulness", "pressure", "paid_ads", "content"):
            self.assertNotIn(removed, QUESTIONS)
        self.assertEqual(verdict_label("deep"), "глубокий")
        self.assertEqual(verdict_label("news"), "новости")
        self.assertEqual(verdict_label("authorial_fun"), "авторский юмор")
        self.assertEqual(verdict_label("noise"), "пустой поток")
    def test_choice_suffix_voice_then_jev_percents(self):
        v = {
            "voice": "news",
            "slop": 0.72,
            "clickbait": 0.08,
            "ads": 0.81,
            "course_seller": 0.04,
        }
        suffix, checked = choice_suffix(v, False)
        self.assertEqual(
            suffix, " | новости | слоп 72% клик 8% рекл 81% курсы 4%"
        )
        self.assertTrue(checked)
        fraud, _ = choice_suffix(v, True, ["накрутка просмотров"])
        self.assertEqual(
            fraud,
            " | новости | накрутка | слоп 72% клик 8% рекл 81% курсы 4%",
        )

    def test_evaluate_fakes_call(self):
        def call(state, questions):
            self.assertEqual(state["channel"], "Канал")
            self.assertEqual(len(state["posts"]), 1)
            self.assertNotIn("metrics", state)

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="deep"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.2),
                    "ads": A(noul=0.3),
                    "course_seller": A(noul=0.4),
                }
            )

        v = verdict.evaluate("Канал", [_post()], call=call)
        self.assertEqual(v["voice"], "deep")
        self.assertAlmostEqual(v["slop"], 0.1)
        self.assertNotIn("usefulness", v)
        self.assertNotIn("confidence", v)

    def test_evaluate_api_error_returns_error_dict(self):
        def boom(state, questions):
            raise RuntimeError("no key")

        v = verdict.evaluate("Канал", [_post()], call=boom)
        self.assertIsNotNone(v)
        self.assertIn("error", v)
    def test_evaluate_description_included_and_truncated(self):
        def call(state, questions):
            self.assertEqual(state["channel"], "Канал")
            self.assertEqual(state["description"], "x" * 500)
            self.assertEqual(len(state["description"]), 500)
            self.assertNotIn("metrics", state)

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="deep"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.2),
                    "ads": A(noul=0.3),
                    "course_seller": A(noul=0.4),
                }
            )

        v = verdict.evaluate("Канал", [_post()], description="x" * 600, call=call)
        self.assertIsNotNone(v)

    def test_evaluate_none_description_defaults_to_empty(self):
        def call(state, questions):
            self.assertEqual(state["description"], "")

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="deep"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.2),
                    "ads": A(noul=0.3),
                    "course_seller": A(noul=0.4),
                }
            )

        v = verdict.evaluate("Канал", [_post()], description=None, call=call)
        self.assertIsNotNone(v)
    def test_load_typesafe_env_setdefault(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "TYPESAFE_API_KEY=from-file\nTYPESAFE_BASE_URL=https://openrouter.ai/api\n",
                encoding="utf-8",
            )
            for key in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL"):
                old = os.environ.pop(key, None)
                # LIFO-безопасно: один cleanup на ключ — вернуть старое или удалить
                self.addCleanup(
                    os.environ.__setitem__ if old is not None else os.environ.pop,
                    key,
                    old,
                )

            os.environ["TYPESAFE_API_KEY"] = "from-env"  # окружение выигрывает
            verdict.load_typesafe_env(env_file)
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "from-env")
            self.assertEqual(os.environ["TYPESAFE_BASE_URL"], "https://openrouter.ai/api")


if __name__ == "__main__":
    unittest.main()
