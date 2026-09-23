"""Runnable checks for catalog, purge and session logic. No pytest, no network."""

import json
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
    membership_catalog,
    resolve_selection,
    select_from_dialogs,
)
from purge import batch_pause, chat_pause, flood_pause, purge_chat, purge_chats
from session import resolve_credentials


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


def _check_catalog_classification():
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

    dialogs = [
        FakeDialog(c_pub),
        FakeDialog(c_priv_none),
        FakeDialog(c_priv_empty),
        FakeDialog(c_basic),
        FakeDialog(c_channel),
        FakeDialog(c_private_chat),
        FakeDialog(c_bot),
    ]
    pub, priv, basic = membership_catalog(dialogs)
    assert pub == [c_pub]
    assert priv == [c_priv_none, c_priv_empty]
    assert basic == [c_basic]

    c_basic_first = FakeChat(10, "First Basic", type=enums.ChatType.GROUP)
    c_pub_second = FakeChat(10, "Second Pub", username="pub", type=enums.ChatType.SUPERGROUP)
    pub_dup, priv_dup, basic_dup = membership_catalog(
        [FakeDialog(c_basic_first), FakeDialog(c_pub_second)]
    )
    assert basic_dup == [c_basic_first]
    assert pub_dup == []
    assert priv_dup == []


def _check_resolve_selection():
    pub_1 = FakeChat(101, "P1", username="p1", type=enums.ChatType.SUPERGROUP)
    pub_2 = FakeChat(102, "P2", username="p2", type=enums.ChatType.SUPERGROUP)
    priv_1 = FakeChat(103, "Pr1", username=None, type=enums.ChatType.SUPERGROUP)
    b_1 = FakeChat(104, "B1", type=enums.ChatType.GROUP)
    b_2 = FakeChat(105, "B2", type=enums.ChatType.GROUP)

    resolved = resolve_selection(
        [pub_1, pub_2], [priv_1], [b_1, b_2], [ALL_PUBLIC, b_1.id]
    )
    assert resolved == [pub_1, pub_2, b_1]

    resolved_all = resolve_selection(
        [pub_1, pub_2], [priv_1], [b_1, b_2], [ALL]
    )
    assert resolved_all == [pub_1, pub_2, priv_1, b_1, b_2]

    assert resolve_selection([pub_1], [], [b_1], [ALL_PRIVATE]) == []
    assert resolve_selection([], [], [b_1], [ALL_BASIC]) == [b_1]

    assert resolve_selection([pub_1], [priv_1], [b_1], [999, "unknown_str"]) == []
    res_dedup = resolve_selection([pub_1, pub_2], [priv_1], [b_1], [ALL_PUBLIC, pub_1.id])
    assert res_dedup == [pub_1, pub_2]


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

    first_chat_choices = [
        c for c in captured_choices[0] if hasattr(c, "value") and isinstance(c.value, int)
    ]
    assert all(not getattr(c, "checked", False) for c in first_chat_choices)

    second_chat_choices = [
        c for c in captured_choices[1] if hasattr(c, "value") and isinstance(c.value, int)
    ]
    second_checked_map = {c.value: getattr(c, "checked", False) for c in second_chat_choices}
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

    log_none = []
    res_none = select_from_dialogs(
        [FakeDialog(c_channel)],
        ask=lambda choices: (_ for _ in ()).throw(AssertionError("ask should not be called")),
        confirm=lambda msg: (_ for _ in ()).throw(AssertionError("confirm should not be called")),
        log=log_none.append,
    )
    assert res_none == []
    assert "Нет чатов для выбора." in log_none


def run_catalog_cases():
    _check_catalog_classification()
    _check_resolve_selection()
    _check_select_from_dialogs()


# --- pauses (purge seam) ---
assert batch_pause(lambda a, b: a) == 8
assert batch_pause(lambda a, b: b) == 25
assert chat_pause(lambda a, b: a) == 25
assert chat_pause(lambda a, b: b) == 75
assert flood_pause(10, lambda a, b: a) == 11


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

run_catalog_cases()
run_flood_case()
run_short_batch_case()
run_purge_chats_case()
run_credentials_cases()
print("OK: all checks passed")
