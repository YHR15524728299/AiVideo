from __future__ import annotations

from pathlib import Path

import pytest

import aicf.atomic_io as atomic_io


def _windows_error(winerror: int) -> OSError:
    error = OSError(f"injected WinError {winerror}")
    error.winerror = winerror
    return error


@pytest.mark.parametrize("winerror", [5, 32])
def test_atomic_replace_retries_windows_sharing_conflicts_with_exponential_delays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    winerror: int,
) -> None:
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    source.write_text("new", encoding="utf-8")
    attempts = 0
    sleeps: list[float] = []

    def flaky_replace(current: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise _windows_error(winerror)
        current.rename(destination)

    monkeypatch.setattr(atomic_io, "_IS_WINDOWS", True)
    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", sleeps.append)

    atomic_io.atomic_replace(source, target)

    assert attempts == 3
    assert sleeps == [0.01, 0.02]
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("winerror", [2, 3, 87])
def test_atomic_replace_immediately_raises_other_windows_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    winerror: int,
) -> None:
    source = tmp_path / "source.json"
    target = tmp_path / "target.json"
    error = _windows_error(winerror)
    sleeps: list[float] = []

    monkeypatch.setattr(atomic_io, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        atomic_io.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(atomic_io.time, "sleep", sleeps.append)

    with pytest.raises(OSError) as caught:
        atomic_io.atomic_replace(source, target)

    assert caught.value is error
    assert sleeps == []


def test_atomic_replace_immediately_raises_sharing_error_off_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error = _windows_error(5)
    sleeps: list[float] = []

    monkeypatch.setattr(atomic_io, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        atomic_io.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(atomic_io.time, "sleep", sleeps.append)

    with pytest.raises(OSError) as caught:
        atomic_io.atomic_replace(tmp_path / "source", tmp_path / "target")

    assert caught.value is error
    assert sleeps == []


def test_atomic_replace_stops_retrying_within_one_second(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error = _windows_error(32)
    elapsed = 0.0
    attempts = 0

    def always_conflicts(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    def monotonic() -> float:
        return elapsed

    def sleep(delay: float) -> None:
        nonlocal elapsed
        elapsed += delay

    monkeypatch.setattr(atomic_io, "_IS_WINDOWS", True)
    monkeypatch.setattr(atomic_io.os, "replace", always_conflicts)
    monkeypatch.setattr(atomic_io.time, "monotonic", monotonic)
    monkeypatch.setattr(atomic_io.time, "sleep", sleep)

    with pytest.raises(OSError) as caught:
        atomic_io.atomic_replace(tmp_path / "source", tmp_path / "target")

    assert caught.value is error
    assert elapsed <= 1.0
    assert attempts > 1
