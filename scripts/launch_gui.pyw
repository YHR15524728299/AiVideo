# -*- coding: utf-8 -*-
"""AI Content Factory 独立启动脚本（供 pythonw 直接调用）。"""
import sys
import os
from pathlib import Path


def _setup() -> Path:
    # scripts/launch_gui.pyw -> parent = scripts/, parent.parent = 项目根目录
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(str(project_root))
    src_path = str(project_root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    os.environ["AICF_PROJECT_ROOT"] = str(project_root)
    return project_root


if __name__ == "__main__":
    _setup()
    from aicf.gui import launch

    launch()
