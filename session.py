import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pyrogram import Client

from ui import ask_confirm as _ask_confirm
from ui import ask_select as _ask_select
from ui import ask_text as _ask_text

ENV_ID = "TELEGRAM_API_ID"
ENV_HASH = "TELEGRAM_API_HASH"
SESSION_NAME = "cleaner"
SESSIONS_DIR_NAME = "sessions"
ALL_SESSIONS = "__all_sessions__"
CREATE_SESSION = "__create_session__"
DELETE_SESSION = "__delete_session__"

PROXY_SCHEME = "TELEGRAM_PROXY_SCHEME"
PROXY_HOSTNAME = "TELEGRAM_PROXY_HOSTNAME"
PROXY_PORT = "TELEGRAM_PROXY_PORT"
PROXY_USERNAME = "TELEGRAM_PROXY_USERNAME"
PROXY_PASSWORD = "TELEGRAM_PROXY_PASSWORD"  # noqa: S105
PROXY_KEYS = (PROXY_SCHEME, PROXY_HOSTNAME, PROXY_PORT, PROXY_USERNAME, PROXY_PASSWORD)
PROXY_SCHEMES = ("socks5", "socks4", "http")
_MAX_PORT = 65535


def env_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def cache_path() -> Path:
    return Path(__file__).resolve().parent / "cache.json"


def sessions_dir(root: Path | None = None) -> Path:
    base = Path(__file__).resolve().parent if root is None else Path(root)
    directory = base / SESSIONS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def migrate_legacy_sessions(root: Path | None = None) -> list[str]:
    base = Path(__file__).resolve().parent if root is None else Path(root)
    dest = sessions_dir(base)
    migrated: set[str] = set()
    for p in base.glob("*.session"):
        if p.is_file():
            p.replace(dest / p.name)
            migrated.add(p.stem)
            journal = p.with_name(f"{p.name}-journal")
            if journal.is_file():
                journal.replace(dest / journal.name)
    return sorted(migrated)


def list_sessions(root: Path | None = None) -> list[str]:
    migrate_legacy_sessions(root)
    dest = sessions_dir(root)
    return sorted(p.stem for p in dest.glob("*.session") if p.is_file())


def delete_session(name: str, root: Path | None = None) -> bool:
    dest = sessions_dir(root)
    session_file = dest / f"{name}.session"
    journal_file = dest / f"{name}.session-journal"
    if session_file.is_file():
        session_file.unlink()
        if journal_file.is_file():
            journal_file.unlink()
        return True
    return False


def _is_valid_session_name(name: str) -> bool:
    # ponytail: forbid path separators and control chars
    return bool(name) and not any(
        ch in name for ch in ("/", "\\", "\0", ":", "*", "?", '"', "<", ">", "|")
    )


def _prompt_new_session(ask_text: Callable[..., Any], existing: list[str]) -> str | None:
    while True:
        raw_name = ask_text("Введите имя новой сессии: ")
        if raw_name is None:
            return None
        name = raw_name.strip()
        if not name:
            return None
        if not _is_valid_session_name(name) or name in existing:
            continue
        return name


CANCEL_SESSION = "__cancel_session__"


def _handle_delete_session(
    root: Path | None,
    existing: list[str],
    ask_select: Callable[..., Any],
    ask_confirm: Callable[..., Any],
) -> None:
    del_choices = [(s, s) for s in existing] + [("Отмена", CANCEL_SESSION)]
    to_delete = ask_select("Выберите сессию для удаления:", del_choices)
    if to_delete not in (None, CANCEL_SESSION) and ask_confirm(
        f"Точно удалить сессию '{to_delete}'?", default=False
    ):
        delete_session(to_delete, root)


def prompt_sessions(
    root: Path | None = None,
    *,
    ask_select: Callable[..., Any] = _ask_select,
    ask_text: Callable[..., Any] = _ask_text,
    ask_confirm: Callable[..., Any] = _ask_confirm,
) -> list[str]:
    while True:
        existing = list_sessions(root)
        if not existing:
            name = _prompt_new_session(ask_text, existing)
            return [name] if name else []

        choices: list[Any] = []
        if len(existing) > 1:
            choices.append(("Все сессии", ALL_SESSIONS))  # noqa: RUF001
        for s in existing:
            choices.append((s, s))
        choices.append(("+ Создать новую сессию", CREATE_SESSION))
        choices.append(("- Удалить сессию", DELETE_SESSION))

        choice = ask_select("Выберите сессию для работы:", choices)
        if choice is None:
            return []
        if choice == ALL_SESSIONS:
            return existing
        if choice in existing:
            return [choice]
        if choice == CREATE_SESSION:
            name = _prompt_new_session(ask_text, existing)
            if name:
                return [name]
        elif choice == DELETE_SESSION:
            _handle_delete_session(root, existing, ask_select, ask_confirm)

def _parse_env(content: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        v = value.strip()
        if (
            ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")))
            and v not in ('"', "'")
        ):
            v = v[1:-1]
        if k in (ENV_ID, ENV_HASH, *PROXY_KEYS):
            data[k] = v
    return data


def _write_env(path: Path, api_id: int, api_hash: str) -> None:
    existing_data: dict[str, str] = {}
    if path.is_file():
        existing_data = _parse_env(path.read_text(encoding="utf-8"))
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{ENV_ID}={api_id}", f"{ENV_HASH}={api_hash}"]
    for key in PROXY_KEYS:
        if key in existing_data:
            lines.append(f"{key}={existing_data[key]}")
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")


def resolve_credentials(
    path: Path | str | None = None,
    *,
    cache: Path | str | None = None,
    environ: Mapping[str, str] = os.environ,
    input_fn: Any = input,
) -> tuple[int, str]:
    if ENV_ID in environ and ENV_HASH in environ:
        return int(environ[ENV_ID]), str(environ[ENV_HASH])

    target_path = Path(path) if path is not None else env_path()
    cache_file = Path(cache) if cache is not None else cache_path()

    if target_path.is_file():
        try:
            content = target_path.read_text(encoding="utf-8")
            data = _parse_env(content)
            if ENV_ID in data and ENV_HASH in data:
                return int(data[ENV_ID]), data[ENV_HASH]
        except (ValueError, OSError):
            pass
    elif cache_file.is_file():
        try:
            content = cache_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict) and "API_ID" in data and "API_HASH" in data:
                api_id = int(data["API_ID"])
                api_hash = str(data["API_HASH"])
                _write_env(target_path, api_id, api_hash)
                return api_id, api_hash
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            pass

    api_id = int(input_fn("Введите ваш Telegram API ID: "))
    api_hash = str(input_fn("Введите ваш Telegram API HASH: "))

    _write_env(target_path, api_id, api_hash)
    return api_id, api_hash

def _load_proxy_source(
    path: Path | str | None,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    env_proxy = {k: environ[k].strip() for k in PROXY_KEYS if k in environ and environ[k].strip()}
    if env_proxy:
        return env_proxy
    target_path = Path(path) if path is not None else env_path()
    if not target_path.is_file():
        return None
    file_data = _parse_env(target_path.read_text(encoding="utf-8"))
    file_proxy = {
        k: file_data[k].strip()
        for k in PROXY_KEYS
        if k in file_data and file_data[k].strip()
    }
    return file_proxy or None


def resolve_proxy(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, Any] | None:
    source = _load_proxy_source(path, environ)
    if source is None:
        return None

    if PROXY_SCHEME not in source or PROXY_HOSTNAME not in source or PROXY_PORT not in source:
        raise ValueError(
            "Неполная настройка прокси: "
            "нужны TELEGRAM_PROXY_SCHEME, TELEGRAM_PROXY_HOSTNAME и TELEGRAM_PROXY_PORT."
        )

    scheme_raw = source[PROXY_SCHEME]
    scheme_lower = scheme_raw.lower()
    if scheme_lower not in PROXY_SCHEMES:
        raise ValueError(
            f"Неизвестная схема прокси: {scheme_raw}. Допустимо: socks5, socks4, http."
        )

    port_raw = source[PROXY_PORT]
    try:
        port = int(port_raw)
        if not (1 <= port <= _MAX_PORT):
            raise ValueError
    except ValueError:
        raise ValueError(f"Некорректный порт прокси: {port_raw}.") from None

    hostname = source[PROXY_HOSTNAME]
    if "://" in hostname:
        raise ValueError("TELEGRAM_PROXY_HOSTNAME должен быть хостом, не ссылкой.")

    has_username = PROXY_USERNAME in source
    has_password = PROXY_PASSWORD in source
    if has_username != has_password:
        raise ValueError("Логин и пароль прокси задаются только вместе.")

    proxy: dict[str, Any] = {
        "scheme": scheme_lower,
        "hostname": hostname,
        "port": port,
    }
    if has_username and has_password:
        proxy["username"] = source[PROXY_USERNAME]
        proxy["password"] = source[PROXY_PASSWORD]
    return proxy


def restore_client_loop(app: Any) -> None:
    """prompt_toolkit (questionary) подменяет thread loop через asyncio.run().

    Синхронные вызовы Pyrogram и client.stop() требуют loop клиента —
    вызывайте после любых промптов внутри блока telegram_client.
    """
    loop = getattr(app, "loop", None)
    if loop is not None and not loop.is_closed():
        asyncio.set_event_loop(loop)


@contextmanager
def telegram_client(
    session_name: str,
    *,
    workdir: Path | str | None = None,
    client_factory: Any = Client,
    environ: Mapping[str, str] = os.environ,
    env_file: Path | str | None = None,
):
    api_id, api_hash = resolve_credentials(env_file, environ=environ)
    proxy = resolve_proxy(env_file, environ=environ)
    # Python 3.12+ / questionary: prompt_toolkit's asyncio.run() clears the thread
    # loop; Pyrogram 2.0.106 Dispatcher still calls get_event_loop(). This module
    # owns loop hygiene: ensure a loop exists for the client, restore the original
    # after the block so the next session starts clean.
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    target_workdir = str(workdir) if workdir is not None else str(sessions_dir())
    if proxy is None:
        logging.info("Прямое соединение с Telegram.")
    else:
        logging.info(
            f"Соединение с Telegram через прокси {proxy['scheme']}://{proxy['hostname']}:{proxy['port']}"
        )
    try:
        with client_factory(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy,
            workdir=target_workdir,
        ) as app:
            yield app
    finally:
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
        else:
            asyncio.set_event_loop(loop)
