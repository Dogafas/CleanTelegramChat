"""Leave retries, checkbox style, and Jev verdict. Routing lives in test_review."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Chat

from pyrogram.errors import FloodWait

import subs
import verdict
from clean_my_subs import _manage_action_plan, choice_row
from plan import ActionPlan
from review import Candidate
from tests.fakes import FakeUi
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

    def test_choice_row_supports_action_note_and_checked_override(self):
        chat = Chat(id=2, title="Канал 2", username="k2")
        title, value, checked = choice_row(
            Candidate("yellow", ["частые посты"], None, False, chat),
            checked=True,
            action_note="👉 в плане: без звука",
        )
        self.assertEqual(value, chat)
        self.assertTrue(checked)
        self.assertIn("👉 в плане: без звука", str(title))


class ActionPlanStatePreservationTests(unittest.TestCase):
    def test_selection_state_preserved_across_interactions(self):
        chat1 = Chat(id=1, title="Канал 1", username="c1")
        chat2 = Chat(id=2, title="Канал 2", username="c2")
        candidates = [
            Candidate("red", ["нейрослоп"], None, True, chat1),
            Candidate("yellow", ["частые посты"], None, False, chat2),
        ]
        plan = ActionPlan()
        state: dict = {}

        prompted_choices: list = []

        def fake_ask_chats(choices, message=""):
            prompted_choices.append([(c[1].id, c[2]) for c in choices])
            return [chat2]

        actions_sequence = ["leave", "back"]

        def fake_select(msg, choices):
            return actions_sequence.pop(0)

        _manage_action_plan(
            candidates,
            plan,
            state,
            ui=FakeUi(
                chats=fake_ask_chats, select=fake_select, text=lambda _message: "Чтение"
            ),
            app=None,
            log=lambda *_: None,
        )

        # Первый вход: chat1 был True (red default), chat2 был False
        self.assertEqual(prompted_choices[0], [(1, True), (2, False)])
        self.assertTrue(plan.actions[2].leave)
        self.assertNotIn(1, plan.actions)

        # Повторный вход с тем же state: chat2 остаётся True, chat1 остаётся False
        prompted_choices.clear()
        actions_sequence = ["leave", "back"]

        _manage_action_plan(
            candidates,
            plan,
            state,
            ui=FakeUi(
                chats=fake_ask_chats, select=fake_select, text=lambda _message: "Чтение"
            ),
            app=None,
            log=lambda *_: None,
        )

        self.assertEqual(prompted_choices[0], [(1, False), (2, True)])

    def test_esc_cancel_preserves_existing_state(self):
        chat1 = Chat(id=10, title="Канал 10")
        candidates = [Candidate("red", ["спам"], None, True, chat1)]
        plan = ActionPlan()
        plan.set_leave(10, "Канал 10")
        state = {"leave_initialized": True}

        actions_sequence = ["leave", "back"]
        _manage_action_plan(
            candidates,
            plan,
            state,
            ui=FakeUi(
                chats=lambda *_: None,
                select=lambda *_, **__: actions_sequence.pop(0),
                text=lambda _message: "",
            ),
            app=None,
            log=lambda *_: None,
        )

        self.assertTrue(plan.actions[10].leave)

    def test_unchecked_mode_menu_keeps_leave_plan(self):
        chat1 = Chat(id=10, title="Канал 10")
        candidates = [Candidate("red", ["спам"], None, True, chat1)]
        plan = ActionPlan()
        plan.set_leave(10, "Канал 10")

        actions_sequence = ["mute", "back"]
        # меню «без звука»: канал не отмечен, подтверждение с пустым выбором
        _manage_action_plan(
            candidates,
            plan,
            {"leave_initialized": True},
            ui=FakeUi(
                chats=lambda *_: [],
                select=lambda *_, **__: actions_sequence.pop(0),
                text=lambda _message: "",
            ),
            app=None,
            log=lambda *_: None,
        )

        self.assertTrue(plan.actions[10].leave)


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
