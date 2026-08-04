# Worker 进程探测与停止 IPC 模块拆分设计

## 目标

将 `background_worker.py` 中已经验证的进程身份探测和停止 IPC 实现迁移到两个独立模块，降低 Worker 生命周期编排器的认知负担，同时保持现有运行协议、文件格式、CLI、GUI和安全边界不变。

本次是纯结构重构，不新增功能，不改变用户行为。

## 当前问题

`background_worker.py` 当前同时承担：

- Worker记录和启动结果模型；
- Windows/POSIX进程探测；
- Worker启动、握手和生命周期锁；
- 实例停止请求路径、Monitor和自终止；
- 电源抑制；
- Worker执行、终态提交和状态查询。

文件已经超过600行。进程平台细节和IPC协议与生命周期状态机混在一起，使安全审查、单元测试和后续维护成本持续增加。

## 设计原则

1. 每项基础设施能力只有一个规范所有者。
2. 移动代码，不复制代码；迁移后删除旧定义。
3. 保持现有安全协议，不增加兼容分支或回退路径。
4. 保持 `worker.json`、停止请求文件和生命周期锁位置不变。
5. 保持现有CLI和GUI调用方式不变。
6. 所有行为先由现有测试锁定，再迁移模块。

## 模块边界

### `process_identity.py`

进程探测的唯一所有者。

负责：

- `ProcessIdentity`
- `ProcessProbe`
- `ProcessProbeStatus`
- `probe_process_identity()`
- `get_process_identity()`
- `process_is_running()`

依赖：

- Python标准库；
- Windows `kernel32` API；
- POSIX `/proc`。

禁止依赖：

- Worker记录；
- Job目录；
- SQLite；
- GUI；
- 停止IPC。

探测语义保持三态：

- `RUNNING`：进程存在且身份完整可读；
- `NOT_RUNNING`：明确证明进程不存在或已经结束；
- `UNKNOWN`：权限、瞬时错误或身份读取不完整。

调用方只能在 `NOT_RUNNING` 时替换旧Worker。`UNKNOWN`必须失败关闭。

### `worker_stop_ipc.py`

实例停止文件协议的唯一所有者。

负责：

- `WorkerIdentityError`
- `stop_request_path()`
- `StopRequestMonitor`
- Worker自身终止进程树的内部实现；
- `.request`、`.ack`、`.error`文件处理；
- 实例令牌格式校验。

依赖：

- `atomic_write_text()`；
- Python标准库；
- 当前进程ID和平台终止能力。

禁止依赖：

- Worker记录模型；
- 数据库；
- GUI；
- 进程探测模块；
- 生命周期锁。

IPC协议保持：

```text
_work/runtime/stop-<instance_id>.request
_work/runtime/stop-<instance_id>.ack
_work/runtime/stop-<instance_id>.error
```

外部调用方只创建实例专属请求。只有Worker自身可以终止自己的进程树。

### `background_worker.py`

Worker生命周期编排的唯一所有者。

保留：

- `WorkerRecord`
- `WorkerStartResult`
- `SleepInhibitor`
- Worker记录读写；
- `_identity_matches()`；
- `WorkerLauncher`
- `run_worker()`
- `_commit_terminal_record()`
- `stop_worker()`
- `worker_status()`

通过导入 `process_identity.py` 和 `worker_stop_ipc.py` 组合能力。

## 公共符号兼容

现有测试和内部代码从 `aicf.background_worker` 导入进程和IPC符号。重构后 `background_worker.py` 必须通过普通导入重新导出这些符号：

```python
from .process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_is_running,
)
from .worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
)
```

该兼容层不包含包装函数或第二份实现，不形成双重所有权。

新增模块测试直接从新模块导入，验证其正式边界。已有Worker集成测试可以继续从 `background_worker` 导入，确保兼容性没有破坏。

## 数据流

### 启动

1. `WorkerLauncher`获取生命周期锁。
2. 调用 `process_identity.probe_process_identity()`验证旧记录。
3. 创建新Worker并预登记身份。
4. Worker通过 `run_worker()`验证Launcher记录。
5. `background_worker`负责记录状态，进程模块不接触Job数据。

### 停止

1. `stop_worker()`获取生命周期锁。
2. 使用进程模块验证记录身份。
3. 调用 `worker_stop_ipc.stop_request_path()`写入实例停止请求。
4. `StopRequestMonitor`只监听当前实例请求。
5. Worker自行终止自己的进程树。

### 完成

1. `run_worker()`在Monitor仍存活时获取生命周期锁。
2. 若停止请求已经存在，则停止胜出。
3. 否则提交正常终态。
4. IPC模块不决定数据库或Worker终态。

## 错误处理

- 进程探测返回 `UNKNOWN`：Worker编排器拒绝启动或停止。
- 停止令牌格式无效：IPC模块抛出 `WorkerIdentityError`。
- Worker自终止失败：IPC模块保留请求并写 `.error`。
- 模块导入或循环依赖：测试和 `compileall`必须阻断合入。
- 兼容导出缺失：现有Worker测试必须失败。

## 测试设计

### 新增 `test_process_identity.py`

覆盖：

- 无效PID返回 `NOT_RUNNING`；
- 当前进程返回 `RUNNING`和完整身份；
- `get_process_identity()`只在 `RUNNING`时返回身份；
- `process_is_running()`使用三态探测结果；
- Windows/POSIX分支通过依赖注入或平台条件测试。

### 新增 `test_worker_stop_ipc.py`

覆盖：

- 合法与非法实例令牌；
- Monitor忽略其他实例请求；
- Monitor处理启动前已存在的请求；
- 自终止失败保留请求并写 `.error`；
- 成功处理写 `.ack`；
- 退出Monitor不会误删请求。

### 保留 `test_background_worker.py`

继续覆盖：

- 并发启动唯一性；
- legacy记录失败关闭；
- Launcher令牌和进程身份；
- 启动、停止、完成的生命周期锁；
- 停止/完成线性化；
- Worker自然完成；
- 电源请求释放。

## 验证矩阵

必须全部通过：

1. 新模块专项测试；
2. `test_background_worker.py`；
3. GUI和CLI相关回归；
4. 完整 `pytest`；
5. `compileall`；
6. `git diff --check`；
7. 真实Windows并发 `worker-start`；
8. 真实Windows协作停止；
9. 独立代码复审；
10. 搜索确认旧实现不再留在 `background_worker.py`。

## 复杂度目标

- `background_worker.py`显著低于当前约670行；
- 新模块各自保持单一职责；
- 不新增分支、回退或协议格式；
- 不保留重复的Windows API或停止请求实现；
- GUI不新增系统级进程逻辑。

## 非目标

本次不处理：

- 迁移事务；
- 完成态与用户交付发布顺序；
- 休眠恢复；
- 删除任务数据库记录；
- GUI任务语义和布局；
- Worker协议格式升级；
- 数据库或用户输出清理。

## 删除与兼容声明

- 删除类别：内部代码退役。
- 旧所有者：`background_worker.py` 内的进程探测和停止IPC实现。
- 新所有者：`process_identity.py`、`worker_stop_ipc.py`。
- 保留行为：所有现有进程身份、停止请求、安全失败和锁语义。
- 退役行为：旧文件中的重复定义。
- 外部边界：不改变。
- 源数据风险：无。
- 用户确认要求：无。

## 验收标准

满足以下条件时完成：

- 两个新模块成为唯一实现；
- `background_worker.py`只编排，不再包含平台进程API和停止Monitor实现；
- 现有导入保持兼容；
- 所有自动化和真实Windows冒烟通过；
- 独立复审无合入阻塞；
- Git提交不包含运行数据、媒体、日志或个人信息。
