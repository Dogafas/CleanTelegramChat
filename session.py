import inspect
import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pyrogram import Client

ENV_ID = "TELEGRAM_API_ID"
ENV_HASH = "TELEGRAM_API_HASH"
SESSION_NAME = "cleaner"


def env_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def cache_path() -> Path:
    return Path(__file__).resolve().parent / "cache.json"


def _ask(input_fn: Any, prompt_text: str) -> str:
    try:
        sig = inspect.signature(input_fn)
        if not sig.parameters:
            return str(input_fn())
    except (ValueError, TypeError):
        pass
    try:
        return str(input_fn(prompt_text))
    except TypeError:
        return str(input_fn())


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
        if k in (ENV_ID, ENV_HASH):
            data[k] = v
    return data


def _write_env(path: Path, api_id: int, api_hash: str) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    content = f"{ENV_ID}={api_id}\n{ENV_HASH}={api_hash}\n"
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

    raw_id = _ask(input_fn, "Введите ваш Telegram API ID: ")
    api_id = int(raw_id)
    api_hash = _ask(input_fn, "Введите ваш Telegram API HASH: ")

    _write_env(target_path, api_id, api_hash)
    return api_id, api_hash

@contextmanager
def telegram_client(session_name: str = SESSION_NAME):
    api_id, api_hash = resolve_credentials()
    with Client(session_name, api_id=api_id, api_hash=api_hash) as app:
        yield app
