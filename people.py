import sys

from session import SESSION_NAME, telegram_client

_MIN_ARGS = 2
_MAX_ARGS = 3


def main(argv: list[str]) -> int:
    if len(argv) < _MIN_ARGS or len(argv) > _MAX_ARGS:
        print("usage: python people.py CHAT_ID [SESSION_NAME]", file=sys.stderr)
        return 2
    try:
        chat_id = int(argv[1])
    except ValueError:
        print("usage: python people.py CHAT_ID [SESSION_NAME]", file=sys.stderr)
        return 2
    session_name = argv[2] if len(argv) >= _MAX_ARGS else SESSION_NAME
    with telegram_client(session_name=session_name) as app:
        for member in app.get_chat_members(chat_id):
            user = member.user
            print(
                f"User ID: {user.id}, Username: {user.username}, "
                f"Full Name: {user.first_name} {user.last_name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
