"""Проверки подписок на каналы: метрики, косвенные сигналы, классификация кандидатов и UI."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as Chat
from typing import Any
from unittest import mock

from pyrogram import enums
from pyrogram.errors import FloodWait

import clean_my_subs
import subs
import verdict
from clean_my_subs import _build_candidate_row, _choices, classify_candidate
from subs import compute_metrics, fetch_creation_date, metric_signals
from ui import StyledTitle
from verdict import QUESTIONS, is_valid_verdict, text_reasons, verdict_label


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


class MetricTests(unittest.TestCase):
    def test_compute_metrics_healthy_channel(self):
        posts = [_post(views=100 + i * 3, day=i) for i in range(30, 0, -1)]
        m = compute_metrics(Chat(title="Хороший канал"), posts, members=1000)
        self.assertIsNotNone(m["err"])
        err = m["err"]
        assert err is not None
        self.assertGreater(err, 10.0)
        self.assertIsNotNone(m["cv_views"])
        signals = metric_signals(m)
        self.assertEqual(signals, [])

    def test_compute_metrics_missing_views_is_none(self):
        # Отсутствие просмотров не должно симулировать нулевой охват
        posts = [
            Chat(views=None, reactions=None, forwards=None, text="пост", caption=None, date=None)
        ]
        m = compute_metrics(Chat(title="Без просмотров"), posts, members=1000)
        self.assertIsNone(m["err"])
        self.assertIsNone(m["er"])
        self.assertIsNone(m["cv_views"])
        # Сигнал низкой вовлечённости не выставляется при отсутствии данных
        self.assertNotIn("низкая вовлечённость", " ".join(metric_signals(m)))

    def test_compute_metrics_zero_members_is_none(self):
        posts = [_post(views=100)]
        m = compute_metrics(Chat(title="Без подписчиков"), posts, members=0)
        self.assertIsNone(m["err"])
        self.assertNotIn("низкая вовлечённость", " ".join(metric_signals(m)))

    def test_metric_signals_inflated_views(self):
        posts = [_post(views=2000, reactions=30) for _ in range(20)]
        m = compute_metrics(Chat(title="Канал"), posts, members=1000)
        signals = metric_signals(m)
        self.assertTrue(any("аномально высокий охват" in s for s in signals))
        self.assertFalse(any("накрутка" in s for s in signals))

    def test_metric_signals_low_engagement(self):
        posts = [_post(views=10, reactions=0, forwards=0) for _ in range(20)]
        m = compute_metrics(Chat(title="Мёртвый"), posts, members=1000)
        signals = metric_signals(m)
        self.assertTrue(any("низкая вовлечённость" in s for s in signals))

    def test_metric_signals_uniform_reach(self):
        posts = [_post(views=100, reactions=5) for _ in range(20)]
        m = compute_metrics(Chat(title="Ровный"), posts, members=1000)
        signals = metric_signals(m)
        self.assertTrue(any("подозрительно ровный охват" in s for s in signals))
        self.assertFalse(any("боты" in s for s in signals))

    def test_metric_signals_high_frequency(self):
        # 20 постов за 5 дней -> ~120/мес > 60
        posts_dense = [_post(day=i % 5) for i in range(20)]
        m_dense = compute_metrics(Chat(title="Частый"), posts_dense, members=1000)
        signals = metric_signals(m_dense)
        self.assertTrue(any("частое обновление" in s for s in signals))

    def test_metric_signals_ad_showcase(self):
        posts = [_post(text="переходи в наш канал, промокод СКИДКА") for _ in range(20)]
        m = compute_metrics(Chat(title="Витрина"), posts, members=1000)
        signals = metric_signals(m)
        self.assertTrue(any("высокая доля рекламы" in s for s in signals))

    def test_metric_signals_rapid_growth(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=45)
        posts = [_post(views=2000, day=i) for i in range(10)]
        for p in posts:
            p.date = now
        m = compute_metrics(Chat(title="Ферма"), posts, members=120_000, created_at=created)
        signals = metric_signals(m)
        self.assertTrue(any("резкий прирост аудитории" in s for s in signals))
        self.assertFalse(any("накрутка" in s for s in signals))

    def test_metric_signals_abandoned_channel(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=8 * 365)
        p1 = _post(views=100)
        p1.date = now - timedelta(days=180)
        p2 = _post(views=100)
        p2.date = now
        m = compute_metrics(Chat(title="Заброшенный"), [p1, p2], members=50_000, created_at=created)
        signals = metric_signals(m)
        self.assertTrue(any("заброшенный канал" in s for s in signals))

    def test_reactions_as_message_reactions_object(self):
        wrapper = Chat(reactions=[Chat(count=3), Chat(count=4)])
        post = _post()
        post.reactions = wrapper
        m = compute_metrics(Chat(title="T"), [post], members=100)
        self.assertEqual(m["er"], 9.0)

    def test_growth_and_age_computed_when_created_at_provided(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        created = now - timedelta(days=60)
        post = _post(views=100)
        post.date = now
        m = compute_metrics(Chat(title="T"), [post], members=6000, created_at=created)
        self.assertIsNotNone(m["age_days"])
        age = m["age_days"]
        assert age is not None
        self.assertAlmostEqual(age, 60.0, delta=1.0)
        self.assertIsNotNone(m["growth_per_month"])
        growth = m["growth_per_month"]
        assert growth is not None
        self.assertGreater(growth, 2500)

    def test_fetch_creation_date(self):
        created = datetime(2020, 1, 1, tzinfo=timezone.utc)
        app_ok = Chat(get_messages=lambda cid, mid: Chat(date=created, empty=False))
        self.assertEqual(fetch_creation_date(app_ok, 123), created)

        app_err = Chat(get_messages=lambda cid, mid: (_ for _ in ()).throw(RuntimeError("fail")))
        self.assertIsNone(fetch_creation_date(app_err, 123))

        app_empty = Chat(get_messages=lambda cid, mid: Chat(empty=True, date=None))
        self.assertIsNone(fetch_creation_date(app_empty, 123))


class CatalogAndLeaveTests(unittest.TestCase):
    def test_channel_subscriptions_filters_channels(self):
        app = Chat(
            get_dialogs=lambda: [
                Chat(chat=Chat(id=1, type=enums.ChatType.CHANNEL, title="Канал")),
                Chat(chat=Chat(id=2, type=enums.ChatType.SUPERGROUP, title="Группа")),
            ]
        )
        self.assertEqual([c.id for c in subs.channel_subscriptions(app)], [1])

    def test_scan_channels_extracts_description(self):
        full_chat = Chat(members_count=100, description="Био канала")
        channel = Chat(id=1, type=enums.ChatType.CHANNEL, title="Канал")
        app = Chat(
            get_dialogs=lambda: [Chat(chat=channel)],
            get_chat=lambda cid: full_chat,
            get_chat_history=lambda cid, limit: [_post()],
            get_messages=lambda cid, mid: None,
        )
        results = clean_my_subs.scan_channels(app, log=lambda *_: None)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["description"], "Био канала")

    def test_scan_channels_with_floodwait(self):
        full_chat = Chat(members_count=100, description="Био")
        channel = Chat(id=1, type=enums.ChatType.CHANNEL, title="Канал")
        calls = {"get_chat": 0}

        def get_chat(cid):
            calls["get_chat"] += 1
            if calls["get_chat"] == 1:
                raise FloodWait(value=1)
            return full_chat

        app = Chat(
            get_dialogs=lambda: [Chat(chat=channel)],
            get_chat=get_chat,
            get_chat_history=lambda cid, limit: [_post()],
            get_messages=lambda cid, mid: None,
        )
        sleeps: list[float] = []
        with mock.patch("clean_my_subs.sleep", side_effect=sleeps.append):
            results = clean_my_subs.scan_channels(
                app, log=lambda *_: None, rng=lambda a, b: 1.0
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(calls["get_chat"], 2)
        self.assertEqual(sleeps, [2.0])

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
        self.assertEqual(sleeps, [2.0])


class CandidateClassificationTests(unittest.TestCase):
    def test_red_text_fail_checked_by_default(self):
        chat = Chat(id=1, title="Слоп-канал")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": [],
            "verdict": {
                "voice": "news",
                "slop": 0.6,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "red")
        self.assertEqual(cand["label"], "[Отписаться]")
        self.assertTrue(cand["checked"])
        self.assertIn("нейрослоп", cand["reasons"])

    def test_red_emotional_pressure_and_repetition(self):
        chat = Chat(id=2, title="Давящий")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": [],
            "verdict": {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.7,
                "repetition": 0.8,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "red")
        self.assertTrue(cand["checked"])
        self.assertIn("эмоциональное давление", cand["reasons"])
        self.assertIn("повторы", cand["reasons"])

    def test_yellow_metric_signals_only_unchecked_by_default(self):
        chat = Chat(id=3, title="Частый автор")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": ["частое обновление (95 постов/мес)"],
            "verdict": {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "yellow")
        self.assertEqual(cand["label"], "[Проверить]")
        self.assertFalse(cand["checked"])
        self.assertIn("частое обновление (95 постов/мес)", cand["reasons"])

    def test_gray_missing_text_unchecked_by_default(self):
        chat = Chat(id=4, title="Только картинки")
        empty_post = Chat(
            views=100, text=None, caption=None, date=None, reactions=None, forwards=None
        )
        row = {
            "chat": chat,
            "posts": [empty_post],
            "signals": ["подозрительно ровный охват"],
            "verdict": None,
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "gray")
        self.assertEqual(cand["label"], "[Недостаточно данных]")
        self.assertFalse(cand["checked"])
        self.assertIn("нет текста", cand["reasons"])
        self.assertIn("подозрительно ровный охват", cand["reasons"])

    def test_gray_verdict_error_takes_precedence_over_metrics(self):
        chat = Chat(id=5, title="Ошибка API")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": ["частое обновление (80 постов/мес)"],
            "verdict": {"error": "rate limit"},
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "gray")
        self.assertEqual(cand["label"], "[Недостаточно данных]")
        self.assertFalse(cand["checked"])
        self.assertIn("ошибка вердикта", cand["reasons"])

    def test_gray_malformed_model_output(self):
        chat = Chat(id=6, title="Битая модель")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": [],
            "verdict": {
                "voice": "news",
                "slop": 1.5,  # out of [0, 1] range!
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        self.assertEqual(cand["category"], "gray")
        self.assertFalse(cand["checked"])

    def test_clean_channel_excluded(self):
        chat = Chat(id=7, title="Чистый")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": [],
            "verdict": {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        self.assertIsNone(classify_candidate(row))

    def test_course_sale_alone_not_text_fail(self):
        # Продажа нормального курса не является независимым провалом
        v = {
            "voice": "deep",
            "slop": 0.1,
            "clickbait": 0.1,
            "ads": 0.1,
            "emotional_pressure": 0.1,
            "repetition": 0.1,
        }
        self.assertEqual(text_reasons(v), [])


class FormattingAndUITests(unittest.TestCase):
    def test_candidate_row_formatting_red(self):
        chat = Chat(id=1, title="Криптоканал", username="crypto")
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": [],
            "verdict": {
                "voice": "news",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.7,
                "repetition": 0.8,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        title, val, checked = _build_candidate_row(cand)
        self.assertEqual(val, chat)
        self.assertTrue(checked)
        self.assertIsInstance(title, StyledTitle)
        self.assertEqual(title[0], ("fg:ansired", "[Отписаться]"))
        self.assertEqual(
            str(title),
            "[Отписаться] Криптоканал (@crypto) — 1 | новости | эмоциональное давление, повторы",
        )

    def test_candidate_row_formatting_yellow(self):
        chat = Chat(id=2, title="Мысли вслух", username=None)
        row = {
            "chat": chat,
            "posts": [_post()],
            "signals": ["частое обновление (95 постов/мес)"],
            "verdict": {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        cand = classify_candidate(row)
        assert cand is not None
        title, _, checked = _build_candidate_row(cand)
        self.assertFalse(checked)
        self.assertEqual(title[0], ("fg:ansiyellow", "[Проверить]"))
        self.assertEqual(
            str(title),
            "[Проверить] Мысли вслух — 2 | глубокий | частое обновление (95 постов/мес)",
        )

    def test_candidate_row_formatting_gray(self):
        chat = Chat(id=3, title="Медиа", username=None)
        row = {
            "chat": chat,
            "posts": [
                Chat(views=None, text=None, caption=None, date=None, reactions=None, forwards=None)
            ],
            "signals": [],
            "verdict": None,
        }
        cand = classify_candidate(row)
        assert cand is not None
        title, _, checked = _build_candidate_row(cand)
        self.assertFalse(checked)
        self.assertEqual(title[0], ("fg:ansibrightblack", "[Недостаточно данных]"))
        self.assertEqual(str(title), "[Недостаточно данных] Медиа — 3 | нет текста")

    def test_sorting_priority_red_then_yellow_then_gray(self):
        chat_gray = Chat(id=1, title="Серая")
        chat_yellow = Chat(id=2, title="Жёлтая")
        chat_red = Chat(id=3, title="Красная")
        candidates = [
            {"chat": chat_gray, "posts": [], "signals": [], "verdict": None},
            {
                "chat": chat_yellow,
                "posts": [_post()],
                "signals": ["частое обновление (70 постов/мес)"],
                "verdict": {
                    "voice": "deep",
                    "slop": 0.1,
                    "clickbait": 0.1,
                    "ads": 0.1,
                    "emotional_pressure": 0.1,
                    "repetition": 0.1,
                },
            },
            {
                "chat": chat_red,
                "posts": [_post()],
                "signals": [],
                "verdict": {
                    "voice": "news",
                    "slop": 0.7,
                    "clickbait": 0.1,
                    "ads": 0.1,
                    "emotional_pressure": 0.1,
                    "repetition": 0.1,
                },
            },
        ]
        choices = _choices(candidates)
        self.assertEqual(len(choices), 3)
        self.assertIn("[Отписаться]", str(choices[0][0]))
        self.assertIn("[Проверить]", str(choices[1][0]))
        self.assertIn("[Недостаточно данных]", str(choices[2][0]))


class VerdictTests(unittest.TestCase):
    def test_jev_questions_shape(self):
        required = ("voice", "slop", "clickbait", "ads", "emotional_pressure", "repetition")
        for req in required:
            self.assertIn(req, QUESTIONS)
        for removed in ("course_seller", "usefulness", "pressure", "paid_ads", "content"):
            self.assertNotIn(removed, QUESTIONS)
        self.assertEqual(verdict_label("deep"), "глубокий")
        self.assertEqual(verdict_label("news"), "новости")
        self.assertEqual(verdict_label("authorial_fun"), "авторский юмор")
        self.assertEqual(verdict_label("noise"), "пустой поток")

    def test_is_valid_verdict(self):
        valid = {
            "voice": "deep",
            "slop": 0.1,
            "clickbait": 0.2,
            "ads": 0.3,
            "emotional_pressure": 0.4,
            "repetition": 0.5,
        }
        self.assertTrue(is_valid_verdict(valid))
        # Missing key
        invalid_missing = dict(valid)
        del invalid_missing["repetition"]
        self.assertFalse(is_valid_verdict(invalid_missing))
        # Non-finite / out of range
        invalid_nan = dict(valid, slop=float("nan"))
        self.assertFalse(is_valid_verdict(invalid_nan))
        invalid_range = dict(valid, slop=1.5)
        self.assertFalse(is_valid_verdict(invalid_range))
        # Error dict
        self.assertFalse(is_valid_verdict({"error": "fail"}))

    def test_evaluate_fakes_call(self):
        def call(state, questions):
            self.assertEqual(state["channel"], "Канал")
            self.assertEqual(len(state["posts"]), 1)
            # Числовые метрики не должны передаваться в Jev
            self.assertNotIn("metrics", state)
            self.assertNotIn("score", state)

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="deep"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.2),
                    "ads": A(noul=0.3),
                    "emotional_pressure": A(noul=0.4),
                    "repetition": A(noul=0.5),
                }
            )

        v = verdict.evaluate("Канал", [_post()], call=call)
        self.assertEqual(v["voice"], "deep")
        self.assertAlmostEqual(v["slop"], 0.1)
        self.assertAlmostEqual(v["emotional_pressure"], 0.4)
        self.assertAlmostEqual(v["repetition"], 0.5)
        self.assertNotIn("course_seller", v)

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

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(
                answers={
                    "voice": A(choice="deep"),
                    "slop": A(noul=0.1),
                    "clickbait": A(noul=0.2),
                    "ads": A(noul=0.3),
                    "emotional_pressure": A(noul=0.4),
                    "repetition": A(noul=0.5),
                }
            )

        v = verdict.evaluate("Канал", [_post()], description="x" * 600, call=call)
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

                def cleanup(k=key, v=old):
                    if v is not None:
                        os.environ[k] = v
                    else:
                        os.environ.pop(k, None)

                self.addCleanup(cleanup)

            os.environ["TYPESAFE_API_KEY"] = "from-env"
            verdict.load_typesafe_env(env_file)
            self.assertEqual(os.environ["TYPESAFE_API_KEY"], "from-env")
            self.assertEqual(os.environ["TYPESAFE_BASE_URL"], "https://openrouter.ai/api")


class CompositionEndToEndTests(unittest.TestCase):
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
            "signals": [],
            "metrics": {"err": 3},
            "posts": [_post()],
        }
        evaluated: list[str] = []
        seen_choices: list[Any] = []
        left: list[int] = []

        def evaluate_fn(title, posts, description=""):
            evaluated.append(title)
            return {
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.8,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            }

        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=evaluate_fn,
            ask=lambda choices: seen_choices.extend(choices) or [candidate["chat"]],
            confirm=lambda msg: True,
            leave=lambda app, chats, **kw: left.extend(c.id for c in chats) or chats,
            log=lambda *_: None,
        )
        self.assertEqual(evaluated, ["Рекламный"])
        self.assertEqual(left, [42])
        self.assertTrue(any("[Отписаться]" in str(c[0]) for c in seen_choices))
        self.assertTrue(any("рекламная витрина" in str(c[0]) for c in seen_choices))

    def test_clean_subscriptions_deselected_leaves_nothing(self):
        chat = Chat(id=7, title="Полезный", username="useful")
        candidate = {
            "chat": chat,
            "signals": [],
            "metrics": {},
            "posts": [_post()],
            "verdict": {
                "voice": "deep",
                "slop": 0.9,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        left: list[int] = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=lambda *a, **k: candidate["verdict"],
            ask=lambda choices: [],  # deselect everything
            confirm=lambda m: True,
            leave=lambda app, chats, **kw: left.extend(c.id for c in chats) or chats,
            log=lambda *_: None,
        )
        self.assertEqual(left, [])

    def test_clean_subscriptions_unconfirmed_leaves_nothing(self):
        chat = Chat(id=8, title="Канал", username=None)
        candidate = {
            "chat": chat,
            "signals": [],
            "metrics": {},
            "posts": [_post()],
            "verdict": {
                "voice": "deep",
                "slop": 0.9,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            },
        }
        left: list[int] = []
        clean_my_subs.clean_subscriptions(
            ["s"],
            client=self._client(Chat(id=1)),
            scan=lambda app, log: [candidate],
            restore=lambda app: None,
            evaluate_fn=lambda *a, **k: candidate["verdict"],
            ask=lambda choices: [candidate["chat"]],
            confirm=lambda m: False,  # User says NO!
            leave=lambda app, chats, **kw: left.extend(c.id for c in chats) or chats,
            log=lambda *_: None,
        )
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
