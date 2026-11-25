"""Проверки ui-шва: restore_client_loop и парность ask→restore у адаптеров."""

import asyncio
import unittest
from typing import Any
from unittest import mock

import ui
from ui import ask_chats, ask_confirm, ask_select, ask_text, restore_client_loop


class FakeQuestionary:
    """Подмена questionary: возвращает заготовленный ответ, вызовы пишет в calls."""

    class Choice:
        def __init__(self, title: str, value: Any, checked: bool = False) -> None:
            self.title = title
            self.value = value
            self.checked = checked

    class Separator:
        def __init__(self, label: str) -> None:
            self.label = label

    def __init__(self, answer: Any = None):
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []

    def _askable(self, kind: str, *args: Any) -> Any:
        self.calls.append((kind, args))
        outer = self

        class _Ask:
            def ask(self) -> Any:
                return outer.answer

        return _Ask()

    def select(self, message: str, choices: Any, **_kw: Any) -> Any:
        return self._askable("select", message, choices)

    def text(self, message: str, **_kw: Any) -> Any:
        return self._askable("text", message)

    def confirm(self, message: str, **_kw: Any) -> Any:
        return self._askable("confirm", message)

    def checkbox(self, message: str, choices: Any, **_kw: Any) -> Any:
        return self._askable("checkbox", message, choices)


class RestoreClientLoopTests(unittest.TestCase):
    def test_restore_client_loop(self):
        old_loop = asyncio.get_event_loop()
        client_loop: asyncio.AbstractEventLoop | None = None
        try:
            client_loop = asyncio.new_event_loop()
            app = type("FakeApp", (), {"loop": client_loop})()

            # Симулируем questionary: thread loop подменён и закрыт
            stolen = asyncio.new_event_loop()
            stolen.close()
            asyncio.set_event_loop(stolen)

            restore_client_loop(app)
            self.assertIs(asyncio.get_event_loop(), client_loop)

            # app без loop / с закрытым loop — не падает, ставит рабочий loop
            restore_client_loop(object())
            restore_client_loop(type("FakeApp2", (), {"loop": stolen})())
            self.assertFalse(asyncio.get_event_loop().is_closed())
        finally:
            asyncio.set_event_loop(old_loop)
            if client_loop is not None:
                client_loop.close()


class PromptRestoresLoopTests(unittest.TestCase):
    """Инвариант, чинившийся трижды в истории: каждый промпт завершается restore."""

    def setUp(self):
        try:
            self.saved_loop = asyncio.get_event_loop()
        except RuntimeError:
            self.saved_loop = None
        self.app_loop = asyncio.new_event_loop()
        self.app = type("FakeApp", (), {"loop": self.app_loop})()
        self.fake = FakeQuestionary("ответ")
        patcher = mock.patch.object(ui, "questionary", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        # имитация подменённого промптом thread loop
        self.stolen = asyncio.new_event_loop()
        asyncio.set_event_loop(self.stolen)

    def tearDown(self):
        asyncio.set_event_loop(self.saved_loop)
        self.app_loop.close()
        self.stolen.close()

    def _check(self, adapter: Any, *args: Any, **kwargs: Any) -> None:
        result = adapter(self.app, *args, **kwargs)
        self.assertEqual(result, "ответ")
        # после адаптера thread loop — снова loop клиента
        self.assertIs(asyncio.get_event_loop(), self.app_loop)

    def test_ask_select_restores_loop(self):
        self._check(ask_select, "Сообщение?", [("a", "a")])

    def test_ask_text_restores_loop(self):
        self._check(ask_text, "Сообщение?")

    def test_ask_confirm_restores_loop(self):
        self._check(ask_confirm, "Сообщение?")

    def test_ask_chats_restores_loop(self):
        self._check(ask_chats, [("a", "a", False)])

    def test_none_app_skips_restore(self):
        # до-клиентские меню: app=None — адаптер не падает и loop не трогает
        result = ask_confirm(None, "Сообщение?")
        self.assertEqual(result, "ответ")
        self.assertIs(asyncio.get_event_loop(), self.stolen)


if __name__ == "__main__":
    unittest.main()
