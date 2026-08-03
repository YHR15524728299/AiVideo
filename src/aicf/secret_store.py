from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .atomic_io import atomic_write_text


class SecretStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _default_store_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "AIContentFactory" / "secrets.json"


def _to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("当前系统不支持 Windows 凭据加密")
    source, source_buffer = _to_blob(data)
    result = _DataBlob()
    success = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        "AI Content Factory",
        None,
        None,
        None,
        0,
        ctypes.byref(result),
    )
    del source_buffer
    if not success:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("当前系统不支持 Windows 凭据解密")
    source, source_buffer = _to_blob(data)
    result = _DataBlob()
    success = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(result),
    )
    del source_buffer
    if not success:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _read_store(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SecretStoreError("本地安全凭据文件已损坏") from error
    values = payload.get("secrets", {})
    if not isinstance(values, dict):
        raise SecretStoreError("本地安全凭据文件格式无效")
    return {str(key): str(value) for key, value in values.items()}


def _write_store(path: Path, values: dict[str, str]) -> None:
    payload = {"version": 1, "secrets": values}
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def store_secret(
    name: str,
    value: str,
    *,
    store_path: str | Path | None = None,
) -> None:
    path = Path(store_path) if store_path is not None else _default_store_path()
    values = _read_store(path)
    if value:
        protected = _protect(value.encode("utf-8"))
        values[name] = base64.b64encode(protected).decode("ascii")
    else:
        values.pop(name, None)
    _write_store(path, values)


def load_secret(
    name: str,
    *,
    store_path: str | Path | None = None,
) -> str:
    path = Path(store_path) if store_path is not None else _default_store_path()
    encoded = _read_store(path).get(name)
    if not encoded:
        return ""
    try:
        return _unprotect(base64.b64decode(encoded)).decode("utf-8")
    except (ValueError, UnicodeDecodeError, OSError) as error:
        raise SecretStoreError("无法读取本机安全凭据") from error


def load_runtime_secrets(
    *,
    store_path: str | Path | None = None,
) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for name in ("OPENROUTER_API_KEY",):
        if os.environ.get(name):
            continue
        value = load_secret(name, store_path=store_path)
        if value:
            os.environ[name] = value
            loaded[name] = value
    return loaded


def remove_secret_from_env(env_path: str | Path, name: str) -> bool:
    path = Path(env_path)
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{name}="
    kept = [line for line in lines if not line.lstrip().startswith(prefix)]
    if len(kept) == len(lines):
        return False
    content = "\n".join(kept)
    if content:
        content += "\n"
    atomic_write_text(path, content)
    return True


def migrate_secret_from_env(
    env_path: str | Path,
    name: str,
    *,
    store_path: str | Path | None = None,
) -> bool:
    path = Path(env_path)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, raw_value = line.partition("=")
        if separator and key.strip() == name:
            value = raw_value.strip().strip("\"'")
            if value:
                store_secret(name, value, store_path=store_path)
            remove_secret_from_env(path, name)
            return bool(value)
    return False
