import json
import logging
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pyrogram import Client

ENV_ID = "TELEGRAM_API_ID"
ENV_HASH = "TELEGRAM_API_HASH"
SESSION_NAME = "cleaner"

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


@contextmanager
def telegram_client(
    session_name: str = SESSION_NAME,
    *,
    client_factory: Any = Client,
    environ: Mapping[str, str] = os.environ,
    env_file: Path | str | None = None,
):
    api_id, api_hash = resolve_credentials(env_file, environ=environ)
    proxy = resolve_proxy(env_file, environ=environ)
    if proxy is None:
        logging.info("Прямое соединение с Telegram.")
    else:
        logging.info(
            f"Соединение с Telegram через прокси {proxy['scheme']}://{proxy['hostname']}:{proxy['port']}"
        )
    with client_factory(session_name, api_id=api_id, api_hash=api_hash, proxy=proxy) as app:
        yield app
