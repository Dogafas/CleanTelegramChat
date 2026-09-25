"""Checks for clean_my_messages: purge_all_sessions orchestration."""

import contextlib
import unittest
from typing import Any

from clean_my_messages import purge_all_sessions


class PurgeAllSessionsTests(unittest.TestCase):
    def setUp(self):
        self.events: list[tuple[str, Any]] = []
        self.apps = [object(), object()]
        self.opens = {"n": 0}

    def fake_client(self, session_name: str, **_kw: Any) -> Any:
        app = self.apps[self.opens["n"]]
        self.opens["n"] += 1
        self.events.append(("open", session_name))
        return contextlib.nullcontext(app)

    def fake_select(self, app: Any) -> list[Any]:
        return [] if app is self.apps[1] else ["chat1"]

    def fake_purge(self, app: Any, chats: list[Any], **_kw: Any) -> None:
        self.events.append(("purge", app, chats))

    def _run(self, sessions: list[str], **overrides: Any) -> None:
        kwargs: dict[str, Any] = {
            "client": self.fake_client,
            "select": self.fake_select,
            "purge": self.fake_purge,
            "log": lambda msg: self.events.append(("log", msg)),
        }
        kwargs.update(overrides)
        purge_all_sessions(sessions, **kwargs)

    def test_multi_sessions(self):
        # 1. Multi: header per session, purge for non-empty selection, skip empty
        self._run(["acc1", "acc2"])
        self.assertIn(("log", "=== Сессия: acc1 ==="), self.events)
        self.assertIn(("log", "=== Сессия: acc2 ==="), self.events)
        self.assertIn(("purge", self.apps[0], ["chat1"]), self.events)
        self.assertFalse(any(e[0] == "purge" and e[1] is self.apps[1] for e in self.events))

    def test_single_session_no_header(self):
        # 2. Single: no header, still purges
        self._run(["acc1"])
        self.assertFalse(any(e[0] == "log" and "===" in str(e[1]) for e in self.events))
        self.assertIn(("purge", self.apps[0], ["chat1"]), self.events)

    def test_empty_sessions_exit(self):
        # 3. Empty: one log line, no client opened
        self._run([], client=self.fake_client, log=lambda msg: self.events.append(("log", msg)))
        self.assertEqual(self.events, [("log", "Сессии не выбраны. Завершение работы.")])

    def test_error_isolation(self):
        # 4. Error isolation: failing session doesn't stop the rest
        self.opens["n"] = 0

        def failing_client(session_name: str, **_kw: Any) -> Any:
            if session_name == "bad":
                raise RuntimeError("auth failed")
            return self.fake_client(session_name)

        self._run(["bad", "acc1"], client=failing_client)
        self.assertIn(("purge", self.apps[0], ["chat1"]), self.events)
        self.assertTrue(
            any(
                "bad" in str(e[1]) and "auth failed" in str(e[1])
                for e in self.events
                if e[0] == "log"
            )
        )


if __name__ == "__main__":
    unittest.main()
