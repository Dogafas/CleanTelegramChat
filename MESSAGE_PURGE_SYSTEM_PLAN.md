# UV, human-paced purge, and the three deep modules

## Context

The cleanup script must run with `uv`, stay on Pyrogram 2.0.106 (already the latest official release; do not switch to Kurigram), and purge the user's own messages while honoring Telegram limits and human-like pauses. The three deepenings are in scope: a Message Purge module, a Chat Catalog seam separate from terminal prompts, and a Session module that resolves credentials without import side effects and keeps the session file. `people.py` remains a second entrypoint on that same session module, with hardcoded API keys and chat id removed.

## Approach

### 1. Lock the three deepenings as ADRs

Create `docs/adr/` and these files. No other docs. Do not add implementation terms to `CONTEXT.md`.

`docs/adr/0001-message-purge-module.md`:

```md
# Message Purge is one deep module

Message search, the 100-id delete batch, FloodWait retry, and human pauses live behind one purge interface. Callers pass a chat id and a Telegram client adapter. Chunk size, jitter, and retry stay inside the module so a future script cannot "simplify" them away at the call site.
```

`docs/adr/0002-chat-catalog-seam.md`:

```md
# Chat Catalog is separate from the terminal

Supergroup filtering and 1-based selection parsing are a module. `input()` is only the CLI adapter. Tests call `parse_selection` and never stdin.
```

`docs/adr/0003-session-lifecycle.md`:

```md
# One session module, session file kept

API id/hash resolve from `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`, else `cache.json`, else a prompt that writes the cache. Resolution runs when a client is opened, never at import. Both entrypoints use session name `cleaner`. The process must not delete `*.session`. Deleting the session on exit forced a phone and 2FA login every run; keeping the file is the point of the module.
```

### 2. UV project, drop the pip file

Add `pyproject.toml` at the repo root:

```toml
[project]
name = "clean-telegram-chat"
version = "0.1.0"
description = "Delete your own messages from Telegram supergroups"
requires-python = ">=3.9"
dependencies = ["pyrogram==2.0.106"]

[tool.uv]
package = false
```

Do not pin `pyaes` or `PySocks` (transitive). Do not add TgCrypto. Delete `requirements.txt`.

Replace `install.bat` with:

```bat
@echo off
cd /d %~dp0
uv sync
if errorlevel 1 exit /b 1
echo Successfully installed requirements!
```

Replace `start.bat` with:

```bat
@echo off
cd /d %~dp0
uv run python clean_my_messages.py
```

In `README.md`, replace only the `## Установка и запуск` section body with:

```md
1. Установите [uv](https://docs.astral.sh/uv/) и выполните `uv sync` (или `install.bat`).
2. Запуск: `uv run python clean_my_messages.py` (или `start.bat`).
3. Список участников: `uv run python people.py CHAT_ID`.
```

Leave the API-key and phone-login sections as they are. In `## Аутентификация`, do not claim the session file is deleted.

Append to `.gitignore`:

```
*.session
*.session-journal
```

Untrack auth files without deleting the working copies and without committing:

`git rm --cached -- cleaner.session my_account.session`

### 3. Session module

New file `session.py`. No equivalent exists. No credential prompt and no `Client` construction may remain in `clean_my_messages.py` or `people.py`.

```python
ENV_ID = "TELEGRAM_API_ID"
ENV_HASH = "TELEGRAM_API_HASH"
SESSION_NAME = "cleaner"

def cache_path() -> Path:
    return Path(__file__).resolve().parent / "cache.json"

def resolve_credentials(path=None, *, environ=os.environ, input_fn=input) -> tuple[int, str]:
    # both env vars set -> int(ENV_ID), ENV_HASH, do not write cache
    # else cache.json with API_ID and API_HASH -> those values, do not prompt
    # else prompt the two existing Russian strings, write {"API_ID", "API_HASH"}, return them
    # exactly one env var set -> ignore env and use cache/prompt
    # missing/invalid JSON, missing keys, or non-int API_ID in cache -> fall through to prompt
    # non-int API_ID from env or from the prompt -> raise ValueError (do not loop)

@contextmanager
def telegram_client(session_name: str = SESSION_NAME):
    api_id, api_hash = resolve_credentials()
    with Client(session_name, api_id=api_id, api_hash=api_hash) as app:
        yield app
```

Never call `os.remove` on a session file. Do not read or log the hash.

### 4. Chat Catalog module

New file `catalog.py`.

```python
def parse_selection(raw: str, count: int) -> list[int]:
    # comma-separated tokens; strip; skip blank, non-int, <1, or >count
    # return unique 0-based indexes, first-seen order

def prompt_selection(app, *, input_fn=input, log=logging.info) -> list:
    # list(app.get_dialogs(limit=1000)); do not paginate past 1000
    # log the existing "Итого у Вас доступ к {n} чатам, группам, каналам, ботам..." line
    # keep only dialog.chat.type == enums.ChatType.SUPERGROUP
    # log "№ {i}. Супергруппа: {title} (ID: {id})" with i starting at 1
    # no groups -> log "Нет доступных супергрупп для выбора." and return []
    # log "Введите номера супергрупп для удаления сообщений (через запятую):"
    # raw = input_fn() with no argument (Enter-submitted line, same as today)
    # indexes = parse_selection(raw, len(groups)); chats = [groups[i].chat for i in indexes]
    # empty -> log "Не найдено супергрупп с указанными номерами." and return []
    # else log "Вы выбрали для удаления сообщений в: " + ", ".join(titles) and return chats
```

EOFError from `input_fn` propagates. No other input handling.

### 5. Message Purge module

New file `purge.py`. This is the only place that calls `search_messages`, `delete_messages`, or `sleep` for pacing.

```python
BATCH_LIMIT = 100
BATCH_PAUSE = (8.0, 25.0)    # seconds, inclusive, random.uniform
CHAT_PAUSE = (25.0, 75.0)
FLOOD_JITTER = (1.0, 3.0)

def batch_pause(rng=random.uniform) -> float:
    return rng(*BATCH_PAUSE)

def chat_pause(rng=random.uniform) -> float:
    return rng(*CHAT_PAUSE)

def flood_pause(wait_seconds: int, rng=random.uniform) -> float:
    return float(wait_seconds) + rng(*FLOOD_JITTER)
```

After `uv sync`, read the installed `FloodWait` class. Pyrogram 2.0.106 stores the wait on `value`. If that attribute is absent and `x` exists, use `x`. Do not use both.

```python
def purge_chat(app, chat_id, *, sleep, rng=random.uniform, log=print) -> None:
    previous = None
    while True:
        try:
            messages = list(app.search_messages(chat_id, from_user="me", limit=BATCH_LIMIT))
        except FloodWait as e:
            delay = flood_pause(_wait(e), rng)
            log(f"FloodWait {delay:.0f} с, чат {chat_id}")
            sleep(delay)
            continue
        except RPCError as e:
            log(f"Ошибка поиска в чате {chat_id}: {e}")
            return
        if not messages:
            return
        ids = [m.id for m in messages]
        if ids == previous:
            log(f"Повтор того же батча в чате {chat_id}, стоп")
            return
        try:
            app.delete_messages(chat_id, ids, revoke=True)
        except FloodWait as e:
            delay = flood_pause(_wait(e), rng)
            log(f"FloodWait {delay:.0f} с, чат {chat_id}")
            sleep(delay)
            continue  # retry the same ids; do not apply batch_pause
        except RPCError as e:
            log(f"Ошибка удаления в чате {chat_id}: {e}")
            return  # this chat only
        previous = ids
        log(f"Удалено {len(ids)} сообщений из чата {chat_id}.")
        if len(messages) < BATCH_LIMIT:
            return  # last partial batch already deleted; no trailing batch pause
        delay = batch_pause(rng)
        log(f"Пауза {delay:.1f} с в чате {chat_id}")
        sleep(delay)

def purge_chats(app, chats, *, sleep, rng=random.uniform, log=print) -> None:
    for i, chat in enumerate(chats):
        if i:
            delay = chat_pause(rng)
            log(f"Пауза {delay:.1f} с перед чатом {chat.title}")
            sleep(delay)
        log(f"Удаление сообщений из чата: {chat.title} (ID: {chat.id})")
        purge_chat(app, chat.id, sleep=sleep, rng=rng, log=log)
```

Chats are sequential. No thread/async pool. No per-message delete. `revoke=True` stays. Unlimited FloodWait retries. No pause before the first chat and no pause after the last chat.

### 6. Entry points

`clean_my_messages.py` keeps logging config, but only inside `main()`, not at import (import must not truncate `app.log` or prompt):

```python
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
```

Delete the `Cleaner` class, the import-time cache block, and the session-file deletion block.

`people.py` becomes:

```python
def main(argv: list[str]) -> int:
    if len(argv) != 2:
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
            print(f"User ID: {user.id}, Username: {user.username}, Full Name: {user.first_name} {user.last_name}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

No api id, api hash, or chat id literals. Leave `my_account.session` on disk unused. Do not copy it.

### 7. One runnable check

New `test_logic.py`, no pytest, no network. `assert` failures must be loud (`python test_logic.py` exits non-zero).

- `parse_selection("1, 3, 3, 9, x, , 0", 3) == [0, 2]`
- `parse_selection("", 3) == []` and `parse_selection("  ", 1) == []`
- `batch_pause(lambda a, b: a) == 8` and `batch_pause(lambda a, b: b) == 25`
- `chat_pause` bounds 25 and 75 the same way
- `flood_pause(10, lambda a, b: a) == 11`
- Fake app: `search_messages` yields 100 ids, then the same 100 ids. `delete_messages` raises `FloodWait` once (`value` or `x`, whichever the installed class uses) then succeeds. Expect: flood sleep once, delete called twice with the same ids, then the repeat-batch stop, and no `batch_pause` before that stop.
- Second fake: search yields 2 ids then would yield more. After one successful delete of those 2, `purge_chat` returns without sleeping.
- `purge_chats` with two chats sleeps `chat_pause` once, between them, not before the first.
- `resolve_credentials` with both env vars set does not call `input_fn` and does not write the cache file. Cache file with `API_ID`/`API_HASH` is used when env is empty. Empty cache path prompts and writes the cache.

Run `uv lock` (creates `uv.lock`; do not hand-edit it) and `uv sync` before the test.

## Critical files & anchors

- `clean_my_messages.py` `delete_messages`: current loop `break`s when a refetch returns `< 100`, so the last partial batch is never deleted. The new loop deletes that batch, then returns.
- `clean_my_messages.py` `except RPCError` inside the delete loop: `FloodWait` subclasses `RPCError`, so today's handler swallows it and does not wait. Catch `FloodWait` first.
- `clean_my_messages.py` lines 123–129: deletes `cleaner.session`. Remove. Do not replace with another unlink.
- `people.py` lines 3–8: hardcoded api id, api hash, and chat id. Delete those literals. Do not copy the values anywhere.
- Tracked `cleaner.session` and `my_account.session`: auth material. Untrack with `git rm --cached` only.

## Verification

Working directory: repo root. Prerequisite: `uv` on PATH. Do not run `clean_my_messages.py` or `people.py` against Telegram (phone login).

1. `uv sync` exits 0 and the venv has `pyrogram==2.0.106`.
2. `uv run python test_logic.py` exits 0.
3. `uv run python -c "import clean_my_messages, people, session, catalog, purge"` exits 0 and does not prompt.
4. Grep the repo for the old api hash string and for `os.remove` of a session file: both absent.

## Assumptions & contingencies

- Official Pyrogram has no release after 2.0.106. If `uv lock` refuses that pin, stop and report the resolver error. Do not substitute Kurigram.
- FloodWait seconds attribute is `value`. If the installed class has only `x`, use `x` in `_wait` and in the test's exception constructor. One attribute, not a fallback chain at runtime.
- Both scripts share session name `cleaner`. No migration from `my_account.session`.
- A non-flood `RPCError` stops that chat and `purge_chats` continues to the next chat after `chat_pause`.
- `git rm --cached` must not delete the local session files. If the index already lacks them, skip the command.
