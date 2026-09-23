import logging
from time import sleep

from catalog import prompt_selection
from purge import purge_chats
from session import telegram_client


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("app.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    with telegram_client() as app:
        purge_chats(app, prompt_selection(app), sleep=sleep, log=logging.info)


if __name__ == "__main__":
    main()
