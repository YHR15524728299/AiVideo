# 模块A/B/C 生命周期、Repository与恢复授权实施证据

## 范围

- 处理 Worker 终态提交、项目租约、Repository 快照一致性、服务层唯一恢复授权和对应测试。
- 已处理 `src/aicf/job_actions.py`，按钮权限直接映射服务层动作授权。
- 未创建 Git 提交。

## 已实现

1. 项目租约保存对应 Job 目录，并兼容没有该字段的旧租约记录。
2. `RuntimeLease.acquire()` 不再直接覆盖死进程遗留租约；未完成恢复时 fail-closed。
3. Worker 启动前检查遗留租约：
   - 仅在完整进程身份确认原 Worker 已死或 PID 已复用后进入恢复。
   - 使用租约中的 `job_id`、`instance_id` 和 Job 目录提交 `FAILED`。
   - 重新读取 `worker.json`，确认相同实例已有 `finished_at` 后才按实例释放租约。
   - 锁失败、原子写失败、实例不一致或落盘不可确认时保留租约并阻止新 Worker 启动。
4. Worker 正常退出、业务失败、停止和心跳失败均以 `worker.json` 的相同实例
   `finished_at` 为租约释放屏障；终态提交失败时保留租约并以非零结果退出。
5. 租约获取失败路径也仅在 FAILED 终态确认落盘后尝试清理同实例租约。

### 模块B：Repository 单一状态写入

1. SQLite 事务提交任何新版本时先持久化 `snapshot_dirty=True`。
2. `status.json` 仅写入 `snapshot_dirty=False` 的目标版本；写入成功后再以
   `job_id + version` CAS 清除 SQLite 脏标记。
3. 旧版本快照写入者若观察到更高数据库版本，不会清除更高版本的脏标记。
4. `rebuild_snapshot()` 同样先持久化脏标记，再强制写权威版本并按版本清脏。
5. Repository 初始化会扫描脏、缺失、损坏或版本不一致的快照，以 SQLite
   为权威源自动重建；修复失败时保留脏标记供后续重试。

### 模块C：服务层唯一恢复授权

1. `ResumeDecision` 同时携带 `ResearchResumeStrategy` 和
   `ResumeAction` 权限；GUI按钮、CLI入口和Worker边界均消费同一服务层决策。
2. 新增持久化 `FailureKind`，同时进入SQLite权威状态和 `status.json`
   快照；旧状态按 `UNKNOWN` fail-closed 兼容。
3. `AUTO_REOPEN` 只允许 `TRANSIENT_EXTERNAL` 且失败阶段命中外部调用白名单；
   未分类失败和仅凭错误文本命中的失败一律要求人工确认。
4. 自动重开的Repository原因固定为 `external_service_retry`。
5. `worker-start` 和 `worker-run` 均调用 `JobService.authorize_worker()`；
   `worker-run` 构成二次授权守卫，拒绝未由当前状态授予的研究策略。
6. GUI生成的命令经CLI、Worker回调到真实 `M2ContentRunner` 的策略传递测试已覆盖。
7. `INTERNAL_KNOWLEDGE` 与 `RETRY_SOURCES` 均执行真实M2恢复调用，分别验证
   source discovery调用次数以及 `research_attempt.json`、`research.json` 产物。
8. Provider/运行层异常在写入失败状态时显式分类；恢复服务不再解析错误文本授权。

## 自动化证据

### 聚焦回归

命令：

```text
.\.venv\Scripts\python.exe -m pytest tests\test_background_worker.py tests\test_job_runtime.py -q
```

结果：`48 passed`，退出码 `0`。

覆盖的新增闭环：

- 生命周期锁失败：终态未写入、租约保留、Worker 非零退出。
- `worker.json` 原子写失败：终态未写入、租约保留、Worker 非零退出。
- 死进程恢复成功：按租约实例提交 `FAILED`，确认 `finished_at` 后清租约。
- 死租约覆盖保护：未恢复前拒绝新实例覆盖。
- 真实 `RuntimeLeaseHeartbeat` 线程写失败：成功结果不得覆盖失败，
  最终提交 `FAILED`，随后释放租约并返回非零。

### 全量测试与覆盖率

命令：

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`513 passed`，退出码 `0`；覆盖率 `81.22%`，达到 `80%` 门槛。

模块B新增故障与并发证据：

- 数据库新版本已提交、快照写入前模拟崩溃：SQLite 保持
  `snapshot_dirty=True`，重启后自动恢复到同版本快照并清脏。
- 旧版本写入者完成快照后遇到并发更高版本提交：版本 CAS 拒绝清除更高版本
  的 `snapshot_dirty=True`。
- Repository 重启时发现缺失或旧版本 `status.json`：自动以 SQLite 权威状态
  重建，版本、阶段和脏标记一致。

模块C新增授权与策略证据：

- SQLite与快照持久化相同的 `FailureKind`。
- `TRANSIENT_EXTERNAL + 白名单阶段`允许自动重开；未知分类、错误文本和非白名单
  阶段均拒绝自动重开。
- GUI命令经CLI与Worker二次守卫后把服务层授权策略传入M2。
- 未授权 `RETRY_SOURCES` 不能通过直接调用 `worker-run` 绕过服务层。
- 两种研究恢复策略均生成带对应原因的研究尝试及研究结果产物。

### 编译检查

命令：

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
```

结果：退出码 `0`。

### CLI冒烟

命令：

```text
.\.venv\Scripts\python.exe -m aicf --help
```

结果：退出码 `0`，命令入口正常列出。

### Diff 检查

`git diff --check` 退出码 `0`。Git 仅报告工作区 LF/CRLF 转换提醒，没有空白错误。

## 兼容命令适配器收口（2026-08-20）

### 实施结果

1. `autopilot`、`resume`、`retry` 和 `worker-start` 统一通过
   `JobService.resume_job()` 授权，并由 `WorkerLauncher` 启动后台 Worker；
   兼容命令不再在 CLI 进程中直接运行 Autopilot。
2. `retry --stage` 必须与 SQLite 中的 `FAILED_RETRYABLE.failed_stage`
   严格一致；不一致时拒绝启动，且版本、当前状态、失败阶段和快照均不变。
3. 匹配的 retry 在 Worker 真正执行前不调用 `start_stage()`，启动租约守卫会在
   生命周期锁内再次核对同一失败阶段，避免检查与启动之间的状态旁路。
4. 删除 Autopilot `_reset_failed_stage()` 及其直接
   `reopen_failed_attention()`/静默吞异常路径；内部自动重试直接由 Repository
   的“仅可启动原失败阶段”合同推进。
5. Repository 自动重开 reason 收紧为
   `credentials_restored`、`external_service_restored`、
   `external_service_retry`、`dependency_restored`；拒绝
   `auto_retry`、`transient_error`、`user_requested_retry` 和任意未知值。
6. 自动重开或 Worker 启动异常会返回非零退出码和可见、已脱敏的
   `START_REJECTED`/`FAILED_NEEDS_ATTENTION` JSON，不再吞掉异常或假报启动。

### 新增/更新测试证据

- 兼容命令旁路：断言 `autopilot`、`resume` 不直接构造或运行 Autopilot，
  而是进入 `JobService + WorkerLauncher`。
- 假运行防护：父 CLI 中若调用 `build_autopilot()`，测试立即失败。
- retry 严格阶段：覆盖阶段不匹配拒绝且状态不变、阶段匹配但启动前状态仍为
  `FAILED_RETRYABLE`。
- 租约边界：统一适配器仍把项目根目录和二次 `launch_guard` 交给
  `WorkerLauncher`；全量回归包含 Worker 生命周期与 RuntimeLease 的
  41 + 11 项测试。
- 非法 reason：4 组非法值均抛出 `TransitionError`，数据库版本和
  `status.json` 字节保持不变。
- 异常可见：模拟自动重开快照写失败，确认不构造 Worker 且 CLI 返回可见错误。
- Autopilot 内部重试：模拟渲染器首次失败、第二次成功，确认没有任何 Repository
  reopen 调用。

### 最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`524 passed`，退出码 `0`；总覆盖率 `81.29%`。

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
git diff --check
```

结果：三项退出码均为 `0`；CLI 正常列出全部兼容命令；diff 检查仅有既有
LF/CRLF 转换提醒，无空白错误。未创建 Git 提交。

## M2 `content-run` 内部入口收口（2026-08-20）

### 实施结果

1. `content-run` 仅接受 `WorkerLauncher` 启动上下文；缺少
   `AICF_WORKER_LAUNCHED=1` 或 instance ID 时返回 `START_REJECTED`，不构造
   M2 Runner。
2. 入口重新读取对应 Job 的 `worker.json`，要求 job ID、instance ID、PID、
   进程创建时间和可执行文件与当前进程完整匹配；记录缺失、损坏、实例已结束或
   身份不一致均 fail-closed。
3. `WorkerLauncher` 将授权的研究策略写入 `worker.json`。`content-run` 的显式
   策略必须与该记录一致，并再次调用 `JobService.authorize_worker()`；服务层
   决策的研究策略与启动记录不一致时拒绝执行。
4. M2 阶段失败不再生成或持久化 `content-run` 恢复命令：
   - 可重试失败生成精确阶段的 `retry --stage`；
   - 其他失败生成 `resume`。
5. `content-run` 保留为受保护的内部执行边界，不作为外部恢复入口；用户可见
   恢复合同只暴露 `resume` 或 `retry`。

### 合同测试

- 外部直跑：无启动令牌和 instance ID 时拒绝，且验证 M2 Runner 未构造。
- 进程身份：当前进程与 `worker.json` 创建时间不一致时拒绝。
- 二次授权与策略：请求策略与 Worker 启动记录不一致时拒绝；完整身份和策略
  通过时，真实调用 `JobService` 决策并把授权策略传给 M2 Runner。
- 恢复命令：分别注入可重试与不可重试 M2 失败，断言仅生成精确 `retry` 或
  `resume`，且命令中不含 `content-run`。

### 最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`530 passed`，退出码 `0`；总覆盖率 `81.34%`，达到 `80%` 门槛。

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
git diff --check
```

结果：三项退出码均为 `0`；CLI 命令表正常，diff 检查仅有既有 LF/CRLF
转换提醒，无空白错误。`git status --short` 确认工作区仍为未提交状态，未创建
Git 提交。

## 模块D：后台ViewModel与异常可见性（2026-08-20）

### 实施结果

1. GUI 新增串行后台命令队列。新建、恢复、研究重试、停止、强制清理、删除、
   打开目录/视频、历史日志加载、默认配置读取和首次运行检查中的 SQLite、
   状态文件、日志、文件及进程 IO 均由后台执行；UI 回调只采集控件值、提交
   命令并从 `ui_queue` 异步接收结果。
2. Repository、Worker、锁、快照、日志、进程、视频、研究文件和恢复规划继续
   由 `JobViewModelBuilder` 聚合为不可变 generation 快照；UI 仅渲染快照并
   忽略旧 generation。
3. 新增 `JobViewModelPoller`。单轮顶层异常会发布新 generation 的
   `UNKNOWN`、关闭危险动作并限流记录结构化异常；下一轮继续构造新 Builder，
   不因前一轮异常终止轮询。
4. 全局健康度仍反映全部历史任务，但按钮权限只按当前选中任务的健康度
   fail-closed。未选中的历史任务为 `DEGRADED` 时，不再无故关闭健康选中任务
   的视频或恢复动作。
5. 保留原有中文文案、列表选择、阶段颜色、日志分段/颜色/滚动及命令启动体验；
   历史日志改为后台读取后一次性异步回填。

### 新增故障与非阻塞测试

- 慢 Repository/文件操作提交命令后 UI 调用立即返回，实际操作由后台执行。
- 顶层轮询首次异常发布 generation 1 `UNKNOWN`，下一轮 generation 2 恢复
  `HEALTHY`。
- Worker记录、锁、日志、最终视频、研究文件、恢复 planner 和快照版本异常
  均验证对应 `UNKNOWN/DEGRADED`，开始、恢复、停止、视频或研究动作
  fail-closed。
- 双任务场景验证未选中的旧任务快照损坏只使全局状态 `DEGRADED`，健康选中
  任务仍保持既有可用动作。
- 生命周期 GUI 测试通过命令队列适配器继续验证停止和强制清理由协调器/
  Repository 持有业务状态写权限，GUI 不写 `status.json`。

### 最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`548 passed`，退出码 `0`；总覆盖率 `81.54%`，达到 `80%` 门槛。

```text
.\.venv\Scripts\python.exe -m pytest -q --no-cov
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
git diff --check
```

结果：四项退出码均为 `0`；全量无覆盖率运行通过，编译与 CLI 冒烟正常，
diff 检查仅有既有 LF/CRLF 转换提醒，无空白错误。`git status --short`
确认所有修改仍在工作区，未创建 Git 提交。

## 模块D推荐方案收口：mainloop、消息协议与日志健康（2026-08-20）

### 实施结果

1. `AicfGUI.run()` 通过 `root.after(0, ...)` 安排后台服务，先进入真实 Tk
   `mainloop`，再启动环境、运行时密钥、GUI偏好和 provider 探测；`launch()`
   不再在创建窗口前同步加载环境或密钥。
2. `ui_queue` 统一只接受冻结的 `UiMessage(generation, kind, payload)`；
   generation 在锁内分配并与入队形成同一临界区，UI忽略旧 generation。
   旧二元组/三元组分支已删除，遗留元组进入队列会立即报错。
3. 删除 `_refresh_job_list` 兼容适配器，删除与强制清理完成后直接设置
   `_force_refresh_event`，由后台发布下一代 ViewModel。
4. 增量 `worker.log` 的 `stat/open/read` 异常不再只记录后吞掉：
   异常被追加为 `HealthIssue(source="log")`，本轮 ViewModel 和受影响 Job
   降级为 `DEGRADED`，开始、恢复、停止、视频等动作统一 fail-closed。

### 新增测试

- 冻结 `UiMessage`、generation 单调递增、旧 generation 忽略和旧三元组拒绝。
- 增量日志读取异常进入 ViewModel 健康问题并关闭恢复动作。
- 真实 `Tk + AicfGUI + mainloop` 冒烟：后台启动 IO 被故意阻塞时，50ms UI
  心跳仍执行，窗口在250ms自动关闭，证明慢 IO 未阻塞主循环。

### 最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`556 passed`，退出码 `0`；总覆盖率 `81.65%`，达到 `80%` 门槛。

```text
.\.venv\Scripts\python.exe -m pytest tests\test_gui_settings.py::test_real_tk_aicfgui_mainloop_stays_responsive_during_slow_startup_io -q
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

结果：真实 GUI 冒烟 `1 passed`；编译与 diff 检查退出码均为 `0`。diff
检查仅报告既有 LF/CRLF 转换提醒，无空白错误。`git status --short --branch`
确认当前仍为 `fix/job-lifecycle-consistency` 未提交工作区，本轮未创建 Git 提交。

## 生命周期单一Owner与兼容薄适配器收口（2026-08-20）

### 实施结果

1. `JobLifecycleCoordinator.delete_job()` 成为删除用例Owner：在项目生命周期锁内
   核对任务、Worker、进程身份、活跃锁和全局租约；状态不可确认或Worker仍运行时
   fail-closed，不删除数据库或文件。
2. 删除提交后统一清理同实例租约、Job工作目录和用户交付目录；数据库已提交但
   文件或租约清理失败时返回 `COMMITTED_NEEDS_REPAIR` 及逐项错误，避免假报彻底
   删除。
3. GUI删除入口不再直接调用Repository或执行目录删除，只保留确认、异步提交和
   结果展示。旧停止与强制清理入口同样只委托Coordinator执行生命周期变更。
4. 新增 `normalized_snapshot_semantics()`，Repository启动修复和ViewModel健康
   检查共享同一份生命周期语义摘要；即使版本号相同，阶段、失败阶段、失败原因、
   重试及用量等语义漂移也会被识别。
5. 锁探测异常，以及活跃运行锁缺少可确认活动Worker记录，统一标记为
   `UNKNOWN` 并关闭危险动作；Worker读取异常保持相同fail-closed语义。

### 新增测试证据

- 删除闭环：覆盖数据库、工作目录、交付目录和同实例租约全部清理。
- 删除故障：进程探测异常时保留全部状态；数据库提交后的目录清理失败产生可修复
  结果。
- 同版本漂移：Repository重启自动修复伪造的同版本阶段/失败原因，ViewModel在
  修复前识别语义不一致并关闭动作。
- 锁与Worker异常：覆盖锁探测异常、活跃锁但Worker记录缺失、活跃锁同时Worker
  读取异常，均发布 `UNKNOWN`。
- GUI适配器：断言删除只调用 `JobLifecycleCoordinator.delete_job()`，不直接
  调用Repository；停止/强制清理薄委托测试继续通过。

### 最终验证

```text
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
```

结果：`565 passed`，退出码 `0`；总覆盖率 `81.69%`，达到 `80%` 门槛。

```text
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
git status --short --branch
```

结果：编译与diff检查退出码均为 `0`；diff检查仅报告既有LF/CRLF转换提醒，
无空白错误。分支仍为 `fix/job-lifecycle-consistency`，所有变更保持未提交。

## 模块E：计划核对与最终回归（2026-08-20）

### Owner 决策

新增 `docs/adr/0001-job-lifecycle-ownership.md`，确认：

- `JobRepository` 拥有 Pipeline 业务状态和派生快照写入；
- `RuntimeLease` 拥有项目级单 Worker 运行权；
- `JobLifecycleCoordinator` 拥有停止、强制中断和删除用例；
- `JobService` 拥有 GUI、CLI、Worker 共用的恢复授权；
- `JobViewModelBuilder` / `JobViewModelPoller` 拥有 GUI 不可变状态投影。

ADR 同时记录保留的 CLI、目录、中文 Job ID 和后台 Worker 兼容边界，以及 GUI
直接写快照、入口各自推导恢复、身份不匹配仍终止、私有 atexit 和 UI 同步状态
I/O 的退休路径。

### 12 项验收核对

| # | 结果 | 证据 |
|---:|---|---|
| 1 | 通过 | `pytest -q --no-cov` 退出码 0；收集并执行 566 项测试，全部通过。 |
| 2 | 通过 | `pytest -q --cov=aicf --cov-report=term-missing` 退出码 0；总覆盖率 81.70%。 |
| 3 | 通过 | `python -m compileall -q src tests` 退出码 0。 |
| 4 | 通过 | `git diff --check` 退出码 0；仅有 Git 的 LF/CRLF 转换提醒，无空白错误。 |
| 5 | 通过 | `test_force_interrupt_rejects_pid_reuse_without_termination` 覆盖 PID 复用且不调用终止器。 |
| 6 | 通过 | `test_force_interrupt_termination_failure_changes_no_owner` 覆盖终止失败时 Repository、Worker 记录和租约均不提交停止终态。 |
| 7 | 通过 | `test_gui_force_stop_delegates_business_state_to_repository`、`test_gui_force_clean_delegates_business_state_to_repository` 和 `test_gui_delete_is_thin_lifecycle_coordinator_adapter` 证明 GUI 只委托 Owner；源码搜索仅发现后台日志读取 `status.json`，未发现 GUI 写业务快照。 |
| 8 | 通过 | `test_mark_interrupted_commits_matching_database_and_snapshot`、`test_force_interrupt_keeps_three_owners_consistent` 与同版本语义漂移测试覆盖版本、阶段和失败原因一致。 |
| 9 | 通过 | `test_sqlite_decision_maps_to_same_gui_action_contract` 证明 GUI 动作映射与 CLI 共用同一 `JobService` 决策。 |
| 10 | 通过 | `test_runtime_lease_allows_only_one_job` 及并发启动测试证明不同 Job 仅一个获得项目租约。 |
| 11 | 通过 | `test_database_failure_is_unknown_and_fail_closed` 及 Repository、快照、Worker、锁和进程异常参数化测试证明未知状态关闭开始/恢复等危险动作。 |
| 12 | 通过 | `test_current_job_actions_only_reads_cached_view_model`、慢 I/O 心跳测试和真实 Tk mainloop 冒烟证明 UI 线程只消费缓存 ViewModel，Repository、文件和进程 I/O 在后台执行。 |

### 静态检查与异常可见性

- 搜索 `atexit` 和 `_run_exitfuncs`：`src/aicf` 无匹配。
- 搜索 GUI 的 `status.json`、Repository 变更和文件写入：生命周期入口只通过
  Coordinator/Repository 后台命令；`_request_job_logs()` 中存在后台只读日志聚合。
- 搜索 `except ...: pass`：生命周期范围仍有启动失败后的 best-effort 子进程清理、
  停止请求补写，以及租约心跳取消回调保护。前两处继续抛出原始
  `WorkerIdentityError`，心跳路径先保存 `RuntimeLeaseError` 再请求取消，因此
  主失败事实没有被静默改写为成功。本模块未改生产代码。

### 最终命令结果

```text
.\.venv\Scripts\python.exe -m pytest -q --no-cov
.\.venv\Scripts\python.exe -m pytest -q --cov=aicf --cov-report=term-missing
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m aicf --help
git diff --check
git status --short --branch
```

结果：`566 passed`；覆盖率 `81.70%`；测试、编译、CLI 冒烟和 diff 检查退出码
均为 0。分支为 `fix/job-lifecycle-consistency`，生命周期生产代码、测试与文档
仍在未提交工作区，本轮未创建提交。

### 外部 Provider 边界

- OpenRouter 真实调用仍需要凭据、网络和实时 `/models` 免费证明。
- Dreamina 真实素材生成仍需要可用 CLI 与凭据。
- EdgeTTS 真实合成依赖网络和服务；Windows SAPI 降级依赖本机语音组件。
- FFmpeg/ffprobe 真实渲染与媒体验收依赖本机安装和实际输入产物。
- 自动化测试使用 Fake、注入 transport 或命令合同，不把上述外部可用性记为通过。
