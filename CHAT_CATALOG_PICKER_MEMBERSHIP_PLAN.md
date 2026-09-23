# Membership catalog picker

## Context

Очистка больше не притворяется конфигом чатов. API ID и API HASH уходят из `cache.json` в `.env`: процессные `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` важнее файла, годный `cache.json` копируется в `.env` один раз и больше не читается, сам файл не удаляется. Каталог — живые диалоги аккаунта, включая архив, тремя секциями. Пользователь отмечает чаты в `questionary` и подтверждает необратимое удаление только своих сообщений. Чаты, из которых аккаунт вышел, в каталог не входят: `channels.deleteMessages` отвечает `CHANNEL_PRIVATE`, пока аккаунта нет в супергруппе, и скрипт туда не вступает заново.

## Approach

### 1. Glossary

В `CONTEXT.md` не писать импорт, `questionary`, `folder_id`, лимиты. Обновить **Target Chat** и добавить четыре термина. Существующие **Supergroup**, **Message Purge**, **Message Batch**, **Telegram Session** не менять.

**Target Chat**:
Чат, в котором аккаунт состоит и который выбран для удаления его сообщений: публичная супергруппа, закрытая супергруппа или обычная группа.
_Avoid_: dialog, channel, left chat, личка

**Public Supergroup**:
Супергруппа с username. Аккаунт в ней состоит.
_Avoid_: channel, public chat

**Private Supergroup**:
Супергруппа без username. Аккаунт в ней состоит.
_Avoid_: closed chat, secret chat, left chat

**Basic Group**:
Обычная группа, не супергруппа. Аккаунт в ней состоит.
_Avoid_: supergroup, closed chat

**Left Chat**:
Чат, в котором аккаунта уже нет. Не Target Chat: свои сообщения там удалить нельзя.
_Avoid_: archived dialog, private supergroup

Архив в глоссарий не добавлять: это та же принадлежность, не отдельный вид чата.

### 2. ADR

Создать `docs/adr/0004-membership-catalog.md` дословно:

```md
# Membership catalog, not a chat config

The picker lists every dialog the account still belongs to, including the archive folder, in three sections: public supergroups (username), private supergroups (no username), and basic groups. Channels, private chats, bots, and left chats are absent. Left chats stay absent because channels.deleteMessages returns CHANNEL_PRIVATE until the account rejoins, and this tool does not rejoin. questionary checkboxes are the only selector. Four sentinels union with hand-picked rows at submit time; a row cannot be excluded from a sentinel on that screen. Empty sections are hidden. N on confirm returns to the same checkboxes without refetching.
```

В начало `docs/adr/0002-chat-catalog-seam.md` добавить одну строку, остальное не трогать:

```md
Superseded for the supergroup-only filter and the stdin adapter by 0004.
```

### 3. Dependency

Из корня репозитория: `uv add questionary`. Версию не выдумывать и `uv.lock` руками не править. `questionary` импортировать только внутри обёрток по умолчанию, не на уровне модуля: `import catalog` не должен требовать TTY и не должен открывать промпт.

### 4. Catalog seam

Переписать выбор в `catalog.py`. `parse_selection` удалить: числовой ввод больше не адаптер, других вызовов нет. `clean_my_messages.py` не менять: он уже вызывает `prompt_selection(app)`.

Сигнатуры:

```python
ALL = "all"
ALL_PUBLIC = "public"
ALL_PRIVATE = "private"
ALL_BASIC = "basic"

def chat_label(chat) -> str:
    # title = chat.title or "без названия"
    # username truthy -> f"{title} (@{username}) — {chat.id}"
    # else -> f"{title} — {chat.id}"

def membership_catalog(dialogs) -> tuple[list, list, list]:
    # returns (public, private, basic), first-seen order, deduped by chat.id

def resolve_selection(public, private, basic, values) -> list:
    # union of sentinels and hand-picked ids; no subtraction

def select_from_dialogs(dialogs, *, ask, confirm, log) -> list:
    # checkbox loop + confirm; no Telegram

def prompt_selection(app, *, ask=None, confirm=None, log=logging.info) -> list:
    # fetch, then select_from_dialogs
```

Классификация одного диалога, по порядку:

- `chat.type == ChatType.SUPERGROUP` и непустой `chat.username` → public
- `chat.type == ChatType.SUPERGROUP` иначе → private
- `chat.type == ChatType.GROUP` → basic
- `CHANNEL`, `PRIVATE`, `BOT` и любой другой тип → выбросить, даже если есть username

`membership_catalog` склеивает уже загруженный список. Повторный `chat.id` пропускать (первое вхождение остаётся). Это покрывает и теоретический дубль main/archive.

`resolve_selection`:

- значение `ALL` добавляет public, затем private, затем basic
- `ALL_PUBLIC` / `ALL_PRIVATE` / `ALL_BASIC` добавляет только свою секцию, даже если секция пустая (добавлять нечего)
- целочисленный id добавляет чат из любой секции, если он там есть; неизвестный id игнорировать
- строка не из четырёх сентинелов игнорировать
- порядок результата: public, затем private, затем basic, внутри секции порядок каталога
- один чат в результате один раз
- вычитания нет: отмеченный «Все публичные» плюс отсутствие галочки на одном публичном чате всё равно включает весь public

### 5. Fetch

Основная папка: `list(app.get_dialogs())` без `limit`. Установленный Pyrogram при `limit=0` листает все диалоги основной папки. `limit=1000` убрать. Элементы — `Dialog` с полем `.chat`.

Архив `get_dialogs` не умеет: установленный метод не передаёт `folder_id`. Вторая функция в `catalog.py` не копирует разбор сообщений из `get_dialogs.py`. `types.Message._parse` — async и не обёрнут sync-слоем (`_` в имени). Для каталога нужен только чат.

`types.Chat._parse_dialog(app, peer, users, chats)` — sync, проверено в установленном `pyrogram/types/user_and_chats/chat.py`. Сырой `Dialog.top_message` — `int` (id сообщения), не объект. Дата страницы — `message.date` у сырого сообщения с этим id (`Message` и `MessageService` имеют `id` и `date`; `MessageEmpty` не трогать). `date` уже unix-int. `datetime_to_timestamp` не вызывать.

```python
def _iter_archive(app):
    from pyrogram import raw, types

    offset_date = 0
    offset_id = 0
    offset_peer = raw.types.InputPeerEmpty()
    previous_last = None
    while True:
        fetched = app.invoke(
            raw.functions.messages.GetDialogs(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=100,
                hash=0,
                folder_id=1,
            ),
            sleep_threshold=60,
        )
        users = {item.id: item for item in fetched.users}
        chats = {item.id: item for item in fetched.chats}
        dates = {
            message.id: message.date
            for message in fetched.messages
            if not isinstance(message, raw.types.MessageEmpty)
            and hasattr(message, "id")
            and hasattr(message, "date")
        }
        page = []
        last_id = None
        last_top = None
        last_date = None
        for dialog in fetched.dialogs:
            if not isinstance(dialog, raw.types.Dialog):
                continue
            try:
                chat = types.Chat._parse_dialog(app, dialog.peer, users, chats)
            except (KeyError, AttributeError):
                continue
            page.append(type("Row", (), {"chat": chat})())
            last_id = chat.id
            last_top = dialog.top_message
            last_date = dates.get(dialog.top_message)
        if not page:
            return
        if last_id == previous_last or last_date is None:
            return
        previous_last = last_id
        yield from page
        offset_id = last_top
        offset_date = last_date
        offset_peer = app.resolve_peer(last_id)
```

Порядок загрузки: сначала основная папка, потом архив. Склейка — конкатенация объектов с `.chat`. Дедуп делает `membership_catalog` по `chat.id`. Пометки «архив» в строке нет. Неполная последняя страница архива (нет даты верхнего сообщения) — стоп, уже отданные строки оставить. Это не `RPCError` и не повод прятать весь каталог.

Ошибки, оба прохода отдельно. `FloodWait` не ловить отдельно: `invoke`/`get_dialogs` уже спят до `sleep_threshold=60`, а `FloodWait` есть подкласс `RPCError`.

- `RPCError` на основной папке → `log(f"Список диалогов не прочитан: {e}")`, вернуть `[]`, архив не читать, чекбоксы не открывать
- `RPCError` на архиве → `log(f"Архив диалогов не прочитан: {e}")`, вернуть `[]`, чекбоксы не открывать. Неполный каталог не показывать
- пустой архив — не ошибка

После обоих успешных проходов, до фильтра:

`log(f"Итого у Вас доступ к {n} чатам, группам, каналам, ботам...")`

`n` — число диалогов до классификации.
### 6. Checkbox loop

`select_from_dialogs` строит секции один раз. Повторный fetch внутри цикла запрещён.

Если все три секции пустые: `log("Нет чатов для выбора.")`, `ask` не вызывать, вернуть `[]`.

Иначе `questionary.checkbox`. Сообщение: `Отметьте чаты для удаления своих сообщений`. Инструкция: `Пробел — отметить, Enter — дальше`. Встроенную клавишу toggle-all библиотеки не отключать и в инструкции не упоминать: если она отметит все строки, `resolve_selection` всё равно сделает объединение.

Порядок пунктов:

1. `questionary.Choice("Все показанные", value=ALL)` — всегда, раз экран открыт
2. `Choice("Все публичные", value=ALL_PUBLIC)` только если public не пуст
3. `Choice("Все закрытые", value=ALL_PRIVATE)` только если private не пуст
4. `Choice("Все обычные", value=ALL_BASIC)` только если basic не пуст
5. `questionary.Separator("Публичные супергруппы")` и строки секции, только если public не пуст
6. то же для `Закрытые супергруппы`
7. то же для `Обычные группы`

Строка чата — `chat_label`. Значение — `chat.id` (`int`). `checked=True`, если это значение было в предыдущем ответе `ask`. Первый показ — ничего не отмечено.

`ask` возвращает список значений, `None` (отмена) или пустой список.

- `None` → `log("Выбор отменён.")`, вернуть `[]`, `confirm` не вызывать
- `[]` → `log("Ничего не выбрано.")`, вернуть `[]`, `confirm` не вызывать
- иначе `resolve_selection`. Если результат пуст (в выбор попали только игнорируемые значения) — та же ветка, что `[]`

Подтверждение, до `confirm`:

```text
Будет очищено {n} чатов:
{chat_label по одному на строку, через log}
```

Вопрос `confirm`: `Удалить свои сообщения в этих чатах?` Обёртка по умолчанию вызывает `questionary.confirm(..., default=False).ask()`.

- `True` → залогировать `Вы выбрали для удаления сообщений в: ` + `", ".join(title or "без названия")` и вернуть чаты. Этот текст уже есть в текущем `prompt_selection`; оставить его единственной сводкой после согласия
- `False` или `None` → не логировать сводку, не refetch, открыть тот же checkbox с восстановленными `checked`
- `KeyboardInterrupt` не ловить ни в `ask`, ни в `confirm`: процесс выходит, purge не начинается

Обёртки по умолчанию:

```python
def _ask(choices):
    import questionary
    return questionary.checkbox(
        "Отметьте чаты для удаления своих сообщений",
        choices=choices,
        instruction="Пробел — отметить, Enter — дальше",
    ).ask()

def _confirm(message: str):
    import questionary
    return questionary.confirm(message, default=False).ask()
```

Не-TTY не обрабатывать отдельно. `start.bat` запускает скрипт в консоли; ошибка `questionary` снаружи лога — достаточный отказ. Не чистить сообщения, если промпт не вернул список.

`purge.py` не менять. Пустой список из `prompt_selection` по-прежнему означает, что `purge_chats` ничего не удаляет. Вступать в чат, выходить из чата, удалять чужие сообщения — не добавлять.

### 7. Tests

В `test_logic.py` удалить проверки `parse_selection`. Сеть и `questionary` не вызывать. Фейковые чаты — объекты с `id`, `title`, `username`, `type`.

- супергруппа с username → public; супергруппа без username и с `""` → private; `GROUP` → basic; `CHANNEL` с username и `PRIVATE` → никуда
- два диалога с одним `id`, первый basic, второй public → остаётся basic (first-seen)
- `resolve_selection(..., [ALL_PUBLIC, basic_id])` возвращает все public и этот basic, private нет
- `[ALL]` возвращает public + private + basic в этом порядке
- `[ALL_PRIVATE]` при пустом private возвращает `[]`
- неизвестный id игнорируется; ручной id не удваивается, если тот же чат уже пришёл из сентинела
- `select_from_dialogs`: `confirm` сначала `False`, потом `True` → `ask` вызван дважды, результат равен чатам второго прохода, в списке `ask` на втором вызове у ранее выбранного id `checked` истинно
- `ask` вернул `[]` → `confirm` не вызван, результат `[]`
- три пустые секции → `ask` не вызван, в логе есть `Нет чатов для выбора.`

### 8. Credentials in .env

`python-dotenv` не добавлять. `.env` уже в `.gitignore`. В `CONTEXT.md` имя файла не писать. `people.py` и `clean_my_messages.py` не трогать.

В `docs/adr/0003-session-lifecycle.md` заменить только первое предложение на:

```md
API id/hash resolve from `TELEGRAM_API_ID` + `TELEGRAM_API_HASH`, else `.env`, else a one-time read of `cache.json` that writes `.env`, else a prompt that writes `.env`. `cache.json` is not deleted.
```

Остальное в 0003 не менять: резолв при открытии клиента, не при импорте; сессия `cleaner`; `*.session` не удалять.

В `README.md` строку про сохранение ключей заменить на: `5. (При первом запуске скрипт запросит у вас API ID и API HASH, и сохранит их в файл `.env`.)` Другие секции README не трогать. `.env.example` не создавать.

`session.py`:

```python
def env_path() -> Path:
    return Path(__file__).resolve().parent / ".env"

def resolve_credentials(
    path: Path | str | None = None,
    *,
    cache: Path | str | None = None,
    environ: Mapping[str, str] = os.environ,
    input_fn: Any = input,
) -> tuple[int, str]:
```

`path` по умолчанию — `env_path()`. `cache` по умолчанию — `cache_path()` (`cache.json` рядом со скриптом). `os.environ` не мутировать. Хеш не логировать.

Порядок:

1. Обе переменные есть в `environ` → `int(ENV_ID), str(ENV_HASH)`. Файл не читать и не писать. Не-int → `ValueError`, без цикла.
2. Ровно одна переменная в `environ` → игнорировать `environ` и идти в файлы.
3. `.env` существует (`is_file()`) и в нём оба ключа, а id парсится в `int` → вернуть их, не писать, не читать `cache.json`, не звать `input_fn`.
4. `.env` не существует, `cache.json` существует и в нём оба ключа `API_ID` / `API_HASH`, id парсится в `int` → записать `.env`, вернуть эти значения, `input_fn` не звать, `cache.json` не удалять и не менять.
5. Иначе промпт теми же строками: `Введите ваш Telegram API ID: ` и `Введите ваш Telegram API HASH: `. Не-int id → `ValueError`, файл не писать. Иначе записать `.env` и вернуть значения.

Битый или неполный `.env` (файл есть, но нет ключа, не-int, битая строка, `OSError` при чтении) — это не «файла нет». `cache.json` в этом случае не читать. Падать в промпт и перезаписывать `.env`.

Парсер, только для наших двух ключей:

- utf-8, `splitlines`
- пустые строки и строки, у которых `strip()` начинается с `#`, пропускать
- резать по первому `=`
- ключ и значение `strip`
- если значение в парных `'` или `"`, снять одну пару
- `${...}` не раскрывать
- лишние ключи игнорировать

Запись `.env`, utf-8, без кавычек, с переводом строки в конце:

```text
TELEGRAM_API_ID=77
TELEGRAM_API_HASH=secret
```

Родителя нет — `mkdir(parents=True)`, как сейчас для кэша. Ошибку записи не глушить.

В `test_logic.py` старые проверки `resolve_credentials` про `cache.json` как основное хранилище заменить. Каталожные проверки из раздела 7 оставить. Новые, без сети, пути во временном каталоге:

- обе переменные в `environ` → `(123, "abc")`, `input_fn` не вызван, `.env` не создан
- `environ` пуст, `.env` с `TELEGRAM_API_ID=42` и `TELEGRAM_API_HASH=zzz` → `(42, "zzz")`, `input_fn` не вызван, файл не переписан
- `.env` нет, `cache.json` с `{"API_ID": 42, "API_HASH": "zzz"}` → `(42, "zzz")`, `input_fn` не вызван, `.env` содержит эти два ключа, `cache.json` на диске с исходным JSON
- нет обоих файлов → промпт `77` / `secret`, `.env` записан, `cache.json` не создан
- `.env` есть, но без хеша, и рядом годный `cache.json` → `cache.json` не используется, идёт промпт, `.env` перезаписан

## Critical files & anchors

- `catalog.py` `prompt_selection`: сейчас `get_dialogs(limit=1000)` и фильтр только `SUPERGROUP`; это место заменяется, числовой `input()` удаляется
- `.venv/Lib/site-packages/pyrogram/types/user_and_chats/chat.py` `Chat._parse_dialog`: sync-разбор peer в `Chat`. Сырой `Dialog.top_message` — int; дату брать из `message.date`. `Message._parse` не вызывать.
- `docs/adr/0002-chat-catalog-seam.md`: ещё обещает stdin и только супергруппы; первая строка должна указать на 0004
- `test_logic.py` проверки `parse_selection`: удалить вместе с функцией, не переписывать под чекбоксы
- `session.py` `resolve_credentials`: сейчас пишет `cache.json`. Заменяется на `.env` с одноразовым чтением `cache.json`. `os.environ` не мутировать.

## Verification

Рабочая директория — корень репозитория. Telegram не вызывать: ни `clean_my_messages.py`, ни `people.py`.

1. `uv run python test_logic.py` завершается 0 и печатает `OK: all checks passed`.
2. `uv run python -c "import clean_my_messages, catalog"` завершается 0 и ничего не спрашивает.
3. `uv run ruff check .` завершается 0.
4. `parse_selection` отсутствует в `catalog.py` и `test_logic.py`.

## Assumptions & contingencies

- `questionary.checkbox(...).ask()` возвращает список значений, пустой список или `None`. Если установленная версия при отмене бросает только `KeyboardInterrupt`, не глушить его: выход без purge уже нужное поведение, ветка `None` остаётся для обёртки, которая вернёт `None`.
- Если `uv add questionary` не резолвится на `requires-python = ">=3.9,<3.14"`, остановиться и показать ошибку резолвера. Другую TUI-библиотеку не ставить.
- Если у `messages.GetDialogs` в установленном Pyrogram нет аргумента `folder_id`, остановиться. Не фильтровать архив эвристикой по полям диалога.
