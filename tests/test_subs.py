"""Leave retries, checkbox style, and Jev verdict. Routing lives in test_review."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Chat

from pyrogram.errors import FloodWait

import subs
import verdict
from clean_my_subs import choice_row
from review import Candidate
from verdict import QUESTIONS, is_valid_verdict, text_reasons, verdict_label


def _post(text="мысли о жизни"):
    return Chat(id=1, text=text, caption=None, date=None, views=100, reactions=None, forwards=0)


class LeaveTests(unittest.TestCase):
    def test_leave_chat_retries_floodwait(self):
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
        done = subs.leave_chat(
            Flaky(),
            5,
            sleep=sleeps.append,
            rng=lambda a, b: a,
            log=lambda *_: None,
        )
        self.assertTrue(done)
        self.assertEqual(calls, [5, 5])
        self.assertEqual(sleeps, [2.0])


class StyleTests(unittest.TestCase):
    def test_red_label_is_styled_and_not_a_routing_rule(self):
        chat = Chat(id=1, title="Канал", username="k")
        title, value, checked = choice_row(
            Candidate("red", ["нейрослоп"], "news", True, chat)
        )
        self.assertEqual(value, chat)
        self.assertTrue(checked)
        self.assertEqual(title[0], ("fg:ansired", "[Отписаться]"))
        self.assertIn("нейрослоп", str(title))


class VerdictTests(unittest.TestCase):
    def test_course_sale_alone_is_not_a_text_fail(self):
        self.assertEqual(
            text_reasons({
                "voice": "deep",
                "slop": 0.1,
                "clickbait": 0.1,
                "ads": 0.1,
                "emotional_pressure": 0.1,
                "repetition": 0.1,
            }),
            [],
        )

    def test_jev_questions_shape(self):
        required = ("voice", "slop", "clickbait", "ads", "emotional_pressure", "repetition")
        for req in required:
            self.assertIn(req, QUESTIONS)
        for removed in ("course_seller", "usefulness", "pressure", "paid_ads", "content"):
            self.assertNotIn(removed, QUESTIONS)
        self.assertEqual(verdict_label("deep"), "глубокий")
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
        missing = dict(valid)
        del missing["repetition"]
        self.assertFalse(is_valid_verdict(missing))
        self.assertFalse(is_valid_verdict(dict(valid, slop=float("nan"))))
        self.assertFalse(is_valid_verdict(dict(valid, slop=1.5)))
        self.assertFalse(is_valid_verdict({"error": "fail"}))

    def test_evaluate_does_not_receive_metrics(self):
        def call(state, questions):
            self.assertEqual(state["channel"], "Канал")
            self.assertNotIn("metrics", state)
            self.assertNotIn("score", state)

            class A:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            return Chat(answers={
                "voice": A(choice="deep"),
                "slop": A(noul=0.1),
                "clickbait": A(noul=0.2),
                "ads": A(noul=0.3),
                "emotional_pressure": A(noul=0.4),
                "repetition": A(noul=0.5),
            })

        result = verdict.evaluate("Канал", [_post()], call=call)
        self.assertEqual(result["voice"], "deep")
        self.assertNotIn("course_seller", result)

    def test_evaluate_api_error_returns_error_dict(self):
        def boom(state, questions):
            raise RuntimeError("no key")

        result = verdict.evaluate("Канал", [_post()], call=boom)
        self.assertIn("error", result)

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
