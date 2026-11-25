"""Unit tests for bot scan, signals, candidates, actions, probe (ADR 0011)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyrogram import enums
from pyrogram.errors import FloodWait

from bots import BotInfo, bot_signals, classify, probe_bot, scan_bots
from clean_my_bots import _bot_row, _ordered, _probe_menu
from plan import (
    FOREVER_MUTE,
    ActionPlan,
    BotAction,
    apply_action_plan,
    apply_bot_actions,
    load_rollback_journal,
    rollback_session,
)
from review import Candidate


def must(cand: Candidate | None) -> Candidate:
    """Кандидат обязателен в этих сценариях — падение здесь, а не на None.attribute."""
    assert cand is not None
    return cand

_NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def NO_SLEEP(_seconds: float) -> None:
    pass


def msg(
    *,
    days_ago: float = 1.0,
    outgoing: bool = False,
    text: str = "",
    entities: tuple[Any, ...] = (),
    caption_entities: tuple[Any, ...] = (),
    service: bool = False,
    mid: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        date=_NOW - timedelta(days=days_ago),
        outgoing=outgoing,
        service=service,
        text=text or None,
        entities=list(entities),
        caption_entities=list(caption_entities),
    )


def bot_dialog(chat_id: int = 1) -> SimpleNamespace:
    chat = SimpleNamespace(
        id=chat_id, type=enums.ChatType.BOT, first_name=f"Бот{chat_id}", username=f"bot{chat_id}"
    )
    return SimpleNamespace(chat=chat)


def make_info(**kw: Any) -> BotInfo:
    chat_id = kw.pop("chat_id", 1)
    return BotInfo(dialog=bot_dialog(chat_id), chat_id=chat_id, archived=False, **kw)


def verdict(**noul: float) -> dict[str, Any]:
    base: dict[str, Any] = {
        "voice": "noise",
        "slop": 0.1,
        "clickbait": 0.1,
        "ads": 0.1,
        "emotional_pressure": 0.1,
        "repetition": 0.1,
    }
    base.update(noul)
    return base


def url_entity() -> SimpleNamespace:
    return SimpleNamespace(type=enums.MessageEntityType.URL)


class FakeApp:
    """Фейковый клиент по образцу тестов плана: диалоги, профили, истории, блокировки."""

    def __init__(
        self,
        *,
        dialogs: tuple[Any, ...] = (),
        archived: tuple[Any, ...] = (),
        blocked: dict[int, bool] | None = None,
        users: dict[int, Any] | None = None,
        chats: dict[int, Any] | None = None,
        histories: dict[int, list[Any]] | None = None,
        fail_users: tuple[int, ...] = (),
        fail_histories: tuple[int, ...] = (),
    ) -> None:
        self._main = list(dialogs)
        self._archived = list(archived)
        self.blocked = dict(blocked or {})
        self.users = users or {}
        self.chats = chats or {}
        self.histories = histories or {}
        self.fail_users = set(fail_users)
        self.fail_histories = set(fail_histories)
        self.calls: list[tuple[str, int]] = []

    def get_dialogs(self, from_archive: bool) -> list[Any]:
        return self._archived if from_archive else self._main

    def get_users(self, user_id: int) -> Any:
        self.calls.append(("get_users", user_id))
        if user_id in self.fail_users:
            raise ValueError("user rpc down")
        return self.users.get(user_id, SimpleNamespace(is_deleted=False))

    def get_chat(self, chat_id: int) -> Any:
        self.calls.append(("get_chat", chat_id))
        return self.chats.get(chat_id, SimpleNamespace(bio=""))

    def get_chat_history(self, chat_id: int, limit: int = 0) -> Any:
        self.calls.append(("history", chat_id))
        if chat_id in self.fail_histories:
            raise ValueError("history rpc down")
        return iter(self.histories.get(chat_id, []))

    def resolve_peer(self, chat_id: int) -> Any:
        return SimpleNamespace(user_id=chat_id, access_hash=1)

    def invoke(self, req: Any) -> Any:
        if "GetFullUser" in type(req).__name__:
            blocked = self.blocked.get(req.id.user_id, False)
            return SimpleNamespace(full_user=SimpleNamespace(blocked=blocked))
        return None

    def block_user(self, chat_id: int) -> bool:
        self.calls.append(("block", chat_id))
        self.blocked[chat_id] = True
        return True

    def unblock_user(self, chat_id: int) -> bool:
        self.calls.append(("unblock", chat_id))
        self.blocked[chat_id] = False
        return True

    def delete_chat_history(self, chat_id: int) -> int:
        self.calls.append(("delete", chat_id))
        return 1

    def send_message(self, chat_id: int, text: str) -> Any:
        self.calls.append(("send", chat_id))
        if chat_id in self.fail_histories:  # переиспользуем маркер сбоя
            raise ValueError("send rpc down")
        return SimpleNamespace(id=1000, outgoing=True)


class BotActionsTests(unittest.TestCase):
    def test_apply_stop_and_rollback(self):
        app = FakeApp()
        with tempfile.TemporaryDirectory() as td:
            reports = apply_bot_actions(
                app, [BotAction(700, "Бот 700", stop=True)], session_name="b1", journal_dir=Path(td)
            )
            self.assertEqual(reports[0]["status"], "applied")
            self.assertTrue(app.blocked[700])
            loaded = load_rollback_journal("b1", root_dir=Path(td))
            self.assertEqual(loaded[0]["action"], "stop")
            self.assertEqual(loaded[0]["prev_state"], {"blocked": False})
            self.assertEqual(loaded[0]["applied_state"], {"blocked": True})

            results = rollback_session(app, session_name="b1", journal_dir=Path(td))
            self.assertEqual(results[0]["status"], "restored")
            self.assertFalse(app.blocked[700])
            self.assertEqual(load_rollback_journal("b1", root_dir=Path(td)), [])

    def test_stop_already_blocked_is_noop(self):
        # уже заблокированный бот: RPC block_user не вызывается, записи в журнале нет
        app = FakeApp(blocked={701: True})
        with tempfile.TemporaryDirectory() as td:
            reports = apply_bot_actions(
                app, [BotAction(701, "Бот 701", stop=True)], session_name="b2", journal_dir=Path(td)
            )
            self.assertEqual(reports[0]["status"], "already_set")
            self.assertNotIn(("block", 701), app.calls)
            self.assertEqual(load_rollback_journal("b2", root_dir=Path(td)), [])
            self.assertEqual(rollback_session(app, session_name="b2", journal_dir=Path(td)), [])

    def test_rollback_skips_if_user_manually_unblocked(self):
        # state-guard: пользователь разблокировал бота сам — откат не блокирует повторно
        app = FakeApp()
        with tempfile.TemporaryDirectory() as td:
            apply_bot_actions(
                app, [BotAction(702, "Бот 702", stop=True)], session_name="b3", journal_dir=Path(td)
            )
            app.blocked[702] = False  # ручная разблокировка после apply
            results = rollback_session(app, session_name="b3", journal_dir=Path(td))
            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("вручную", results[0]["detail"])
            self.assertNotIn(("unblock", 702), app.calls)

    def test_delete_chat_is_recorded_not_restorable(self):
        app = FakeApp()
        with tempfile.TemporaryDirectory() as td:
            reports = apply_bot_actions(
                app,
                [BotAction(703, "Бот 703", delete=True)],
                session_name="b4",
                journal_dir=Path(td),
            )
            self.assertEqual(reports[0]["status"], "applied")
            self.assertIn(("delete", 703), app.calls)
            loaded = load_rollback_journal("b4", root_dir=Path(td))
            self.assertEqual(loaded[0]["action"], "delete_chat")
            self.assertIs(loaded[0]["restorable"], False)

            results = rollback_session(app, session_name="b4", journal_dir=Path(td))
            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("необратимо", results[0]["detail"])
            self.assertNotIn(("unblock", 703), app.calls)

    def test_stop_precedes_delete_in_apply_and_journal(self):
        app = FakeApp()
        with tempfile.TemporaryDirectory() as td:
            reports = apply_bot_actions(
                app,
                [BotAction(704, "Бот 704", stop=True, delete=True)],
                session_name="b5",
                journal_dir=Path(td),
            )
            self.assertEqual([r["action"] for r in reports], ["stop", "delete_chat"])
            self.assertEqual(app.calls, [("block", 704), ("delete", 704)])
            loaded = load_rollback_journal("b5", root_dir=Path(td))
            self.assertEqual([r["action"] for r in loaded], ["stop", "delete_chat"])

    def test_mutation_error_reported(self):
        class BrokenApp(FakeApp):
            def block_user(self, chat_id: int) -> bool:
                raise ValueError("rpc down")

        app = BrokenApp()
        with tempfile.TemporaryDirectory() as td:
            reports = apply_bot_actions(
                app, [BotAction(705, "Бот 705", stop=True)], session_name="b6", journal_dir=Path(td)
            )
            self.assertEqual(reports[0]["status"], "error")
            self.assertEqual(load_rollback_journal("b6", root_dir=Path(td)), [])


class SignalsTests(unittest.TestCase):
    def test_silence_signal(self):
        self.assertNotIn("молчит ≥ 3 мес.", bot_signals([msg(days_ago=10)], now=_NOW))
        self.assertIn("молчит ≥ 3 мес.", bot_signals([msg(days_ago=90)], now=_NOW))
        self.assertIn("молчит ≥ 3 мес.", bot_signals([], now=_NOW))

    def test_unused_counts_full_history_not_slice(self):
        # 2 исходящих за всю историю (свежее + старое) — «использован»
        history = [
            msg(days_ago=1, outgoing=True, text="стоп"),
            msg(days_ago=400, outgoing=True, text="/start"),
            msg(days_ago=2, text="ответ"),
        ]
        self.assertNotIn("не использован", bot_signals(history, now=_NOW))
        # только /start когда-то — «не использован»
        self.assertIn("не использован", bot_signals(history[1:], now=_NOW))

    def test_spam_after_outgoing(self):
        noisy = [msg(), msg(), msg(), msg(outgoing=True), msg(), msg()]
        self.assertIn("спам без участия", bot_signals(noisy, now=_NOW))
        engaged = [msg(), msg(outgoing=True), msg(), msg()]
        self.assertNotIn("спам без участия", bot_signals(engaged, now=_NOW))

    def test_ad_share_markers_entities_and_denominator(self):
        ads = [msg(text="подпишись на курс!") for _ in range(3)]
        entity_ads = [msg(entities=(url_entity(),)) for _ in range(1)]
        plain = [msg(text="привет") for _ in range(8)]
        self.assertIn("рекламная витрина", bot_signals(ads + entity_ads + plain, now=_NOW))
        self.assertNotIn("рекламная витрина", bot_signals(ads[:2] + plain, now=_NOW))
        # исходящие и сервисные не в знаменателе
        self.assertNotIn(
            "рекламная витрина",
            bot_signals([msg(text="купить", outgoing=True), *plain], now=_NOW),
        )


class RoutingTests(unittest.TestCase):
    def test_deleted_is_red_checked(self):
        cand = must(classify(make_info(deleted=True)))
        self.assertEqual(cand.category, "red")
        self.assertTrue(cand.checked)
        self.assertIn("удалён / забанен", cand.reasons)

    def test_probe_silent_is_red(self):
        cand = must(classify(make_info(probe="silent")))
        self.assertEqual(cand.category, "red")
        self.assertTrue(cand.checked)

    def test_history_error_is_gray(self):
        cand = must(classify(make_info(error="ValueError: x")))
        self.assertEqual(cand.category, "gray")
        self.assertFalse(cand.checked)

    def test_verdict_error_is_gray(self):
        info = make_info(messages=[msg(text="привет")], verdict={"error": "boom"})
        self.assertEqual(must(classify(info)).category, "gray")

    def test_signals_are_yellow_unchecked(self):
        info = make_info(signals=["молчит ≥ 3 мес."])
        cand = must(classify(info))
        self.assertEqual(cand.category, "yellow")
        self.assertFalse(cand.checked)

    def test_no_texts_is_gray(self):
        cand = must(classify(make_info(messages=[msg(text="  ")])))
        self.assertEqual(cand.category, "gray")

    def test_healthy_bot_is_not_candidate(self):
        info = make_info(
            messages=[
                msg(text="ответ"),
                msg(text="привет"),
                msg(outgoing=True, text="ещё раз"),
            ]
        )
        self.assertIsNone(classify(info))

    def test_verdict_ads_with_unused_is_red_checked(self):
        info = make_info(signals=["не использован"], verdict=verdict(ads=0.9))
        cand = must(classify(info))
        self.assertEqual(cand.category, "red")
        self.assertTrue(cand.checked)
        self.assertEqual(cand.reasons, ["рекламная витрина", "не использован"])

    def test_verdict_ads_without_unused_is_yellow(self):
        info = make_info(messages=[msg(text="текст")], verdict=verdict(ads=0.9))
        cand = must(classify(info))
        self.assertEqual(cand.category, "yellow")
        self.assertIn("рекламная витрина", cand.reasons)

    def test_verdict_slop_joins_signals_in_yellow(self):
        info = make_info(
            signals=["молчит ≥ 3 мес."], verdict=verdict(slop=0.9, repetition=0.8)
        )
        cand = must(classify(info))
        self.assertEqual(cand.category, "yellow")
        self.assertEqual(
            cand.reasons,
            ["молчит ≥ 3 мес.", "нейрослоп", "повторы", "пустой поток"],
        )


class ScenarioOrderTests(unittest.TestCase):
    def test_ordering_red_yellow_gray_then_healthy(self):
        healthy = make_info(
            chat_id=1,
            messages=[
                msg(text="т"),
                msg(outgoing=True, text="х"),
                msg(outgoing=True, text="и"),
            ],
        )
        yellow = make_info(chat_id=2, signals=["молчит ≥ 3 мес."])
        gray = make_info(chat_id=3, error="x")
        red = make_info(chat_id=4, deleted=True)
        ordered = _ordered([healthy, yellow, gray, red])
        self.assertEqual([info.dialog.chat.id for info, _ in ordered], [4, 2, 3, 1])
        # RED предотмечен в чекбоксе, остальные нет
        checks = {row[1]: row[2] for row in (_bot_row(i, c) for i, c in ordered)}
        self.assertTrue(checks[4])
        self.assertFalse(checks[2])
        self.assertFalse(checks[3])
        self.assertFalse(checks[1])

    def test_probe_menu_requires_explicit_confirm(self):
        app = FakeApp()
        infos = [make_info(chat_id=9)]
        calls = {"probe": 0}

        def fake_probe(app_, chat_id, **kw):
            calls["probe"] += 1
            return "silent"

        _probe_menu(
            app,
            infos,
            restore=lambda a: None,
            ask=lambda choices, message: [],
            confirm=lambda m: False,
            probe_fn=fake_probe,
            log=lambda m: None,
        )
        self.assertEqual(calls["probe"], 0)

        confirmed = []

        def fake_ask(choices, message):
            confirmed.append(message)
            return [9]

        _probe_menu(
            app,
            infos,
            restore=lambda a: None,
            ask=fake_ask,
            confirm=lambda m: True,
            probe_fn=fake_probe,
            log=lambda m: None,
        )
        self.assertEqual(calls["probe"], 1)
        self.assertEqual(infos[0].probe, "silent")
        self.assertEqual(must(classify(infos[0])).category, "red")


class ScanTests(unittest.TestCase):
    def test_scan_covers_archive_and_computes_signals(self):
        dialog = bot_dialog(1)
        app = FakeApp(
            archived=(dialog,),
            chats={1: SimpleNamespace(bio="полезный бот")},
            histories={
                1: [
                    msg(days_ago=5, text="ответ"),
                    msg(days_ago=10, outgoing=True, text="/start"),
                ]
            },
        )
        seen: dict[str, Any] = {}

        def fake_evaluate(name, posts, *, description="", **kw):
            seen.update(name=name, posts=posts, description=description)
            return verdict()

        infos = scan_bots(
            app,
            lambda m: None,
            sleep=NO_SLEEP,
            rng=random_uniform,
            evaluate=fake_evaluate,
        )
        self.assertEqual(len(infos), 1)
        info = infos[0]
        self.assertTrue(info.archived)
        self.assertEqual(info.bio, "полезный бот")
        self.assertEqual(info.outgoing, 1)
        self.assertIn("не использован", info.signals)
        # маппинг Jev: имя бота → channel, тексты → posts, bio → description
        self.assertEqual(seen["name"], info.dialog.chat.first_name)
        self.assertEqual(seen["posts"], info.messages)
        self.assertEqual(seen["description"], "полезный бот")
        assert info.verdict is not None
        self.assertTrue(info.verdict.get("voice"))

    def test_scan_deleted_bot_skips_history(self):
        dialog = bot_dialog(2)
        app = FakeApp(dialogs=(dialog,), fail_users=(2,))
        infos = scan_bots(app, lambda m: None, sleep=NO_SLEEP, rng=random_uniform)
        self.assertTrue(infos[0].deleted)
        self.assertEqual([c for c in app.calls if c[0] == "history"], [])


def random_uniform(*_args: float) -> float:
    return 0.0


class ProbeTests(unittest.TestCase):
    def test_silent_after_start(self):
        app = FakeApp(histories={5: []})
        sleeps: list[float] = []
        result = probe_bot(app, 5, wait_s=6, poll_s=2, sleep=sleeps.append)
        self.assertEqual(result, "silent")
        self.assertEqual(len(sleeps), 3)

    def test_reply_within_wait(self):
        app = FakeApp(histories={6: [msg(mid=1001)]})
        self.assertEqual(probe_bot(app, 6, wait_s=6, poll_s=2, sleep=lambda _s: None), "replied")

    def test_own_start_message_is_not_a_reply(self):
        # последнее сообщение — наш собственный /start, это не ответ бота
        app = FakeApp(histories={7: [msg(mid=1000, outgoing=True)]})
        self.assertEqual(probe_bot(app, 7, wait_s=4, poll_s=2, sleep=lambda _s: None), "silent")

    def test_send_failure_is_error(self):
        app = FakeApp(fail_histories=(8,))
        self.assertEqual(probe_bot(app, 8, wait_s=6, poll_s=2, sleep=lambda _s: None), "error")

    def test_send_retries_on_flood(self):
        app = FakeApp(histories={9: []})
        original = app.send_message
        state = {"raised": False}

        def flaky(chat_id: int, text: str) -> Any:
            if not state["raised"]:
                state["raised"] = True
                raise FloodWait(value=1)
            return original(chat_id, text)

        app.send_message = flaky
        result = probe_bot(app, 9, wait_s=6, poll_s=2, sleep=lambda _s: None)
        self.assertEqual(result, "silent")
        self.assertTrue(state["raised"])


class RollbackFilterTests(unittest.TestCase):
    def test_action_filter_keeps_other_records(self):
        # откат остановки не трогает mute-запись прошлых запусков в том же журнале
        notify_state = {"mute_until": 0}

        app = FakeApp()
        blocked = app.blocked  # один dict: FakeApp копирует переданный
        blocked[910] = False

        def fake_invoke(req: Any) -> Any:
            name = type(req).__name__
            if "GetNotifySettings" in name:
                return SimpleNamespace(mute_until=notify_state["mute_until"])
            if "UpdateNotifySettings" in name:
                notify_state["mute_until"] = req.settings.mute_until
                return True
            if "GetFullUser" in name:
                return SimpleNamespace(full_user=SimpleNamespace(blocked=blocked[req.id.user_id]))
            return None

        app.invoke = fake_invoke
        with tempfile.TemporaryDirectory() as td:
            plan = ActionPlan()
            plan.set_mute(900, "Канал 900")
            apply_action_plan(
                app, plan, session_name="mix", journal_dir=Path(td), log=lambda _m: None
            )
            apply_bot_actions(
                app,
                [BotAction(910, "Бот 910", stop=True)],
                session_name="mix",
                journal_dir=Path(td),
                log=lambda _m: None,
            )
            self.assertEqual(notify_state["mute_until"], FOREVER_MUTE)
            self.assertTrue(blocked[910])

            results = rollback_session(
                app, "mix", journal_dir=Path(td), actions={"stop"}, log=lambda _m: None
            )
            self.assertEqual([r["action"] for r in results], ["stop"])
            self.assertFalse(blocked[910])
            self.assertEqual(notify_state["mute_until"], FOREVER_MUTE)  # mute не тронут
            kept = load_rollback_journal("mix", root_dir=Path(td))
            self.assertEqual([r["action"] for r in kept], ["mute"])

            # полный откат снимает mute
            results = rollback_session(app, "mix", journal_dir=Path(td), log=lambda _m: None)
            self.assertEqual([r["action"] for r in results], ["mute"])
            self.assertEqual(notify_state["mute_until"], 0)
            self.assertEqual(load_rollback_journal("mix", root_dir=Path(td)), [])


if __name__ == "__main__":
    unittest.main()
