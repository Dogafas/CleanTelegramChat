import logging
from collections.abc import Callable
from time import sleep
from typing import Any

from catalog import prompt_selection
from purge import purge_chats
from session import prompt_sessions, telegram_client


def purge_all_sessions(
    sessions: list[str],
    *,
    client: Callable[..., Any] = telegram_client,
    select: Callable[..., list[Any]] = prompt_selection,
    purge: Callable[..., None] = purge_chats,
    log: Callable[[str], None] = logging.info,
) -> None:
    if not sessions:
        log("Сессии не выбраны. Завершение работы.")
        return
    multi = len(sessions) > 1
    for session_name in sessions:
        if multi:
            log(f"=== Сессия: {session_name} ===")
        try:
            with client(session_name=session_name) as app:
                selected_chats = select(app)
                if selected_chats:
                    purge(app, selected_chats, sleep=sleep, log=log)
        except Exception as e:  # упавшая сессия не отменяет остальные
            log(f"Сессия {session_name} прервана: {e}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("app.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    purge_all_sessions(prompt_sessions())


if __name__ == "__main__":
    main()
