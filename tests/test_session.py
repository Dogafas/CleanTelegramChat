"""Checks for session logic: sessions dir, credentials, proxy, prompt menu, telegram_client."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any

from session import (
    ALL_SESSIONS,
    CANCEL_SESSION,
    CREATE_SESSION,
    DELETE_SESSION,
    delete_session,
    list_sessions,
    migrate_legacy_sessions,
    prompt_sessions,
    resolve_credentials,
    resolve_proxy,
    sessions_dir,
    telegram_client,
)


class ResolveCredentialsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.env_path = self.tmp_path / ".env"
        self.cache_path = self.tmp_path / "cache.json"

    def test_all_sources(self):
        env_path, cache_path = self.env_path, self.cache_path

        # (a) both env vars "123"/"abc" -> (123, "abc"), input_fn never called, no .env created
        with self.subTest(case="a"):
            calls = []
            self.assertEqual(
                resolve_credentials(
                    env_path,
                    cache=cache_path,
                    environ={"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "abc"},
                    input_fn=lambda *a, **k: calls.append(1) or "",
                ),
                (123, "abc"),
            )
            self.assertEqual(calls, [])
            self.assertFalse(env_path.exists())

        # (b) environ={}, .env pre-written with TELEGRAM_API_ID=42 / TELEGRAM_API_HASH=zzz ->
        # (42, "zzz"), no input_fn, .env content byte-unchanged after
        with self.subTest(case="b"):
            content_b = "TELEGRAM_API_ID=42\nTELEGRAM_API_HASH=zzz\n"
            env_path.write_text(content_b, encoding="utf-8")
            self.assertEqual(
                resolve_credentials(
                    env_path,
                    cache=cache_path,
                    environ={},
                    input_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")),
                ),
                (42, "zzz"),
            )
            self.assertEqual(env_path.read_text(encoding="utf-8"), content_b)

        # (c) no .env, cache.json {"API_ID": 42, "API_HASH": "zzz"} -> (42, "zzz"), no input_fn,
        # .env now exists and contains both TELEGRAM_ keys,
        # cache.json still on disk with original JSON
        with self.subTest(case="c"):
            env_path.unlink()
            cache_raw_c = json.dumps({"API_ID": 42, "API_HASH": "zzz"})
            cache_path.write_text(cache_raw_c, encoding="utf-8")
            self.assertEqual(
                resolve_credentials(
                    env_path,
                    cache=cache_path,
                    environ={},
                    input_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")),
                ),
                (42, "zzz"),
            )
            self.assertTrue(env_path.is_file())
            env_c = env_path.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_API_ID=42", env_c)
            self.assertIn("TELEGRAM_API_HASH=zzz", env_c)
            self.assertEqual(cache_path.read_text(encoding="utf-8"), cache_raw_c)

        # (d) neither file exists -> prompt answers 77/secret, .env written with both keys,
        # cache.json not created
        with self.subTest(case="d"):
            env_path.unlink()
            cache_path.unlink()
            answers_d = iter(["77", "secret"])
            self.assertEqual(
                resolve_credentials(
                    env_path,
                    cache=cache_path,
                    environ={},
                    input_fn=lambda *a, **k: next(answers_d),
                ),
                (77, "secret"),
            )
            self.assertTrue(env_path.is_file())
            env_d = env_path.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_API_ID=77", env_d)
            self.assertIn("TELEGRAM_API_HASH=secret", env_d)
            self.assertFalse(cache_path.exists())

        # (e) .env exists but has only TELEGRAM_API_ID (no hash) next to a valid cache.json ->
        # cache.json NOT used (input_fn IS called), .env overwritten with prompted values
        with self.subTest(case="e"):
            env_path.write_text("TELEGRAM_API_ID=999\n", encoding="utf-8")
            cache_raw_e = json.dumps({"API_ID": 888, "API_HASH": "from_cache"})
            cache_path.write_text(cache_raw_e, encoding="utf-8")
            answers_e = iter(["55", "new_hash"])
            self.assertEqual(
                resolve_credentials(
                    env_path,
                    cache=cache_path,
                    environ={},
                    input_fn=lambda *a, **k: next(answers_e),
                ),
                (55, "new_hash"),
            )
            env_e = env_path.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_API_ID=55", env_e)
            self.assertIn("TELEGRAM_API_HASH=new_hash", env_e)
            self.assertEqual(cache_path.read_text(encoding="utf-8"), cache_raw_e)


class ResolveProxySourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_no_config_returns_none_without_file(self):
        # 1. No proxy keys, no file -> None, file not created
        p1 = self.tmp_path / "absent.env"
        self.assertIsNone(resolve_proxy(p1, environ={}))
        self.assertFalse(p1.exists())

    def test_environ_wins_over_file(self):
        # 2. environ with scheme socks5, host 10.0.0.1, port 1080; .env with other host ->
        # dict exactly {"scheme": "socks5", "hostname": "10.0.0.1", "port": 1080},
        # file not used as source (host from file is not in result)
        p2 = self.tmp_path / "file2.env"
        p2.write_text(
            "TELEGRAM_PROXY_SCHEME=http\n"
            "TELEGRAM_PROXY_HOSTNAME=192.168.1.1\n"
            "TELEGRAM_PROXY_PORT=8080\n",
            encoding="utf-8",
        )
        self.assertEqual(
            resolve_proxy(
                p2,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "socks5",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                    "TELEGRAM_PROXY_PORT": "1080",
                },
            ),
            {"scheme": "socks5", "hostname": "10.0.0.1", "port": 1080},
        )

    def test_file_source_full_dict(self):
        # 3. environ empty, .env contains 5 keys -> dict with 5 fields, port int
        p3 = self.tmp_path / "file3.env"
        p3.write_text(
            "TELEGRAM_PROXY_SCHEME=http\n"
            "TELEGRAM_PROXY_HOSTNAME=proxy.example.com\n"
            "TELEGRAM_PROXY_PORT=3128\n"
            "TELEGRAM_PROXY_USERNAME=user\n"
            "TELEGRAM_PROXY_PASSWORD=pass\n",
            encoding="utf-8",
        )
        res3 = resolve_proxy(p3, environ={})
        self.assertEqual(
            res3,
            {
                "scheme": "http",
                "hostname": "proxy.example.com",
                "port": 3128,
                "username": "user",
                "password": "pass",
            },
        )
        self.assertIsInstance(res3["port"], int)

    def test_schemes_normalized_to_lowercase(self):
        # 4. Schemes SOCKS4 and http accepted, in lowercase in dict
        p1 = self.tmp_path / "absent.env"
        self.assertEqual(
            resolve_proxy(
                p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "SOCKS4",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.2",
                    "TELEGRAM_PROXY_PORT": "1080",
                },
            ),
            {"scheme": "socks4", "hostname": "10.0.0.2", "port": 1080},
        )
        self.assertEqual(
            resolve_proxy(
                p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "http",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.3",
                    "TELEGRAM_PROXY_PORT": "8080",
                },
            ),
            {"scheme": "http", "hostname": "10.0.0.3", "port": 8080},
        )


class ResolveProxyValidationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p1 = Path(tmp.name) / "absent.env"

    def test_mtproto_rejected(self):
        # 5. Scheme mtproto -> ValueError, message contains mtproto and socks5
        with self.assertRaises(ValueError) as cm:
            resolve_proxy(
                self.p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "mtproto",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                    "TELEGRAM_PROXY_PORT": "1080",
                },
            )
        msg = str(cm.exception)
        self.assertIn("mtproto", msg)
        self.assertIn("socks5", msg)

    def test_link_host_rejected(self):
        # 6. Host tg://proxy?server=1.2.3.4 -> ValueError about link, not dict
        with self.assertRaises(ValueError) as cm:
            resolve_proxy(
                self.p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "socks5",
                    "TELEGRAM_PROXY_HOSTNAME": "tg://proxy?server=1.2.3.4",
                    "TELEGRAM_PROXY_PORT": "1080",
                },
            )
        self.assertIn("ссылкой", str(cm.exception))

    def test_incomplete_config_rejected(self):
        # 7. Only TELEGRAM_PROXY_HOSTNAME -> ValueError about incomplete config, not None
        with self.assertRaises(ValueError) as cm:
            resolve_proxy(
                self.p1,
                environ={"TELEGRAM_PROXY_HOSTNAME": "10.0.0.1"},
            )
        self.assertIn("Неполная настройка прокси", str(cm.exception))

    def test_username_without_password_rejected(self):
        # 8. Username without password -> ValueError about pair
        with self.assertRaises(ValueError) as cm:
            resolve_proxy(
                self.p1,
                environ={
                    "TELEGRAM_PROXY_SCHEME": "socks5",
                    "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                    "TELEGRAM_PROXY_PORT": "1080",
                    "TELEGRAM_PROXY_USERNAME": "user_only",
                },
            )
        self.assertIn("Логин и пароль прокси задаются только вместе", str(cm.exception))

    def test_bad_ports_rejected(self):
        # 9. Port 0, 65536, abc -> ValueError about port
        for bad_port in ("0", "65536", "abc"):
            with self.subTest(port=bad_port), self.assertRaises(ValueError) as cm:
                resolve_proxy(
                    self.p1,
                    environ={
                        "TELEGRAM_PROXY_SCHEME": "socks5",
                        "TELEGRAM_PROXY_HOSTNAME": "10.0.0.1",
                        "TELEGRAM_PROXY_PORT": bad_port,
                    },
                )
            self.assertIn("Некорректный порт прокси", str(cm.exception))
            self.assertIn(bad_port, str(cm.exception))


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_corrupted_env_preserves_proxy_lines(self):
        # 10. Corrupted .env (TELEGRAM_API_ID=999, no hash) with 3 valid proxy lines,
        # valid cache.json nearby: credentials prompted, .env rewritten with new credentials
        # and preserves same 3 proxy lines, cache.json unchanged
        p10 = self.tmp_path / "env10.env"
        p10.write_text(
            "TELEGRAM_API_ID=999\n"
            "TELEGRAM_PROXY_SCHEME=socks5\n"
            "TELEGRAM_PROXY_HOSTNAME=127.0.0.1\n"
            "TELEGRAM_PROXY_PORT=1080\n",
            encoding="utf-8",
        )
        c10 = self.tmp_path / "cache10.json"
        raw_c10 = json.dumps({"API_ID": 888, "API_HASH": "from_cache"})
        c10.write_text(raw_c10, encoding="utf-8")
        answers_10 = iter(["55", "new_hash"])
        self.assertEqual(
            resolve_credentials(
                p10,
                cache=c10,
                environ={},
                input_fn=lambda *a, **k: next(answers_10),
            ),
            (55, "new_hash"),
        )
        content_10 = p10.read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_API_ID=55", content_10)
        self.assertIn("TELEGRAM_API_HASH=new_hash", content_10)
        self.assertIn("TELEGRAM_PROXY_SCHEME=socks5", content_10)
        self.assertIn("TELEGRAM_PROXY_HOSTNAME=127.0.0.1", content_10)
        self.assertIn("TELEGRAM_PROXY_PORT=1080", content_10)
        self.assertEqual(c10.read_text(encoding="utf-8"), raw_c10)

    def test_client_logs_host_but_not_password(self):
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

        p11 = self.tmp_path / "non_existent.env"
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
                self.assertIsInstance(app, RecordingClientFactory)
                self.assertEqual(app.kwargs["proxy"]["hostname"], "proxy.myhost.net")
                self.assertEqual(app.kwargs["proxy"]["password"], "s3cret")

            logged = " ".join(handler.records)
            self.assertIn("proxy.myhost.net", logged)
            self.assertNotIn("s3cret", logged)
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)


class SessionDirectoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_sessions_dir_creation(self):
        sdir = sessions_dir(self.tmp_path)
        self.assertTrue(sdir.is_dir())
        self.assertEqual(sdir, self.tmp_path / "sessions")

    def test_legacy_migration(self):
        root = self.tmp_path / "migration_root"
        root.mkdir()
        session_file = root / "cleaner.session"
        journal_file = root / "cleaner.session-journal"
        session_file.write_text("dummy-session-data", encoding="utf-8")
        journal_file.write_text("dummy-journal-data", encoding="utf-8")

        migrated = migrate_legacy_sessions(root)
        self.assertEqual(migrated, ["cleaner"])
        self.assertFalse(session_file.exists())
        self.assertFalse(journal_file.exists())

        sdir = root / "sessions"
        self.assertTrue((sdir / "cleaner.session").is_file())
        self.assertEqual(
            (sdir / "cleaner.session").read_text(encoding="utf-8"), "dummy-session-data"
        )
        self.assertTrue((sdir / "cleaner.session-journal").is_file())
        self.assertEqual(
            (sdir / "cleaner.session-journal").read_text(encoding="utf-8"), "dummy-journal-data"
        )

        migrated2 = migrate_legacy_sessions(root)
        self.assertEqual(migrated2, [])

    def test_list_and_delete_sessions(self):
        root = self.tmp_path / "list_delete_root"
        root.mkdir()
        sdir = sessions_dir(root)
        (sdir / "acc_b.session").write_text("b", encoding="utf-8")
        (sdir / "acc_a.session").write_text("a", encoding="utf-8")
        (sdir / "acc_a.session-journal").write_text("a-j", encoding="utf-8")
        (sdir / "not_a_session.txt").write_text("txt", encoding="utf-8")

        sessions = list_sessions(root)
        self.assertEqual(sessions, ["acc_a", "acc_b"])

        self.assertFalse(delete_session("non_existent", root))
        self.assertTrue(delete_session("acc_a", root))
        self.assertFalse((sdir / "acc_a.session").exists())
        self.assertFalse((sdir / "acc_a.session-journal").exists())
        self.assertEqual(list_sessions(root), ["acc_b"])


class PromptSessionsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_empty_dir_prompts_name_and_cancel(self):
        root = self.tmp_path / "prompt_empty_root"
        root.mkdir()
        calls: list[str] = []

        def fake_ask_text(msg: str) -> str:
            calls.append(msg)
            return " my_acc "

        res = prompt_sessions(root, ask_text=fake_ask_text)
        self.assertEqual(res, ["my_acc"])
        self.assertEqual(len(calls), 1)

        res_cancel = prompt_sessions(root, ask_text=lambda _msg: None)
        self.assertEqual(res_cancel, [])

    def test_select_single_and_cancel(self):
        root = self.tmp_path / "prompt_single_root"
        sdir = sessions_dir(root)
        (sdir / "acc1.session").write_text("1", encoding="utf-8")

        res = prompt_sessions(root, ask_select=lambda _msg, _choices: "acc1")
        self.assertEqual(res, ["acc1"])

        res_cancel = prompt_sessions(root, ask_select=lambda _msg, _choices: None)
        self.assertEqual(res_cancel, [])

    def test_select_all(self):
        root = self.tmp_path / "prompt_all_root"
        sdir = sessions_dir(root)
        (sdir / "acc1.session").write_text("1", encoding="utf-8")
        (sdir / "acc2.session").write_text("2", encoding="utf-8")

        res = prompt_sessions(root, ask_select=lambda _msg, _choices: ALL_SESSIONS)
        self.assertEqual(res, ["acc1", "acc2"])

    def test_create_and_delete_flows(self):
        root = self.tmp_path / "prompt_create_del_root"
        sdir = sessions_dir(root)
        (sdir / "acc1.session").write_text("1", encoding="utf-8")
        (sdir / "acc2.session").write_text("2", encoding="utf-8")

        select_answers = [CREATE_SESSION]
        text_answers = ["acc3"]
        res_create = prompt_sessions(
            root,
            ask_select=lambda _msg, _choices: select_answers.pop(0),
            ask_text=lambda _msg: text_answers.pop(0),
        )
        self.assertEqual(res_create, ["acc3"])

        select_queue = [DELETE_SESSION, "acc1", "acc2"]
        confirm_calls: list[str] = []

        def fake_confirm(msg: str, default: bool = False) -> bool:
            confirm_calls.append(msg)
            return True

        res_del = prompt_sessions(
            root,
            ask_select=lambda _msg, _choices: select_queue.pop(0),
            ask_confirm=fake_confirm,
        )
        self.assertEqual(res_del, ["acc2"])
        self.assertEqual(len(confirm_calls), 1)
        self.assertIn("acc1", confirm_calls[0])
        self.assertFalse((sdir / "acc1.session").exists())

        # «Отмена» в подменю удаления не спрашивает подтверждение и ничего не удаляет
        select_queue = [DELETE_SESSION, CANCEL_SESSION, "acc2"]
        confirm_calls.clear()
        res_cancel = prompt_sessions(
            root,
            ask_select=lambda _msg, _choices: select_queue.pop(0),
            ask_confirm=fake_confirm,
        )
        self.assertEqual(res_cancel, ["acc2"])
        self.assertFalse(confirm_calls)
        self.assertTrue((sdir / "acc2.session").exists())


class TelegramClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_passes_workdir(self):
        class DummyRecordingClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.args = args
                self.kwargs = kwargs

            def __enter__(self) -> "DummyRecordingClient":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        env = {
            "TELEGRAM_API_ID": "111",
            "TELEGRAM_API_HASH": "hash111",
        }
        custom_workdir = self.tmp_path / "custom_workdir"
        with telegram_client(
            session_name="my_session",
            workdir=custom_workdir,
            client_factory=DummyRecordingClient,
            environ=env,
        ) as app:
            self.assertEqual(app.args[0], "my_session")
            self.assertEqual(app.kwargs["workdir"], str(custom_workdir))

        with telegram_client(
            session_name="cleaner",
            client_factory=DummyRecordingClient,
            environ=env,
        ) as app:
            self.assertEqual(app.kwargs["workdir"], str(sessions_dir()))

    def test_restores_event_loop(self):
        class NoopClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.dispatcher_loop = asyncio.get_event_loop()

            def __enter__(self) -> "NoopClient":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h"}

        # Цикл event loop очищен (как после questionary/asyncio.run) — клиент должен работать
        old_loop = asyncio.get_event_loop()
        asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().close()
        asyncio.set_event_loop(None)
        try:
            with telegram_client(
                session_name="s",
                workdir=self.tmp_path,
                client_factory=NoopClient,
                environ=env,
            ) as app:
                self.assertFalse(app.dispatcher_loop.is_closed())
                # Симулируем prompt_toolkit: заменили loop на закрытый
                broken = asyncio.new_event_loop()
                broken.close()
                asyncio.set_event_loop(broken)
            # После выхода из блока исходный (закрытый до входа нельзя — берём рабочий)
            # восстановлен
            restored = asyncio.get_event_loop()
            self.assertFalse(restored.is_closed())
        finally:
            asyncio.set_event_loop(old_loop)


if __name__ == "__main__":
    unittest.main()
