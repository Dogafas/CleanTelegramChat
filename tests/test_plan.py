"""Unit tests for reading modes, action plan, and safe rollback (Section 3)."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as Chat
from typing import Any

from plan import (
    FOREVER_MUTE,
    ActionPlan,
    apply_action_plan,
    load_rollback_journal,
    rollback_session,
    save_rollback_journal,
)


class ActionPlanConflictTests(unittest.TestCase):
    def test_leave_cancels_mode_changes(self):
        plan = ActionPlan()
        plan.set_mute(10, "Канал 10")
        plan.set_archive(10, "Канал 10")
        plan.set_folder(10, "Канал 10", "Чтение")

        act = plan.actions[10]
        self.assertTrue(act.mute)
        self.assertTrue(act.archive)
        self.assertEqual(act.folder_title, "Чтение")
        self.assertFalse(act.leave)

        # Выбор отписки сбрасывает mute, archive, folder
        plan.set_leave(10, "Канал 10")
        act = plan.actions[10]
        self.assertTrue(act.leave)
        self.assertFalse(act.mute)
        self.assertFalse(act.archive)
        self.assertIsNone(act.folder_title)

    def test_mode_change_cancels_leave(self):
        plan = ActionPlan()
        plan.set_leave(20, "Канал 20")
        self.assertTrue(plan.actions[20].leave)

        # Выбор режима чтения сбрасывает отписку
        plan.set_mute(20, "Канал 20")
        act = plan.actions[20]
        self.assertFalse(act.leave)
        self.assertTrue(act.mute)

    def test_archive_and_mute_compatible(self):
        plan = ActionPlan()
        plan.set_archive(30, "Канал 30")
        plan.set_mute(30, "Канал 30")
        act = plan.actions[30]
        self.assertTrue(act.archive)
        self.assertTrue(act.mute)
        self.assertFalse(act.leave)

    def test_summary_and_empty(self):
        plan = ActionPlan()
        self.assertTrue(plan.is_empty())
        plan.set_leave(1, "A")
        plan.set_mute(2, "B")
        self.assertFalse(plan.is_empty())
        s = plan.summary()
        self.assertEqual(s["leave"], 1)
        self.assertEqual(s["mute"], 1)
        self.assertEqual(s["total_channels"], 2)


class PlanExecutionAndRollbackTests(unittest.TestCase):
    def test_apply_mute_already_set_is_noop(self):
        # Если канал уже muted, RPC UpdateNotifySettings не вызывается
        future_ts = int(time.time()) + 100_000
        invocations: list[Any] = []

        def fake_invoke(req: Any) -> Any:
            invocations.append(req)
            if "GetNotifySettings" in type(req).__name__:
                return Chat(mute_until=future_ts)
            return None

        app = Chat(
            resolve_peer=lambda cid: Chat(channel_id=cid),
            invoke=fake_invoke,
        )
        plan = ActionPlan()
        plan.set_mute(100, "Канал 100")

        with tempfile.TemporaryDirectory() as td:
            reports = apply_action_plan(app, plan, session_name="test", journal_dir=Path(td))
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["status"], "already_set")
            # UpdateNotifySettings не вызывался
            updates = [r for r in invocations if "UpdateNotifySettings" in type(r).__name__]
            self.assertEqual(len(updates), 0)

    def test_apply_mute_and_safe_rollback(self):
        # 1. Применяем mute
        notify_state = {"mute_until": 0}
        invoked: list[Any] = []

        def fake_invoke(req: Any) -> Any:
            invoked.append(req)
            name = type(req).__name__
            if "GetNotifySettings" in name:
                return Chat(mute_until=notify_state["mute_until"])
            if "UpdateNotifySettings" in name:
                notify_state["mute_until"] = req.settings.mute_until
                return True
            return None

        app = Chat(
            resolve_peer=lambda cid: Chat(channel_id=cid),
            invoke=fake_invoke,
        )
        plan = ActionPlan()
        plan.set_mute(200, "Канал 200")

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            reports = apply_action_plan(app, plan, session_name="s1", journal_dir=j_dir)
            self.assertEqual(reports[0]["status"], "applied")
            self.assertEqual(notify_state["mute_until"], FOREVER_MUTE)

            # 2. Безопасный откат: если состояние совпадает, восстанавливаем prev_state
            results = rollback_session(app, session_name="s1", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "restored")
            self.assertEqual(notify_state["mute_until"], 0)

    def test_rollback_skips_if_user_manually_modified_setting(self):
        notify_state = {"mute_until": 0}

        def fake_invoke(req: Any) -> Any:
            name = type(req).__name__
            if "GetNotifySettings" in name:
                return Chat(mute_until=notify_state["mute_until"])
            if "UpdateNotifySettings" in name:
                notify_state["mute_until"] = req.settings.mute_until
                return True
            return None

        app = Chat(
            resolve_peer=lambda cid: Chat(channel_id=cid),
            invoke=fake_invoke,
        )
        plan = ActionPlan()
        plan.set_mute(300, "Канал 300")

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            apply_action_plan(app, plan, session_name="s2", journal_dir=j_dir)
            self.assertEqual(notify_state["mute_until"], FOREVER_MUTE)

            # Пользователь вручную включил звук в Telegram после запуска инструмента!
            notify_state["mute_until"] = 0

            # Откат не должен затирать ручное действие пользователя
            results = rollback_session(app, session_name="s2", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("вручную", results[0]["detail"])

    def test_journal_persists_without_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            records = [{
                "action": "mute",
                "chat_id": 42,
                "prev_state": {"mute_until": 0},
                "applied_state": {"mute_until": FOREVER_MUTE},
            }]
            p = save_rollback_journal("sec_test", records, root_dir=j_dir)
            loaded = load_rollback_journal("sec_test", root_dir=j_dir)
            self.assertEqual(loaded, records)
            content = p.read_text(encoding="utf-8")
            self.assertNotIn("session_file", content)
            self.assertNotIn("token", content)
            self.assertNotIn("secret", content)

    def test_second_apply_merges_journal_not_overwrites(self):
        # два apply без отката: откат нужен обеим партиям изменений
        mute_by_chat: dict[int, int] = {500: 0, 501: 0}

        def fake_invoke(req: Any) -> Any:
            name = type(req).__name__
            cid = getattr(getattr(req.peer, "peer", None), "channel_id", 0)
            if "GetNotifySettings" in name:
                return Chat(mute_until=mute_by_chat.get(cid, 0))
            if "UpdateNotifySettings" in name:
                mute_by_chat[cid] = req.settings.mute_until
                return True
            return None

        app = Chat(resolve_peer=lambda cid: Chat(channel_id=cid), invoke=fake_invoke)

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            for chat_id in (500, 501):
                plan = ActionPlan()
                plan.set_mute(chat_id, f"Канал {chat_id}")
                apply_action_plan(app, plan, session_name="s3", journal_dir=j_dir)

            loaded = load_rollback_journal("s3", root_dir=j_dir)
            self.assertEqual({r["chat_id"] for r in loaded}, {500, 501})

    def test_rollback_consumes_journal(self):
        # после успешного отката журнал пуст, повторный откат — не «изменено вручную»
        notify_state = {"mute_until": 0}

        def fake_invoke(req: Any) -> Any:
            name = type(req).__name__
            if "GetNotifySettings" in name:
                return Chat(mute_until=notify_state["mute_until"])
            if "UpdateNotifySettings" in name:
                notify_state["mute_until"] = req.settings.mute_until
                return True
            return None

        app = Chat(resolve_peer=lambda cid: Chat(channel_id=cid), invoke=fake_invoke)
        plan = ActionPlan()
        plan.set_mute(600, "Канал 600")

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            apply_action_plan(app, plan, session_name="s4", journal_dir=j_dir)
            results = rollback_session(app, session_name="s4", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "restored")
            self.assertEqual(load_rollback_journal("s4", root_dir=j_dir), [])
            # повторный откат: записей нет, ложного skip нет
            self.assertEqual(rollback_session(app, session_name="s4", journal_dir=j_dir), [])

    def test_archive_rollback_guard(self):
        # откат архива не затирает ручную разархивацию, как у mute
        archive_state = {"folder_id": 0}
        calls: list[str] = []
        app = Chat(
            get_chat=lambda cid: Chat(folder_id=archive_state["folder_id"]),
            archive_chats=lambda ids: (
                (archive_state.update(folder_id=1), calls.append("archive")),
                None,
            )[1],
            unarchive_chats=lambda ids: (
                (archive_state.update(folder_id=0), calls.append("unarchive")),
                None,
            )[1],
        )
        plan = ActionPlan()
        plan.set_archive(700, "Канал 700")

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            apply_action_plan(app, plan, session_name="s5", journal_dir=j_dir)
            self.assertEqual(archive_state["folder_id"], 1)  # fake-архивация применилась

            # откат: состояние всё ещё «в архиве» → restore
            results = rollback_session(app, session_name="s5", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "restored")
            self.assertEqual(archive_state["folder_id"], 0)  # fake-разархивация сработала
            self.assertEqual(calls.count("unarchive"), 1)

            # пользователь разархивировал сам: откат не должен трогать
            apply_action_plan(app, plan, session_name="s5", journal_dir=j_dir)
            archive_state["folder_id"] = 0  # «ручная» разархивация
            results = rollback_session(app, session_name="s5", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("вручную", results[0]["detail"])
            self.assertEqual(calls.count("unarchive"), 1)

    def test_rollback_error_keeps_record_in_journal(self):
        # неудавшийся откат остаётся в журнале для повторной попытки
        def fake_invoke(req: Any) -> Any:
            raise RuntimeError("сеть недоступна")

        app_bad = Chat(
            resolve_peer=lambda cid: Chat(channel_id=cid),
            invoke=fake_invoke,
        )

        def invoke_ok(req: Any) -> Any:
            name = type(req).__name__
            if "GetNotifySettings" in name:
                return Chat(mute_until=0)
            if "UpdateNotifySettings" in name:
                return True
            return None

        app_ok = Chat(resolve_peer=lambda cid: Chat(channel_id=cid), invoke=invoke_ok)
        plan = ActionPlan()
        plan.set_mute(800, "Канал 800")

        with tempfile.TemporaryDirectory() as td:
            j_dir = Path(td)
            apply_action_plan(app_ok, plan, session_name="s6", journal_dir=j_dir)
            results = rollback_session(app_bad, session_name="s6", journal_dir=j_dir)
            self.assertEqual(results[0]["status"], "error")
            loaded = load_rollback_journal("s6", root_dir=j_dir)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["chat_id"], 800)


if __name__ == "__main__":
    unittest.main()
