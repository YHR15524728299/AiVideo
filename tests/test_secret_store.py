from __future__ import annotations

from pathlib import Path

from aicf.secret_store import (
    load_secret,
    load_runtime_secrets,
    migrate_secret_from_env,
    store_secret,
)


def test_secret_store_round_trip_does_not_write_plaintext(tmp_path: Path) -> None:
    store_path = tmp_path / "secrets.json"
    secret = "sk-or-v1-test-secret-that-must-not-be-plain"

    store_secret("OPENROUTER_API_KEY", secret, store_path=store_path)

    assert load_secret("OPENROUTER_API_KEY", store_path=store_path) == secret
    assert secret not in store_path.read_text(encoding="utf-8")


def test_migrate_secret_from_env_removes_plaintext_and_preserves_other_values(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    store_path = tmp_path / "secrets.json"
    env_path.write_text(
        "OPENROUTER_API_KEY=sk-or-v1-local-secret\n"
        "OPENROUTER_MODEL=test/model:free\n",
        encoding="utf-8",
    )

    migrated = migrate_secret_from_env(
        env_path,
        "OPENROUTER_API_KEY",
        store_path=store_path,
    )

    assert migrated is True
    assert load_secret("OPENROUTER_API_KEY", store_path=store_path) == (
        "sk-or-v1-local-secret"
    )
    env_text = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY" not in env_text
    assert "OPENROUTER_MODEL=test/model:free" in env_text


def test_load_runtime_secrets_populates_process_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store_path = tmp_path / "secrets.json"
    store_secret(
        "OPENROUTER_API_KEY",
        "encrypted-runtime-secret",
        store_path=store_path,
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    loaded = load_runtime_secrets(store_path=store_path)

    assert loaded == {"OPENROUTER_API_KEY": "encrypted-runtime-secret"}
    assert (
        __import__("os").environ["OPENROUTER_API_KEY"]
        == "encrypted-runtime-secret"
    )
