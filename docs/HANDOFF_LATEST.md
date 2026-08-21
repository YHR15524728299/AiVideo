# AI Content Factory 最新交接文档

更新时间：2026-08-20

## 1. 当前结论

- M0-M6 与任务生命周期一致性修复的代码、测试和文档均在当前工作区。
- 全量回归：`566 passed`，退出码 0。
- 总覆盖率：`81.70%`，高于 `pyproject.toml` 中 80% 的门槛。
- Python 源码与测试均通过 `compileall`。
- CLI 帮助与状态查询冒烟验证通过。
- 当前仍依赖真实外部环境的能力：OpenRouter 凭据、可用的即梦 CLI、EdgeTTS 网络服务，以及本机 FFmpeg/ffprobe。
- 当前分支为 `fix/job-lifecycle-consistency`；生命周期实现、测试和文档均为
  未提交改动，本轮未擅自创建提交。
- M7 仅生成本地发布包，不执行平台上传或发布。

## 2. 生命周期 Owner 与兼容边界

- `JobRepository`：SQLite Pipeline 状态与 `status.json` 派生快照的唯一写入
  Owner；版本或语义不一致时从 SQLite 修复快照。
- `RuntimeLease`：项目级单 Worker 运行权 Owner；租约绑定 Job、instance ID 和
  完整进程身份。
- `JobLifecycleCoordinator`：停止、强制中断和删除用例 Owner；身份未知、身份
  不匹配、终止失败或持久化失败时不会假报成功。
- `JobService`：GUI、CLI 和 Worker 共用的恢复授权 Owner；失败类型和失败阶段
  共同决定继续、精确重试、自动重开或人工确认。
- `JobViewModelBuilder` / `JobViewModelPoller`：GUI 状态投影 Owner；后台聚合
  Repository、快照、Worker、锁、日志和进程信息，UI 线程只渲染不可变快照。

保留现有 CLI 命令、Job 目录、中文 Job ID、后台 Worker、SQLite 权威状态和
最终交付格式。兼容命令已收口为 `JobService + WorkerLauncher` 薄适配器；
`content-run` 仅允许经 Worker 身份与服务层二次授权后执行。旧记录缺少
`FailureKind` 时按未知失败处理，牺牲自动恢复率以保持 fail-closed。

已退休 GUI 直接写快照、CLI 内假运行 Autopilot、GUI/CLI 各自推导恢复、身份
不匹配仍终止进程、终止失败写停止终态、私有 `atexit._run_exitfuncs()` 和 UI
线程同步状态 I/O 等路径。详见
`docs/adr/0001-job-lifecycle-ownership.md`。

## 3. 本轮已完成修复

### 3.1 Windows 原子替换共享冲突

新增共享 `aicf.atomic_io.atomic_replace`，并将 M2 promotion、M4 JSON/缓存、
database snapshot、artifact commit 及其他原子写调用统一迁移。仅 Windows
`WinError 5/32` 采用 10ms 起步的指数短重试，总等待不超过 1 秒；非 Windows
或任何其他错误立即原样抛出。故障注入测试覆盖成功重试、错误筛选和时间上限。

### 3.2 OpenRouter 免费模型实时证明与 M2 失败关闭

`OpenRouterClient` 不再把模型名的 `:free` 后缀当作充分证明。每次聊天调用
（包括读取本地缓存前）都会实时请求 OpenRouter `/models`。只有所选模型
存在、`pricing` 至少包含 `prompt` 和 `completion`，且目录提供的全部价格
字段均可解析为 0 时才允许 M2 继续。

以下情况全部失败关闭并阻断 M2，不使用缓存或离线兜底：

- `/models` 网络、HTTP、超时或解析失败；
- 目录顶层格式无效；
- 所选模型不在实时目录；
- `pricing` 缺失或关键字段不完整；
- 任一价格字段非零或无法解析。

模型目录请求使用独立的 `model_catalog_transport`，自动化测试可直接注入，
不会访问真实 OpenRouter。

涉及文件：

- `src/aicf/providers/openrouter.py`
- `tests/test_m2_openrouter.py`
- `README.md`

### 3.3 正确暴露失败阶段的恢复命令

`JobStatus.next_resume_command` 现在优先读取失败阶段中持久化的
`next_resume_command`，不再始终退回通用的 `aicf resume` 命令。

涉及文件：

- `src/aicf/database.py`
- `tests/test_m0_m1.py`

### 3.4 子进程失败可恢复并持久化

`Autopilot` 现在捕获 `subprocess.CalledProcessError`，提取 stderr/stdout
作为失败原因，将阶段写为 `FAILED_NEEDS_ATTENTION`，并返回可再次执行的
自动驾驶恢复命令。FFmpeg 等外部程序失败时不再留下未收口的 QA 状态。

涉及文件：

- `src/aicf/autopilot.py`
- `tests/test_m6_delivery.py`

## 4. TDD 证据

本轮 Windows 原子替换修复按严格 TDD 执行：先观察到 6 个故障注入测试因
`aicf.atomic_io.atomic_replace` 尚不存在而 RED，再完成最小实现、迁移调用点并转绿。
新增覆盖包括：

1. Windows `WinError 32` 冲突两次后成功，并验证 10ms、20ms 指数延迟；
2. Windows 非 5/32 错误立即抛出；
3. 非 Windows 即使带 5/32 也立即抛出；
4. 持续共享冲突时总等待不超过 1 秒。

此前保留的两个回归测试：

1. `test_failed_job_exposes_the_recorded_recovery_command`
   - 修复前：返回通用 `python -m aicf resume --job ...`。
   - 修复后：返回失败阶段实际记录的恢复命令。
2. `test_autopilot_persists_unexpected_pipeline_failure_for_recovery`
   - 修复前：`CalledProcessError` 直接逸出。
   - 修复后：返回 `FAILED_NEEDS_ATTENTION`，并同步写入 Job 状态。

## 5. 回归验证

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
$env:AICF_PROJECT_ROOT = (Get-Location).Path
.\.venv\Scripts\python.exe -m aicf status
git diff --check
```

结果：

- 全量回归 `566 passed`，退出码 0
- 覆盖率 `81.70%`，高于 80% 门槛
- `compileall` 退出码 0
- CLI 帮助退出码 0
- `git diff --check` 退出码 0
- 静态搜索未发现 `atexit` 或 `_run_exitfuncs` 调用
- `git status --short --branch` 显示分支
  `fix/job-lifecycle-consistency`，所有生命周期改动仍未提交

## 6. 运行与恢复

```powershell
.\scripts\doctor.ps1
.\run_autopilot.ps1 -Job "<JOB_ID>"
.\.venv\Scripts\python.exe -m aicf status --job "<JOB_ID>"
```

若外部能力或输入产物缺失，系统应进入 `FAILED_NEEDS_ATTENTION`，并在
`data/jobs/<JOB_ID>/status.json` 的失败阶段记录中给出实际恢复命令。

## 7. 外部 Provider 边界

- 自动化测试通过注入模型目录与聊天 transport，不调用真实 OpenRouter 或即梦服务。
- 真实 M2 在每次 OpenRouter 聊天调用前必须能访问 `/models` 并证明所选模型全部价格为 0；无法实时证明即阻断。
- EdgeTTS 真实调用依赖网络和服务可用性；Windows SAPI 只是在本机可用时提供降级。
- 渲染与交付仍依赖本机 FFmpeg/ffprobe，自动化命令合同不能替代真实媒体链路验收。
- M7 的发布边界仅为本地发布包；没有平台上传、账号操作或真实发布。
- 当前机器未配置的外部能力不应被伪装为成功。
- 真实端到端发布验收需先通过 `doctor`，并准备对应 Job 的脚本、音频、
  字幕、主成片、clean 成片和发布文案产物。
