"""Общие рукописные подмены швов для тестов (unittest.mock в коде запрещён)."""

from typing import Any


class FakeUi:
    """Подмена ui-шва: колбэки с интерфейсом без app, сам app игнорируется.

    Незаполненный метод при вызове падает — тест видит незапланированный промпт.
    """

    def __init__(
        self,
        *,
        select: Any = None,
        chats: Any = None,
        text: Any = None,
        confirm: Any = None,
    ) -> None:
        self.select = select
        self.chats = chats
        self.text = text
        self.confirm = confirm

    def ask_select(self, _app: Any, message: Any, choices: Any) -> Any:
        return self.select(message, choices)

    def ask_text(self, _app: Any, message: Any) -> Any:
        return self.text(message)

    def ask_confirm(self, _app: Any, message: Any, **_kwargs: Any) -> Any:
        return self.confirm(message)

    def ask_chats(self, _app: Any, choices: Any, message: str = "") -> Any:
        return self.chats(choices, message)
