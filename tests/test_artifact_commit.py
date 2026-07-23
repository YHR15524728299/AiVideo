from __future__ import annotations

import json
from pathlib import Path

import pytest

from aicf.artifact_commit import DirectoryPromoter, JournaledFileGroup


def test_directory_promote_failure_restores_old_directory(tmp_path: Path) -> None:
    target = tmp_path / "delivery"
    staged = tmp_path / ".delivery.staging"
    target.mkdir()
    staged.mkdir()
    (target / "version.txt").write_text("old", encoding="utf-8")
    (staged / "version.txt").write_text("new", encoding="utf-8")
    calls = 0

    def replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated promote crash")
        source.replace(destination)

    with pytest.raises(OSError, match="simulated"):
        DirectoryPromoter(replace=replace).promote(staged, target)

    assert (target / "version.txt").read_text(encoding="utf-8") == "old"
    journal = json.loads(
        (tmp_path / ".delivery.promote.json").read_text(encoding="utf-8")
    )
    assert journal["phase"] == "rolled_back"


def test_file_group_recovers_interrupted_commit_to_complete_new_version(
    tmp_path: Path,
) -> None:
    target = tmp_path / "final"
    staged = target / ".pending" / "txn-1"
    target.mkdir()
    staged.mkdir(parents=True)
    for name in ("master.mp4", "clean.mp4", "master.render.json", "master.ffprobe.json"):
        (target / name).write_text("old", encoding="utf-8")
        (staged / name).write_text("new", encoding="utf-8")
    group = JournaledFileGroup(target)
    group.prepare(staged, [path.name for path in staged.iterdir()])
    journal = json.loads(group.journal_path.read_text(encoding="utf-8"))
    journal["phase"] = "promoting"
    group.journal_path.write_text(json.dumps(journal), encoding="utf-8")
    (staged / "master.mp4").replace(target / "master.mp4")

    group.recover()

    assert all(
        (target / name).read_text(encoding="utf-8") == "new"
        for name in journal["files"]
    )
    assert not group.journal_path.exists()
