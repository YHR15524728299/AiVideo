# ADR 0001：任务生命周期所有权

- 状态：Accepted
- 日期：2026-08-20
- 适用范围：Job 业务状态、Worker 运行事实、停止/删除/恢复用例和 GUI 状态展示

## 背景

初始审计发现任务生命周期责任分散在 Repository、Worker、CLI 和 GUI。GUI
曾直接写 `status.json`，恢复规则由多个入口分别推导，强制停止路径也可能在
进程身份不匹配时操作复用后的 PID。SQLite、快照、Worker 记录和界面按钮因此
可能表达不同事实。

## 决策

生命周期按五个 Owner 分工，适配层不得复制其规则。

| Owner | 权威范围 | 允许的写入或决策 | 不负责 |
|---|---|---|---|
| `JobRepository` | Pipeline 业务状态及其版本 | 在 SQLite 事务中变更状态；由权威状态生成 `status.json`；修复脏、缺失、损坏或语义漂移的快照 | Worker 进程存活判断、按钮渲染 |
| `RuntimeLease` | 项目级单 Worker 运行权 | 记录 Job、实例和完整进程身份；按实例获取、心跳和释放租约 | Pipeline 阶段转换、恢复授权 |
| `JobLifecycleCoordinator` | 破坏性生命周期用例 | 在项目生命周期锁内协调进程身份核验、停止确认、Repository CAS、租约释放和删除清理 | 自行写业务快照、推导恢复策略 |
| `JobService` | GUI、CLI 和 Worker 共用的恢复授权 | 根据持久化状态生成 `ResumeDecision`；授权继续、精确阶段重试或受限自动重开；在 Worker 边界二次校验 | 进程终止、文件清理、界面状态 |
| `JobViewModelBuilder` / `JobViewModelPoller` | GUI 可渲染状态 | 在后台聚合 Repository、快照、Worker、锁、日志和进程事实，生成带 generation 的不可变 ViewModel；读取失败时 fail-closed | 修改业务状态、启动或停止 Worker |

跨 Owner 的不变量如下：

1. SQLite 是 Job 业务状态权威源，`status.json` 是 Repository 派生快照。
2. 同一项目同时只有一个 RuntimeLease 持有者。
3. 终止进程前必须匹配 PID、创建时间和可执行文件；无法确认时拒绝终止。
4. 终止失败或退出未确认时，不写已停止终态。
5. GUI 线程只渲染 ViewModel 和提交后台命令，不执行 SQLite、状态文件、日志或
   进程身份 I/O。

## 兼容取舍

- 保留现有 CLI 命令、Job 目录结构、中文 Job ID、SQLite 权威状态和关闭 GUI
  后 Worker 继续运行的能力。
- `autopilot`、`resume`、`retry`、`worker-start` 继续可用，但作为
  `JobService + WorkerLauncher` 的薄适配器；它们不再在调用进程中直接运行
  Autopilot。
- `content-run` 保留为 Worker 内部执行边界，不作为用户恢复入口。
- 没有 `FailureKind` 的旧失败记录按 `UNKNOWN` 处理并要求人工确认。该选择牺牲
  自动恢复率，换取旧数据上的 fail-closed 行为。
- 旧租约缺少 Job 目录时仍可读取，但死租约不会被新 Worker 直接覆盖，必须先完成
  可确认的终态恢复。
- 不改变视频业务阶段、Provider 选择和最终交付格式。

## 退休路径

以下路径已退休，并由回归测试防止恢复：

- GUI 直接写 `status.json` 或直接提交 Pipeline 状态。
- GUI、CLI 各自维护自动重开和研究恢复规则。
- `autopilot`、`resume`、`retry` 在 CLI 进程中直接构造并运行 Autopilot。
- 用户直接调用 `content-run` 绕过 Worker 身份与服务层授权。
- 身份不匹配时仍调用 `taskkill`，或终止失败后写入已停止终态。
- 通过私有 `atexit._run_exitfuncs()` 清理运行状态。
- 只比较快照版本、不比较阶段、失败原因、重试和用量语义。
- GUI UI 线程同步读取 Repository、状态文件、日志或进程身份。

兼容适配器在外部调用方迁移完成后可按命令逐项弃用；删除前必须保留公开命令的
等价替代、迁移说明和覆盖同一授权边界的测试。Owner 本身不因适配器退休而迁回
GUI 或 CLI。

## 结果

状态写入、运行权、破坏性操作、恢复授权和界面投影各有单一 Owner。失败读取与
身份不确定统一关闭危险动作；代价是旧状态或外部环境不可确认时需要人工修复，
且 Repository、租约与 Worker 记录之间增加了显式协调步骤。

外部 Provider 仍在本决策之外：OpenRouter、Dreamina、EdgeTTS、FFmpeg/ffprobe
的可用性和凭据由运行环境决定，自动化测试只验证注入边界，不代表真实服务可用。
