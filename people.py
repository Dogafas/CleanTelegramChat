import sys

from session import telegram_client

_EXPECTED_ARGS = 2  # program name + CHAT_ID


def main(argv: list[str]) -> int:
    if len(argv) != _EXPECTED_ARGS:
        print("usage: python people.py CHAT_ID", file=sys.stderr)
        return 2
    try:
        chat_id = int(argv[1])
    except ValueError:
        print("usage: python people.py CHAT_ID", file=sys.stderr)
        return 2
    with telegram_client() as app:
        for member in app.get_chat_members(chat_id):
            user = member.user
            print(
                f"User ID: {user.id}, Username: {user.username}, "
                f"Full Name: {user.first_name} {user.last_name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
