# Clean Telegram Chat — purge your messages, score channel subscriptions, sort dialogs

## Stack

- Python 3.10–3.12, `uv`
- Kurigram, questionary, typesafe-sdk (Jev)

## Commands

```bash
uv sync
uv run ruff check
uv run vulture .
uv run deptry .
uv run python -m unittest discover tests
```

Entry points: `clean_my_messages.py`, `clean_my_subs.py`, `clean_my_bots.py`, `sort_chats.py`, `people.py`. How to run: `README.md`.

## Check

Done means all four commands above (after `uv sync`) are green. Hook: `.githooks/pre-commit`. Fresh clone: `git config core.hooksPath .githooks`.

## Pointers

- Subscription scoring, Jev, unsubscribe, reading modes, rollback → `docs/adr/0007-subscription-quality-scoring.md`, `docs/adr/0009-text-fail-unsubscribe.md`, `docs/adr/0010-reading-modes-and-rollback-journal.md`, `docs/channel-telegram-signals.md`
- Chat folders → `docs/adr/0008-chat-folder-classification.md`
- Боты (скан, стоп/удаление, проба `/start`) → `docs/adr/0011-bot-cleanup.md`
- Kurigram types → `.venv/Lib/site-packages/pyrogram/types/`. Plan already names the fields → write the artifact, skip the library.
- Jev `state` is text only (`channel`, `description`, `posts`). Quality Score and numeric metrics stay out. ADR 0009.
- Слои «CLI-сценарии / ядро» → `tests/test_layers.py`: ядро не импортирует CLI, сценарии не импортируют друг друга. Новый корневой модуль — отнести к слою там же.
- Модули ядра: `review` (скан, покрытие, кандидаты), `plan` (режимы чтения, журнал отката), `overlap` (межканальные совпадения), `chat_folder` (точечные мутации папок), `dialogs` (обход диалогов и архива), `subs` (метрики, склейка альбомов), `purge` (очистка, `retry_on_flood`), `bots` (боты: скан полной истории, сигналы, Jev-вердикт, `/start`-проба).

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles, label string equals the role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` at root + `docs/adr/`. See `docs/agents/domain.md`.
