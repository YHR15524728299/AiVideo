from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable

from aicf.atomic_io import atomic_replace
from aicf.file_lock import os_file_lock


Replace = Callable[[Path, Path], None]


def _write_journal(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    with pending.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    atomic_replace(pending, path)


class DirectoryPromoter:
    def __init__(
        self,
        *,
        replace: Replace | None = None,
        lock_timeout: float = 30.0,
    ) -> None:
        self.replace = replace or atomic_replace
        self.lock_timeout = lock_timeout

    def promote(self, staged: Path, target: Path) -> None:
        staged = Path(staged)
        target = Path(target)
        journal_path = target.parent / f".{target.name}.promote.json"
        lock_path = target.parent / f".{target.name}.lock"
        backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
        with os_file_lock(
            lock_path,
            timeout=self.lock_timeout,
            timeout_message=f"{target.name} 交付目录正在被其他进程发布",
        ):
            journal = {
                "phase": "prepared",
                "staged": str(staged),
                "target": str(target),
                "backup": str(backup),
            }
            _write_journal(journal_path, journal)
            try:
                if target.exists():
                    self.replace(target, backup)
                journal["phase"] = "old_backed_up"
                _write_journal(journal_path, journal)
                self.replace(staged, target)
                journal["phase"] = "promoted"
                _write_journal(journal_path, journal)
                if backup.exists():
                    shutil.rmtree(backup)
                journal_path.unlink(missing_ok=True)
            except Exception:
                if not target.exists() and backup.exists():
                    self.replace(backup, target)
                journal["phase"] = "rolled_back"
                _write_journal(journal_path, journal)
                raise


class JournaledFileGroup:
    def __init__(self, target_dir: str | Path, *, lock_timeout: float = 30.0) -> None:
        self.target_dir = Path(target_dir)
        self.journal_path = self.target_dir / ".render-commit.json"
        self.lock_path = self.target_dir / ".render-commit.lock"
        self.lock_timeout = lock_timeout

    def prepare(self, staged_dir: Path, files: list[str]) -> None:
        _write_journal(
            self.journal_path,
            {
                "phase": "prepared",
                "staged": str(Path(staged_dir)),
                "files": list(files),
            },
        )

    def commit(self, staged_dir: Path, files: list[str]) -> None:
        with os_file_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            timeout_message="render 文件组正在被其他进程提交",
        ):
            self.prepare(staged_dir, files)
            self._recover_locked()

    def recover(self) -> None:
        with os_file_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            timeout_message="render 文件组正在被其他进程恢复",
        ):
            self._recover_locked()

    def _recover_locked(self) -> None:
        if not self.journal_path.is_file():
            return
        journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        staged = Path(journal["staged"])
        journal["phase"] = "promoting"
        _write_journal(self.journal_path, journal)
        for name in journal["files"]:
            source = staged / name
            target = self.target_dir / name
            if source.is_file():
                atomic_replace(source, target)
            elif not target.is_file():
                raise FileNotFoundError(f"render 提交文件丢失: {name}")
        journal["phase"] = "committed"
        _write_journal(self.journal_path, journal)
        if staged.exists():
            shutil.rmtree(staged)
        self.journal_path.unlink(missing_ok=True)
