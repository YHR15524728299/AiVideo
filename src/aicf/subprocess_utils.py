"""Subprocess utilities that hide console windows on Windows."""
from __future__ import annotations

import subprocess
import sys
from typing import Any


# Windows creation flag to prevent console window from appearing
CREATE_NO_WINDOW = 0x08000000


def _windows_creationflags() -> int:
    """Return CREATE_NO_WINDOW flag on Windows, 0 on other platforms."""
    if sys.platform == "win32":
        return CREATE_NO_WINDOW
    return 0


def silent_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """subprocess.run wrapper that hides console windows on Windows.
    
    All arguments are passed through to subprocess.run, with the addition of
    CREATE_NO_WINDOW flag on Windows platforms to prevent cmd windows from popping up.
    """
    if "creationflags" not in kwargs:
        kwargs["creationflags"] = _windows_creationflags()
    else:
        # If caller explicitly provided creationflags, merge with our flag
        kwargs["creationflags"] = kwargs["creationflags"] | _windows_creationflags()
    return subprocess.run(*args, **kwargs)


def silent_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    """subprocess.Popen wrapper that hides console windows on Windows."""
    if "creationflags" not in kwargs:
        kwargs["creationflags"] = _windows_creationflags()
    else:
        kwargs["creationflags"] = kwargs["creationflags"] | _windows_creationflags()
    return subprocess.Popen(*args, **kwargs)
