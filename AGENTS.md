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
uv run basedpyright
uv run python -m unittest discover tests
```

Entry points: `clean_my_messages.py`, `clean_my_subs.py`, `clean_my_bots.py`, `sort_chats.py`, `people.py`. How to run: `README.md`.

## Check

Done means all five commands above (after `uv sync`) are green. Hook: `.githooks/pre-commit`. Fresh clone: `git config core.hooksPath .githooks`.

Гейты `tests/test_layers.py`: слои CLI/ядро, отсутствие циклов импортов, отсутствие `noqa`/`type: ignore`. Подавления — только в конфигах ruff/pyright (`per-file-ignores`, `pyrightconfig.json`) с обоснованием. `typing.cast` и `unittest.mock` запрещены в коде (TID251); фейки — только в тестах, через швы `ui`/client-фабрик. kurigram-итераторы (`get_dialogs`, `get_chat_history`, `search_messages`) — только через `session.sync_list`.

`Any` разрешён ровно в двух местах: (1) `app`-параметры и DI-фабрики клиента — kurigram в аннотациях заявляет `async`, а в рантайме его методы sync (`pyrogram/sync.py`), честных статических типов у него нет; (2) `dict[str, Any]` для внешних JSON-полезных грузов (журнал отката, coverage, вердикт Jev). Везде остальное — типы из `pyrogram.types` (`Message`, `Chat`, `Dialog`) и доменные (`Publication`, `Candidate`).

Конфиг basedpyright: `pyrightconfig.json` — standard-режим; `reportPrivateImportUsage` off (kurigram не реэкспортирует raw TL-типы), `reportUnusedCoroutine` off (kurigram врёт про async — рантайм sync), в tests `reportArgumentType` off (фейки не наследуют Client — шов DI).

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
