# AI Content Factory 最新交接文档

更新时间：2026-07-20

## 1. 当前结论

- M0-M6 的代码与自动化测试均在当前工作区。
- 全量回归已连续执行两次：234 项（227+）测试均全部通过。
- 总覆盖率：87.21%，高于 `pyproject.toml` 中 80% 的门槛。
- Python 源码与测试均通过 `compileall`。
- CLI 帮助与状态查询冒烟验证通过。
- 当前仍依赖真实外部环境的能力：OpenRouter 凭据、可用的即梦 CLI、EdgeTTS 网络服务，以及本机 FFmpeg/ffprobe。
- Git 仓库当前分支尚无提交，项目文件仍显示为未跟踪；本轮未擅自创建提交。
- 真实作业 `M2REAL001` 当前等待 Dreamina CLI/凭据；M7 仅生成本地发布包，不执行平台上传或发布。

## 2. 本轮已完成修复

### 2.1 Windows 原子替换共享冲突

新增共享 `aicf.atomic_io.atomic_replace`，并将 M2 promotion、M4 JSON/缓存、
database snapshot、artifact commit 及其他原子写调用统一迁移。仅 Windows
`WinError 5/32` 采用 10ms 起步的指数短重试，总等待不超过 1 秒；非 Windows
或任何其他错误立即原样抛出。故障注入测试覆盖成功重试、错误筛选和时间上限。

### 2.2 OpenRouter 免费模型实时证明与 M2 失败关闭

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

### 2.3 正确暴露失败阶段的恢复命令

`JobStatus.next_resume_command` 现在优先读取失败阶段中持久化的
`next_resume_command`，不再始终退回通用的 `aicf resume` 命令。

涉及文件：

- `src/aicf/database.py`
- `tests/test_m0_m1.py`

### 2.4 子进程失败可恢复并持久化

`Autopilot` 现在捕获 `subprocess.CalledProcessError`，提取 stderr/stdout
作为失败原因，将阶段写为 `FAILED_NEEDS_ATTENTION`，并返回可再次执行的
自动驾驶恢复命令。FFmpeg 等外部程序失败时不再留下未收口的 QA 状态。

涉及文件：

- `src/aicf/autopilot.py`
- `tests/test_m6_delivery.py`

## 3. TDD 证据

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

## 4. 回归验证

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=aicf --cov-report=term-missing
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
$env:AICF_PROJECT_ROOT = (Get-Location).Path
.\.venv\Scripts\python.exe -m aicf status
git diff --check
```

结果：

- 连续两次均为 `234 passed`
- 既有覆盖率基线 `87.21%`；本轮验收重点为双次全量回归
- `compileall` 退出码 0
- CLI 帮助退出码 0
- 状态查询退出码 0
- `git diff --check` 退出码 0

## 5. 运行与恢复

```powershell
.\scripts\doctor.ps1
.\run_autopilot.ps1 -Job "<JOB_ID>"
.\.venv\Scripts\python.exe -m aicf status --job "<JOB_ID>"
```

若外部能力或输入产物缺失，系统应进入 `FAILED_NEEDS_ATTENTION`，并在
`outputs/<JOB_ID>/status.json` 的失败阶段记录中给出实际恢复命令。

## 6. 外部环境边界

- 自动化测试通过注入模型目录与聊天 transport，不调用真实 OpenRouter 或即梦服务。
- 真实 M2 在每次 OpenRouter 聊天调用前必须能访问 `/models` 并证明所选模型全部价格为 0；无法实时证明即阻断。
- `M2REAL001` 等待 Dreamina 外部能力就绪后恢复，不将环境阻塞记为成功。
- M7 的发布边界仅为本地发布包；没有平台上传、账号操作或真实发布。
- 当前机器未配置的外部能力不应被伪装为成功。
- 真实端到端发布验收需先通过 `doctor`，并准备对应 Job 的脚本、音频、
  字幕、主成片、clean 成片和发布文案产物。
