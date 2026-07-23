from __future__ import annotations

import json
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from aicf.atomic_io import atomic_replace
from aicf.file_lock import os_file_lock


class M2PromotionManager:
    def __init__(
        self,
        output_dir: Path,
        managed_files: set[str],
        fault_injector: Callable[[str], None] | None = None,
        *,
        lock_timeout: float = 10.0,
    ) -> None:
        self.output_dir = output_dir
        self.managed_files = managed_files
        self.fault_injector = fault_injector
        self.lock_timeout = lock_timeout
        self.lock_path = output_dir.parent / f".{output_dir.name}.promotion.lock"
        self.journal_path = (
            output_dir.parent / f".{output_dir.name}.promotion.journal.json"
        )

    def recover(self) -> None:
        if not self.journal_path.exists():
            return
        with self._lock():
            if self.journal_path.exists():
                self._rollback(self._read_journal())

    def promote(self, staging: Path) -> None:
        with self._lock():
            if self.journal_path.exists():
                self._rollback(self._read_journal())
            self._promote_locked(staging)

    def _promote_locked(self, staging: Path) -> None:
        backup = self.output_dir.parent / (
            f".{self.output_dir.name}.backup-{uuid.uuid4().hex}"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        backup.mkdir()
        journal: dict[str, object] = {
            "version": 1,
            "phase": "prepared",
            "output_dir": str(self.output_dir),
            "staging": str(staging),
            "backup": str(backup),
            "promoted": [],
            "pending": None,
        }
        self._write_journal(journal)
        try:
            for name in self.managed_files:
                current = self.output_dir / name
                if current.exists():
                    atomic_replace(current, backup / name)
            journal["phase"] = "current_backed_up"
            self._write_journal(journal)
            self._inject("after_current_backed_up")

            promoted: list[str] = []
            for source in staging.iterdir():
                if source.name not in self.managed_files:
                    raise ValueError(f"M2 staging 包含未管理文件: {source.name}")
                journal["pending"] = source.name
                journal["phase"] = "promoting"
                self._write_journal(journal)
                atomic_replace(source, self.output_dir / source.name)
                self._inject("after_target_replaced_before_journal")
                promoted.append(source.name)
                journal["promoted"] = promoted
                journal["pending"] = None
                journal["phase"] = "promoting"
                self._write_journal(journal)
                self._inject(f"after_promote:{source.name}")
            journal["phase"] = "promoted"
            self._write_journal(journal)
            self._inject("after_all_promoted")
        except Exception:
            self._rollback(journal)
            raise
        else:
            shutil.rmtree(staging, ignore_errors=True)
            self.journal_path.unlink(missing_ok=True)
            self._inject("between_journal_and_backup_cleanup")
            shutil.rmtree(backup, ignore_errors=True)

    def _rollback(self, journal: dict[str, object]) -> None:
        output_dir = Path(str(journal["output_dir"]))
        backup = Path(str(journal["backup"]))
        staging = Path(str(journal["staging"]))
        promoted = journal.get("promoted", [])
        if isinstance(promoted, list):
            for name in promoted:
                (output_dir / str(name)).unlink(missing_ok=True)
        pending = journal.get("pending")
        if isinstance(pending, str):
            (output_dir / pending).unlink(missing_ok=True)
        if backup.exists():
            output_dir.mkdir(parents=True, exist_ok=True)
            for previous in backup.iterdir():
                target = output_dir / previous.name
                target.unlink(missing_ok=True)
                atomic_replace(previous, target)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        self.journal_path.unlink(missing_ok=True)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with os_file_lock(
            self.lock_path,
            timeout=self.lock_timeout,
            timeout_message=f"M2 promotion 文件锁超时: {self.lock_path}",
        ):
            yield

    def _read_journal(self) -> dict[str, object]:
        value = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("M2 promotion journal 顶层必须是对象")
        return value

    def _write_journal(self, value: dict[str, object]) -> None:
        temporary = self.journal_path.with_name(
            f"{self.journal_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        atomic_replace(temporary, self.journal_path)

    def _inject(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)
