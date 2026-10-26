import csv
import re
import sys
from pathlib import Path

from pyrogram.errors import PeerIdInvalid, RPCError

from session import SESSION_NAME, telegram_client

_MIN_ARGS = 2
_MAX_ARGS = 3


def _resolve(app, chat_id: int):
    """Resolve chat for get_chat_members, warming peer cache if needed."""
    try:
        return chat_id, app.get_chat(chat_id)
    except PeerIdInvalid:
        pass
    # Raw ID unknown to this session's peer cache; warm it from dialogs.
    for _ in app.get_dialogs():
        pass
    candidates = [chat_id]
    if chat_id > 0:
        candidates.append(int(f"-100{chat_id}"))
    for candidate in candidates:
        try:
            return candidate, app.get_chat(candidate)
        except PeerIdInvalid:
            continue
    return None, None


def _csv_safe(cell: str) -> str:
    """Prefix formula-triggering cells so Excel doesn't execute them."""
    return f"'{cell}" if cell.startswith(("=", "+", "-", "@", "\t")) else cell


def main(argv: list[str]) -> int:
    if len(argv) < _MIN_ARGS or len(argv) > _MAX_ARGS:
        print("usage: python people.py CHAT_ID|@username [SESSION_NAME]", file=sys.stderr)
        return 2
    chat_arg = argv[1]
    session_name = argv[2] if len(argv) >= _MAX_ARGS else SESSION_NAME
    chat_id: int | None = None
    if not chat_arg.startswith("@"):
        # Validate before the client opens so usage errors don't touch the network.
        try:
            chat_id = int(chat_arg)
        except ValueError:
            print("usage: python people.py CHAT_ID|@username [SESSION_NAME]", file=sys.stderr)
            return 2
    with telegram_client(session_name=session_name) as app:
        # Usernames resolve directly; raw IDs may need cache warm + -100 prefix.
        if chat_id is None:
            try:
                chat = app.get_chat(chat_arg)
                chat_id = chat.id
            except RPCError as e:
                print(f"Ошибка получения чата {chat_arg}: {e}", file=sys.stderr)
                return 1
        else:
            chat_id, chat = _resolve(app, chat_id)
            if chat is None:
                print(
                    f"Пир {chat_arg} не найден в кэше сессии: открой чат этим аккаунтом "
                    "или передай @username",
                    file=sys.stderr,
                )
                return 1
        raw_title = getattr(chat, "title", None) or ""
        title = re.sub(r"[^\w.-]+", "_", raw_title).strip("_") or str(chat_id)
        out_name = f"people_{title}_{chat_id}.csv"
        try:
            with Path(out_name).open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["user_id", "username", "full_name", "link"])
                count = 0
                for member in app.get_chat_members(chat_id):
                    user = member.user
                    username = user.username or ""
                    link = f"https://t.me/{username}" if username else f"tg://user?id={user.id}"
                    full_name = f"{user.first_name} {user.last_name or ''}".strip()
                    writer.writerow(
                        [
                            user.id,
                            _csv_safe(username),
                            _csv_safe(full_name),
                            _csv_safe(link),
                        ]
                    )
                    count += 1
        except RPCError as e:
            print(f"Ошибка выгрузки участников {chat_arg}: {e}", file=sys.stderr)
            return 1
    print(f"Сохранено {count} участников в {out_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
