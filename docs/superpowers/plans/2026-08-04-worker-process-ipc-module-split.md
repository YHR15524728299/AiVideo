# Worker 进程探测与停止 IPC 模块拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将进程身份探测和停止文件IPC从 `background_worker.py` 提取为两个独立规范所有者，同时保持现有安全协议、CLI、GUI、记录格式和用户行为不变。

**Architecture:** 新建 `process_identity.py` 作为三态进程探测唯一实现，新建 `worker_stop_ipc.py` 作为实例停止请求唯一实现；`background_worker.py` 删除原实现，仅导入并重新导出公共符号，然后继续负责编排启动、握手、生命周期锁和终态线性化。迁移采用TDD和delete-first，不保留包装函数或第二份实现。

**Tech Stack:** Python 3.11、Pydantic 2、pytest、Windows `kernel32`、POSIX `/proc`、文件型IPC、Git

---

## 文件结构

**创建**

- `src/aicf/process_identity.py`：进程身份模型、三态探测和便捷查询。
- `src/aicf/worker_stop_ipc.py`：停止请求路径、Monitor、当前Worker自终止。
- `tests/test_process_identity.py`：进程模块边界测试。
- `tests/test_worker_stop_ipc.py`：停止IPC模块边界测试。

**修改**

- `src/aicf/background_worker.py`：删除平台探测和IPC实现，改为导入、重新导出并编排。
- `tests/test_background_worker.py`：保留生命周期集成测试，移除已经下沉到新模块的重复单元测试。

**不修改**

- `src/aicf/gui.py`
- `src/aicf/cli.py`
- `data/`
- `outputs/`
- `worker.json`字段
- `_work/runtime/stop-<instance_id>.*`协议

---

### Task 1: 建立隔离执行环境与行为基线

**Files:**
- Read: `docs/superpowers/specs/2026-08-04-worker-process-ipc-module-split-design.md`
- Read: `src/aicf/background_worker.py`
- Read: `tests/test_background_worker.py`

- [ ] **Step 1: 创建独立worktree**

从仓库根目录执行：

```powershell
& 'C:\Program Files\Git\cmd\git.exe' worktree add '..\ai_content_factory-worker-split' -b 'refactor/worker-module-split'
```

Expected: 新worktree创建成功，分支为 `refactor/worker-module-split`。

- [ ] **Step 2: 确认基线提交和工作区**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' status -sb
& 'C:\Program Files\Git\cmd\git.exe' log -2 --oneline
```

Expected:

```text
## refactor/worker-module-split
833a2d6 docs: design worker module split
793753c fix(worker): secure concurrent start and stop
```

- [ ] **Step 3: 复用主工作区虚拟环境**

worktree不会包含被Git忽略的 `.venv`。在新worktree根目录创建本地目录联接：

```powershell
$mainVenv = (Resolve-Path '..\ai_content_factory\.venv').Path
New-Item -ItemType Junction -Path '.venv' -Target $mainVenv
& '.\.venv\Scripts\python.exe' --version
```

Expected: Python版本为3.11，`.venv`不会出现在Git状态中。

- [ ] **Step 4: 运行Worker行为基线**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_background_worker.py -q
```

Expected: `20 passed`。

- [ ] **Step 5: 运行完整基线**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
```

Expected: 全部通过，仅允许现有的1项条件跳过。

- [ ] **Step 6: 记录基线，不提交代码**

本任务不产生文件变更。若基线不通过，停止实施并先定位环境或基线问题。

---

### Task 2: 提取进程身份探测模块

**Files:**
- Create: `src/aicf/process_identity.py`
- Create: `tests/test_process_identity.py`
- Modify: `src/aicf/background_worker.py`
- Test: `tests/test_process_identity.py`
- Test: `tests/test_background_worker.py`

- [ ] **Step 1: 写新模块边界的失败测试**

创建 `tests/test_process_identity.py`：

```python
from __future__ import annotations

import os

from aicf.process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_is_running,
)


def test_invalid_pid_is_not_running() -> None:
    probe = probe_process_identity(0)
    assert probe == ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)


def test_current_process_has_complete_identity() -> None:
    probe = probe_process_identity(os.getpid())
    assert probe.status == ProcessProbeStatus.RUNNING
    assert probe.identity is not None
    assert probe.identity.pid == os.getpid()
    assert probe.identity.created_at_ns > 0
    assert probe.identity.executable


def test_identity_helpers_only_accept_running_probe(monkeypatch) -> None:
    identity = ProcessIdentity(
        pid=123,
        created_at_ns=456,
        executable="python.exe",
    )
    monkeypatch.setattr(
        "aicf.process_identity.probe_process_identity",
        lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    assert get_process_identity(123) == identity
    assert process_is_running(123) is True

    monkeypatch.setattr(
        "aicf.process_identity.probe_process_identity",
        lambda _pid: ProcessProbe(status=ProcessProbeStatus.UNKNOWN),
    )
    assert get_process_identity(123) is None
    assert process_is_running(123) is False
```

- [ ] **Step 2: 运行测试并确认RED**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_process_identity.py -q
```

Expected: FAIL，错误包含：

```text
ModuleNotFoundError: No module named 'aicf.process_identity'
```

- [ ] **Step 3: 创建进程探测模块**

创建 `src/aicf/process_identity.py`。内容必须从当前 `background_worker.py` 移动，不重新设计语义：

```python
from __future__ import annotations

import ctypes
import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class ProcessIdentity(BaseModel):
    pid: int
    created_at_ns: int
    executable: str


class ProcessProbeStatus(str, Enum):
    RUNNING = "RUNNING"
    NOT_RUNNING = "NOT_RUNNING"
    UNKNOWN = "UNKNOWN"


class ProcessProbe(BaseModel):
    status: ProcessProbeStatus
    identity: ProcessIdentity | None = None


def probe_process_identity(pid: int) -> ProcessProbe:
    if pid <= 0:
        return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
    if os.name == "nt":
        return _probe_windows_process(pid)
    return _probe_proc_process(pid)


def _probe_windows_process(pid: int) -> ProcessProbe:
    process_query_limited_information = 0x1000
    still_active = 259

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", ctypes.c_ulong),
            ("high", ctypes.c_ulong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        status = (
            ProcessProbeStatus.NOT_RUNNING
            if error_code == 87
            else ProcessProbeStatus.UNKNOWN
        )
        return ProcessProbe(status=status)
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        if exit_code.value != still_active:
            return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        size = ctypes.c_ulong(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            image,
            ctypes.byref(size),
        ):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        created_at_ns = ((creation.high << 32) | creation.low) * 100
        return ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=ProcessIdentity(
                pid=pid,
                created_at_ns=created_at_ns,
                executable=str(Path(image.value).resolve()),
            ),
        )
    finally:
        kernel32.CloseHandle(handle)


def _probe_proc_process(pid: int) -> ProcessProbe:
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
    try:
        stat_fields = (proc_dir / "stat").read_text(encoding="utf-8").split()
        executable = str((proc_dir / "exe").resolve(strict=True))
    except (OSError, SystemError):
        return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
    return ProcessProbe(
        status=ProcessProbeStatus.RUNNING,
        identity=ProcessIdentity(
            pid=pid,
            created_at_ns=int(stat_fields[21]),
            executable=executable,
        ),
    )


def get_process_identity(pid: int) -> ProcessIdentity | None:
    probe = probe_process_identity(pid)
    return probe.identity if probe.status == ProcessProbeStatus.RUNNING else None


def process_is_running(pid: int) -> bool:
    return probe_process_identity(pid).status == ProcessProbeStatus.RUNNING
```

- [ ] **Step 4: 运行新模块测试并确认GREEN**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_process_identity.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 在Worker中导入并重新导出进程符号**

在 `src/aicf/background_worker.py` 顶部加入：

```python
from .process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_is_running,
)
```

从 `background_worker.py` 删除：

```python
class ProcessIdentity
class ProcessProbeStatus
class ProcessProbe
def probe_process_identity
def get_process_identity
def process_is_running
```

同时删除只由这些实现使用的顶层导入：

```python
import ctypes
from enum import Enum
```

不得保留包装函数。

- [ ] **Step 6: 运行进程模块与Worker集成回归**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_process_identity.py tests/test_background_worker.py -q
```

Expected: 全部通过。

- [ ] **Step 7: 检查唯一所有者**

```powershell
Get-ChildItem 'src\aicf' -Filter '*.py' |
  Select-String -Pattern '^class ProcessIdentity|^class ProcessProbe|^class ProcessProbeStatus|^def probe_process_identity'
```

Expected: 所有定义只出现在 `process_identity.py`；`background_worker.py`仅有导入。

- [ ] **Step 8: 提交进程模块**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' add `
  'src/aicf/process_identity.py' `
  'src/aicf/background_worker.py' `
  'tests/test_process_identity.py'
& 'C:\Program Files\Git\cmd\git.exe' commit -m 'refactor(worker): extract process identity probe'
```

Expected: 单个原子提交成功。

---

### Task 3: 提取停止 IPC 模块

**Files:**
- Create: `src/aicf/worker_stop_ipc.py`
- Create: `tests/test_worker_stop_ipc.py`
- Modify: `src/aicf/background_worker.py`
- Test: `tests/test_worker_stop_ipc.py`
- Test: `tests/test_background_worker.py`

- [ ] **Step 1: 写停止IPC边界的失败测试**

创建 `tests/test_worker_stop_ipc.py`：

```python
from __future__ import annotations

import threading
import time

import pytest

from aicf.worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
)


def test_stop_request_path_rejects_unsafe_instance_id(tmp_path) -> None:
    for value in ("", "../escape", "a/b", "x" * 65):
        with pytest.raises(WorkerIdentityError, match="令牌"):
            stop_request_path(tmp_path, value)


def test_monitor_ignores_other_instance(tmp_path) -> None:
    terminated = threading.Event()
    other = stop_request_path(tmp_path, "instance-b")
    other.parent.mkdir(parents=True)
    other.write_text("stop", encoding="utf-8")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.01,
    ):
        time.sleep(0.03)

    assert terminated.is_set() is False
    assert other.is_file()


def test_monitor_handles_existing_request_and_writes_ack(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")
    terminated = threading.Event()

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.5,
    ):
        assert terminated.wait(timeout=0.2)

    assert request.with_suffix(".ack").is_file()


def test_monitor_preserves_request_and_writes_error_on_failure(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")

    def fail() -> None:
        raise OSError("stop failed")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=fail,
        poll_interval=0.01,
    ):
        deadline = time.monotonic() + 1
        while not request.with_suffix(".error").is_file():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert request.is_file()
    assert "stop failed" in request.with_suffix(".error").read_text(
        encoding="utf-8"
    )
```

- [ ] **Step 2: 运行测试并确认RED**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_worker_stop_ipc.py -q
```

Expected: FAIL，错误包含：

```text
ModuleNotFoundError: No module named 'aicf.worker_stop_ipc'
```

- [ ] **Step 3: 创建停止IPC模块**

创建 `src/aicf/worker_stop_ipc.py`：

```python
from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from .atomic_io import atomic_write_text


class WorkerIdentityError(RuntimeError):
    pass


def stop_request_path(job_dir: str | Path, instance_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance_id):
        raise WorkerIdentityError("Worker实例令牌格式无效")
    return (
        Path(job_dir)
        / "_work"
        / "runtime"
        / f"stop-{instance_id}.request"
    )


def terminate_current_process_tree() -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        os._exit(130)
    os.kill(os.getpid(), 15)


class StopRequestMonitor(AbstractContextManager["StopRequestMonitor"]):
    def __init__(
        self,
        job_dir: str | Path,
        instance_id: str,
        *,
        terminate_self: Callable[[], None] = terminate_current_process_tree,
        poll_interval: float = 0.2,
    ) -> None:
        self._request_path = stop_request_path(job_dir, instance_id)
        self._terminate_self = terminate_self
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StopRequestMonitor":
        def watch() -> None:
            while not self._stop.is_set():
                if self._request_path.is_file():
                    try:
                        self._terminate_self()
                    except BaseException as error:
                        atomic_write_text(
                            self._request_path.with_suffix(".error"),
                            f"{type(error).__name__}: {error}\n",
                        )
                    else:
                        atomic_write_text(
                            self._request_path.with_suffix(".ack"),
                            "stop signal handled\n",
                        )
                        return
                self._stop.wait(self._poll_interval)

        self._thread = threading.Thread(
            target=watch,
            name="aicf-worker-stop-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1.0)
```

- [ ] **Step 4: 运行IPC测试并确认GREEN**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_worker_stop_ipc.py -q
```

Expected: 全部通过。

- [ ] **Step 5: 在Worker中导入并重新导出IPC符号**

在 `src/aicf/background_worker.py` 顶部加入：

```python
from .worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
    terminate_current_process_tree,
)
```

将所有：

```python
_terminate_current_process_tree()
```

改为：

```python
terminate_current_process_tree()
```

从 `background_worker.py` 删除：

```python
class WorkerIdentityError
def stop_request_path
def _terminate_current_process_tree
class StopRequestMonitor
```

同时删除只由IPC实现使用的导入：

```python
import re
import threading
```

不得保留代理类或包装函数。

- [ ] **Step 6: 更新集成测试补丁目标**

在 `tests/test_background_worker.py` 中，将：

```python
monkeypatch.setattr(
    "aicf.background_worker._terminate_current_process_tree",
    lambda: (_ for _ in ()).throw(SystemExit(130)),
)
```

改为：

```python
monkeypatch.setattr(
    "aicf.background_worker.terminate_current_process_tree",
    lambda: (_ for _ in ()).throw(SystemExit(130)),
)
```

已有从 `aicf.background_worker` 导入 `StopRequestMonitor`和 `WorkerIdentityError`的代码保持不变，用于验证重新导出兼容性。

- [ ] **Step 7: 运行IPC和Worker集成回归**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_worker_stop_ipc.py `
  tests/test_background_worker.py `
  -q
```

Expected: 全部通过。

- [ ] **Step 8: 检查唯一所有者**

```powershell
Get-ChildItem 'src\aicf' -Filter '*.py' |
  Select-String -Pattern '^class StopRequestMonitor|^def stop_request_path|^class WorkerIdentityError'
```

Expected: 定义只出现在 `worker_stop_ipc.py`。

- [ ] **Step 9: 提交IPC模块**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' add `
  'src/aicf/worker_stop_ipc.py' `
  'src/aicf/background_worker.py' `
  'tests/test_worker_stop_ipc.py' `
  'tests/test_background_worker.py'
& 'C:\Program Files\Git\cmd\git.exe' commit -m 'refactor(worker): extract stop ipc'
```

Expected: 单个原子提交成功。

---

### Task 4: 收缩Worker测试并验证兼容导出

**Files:**
- Modify: `tests/test_background_worker.py`
- Modify: `tests/test_process_identity.py`
- Modify: `tests/test_worker_stop_ipc.py`
- Test: `tests/test_background_worker.py`
- Test: `tests/test_process_identity.py`
- Test: `tests/test_worker_stop_ipc.py`

- [ ] **Step 1: 为兼容导出写显式测试**

在 `tests/test_background_worker.py` 加入：

```python
def test_background_worker_reexports_infrastructure_symbols() -> None:
    from aicf import background_worker
    from aicf import process_identity
    from aicf import worker_stop_ipc

    assert background_worker.ProcessIdentity is process_identity.ProcessIdentity
    assert background_worker.ProcessProbe is process_identity.ProcessProbe
    assert (
        background_worker.ProcessProbeStatus
        is process_identity.ProcessProbeStatus
    )
    assert (
        background_worker.StopRequestMonitor
        is worker_stop_ipc.StopRequestMonitor
    )
    assert (
        background_worker.WorkerIdentityError
        is worker_stop_ipc.WorkerIdentityError
    )
    assert background_worker.stop_request_path is worker_stop_ipc.stop_request_path
```

- [ ] **Step 2: 运行兼容测试**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_background_worker.py::test_background_worker_reexports_infrastructure_symbols `
  -q
```

Expected: PASS。若失败，修复普通导入，不增加包装函数。

- [ ] **Step 3: 将纯进程测试从Worker测试移除**

从 `tests/test_background_worker.py` 删除：

```python
def test_process_probe_does_not_terminate_current_process
```

该行为已由 `tests/test_process_identity.py::test_current_process_has_complete_identity`和helper测试覆盖。

- [ ] **Step 4: 将纯IPC Monitor测试从Worker测试移除**

从 `tests/test_background_worker.py` 删除：

```python
def test_stop_monitor_terminates_only_its_instance
def test_stop_monitor_handles_request_present_before_start
```

这些行为已迁移到 `tests/test_worker_stop_ipc.py`。

保留以下Worker集成测试：

```text
test_stop_worker_refuses_reused_pid_identity
test_stop_worker_requests_stop_for_matching_instance
test_stop_request_wins_before_terminal_commit
```

它们验证生命周期层与新模块的组合，不属于重复测试。

- [ ] **Step 5: 运行三个模块测试**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_process_identity.py `
  tests/test_worker_stop_ipc.py `
  tests/test_background_worker.py `
  -q
```

Expected: 全部通过。

- [ ] **Step 6: 检查模块依赖方向**

```powershell
Select-String `
  -Path 'src\aicf\process_identity.py','src\aicf\worker_stop_ipc.py' `
  -Pattern 'background_worker|database|gui|JobRepository|WorkerRecord'
```

Expected: 无输出。

- [ ] **Step 7: 检查Worker文件规模**

```powershell
(Get-Content 'src\aicf\background_worker.py').Count
```

Expected: 明显低于重构前约670行；目标不超过430行。若超过430行，检查是否仍残留平台或IPC实现，不做无关拆分。

- [ ] **Step 8: 提交测试边界整理**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' add `
  'tests/test_background_worker.py' `
  'tests/test_process_identity.py' `
  'tests/test_worker_stop_ipc.py'
& 'C:\Program Files\Git\cmd\git.exe' commit -m 'test(worker): define extracted module boundaries'
```

Expected: 单个测试职责整理提交成功。

---

### Task 5: 执行回归、真实Windows冒烟和结构验证

**Files:**
- Verify: `src/aicf/process_identity.py`
- Verify: `src/aicf/worker_stop_ipc.py`
- Verify: `src/aicf/background_worker.py`
- Verify: `tests/`

- [ ] **Step 1: 运行GUI和CLI相关回归**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests/test_background_worker.py `
  tests/test_gui_settings.py `
  tests/test_cli_and_logging.py `
  -q
```

Expected: 全部通过。

- [ ] **Step 2: 运行完整测试**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
```

Expected: 全部通过，仅允许现有1项条件跳过。

- [ ] **Step 3: 编译检查**

```powershell
& '.\.venv\Scripts\python.exe' -m compileall -q src tests
```

Expected: 退出码0，无输出。

- [ ] **Step 4: 环境诊断**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src')
& '.\.venv\Scripts\python.exe' -m aicf doctor
```

Expected: 总体状态为“就绪，可以运行完整流水线”。

- [ ] **Step 5: 真实Windows并发启动冒烟**

在两个独立PowerShell进程中同时执行：

```powershell
$root = (Resolve-Path '.')
$command = @(
  '-NoProfile',
  '-Command',
  "`$env:PYTHONPATH='$($root.Path)\src'; & '$($root.Path)\.venv\Scripts\python.exe' -m aicf worker-start --job FED_RATE_20260731"
)
$first = Start-Process powershell -ArgumentList $command -PassThru -RedirectStandardOutput "$env:TEMP\aicf-worker-1.txt"
$second = Start-Process powershell -ArgumentList $command -PassThru -RedirectStandardOutput "$env:TEMP\aicf-worker-2.txt"
$first.WaitForExit()
$second.WaitForExit()
Get-Content "$env:TEMP\aicf-worker-1.txt"
Get-Content "$env:TEMP\aicf-worker-2.txt"
```

Expected:

- 两个结果PID相同；
- 一个 `reused: false`；
- 一个 `reused: true`；
- 不生成第二个Worker。

- [ ] **Step 6: 等待Worker自然完成并检查身份**

```powershell
Start-Sleep -Seconds 10
$env:PYTHONPATH = (Resolve-Path 'src')
& '.\.venv\Scripts\python.exe' -m aicf worker-status --job FED_RATE_20260731
```

Expected:

```text
"status": "READY_TO_PUBLISH"
"ready": true
"instance_id": "<非空>"
"process_created_at_ns": <大于0>
"process_executable": "<非空>"
```

- [ ] **Step 7: 运行真实协作停止冒烟**

使用临时目录和子进程运行已有测试逻辑，脚本必须放在系统临时目录，不写入仓库。脚本核心必须：

```python
process = subprocess.Popen(
    [base_python, smoke_script, "--child", str(job_dir)],
    env={
        **os.environ,
        "AICF_WORKER_LAUNCHED": "1",
        "AICF_WORKER_INSTANCE_ID": instance_id,
        "PYTHONPATH": os.pathsep.join([site_packages, source_root]),
    },
)
stop_worker(job_dir)
process.wait(timeout=10)
assert process.returncode is not None
```

Run:

```powershell
& '.\.venv\Scripts\python.exe' "$env:TEMP\aicf_worker_stop_smoke.py"
```

Expected:

```text
{"safe_stop": "PASS"}
```

- [ ] **Step 8: 检查唯一实现与残留引用**

```powershell
Get-ChildItem 'src\aicf' -Filter '*.py' |
  Select-String -Pattern '^class ProcessIdentity|^class ProcessProbe|^class StopRequestMonitor|^def stop_request_path'

Select-String `
  -Path 'src\aicf\background_worker.py' `
  -Pattern 'WinDLL|QueryFullProcessImageNameW|/proc/|class StopRequestMonitor|def stop_request_path'
```

Expected:

- 定义只出现在两个新模块；
- 第二条命令无输出。

- [ ] **Step 9: 检查差异和隐私边界**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' diff --check
& 'C:\Program Files\Git\cmd\git.exe' status -sb
& 'C:\Program Files\Git\cmd\git.exe' diff --stat
```

Expected:

- `diff --check`退出码0；
- 没有 `data/`、`outputs/`、日志、媒体或本机配置；
- 仅有计划内源码和测试改动。

---

### Task 6: 独立复审、收尾提交和推送

**Files:**
- Review: `src/aicf/process_identity.py`
- Review: `src/aicf/worker_stop_ipc.py`
- Review: `src/aicf/background_worker.py`
- Review: `tests/test_process_identity.py`
- Review: `tests/test_worker_stop_ipc.py`
- Review: `tests/test_background_worker.py`

- [ ] **Step 1: 请求独立只读复审**

复审提示必须包含：

```text
确认以下验收项：
1. process_identity.py 是进程探测唯一所有者；
2. worker_stop_ipc.py 是停止IPC唯一所有者；
3. background_worker.py 只编排并通过普通导入重新导出；
4. 没有双重实现、包装回退或循环依赖；
5. RUNNING/NOT_RUNNING/UNKNOWN语义未改变；
6. 停止请求、ACK、ERROR和自终止语义未改变；
7. 启动、停止、完成的生命周期锁与线性化未改变。
只报告合入阻塞和可合入结论，不修改文件。
```

Expected: “可合入”，无阻塞。若发现阻塞，回到对应任务，以失败测试复现后修复。

- [ ] **Step 2: 运行最终新鲜验证**

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
& '.\.venv\Scripts\python.exe' -m compileall -q src tests
& 'C:\Program Files\Git\cmd\git.exe' diff --check
```

Expected: 所有命令退出码0。

- [ ] **Step 3: 暂存允许文件**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' add `
  'src/aicf/process_identity.py' `
  'src/aicf/worker_stop_ipc.py' `
  'src/aicf/background_worker.py' `
  'tests/test_process_identity.py' `
  'tests/test_worker_stop_ipc.py' `
  'tests/test_background_worker.py'
```

不得使用 `git add -A`。

- [ ] **Step 4: 检查暂存清单和隐私**

```powershell
$names = & 'C:\Program Files\Git\cmd\git.exe' diff --staged --name-only
$names
$blocked = $names | Select-String -Pattern '^(outputs/|data/|logs/|config/.*\.yaml$)|\.(mp4|wav|db|sqlite)$'
if ($blocked) {
  $blocked
  exit 2
}
& 'C:\Program Files\Git\cmd\git.exe' diff --staged --check
```

Expected: 仅列出6个计划文件，无敏感或运行文件。

- [ ] **Step 5: 创建最终收尾提交**

如果Task 2至Task 4已分别提交，此步骤只提交复审后产生的必要小修；若无额外改动，跳过，不创建空提交。

若有改动：

```powershell
& 'C:\Program Files\Git\cmd\git.exe' commit -m 'refactor(worker): finalize module boundaries'
```

- [ ] **Step 6: 推送前同步检查**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' fetch origin
$behind = & 'C:\Program Files\Git\cmd\git.exe' rev-list --count HEAD..origin/main
$ahead = & 'C:\Program Files\Git\cmd\git.exe' rev-list --count origin/main..HEAD
"ahead=$ahead behind=$behind"
if ([int]$behind -ne 0) {
  exit 4
}
```

Expected: `behind=0`。

- [ ] **Step 7: 推送功能分支**

```powershell
& 'C:\Program Files\Git\cmd\git.exe' push -u origin 'refactor/worker-module-split'
```

Expected: 分支推送成功。不直接强推 `main`。

- [ ] **Step 8: 最终收口记录**

最终说明必须包含：

```text
证据动作：专项测试、完整pytest、compileall、真实Windows并发启动、真实协作停止、独立复审。
结果：所有检查的退出码和关键输出。
覆盖范围：进程探测、停止IPC、Worker编排兼容。
未覆盖范围：规格中列出的迁移事务、交付顺序、休眠恢复、任务删除和GUI体验。
残余风险：平台API仅在当前Windows环境和现有POSIX实现范围内验证。
置信等级：A或B，依据真实冒烟与复审结果决定。
```

复杂度结果应确认：

- `background_worker.py`显著缩小；
- 两个新模块均低于800行；
- 没有新增协议分支或兼容回退；
- 净熵下降。
