# Clean Telegram Chat — purge your messages, score channel subscriptions, sort dialogs

## Stack

- Python 3.10–3.12, `uv`
- Kurigram, questionary, typesafe-sdk (Jev)

## Commands

```bash
uv sync
uv run ruff check
uv run python -m unittest discover tests
```

Entry points: `clean_my_messages.py`, `clean_my_subs.py`, `sort_chats.py`, `people.py`. How to run: `README.md`.

## Check

Done means both commands above are green. Hook: `.githooks/pre-commit`. Fresh clone: `git config core.hooksPath .githooks`.

## Pointers

- Subscription scoring, Jev, unsubscribe → `docs/adr/0007-subscription-quality-scoring.md`, `docs/adr/0009-text-fail-unsubscribe.md`, `docs/channel-telegram-signals.md`
- Chat folders → `docs/adr/0008-chat-folder-classification.md`
- Kurigram types → `.venv/Lib/site-packages/pyrogram/types/`. Plan already names the fields → write the artifact, skip the library.
- Jev `state` is text only (`channel`, `description`, `posts`). Quality Score and numeric metrics stay out. ADR 0009.
- Слои «CLI-сценарии / ядро» → `tests/test_layers.py`: ядро не импортирует CLI, сценарии не импортируют друг друга. Новый корневой модуль — отнести к слою там же.
