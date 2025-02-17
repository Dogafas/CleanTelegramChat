import os
import json
from time import sleep
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, RPCError
import logging

# Настройка логгирования
log_filename = "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_filename, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# Настройки пути и кэша
cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")

if os.path.exists(cache_path):
    with open(cache_path, "r") as cache_file:
        cache = json.load(cache_file)
    API_ID = cache["API_ID"]
    API_HASH = cache["API_HASH"]
else:
    API_ID = int(input("Введите ваш Telegram API ID: "))
    API_HASH = input("Введите ваш Telegram API HASH: ")

    with open(cache_path, "w") as cache_file:
        json.dump({"API_ID": API_ID, "API_HASH": API_HASH}, cache_file)


class Cleaner:
    def __init__(self, chats=None, delete_chunk_size=100):
        self.chats = chats or []
        self.delete_chunk_size = delete_chunk_size

    @staticmethod
    def get_all_chats(app):
        """Получение всех диалогов с отладочной информацией."""
        dialogs = list(app.get_dialogs(limit=1000))
        logging.info(
            f"Итого у Вас доступ к {len(dialogs)} чатам, группам, каналам, ботам..."
        )

        # Фильтруем диалоги, чтобы оставить только супергруппы
        supergroups = [
            dialog
            for dialog in dialogs
            if dialog.chat.type == enums.ChatType.SUPERGROUP
        ]

        # Вывод супергрупп для проверки
        for i, dialog in enumerate(supergroups):
            logging.info(
                f"№ {i + 1}. Супергруппа: {dialog.chat.title} (ID: {dialog.chat.id})"
            )

        return supergroups

    def select_groups_by_number(self, app):
        """Выбор супергрупп для очистки по номеру."""
        chats = self.get_all_chats(app)

        if not chats:
            logging.info("Нет доступных супергрупп для выбора.")
            return

        logging.info(
            "Введите номера супергрупп для удаления сообщений (через запятую):"
        )
        selected_numbers = input().strip().split(",")
        selected_numbers = [int(num.strip()) for num in selected_numbers]

        self.chats = [
            chats[num - 1].chat for num in selected_numbers if num - 1 < len(chats)
        ]

        if not self.chats:
            logging.info("Не найдено супергрупп с указанными номерами.")
            return

        logging.info(
            f"Вы выбрали для удаления сообщений в: {', '.join(chat.title for chat in self.chats)}"
        )

    def delete_messages(self, app, chat_id):
        """Удаление сообщений."""
        try:
            messages = list(app.search_messages(chat_id, from_user="me", limit=100))
            while messages:
                message_ids = [msg.id for msg in messages]
                try:
                    app.delete_messages(chat_id, message_ids, revoke=True)
                    print(f"Удалено {len(message_ids)} сообщений из чата {chat_id}.")
                except RPCError as e:
                    print(f"Error deleting messages in chat {chat_id}: {e}")
                sleep(2)  # Избегаем флуд
                messages = list(app.search_messages(chat_id, from_user="me", limit=100))
                if len(messages) < 100:
                    break  # Больше нет сообщений для удаления
        except FloodWait as e:
            print(f"Flood wait for {e.x} seconds.")
            sleep(e.x)
        except RPCError as e:
            print(f"Error deleting messages in chat {chat_id}: {e}")

    def run(self, app):
        """Основной запуск."""
        for chat in self.chats:
            logging.info(f"Удаление сообщений из чата: {chat.title} (ID: {chat.id})")
            self.delete_messages(app, chat.id)


if __name__ == "__main__":
    with Client("cleaner", api_id=API_ID, api_hash=API_HASH) as app:
        cleaner = Cleaner()
        cleaner.select_groups_by_number(app)
        cleaner.run(app)

    # Удаляем файл cleaner.session после завершения работы
    session_file = "cleaner.session"
    if os.path.exists(session_file):
        os.remove(session_file)
        # logging.info(f"Файл {session_file} успешно удалён.")
    else:
        logging.info(f"Файл {session_file} не найден.")
