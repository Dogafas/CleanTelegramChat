"""Runnable checks for catalog, purge and session logic. No pytest, no network."""

import json
import logging
import tempfile
from pathlib import Path

from pyrogram import enums
from pyrogram.errors import FloodWait

from catalog import (
    ALL,
    ALL_BASIC,
    ALL_PRIVATE,
    ALL_PUBLIC,
    chat_label,
    prompt_selection,
    select_from_dialogs,
)
from purge import purge_chat, purge_chats
from session import resolve_credentials, resolve_proxy, telegram_client


class FakeChat:
    def __init__(self, id, title, username=None, type=enums.ChatType.SUPERGROUP):
        self.id = id
        self.title = title
        self.username = username
        self.type = type


class FakeDialog:
    def __init__(self, chat):
        self.chat = chat


# --- catalog seam ---


def _check_select_catalog_classification():
    c1 = FakeChat(1, "Chat One", username="user1")
    assert chat_label(c1) == "Chat One (@user1) — 1"
    c2 = FakeChat(2, "Chat Two", username=None)
    assert chat_label(c2) == "Chat Two — 2"
    c3 = FakeChat(3, None, username="")
    assert chat_label(c3) == "без названия — 3"

    c_pub = FakeChat(1, "Pub", username="pub_user", type=enums.ChatType.SUPERGROUP)
    c_priv_none = FakeChat(2, "Priv1", username=None, type=enums.ChatType.SUPERGROUP)
    c_priv_empty = FakeChat(3, "Priv2", username="", type=enums.ChatType.SUPERGROUP)
    c_basic = FakeChat(4, "Basic", username=None, type=enums.ChatType.GROUP)
    c_channel = FakeChat(5, "Chan", username="channel_user", type=enums.ChatType.CHANNEL)
    c_private_chat = FakeChat(6, "Direct", username="direct_user", type=enums.ChatType.PRIVATE)
    c_bot = FakeChat(7, "Bot", username="bot_user", type=enums.ChatType.BOT)
    c_basic_first = FakeChat(10, "First Basic", type=enums.ChatType.GROUP)
    c_pub_second = FakeChat(10, "Second Pub", username="pub", type=enums.ChatType.SUPERGROUP)

    dialogs = [
        FakeDialog(c_pub),
        FakeDialog(c_priv_none),
        FakeDialog(c_priv_empty),
        FakeDialog(c_basic),
        FakeDialog(c_channel),
        FakeDialog(c_private_chat),
        FakeDialog(c_bot),
        FakeDialog(c_basic_first),
        FakeDialog(c_pub_second),
    ]

    captured_choices = []

    def fake_ask(choices):
        captured_choices.append(choices)
        return [ALL]

    res = select_from_dialogs(dialogs, ask=fake_ask, confirm=lambda _: True, log=lambda _: None)
    assert res == [c_pub, c_priv_none, c_priv_empty, c_basic, c_basic_first]

    choices = captured_choices[0]
    sentinel_values = {c[1] for c in choices if len(c) == 3}
    assert {ALL, ALL_PUBLIC, ALL_PRIVATE, ALL_BASIC}.issubset(sentinel_values)
    separators = {c[0] for c in choices if len(c) == 1}
    assert separators == {"Публичные супергруппы", "Закрытые супергруппы", "Обычные группы"}

    # Empty sections hidden
    captured_single = []
    select_from_dialogs(
        [FakeDialog(c_pub)],
        ask=lambda c: captured_single.append(c) or [c_pub.id],
        confirm=lambda _: True,
        log=lambda _: None,
    )
    single_choices = captured_single[0]
    single_values = {c[1] for c in single_choices if len(c) == 3}
    assert ALL_PUBLIC in single_values
    assert ALL_PRIVATE not in single_values
    assert ALL_BASIC not in single_values
    single_separators = {c[0] for c in single_choices if len(c) == 1}
    assert "Публичные супергруппы" in single_separators
    assert "Закрытые супергруппы" not in single_separators
    assert "Обычные группы" not in single_separators


def _check_select_resolution():
    pub_1 = FakeChat(101, "P1", username="p1", type=enums.ChatType.SUPERGROUP)
    pub_2 = FakeChat(102, "P2", username="p2", type=enums.ChatType.SUPERGROUP)
    priv_1 = FakeChat(103, "Pr1", username=None, type=enums.ChatType.SUPERGROUP)
    b_1 = FakeChat(104, "B1", type=enums.ChatType.GROUP)
    b_2 = FakeChat(105, "B2", type=enums.ChatType.GROUP)

    dialogs = [
        FakeDialog(pub_1),
        FakeDialog(pub_2),
        FakeDialog(priv_1),
        FakeDialog(b_1),
        FakeDialog(b_2),
    ]

    r1 = select_from_dialogs(
        dialogs, ask=lambda _: [ALL_PUBLIC, b_1.id], confirm=lambda _: True, log=lambda _: None
    )
    assert r1 == [pub_1, pub_2, b_1]

    r_all = select_from_dialogs(
        dialogs, ask=lambda _: [ALL], confirm=lambda _: True, log=lambda _: None
    )
    assert r_all == [pub_1, pub_2, priv_1, b_1, b_2]

    sub_dialogs = [FakeDialog(pub_1), FakeDialog(b_1)]
    r_priv = select_from_dialogs(
        sub_dialogs, ask=lambda _: [ALL_PRIVATE], confirm=lambda _: True, log=lambda _: None
    )
    assert r_priv == []

    r_basic = select_from_dialogs(
        [FakeDialog(b_1)], ask=lambda _: [ALL_BASIC], confirm=lambda _: True, log=lambda _: None
    )
    assert r_basic == [b_1]

    r_unk = select_from_dialogs(
        dialogs, ask=lambda _: [999, "unknown_str"], confirm=lambda _: True, log=lambda _: None
    )
    assert r_unk == []

    r_dedup = select_from_dialogs(
        dialogs, ask=lambda _: [ALL_PUBLIC, pub_1.id], confirm=lambda _: True, log=lambda _: None
    )
    assert r_dedup == [pub_1, pub_2]


def _check_select_from_dialogs():
    pub_1 = FakeChat(101, "P1", username="p1", type=enums.ChatType.SUPERGROUP)
    priv_1 = FakeChat(103, "Pr1", username=None, type=enums.ChatType.SUPERGROUP)
    b_1 = FakeChat(104, "B1", type=enums.ChatType.GROUP)
    c_channel = FakeChat(5, "Chan", username="channel_user", type=enums.ChatType.CHANNEL)

    captured_choices = []
    ask_answers = [[pub_1.id], [pub_1.id, b_1.id]]
    confirm_answers = [False, True]
    logs = []

    def fake_ask(choices):
        captured_choices.append(choices)
        return ask_answers.pop(0)

    def fake_confirm(msg):
        return confirm_answers.pop(0)

    all_dialogs = [FakeDialog(pub_1), FakeDialog(priv_1), FakeDialog(b_1)]
    result = select_from_dialogs(
        all_dialogs,
        ask=fake_ask,
        confirm=fake_confirm,
        log=logs.append,
    )
    assert result == [pub_1, b_1]
    assert len(captured_choices) == 2

    first_chat_choices = [c for c in captured_choices[0] if len(c) == 3 and isinstance(c[1], int)]
    assert all(not c[2] for c in first_chat_choices)

    second_chat_choices = [c for c in captured_choices[1] if len(c) == 3 and isinstance(c[1], int)]
    second_checked_map = {c[1]: c[2] for c in second_chat_choices}
    assert second_checked_map.get(pub_1.id) is True
    assert second_checked_map.get(b_1.id) is False

    confirm_called = []
    log_empty = []
    res_empty = select_from_dialogs(
        all_dialogs,
        ask=lambda choices: [],
        confirm=lambda msg: confirm_called.append(msg) or True,
        log=log_empty.append,
    )
    assert res_empty == []
    assert confirm_called == []
    assert "Ничего не выбрано." in log_empty

    log_cancel = []
    res_cancel = select_from_dialogs(
        all_dialogs,
        ask=lambda choices: None,
        confirm=lambda msg: True,
        log=log_cancel.append,
    )
    assert res_cancel == []
    assert "Выбор отменён." in log_cancel

    log_none = []
    res_none = select_from_dialogs(
        [FakeDialog(c_channel)],
        ask=lambda choices: (_ for _ in ()).throw(AssertionError("ask should not be called")),
        confirm=lambda msg: (_ for _ in ()).throw(AssertionError("confirm should not be called")),
        log=log_none.append,
    )
    assert res_none == []
    assert "Нет чатов для выбора." in log_none

def _check_prompt_restores_loop():
    import asyncio  # noqa: PLC0415

    previous = asyncio.get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    class _Empty:
        def __init__(self):
            self.users = []
            self.chats = []
            self.messages = []
            self.dialogs = []

    class _App:
        def get_dialogs(self):
            return [FakeDialog(FakeChat(1, "G", type=enums.ChatType.GROUP))]

        def invoke(self, *_args, **_kwargs):
            return _Empty()

    def steal(_choices):
        asyncio.run(asyncio.sleep(0))
        return [1]

    def steal_confirm(_message):
        asyncio.run(asyncio.sleep(0))
        return True

    app = _App()
    app.loop = loop
    try:
        picked = prompt_selection(app, ask=steal, confirm=steal_confirm, log=lambda _msg: None)
        assert [chat.id for chat in picked] == [1]
        assert asyncio.get_event_loop() is loop
    finally:
        asyncio.set_event_loop(previous)
        loop.close()


def run_catalog_cases():
    _check_select_catalog_classification()
    _check_select_resolution()
    _check_select_from_dialogs()
    _check_prompt_restores_loop()


# --- purge_chat: FloodWait on delete, repeat batch stop ---



class FloodOnDeleteApp:
    """search yields 100 ids twice; delete raises FloodWait once then succeeds."""

    def __init__(self):
        self.batches = [
            [type("M", (), {"id": i})() for i in range(100)],
            [type("M", (), {"id": i})() for i in range(100)],
        ]
        self.delete_calls = []
        self.flooded = False

    def search_messages(self, chat_id, from_user, limit):
        return self.batches.pop(0)

    def delete_messages(self, chat_id, ids, revoke):
        self.delete_calls.append(list(ids))
        if not self.flooded:
            self.flooded = True
            raise FloodWait(30)


def run_flood_case():
    app = FloodOnDeleteApp()
    sleeps = []

    def gen(a, b):
        return 1.0 if b == 3.0 else 0.0  # jitter -> 1.0, pauses -> 0.0

    purge_chat(app, -100, sleep=sleeps.append, rng=gen, log=lambda *a: None)
    assert app.flooded, "FloodWait never raised"
    assert sleeps[0] == 31.0, f"expected flood sleep 30+1, got {sleeps[0]}"
    assert sleeps.count(31.0) == 1, f"exactly one flood sleep expected, got {sleeps}"
    assert app.delete_calls[0] == app.delete_calls[1], "delete retried with different ids"
    assert len(app.delete_calls) == 2, app.delete_calls
    assert len(app.batches) == 0, "search refetched after FloodWait"


# --- purge_chat: short final batch returns without sleeping ---


class ShortBatchApp:
    def __init__(self):
        self.batch = [type("M", (), {"id": i})() for i in (7, 8)]
        self.deleted = []
        self.searches = 0

    def search_messages(self, chat_id, from_user, limit):
        self.searches += 1
        return self.batch

    def delete_messages(self, chat_id, ids, revoke):
        self.deleted.append(list(ids))


def run_short_batch_case():
    app = ShortBatchApp()
    sleeps = []
    purge_chat(app, -100, sleep=sleeps.append, rng=lambda a, b: 0, log=lambda *a: None)
    assert app.deleted == [[7, 8]]
    assert app.searches == 1, "search must not refetch after a partial batch"
    assert sleeps == [], sleeps


# --- purge_chats: one chat_pause between chats, none before the first ---


def run_purge_chats_case():
    class NoMessagesApp:
        def search_messages(self, chat_id, from_user, limit):
            return []

    chats = [FakeChat(-1001, "one"), FakeChat(-1002, "two")]
    sleeps = []
    logs = []
    purge_chats(
        NoMessagesApp(),
        chats,
        sleep=sleeps.append,
        rng=lambda a, b: a,
        log=logs.append,
    )
    assert sleeps == [25.0], f"expected exactly one chat_pause between chats, got {sleeps}"
    assert "Удаление сообщений из чата: two" in " ".join(logs)


# --- resolve_credentials (session seam) ---


def run_credentials_cases():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        cache_path = Path(tmp) / "cache.json"

        # (a) both env vars "123"/"abc" -> (123, "abc"), input_fn never called, no .env created
        calls = []
        assert resolve_credentials(
            env_path,
            cache=cache_path,
            environ={"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "abc"},
            input_fn=lambda *a, **k: calls.append(1) or "",
        ) == (123, "abc")
        assert calls == []
        assert not env_path.exists()

        # (b) environ={}, .env pre-written with TELEGRAM_API_ID=42 / TELEGRAM_API_HASH=zzz ->
        # (42, "zzz"), no input_fn, .env content byte-unchanged after
        content_b = "TELEGRAM_API_ID=42\nTELEGRAM_API_HASH=zzz\n"
        env_path.write_text(content_b, encoding="utf-8")
        assert resolve_credentials(
            env_path,
            cache=cache_path,
            environ={},
            input_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")),
        ) == (42, "zzz")
        assert env_path.read_text(encoding="utf-8") == content_b

        # (c) no .env, cache.json {"API_ID": 42, "API_HASH": "zzz"} -> (42, "zzz"), no input_fn,
        # .env now exists and contains both TELEGRAM_ keys,
        # cache.json still on disk with original JSON
        env_path.unlink()
        cache_raw_c = json.dumps({"API_ID": 42, "API_HASH": "zzz"})
        cache_path.write_text(cache_raw_c, encoding="utf-8")
        assert resolve_credentials(
            env_path,
            cache=cache_path,
            environ={},
            input_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")),
        ) == (42, "zzz")
        assert env_path.is_file()
        env_c = env_path.read_text(encoding="utf-8")
        assert "TELEGRAM_API_ID=42" in env_c
        assert "TELEGRAM_API_HASH=zzz" in env_c
        assert cache_path.read_text(encoding="utf-8") == cache_raw_c

        # (d) neither file exists -> prompt answers 77/secret, .env written with both keys,
        # cache.json not created
        env_path.unlink()
        cache_path.unlink()
        answers_d = iter(["77", "secret"])
        assert resolve_credentials(
            env_path,
            cache=cache_path,
            environ={},
            input_fn=lambda *a, **k: next(answers_d),
        ) == (77, "secret")
        assert env_path.is_file()
        env_d = env_path.read_text(encoding="utf-8")
        assert "TELEGRAM_API_ID=77" in env_d
        assert "TELEGRAM_API_HASH=secret" in env_d
        assert not cache_path.exists()

        # (e) .env exists but has only TELEGRAM_API_ID (no hash) next to a valid cache.json ->
        # cache.json NOT used (input_fn IS called), .env overwritten with prompted values
        env_path.write_text("TELEGRAM_API_ID=999\n", encoding="utf-8")
        cache_raw_e = json.dumps({"API_ID": 888, "API_HASH": "from_cache"})
        cache_path.write_text(cache_raw_e, encoding="utf-8")
        answers_e = iter(["55", "new_hash"])
        assert resolve_credentials(
            env_path,
            cache=cache_path,
            environ={},
            input_fn=lambda *a, **k: next(answers_e),
        ) == (55, "new_hash")
        env_e = env_path.read_text(encoding="utf-8")
        assert "TELEGRAM_API_ID=55" in env_e
        assert "TELEGRAM_API_HASH=new_hash" in env_e
        assert cache_path.read_text(encoding="utf-8") == cache_raw_e



def _check_proxy_source_cases(tmp_path: Path) -> None:
    # 1. No proxy keys, no file -> None, file not created
    p1 = tmp_path / "absent.env"
    assert resolve_proxy(p1, environ={}) is None
    assert not p1.exists()

    # 2. environ with scheme socks5, host 10.0.0.1, port 1080; .env with other host ->
    # dict exactly {"scheme": "socks5", "hostname": "10.0.0.1", "port": 1080},
    # file not used as source (host from file is not in result)
    p2 = tmp_path / "file2.env"
    p2.write_text(
        "TELEGRAM_PROXY_SCHEME=http\n"
        "TELEGRAM_PROXY_HOSTNAME=192.168.1.1\n"
        "TELEGRAM_PROXY_PORT=8080\n",
        encoding="utf-8",
    )
    assert resolve_proxy(
        p2,
        environ={
            "TELEGRAM_PROXY_SCHEME": "socks5",
            "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
        },
    ) == {"scheme": "socks5", "hostname": "10.0.0.1", "port": 1080}

    # 3. environ empty, .env contains 5 keys -> dict with 5 fields, port int
    p3 = tmp_path / "file3.env"
    p3.write_text(
        "TELEGRAM_PROXY_SCHEME=http\n"
        "TELEGRAM_PROXY_HOSTNAME=proxy.example.com\n"
        "TELEGRAM_PROXY_PORT=3128\n"
        "TELEGRAM_PROXY_USERNAME=user\n"
        "TELEGRAM_PROXY_PASSWORD=pass\n",
        encoding="utf-8",
    )
    res3 = resolve_proxy(p3, environ={})
    assert res3 == {
        "scheme": "http",
        "hostname": "proxy.example.com",
        "port": 3128,
        "username": "user",
        "password": "pass",
    }
    assert isinstance(res3["port"], int)

    # 4. Schemes SOCKS4 and http accepted, in lowercase in dict
    assert resolve_proxy(
        p1,
        environ={
            "TELEGRAM_PROXY_SCHEME": "SOCKS4",
            "TELEGRAM_PROXY_HOSTNAME": "10.0.0.2",
            "TELEGRAM_PROXY_PORT": "1080",
        },
    ) == {"scheme": "socks4", "hostname": "10.0.0.2", "port": 1080}
    assert resolve_proxy(
        p1,
        environ={
            "TELEGRAM_PROXY_SCHEME": "http",
            "TELEGRAM_PROXY_HOSTNAME": "10.0.0.3",
            "TELEGRAM_PROXY_PORT": "8080",
        },
    ) == {"scheme": "http", "hostname": "10.0.0.3", "port": 8080}


def _check_proxy_validation_cases(tmp_path: Path) -> None:
    p1 = tmp_path / "absent.env"

    # 5. Scheme mtproto -> ValueError, message contains mtproto and socks5
    try:
        resolve_proxy(
            p1,
            environ={
                "TELEGRAM_PROXY_SCHEME": "mtproto",
                "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                "TELEGRAM_PROXY_PORT": "1080",
            },
        )
        raise AssertionError("mtproto should fail")
    except ValueError as exc:
        msg = str(exc)
        assert "mtproto" in msg and "socks5" in msg

    # 6. Host tg://proxy?server=1.2.3.4 -> ValueError about link, not dict
    try:
        resolve_proxy(
            p1,
            environ={
                "TELEGRAM_PROXY_SCHEME": "socks5",
                "TELEGRAM_PROXY_HOSTNAME": "tg://proxy?server=1.2.3.4",
                "TELEGRAM_PROXY_PORT": "1080",
            },
        )
        raise AssertionError("host with :// should fail")
    except ValueError as exc:
        assert "ссылкой" in str(exc)

    # 7. Only TELEGRAM_PROXY_HOSTNAME -> ValueError about incomplete config, not None
    try:
        resolve_proxy(
            p1,
            environ={"TELEGRAM_PROXY_HOSTNAME": "10.0.0.1"},
        )
        raise AssertionError("incomplete proxy should fail")
    except ValueError as exc:
        assert "Неполная настройка прокси" in str(exc)

    # 8. Username without password -> ValueError about pair
    try:
        resolve_proxy(
            p1,
            environ={
                "TELEGRAM_PROXY_SCHEME": "socks5",
                "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                "TELEGRAM_PROXY_PORT": "1080",
                "TELEGRAM_PROXY_USERNAME": "user_only",
            },
        )
        raise AssertionError("user without pass should fail")
    except ValueError as exc:
        assert "Логин и пароль прокси задаются только вместе" in str(exc)

    # 9. Port 0, 65536, abc -> ValueError about port
    for bad_port in ("0", "65536", "abc"):
        try:
            resolve_proxy(
                p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "socks5",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                    "TELEGRAM_PROXY_PORT": bad_port,
                },
            )
            raise AssertionError(f"bad port {bad_port} should fail")
        except ValueError as exc:
            assert "Некорректный порт прокси" in str(exc)
            assert bad_port in str(exc)


def _check_proxy_integration_cases(tmp_path: Path) -> None:
    # 10. Corrupted .env (TELEGRAM_API_ID=999, no hash) with 3 valid proxy lines,
    # valid cache.json nearby: credentials prompted, .env rewritten with new credentials
    # and preserves same 3 proxy lines, cache.json unchanged
    p10 = tmp_path / "env10.env"
    p10.write_text(
        "TELEGRAM_API_ID=999\n"
        "TELEGRAM_PROXY_SCHEME=socks5\n"
        "TELEGRAM_PROXY_HOSTNAME=127.0.0.1\n"
        "TELEGRAM_PROXY_PORT=1080\n",
        encoding="utf-8",
    )
    c10 = tmp_path / "cache10.json"
    raw_c10 = json.dumps({"API_ID": 888, "API_HASH": "from_cache"})
    c10.write_text(raw_c10, encoding="utf-8")
    answers_10 = iter(["55", "new_hash"])
    assert resolve_credentials(
        p10,
        cache=c10,
        environ={},
        input_fn=lambda *a, **k: next(answers_10),
    ) == (55, "new_hash")
    content_10 = p10.read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=55" in content_10
    assert "TELEGRAM_API_HASH=new_hash" in content_10
    assert "TELEGRAM_PROXY_SCHEME=socks5" in content_10
    assert "TELEGRAM_PROXY_HOSTNAME=127.0.0.1" in content_10
    assert "TELEGRAM_PROXY_PORT=1080" in content_10
    assert c10.read_text(encoding="utf-8") == raw_c10

    # 11. telegram_client: factory records kwargs and yields itself as context manager.
    # environ with both credentials and full proxy including password s3cret.
    # env_file points to non-existent temporary path.
    # After with, factory kwargs has proxy["hostname"] and proxy["password"] == "s3cret".
    # Collected logging records contain host and do not contain s3cret.
    class RecordingClientFactory:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    class ListLogHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(self.format(record))

    handler = ListLogHandler()
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    p11 = tmp_path / "non_existent.env"
    try:
        with telegram_client(
            session_name="test_session",
            client_factory=RecordingClientFactory,
            environ={
                "TELEGRAM_API_ID": "123",
                "TELEGRAM_API_HASH": "hash123",
                "TELEGRAM_PROXY_SCHEME": "socks5",
                "TELEGRAM_PROXY_HOSTNAME": "proxy.myhost.net",
                "TELEGRAM_PROXY_PORT": "1080",
                "TELEGRAM_PROXY_USERNAME": "myuser",
                "TELEGRAM_PROXY_PASSWORD": "s3cret",
            },
            env_file=p11,
        ) as app:
            assert isinstance(app, RecordingClientFactory)
            assert app.kwargs["proxy"]["hostname"] == "proxy.myhost.net"
            assert app.kwargs["proxy"]["password"] == "s3cret"  # noqa: S105

        logged = " ".join(handler.records)
        assert "proxy.myhost.net" in logged
        assert "s3cret" not in logged
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(old_level)


def run_proxy_cases():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _check_proxy_source_cases(tmp_path)
        _check_proxy_validation_cases(tmp_path)
        _check_proxy_integration_cases(tmp_path)
run_catalog_cases()
run_flood_case()
run_short_batch_case()
run_purge_chats_case()
run_credentials_cases()
run_proxy_cases()
print("OK: all checks passed")
