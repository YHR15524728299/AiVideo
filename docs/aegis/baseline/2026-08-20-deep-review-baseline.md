# 2026-08-20 深度Review初始基线

## 范围

整个 `ai_content_factory`：生产代码、测试、配置、启动脚本、SQLite/快照持久化、Worker 生命周期、GUI 状态和当前未提交改动。

## 已确认事实

- 全量测试当前结果：`2 failed, 418 passed`。
- 失败项：
  - `test_stop_request_wins_before_terminal_commit`
  - `test_research_failure_exposes_dedicated_retry_and_plain_summary`
- 当前未提交改动涉及 `state_machine.py`、`job_actions.py`、`gui.py`、`background_worker.py`、`worker_stop_ipc.py` 等核心生命周期文件。
- SQLite 与 `status.json` 同时保存 Job 状态；Repository 已具备事务、版本和快照同步能力。
- GUI 强制清理仍存在直接写 `status.json`、随后静默尝试更新数据库的路径。
- `force_kill_worker()` 的强制路径存在身份不匹配时误杀 PID 复用进程的风险。
- GUI 的恢复按钮合同与 CLI/Repository 的实际恢复入口不完全一致。
- 运行状态未知时部分路径会按“没有运行任务”处理，不满足 fail-closed。
- GUI 仍有状态派生路径在 UI 线程读取数据库、文件和进程状态。

## 初始健康评分

| 维度 | 分数 | 主要问题 |
|---|---:|---|
| 代码质量 | 70 | 重复强制恢复逻辑、长函数、静默异常 |
| 架构 | 35 | 状态所有权分裂、缺少全局运行租约 |
| 技术债 | 40 | GUI直接写快照、Worker与Job终态脱节 |
| 测试质量 | 55 | 核心GUI安全路径缺测试，现有合同漂移 |
| 综合 | 48 | 当前不可作为完成基线 |

## 最高优先级风险

1. 强制清理可能误杀 PID 已复用的无关进程。
2. GUI 绕过 Repository 导致数据库与快照分裂。
3. 恢复按钮可点但实际入口可能不能重开失败状态。
4. 不同 Job/GUI/CLI 缺少全局单Worker原子约束。
5. Worker终态没有可靠提交到Job业务状态。

## 兼容边界

- 保留现有 CLI 命令和 Job 目录结构。
- 保留中文 Job ID。
- 保留关闭 GUI 后 Worker 继续运行的能力。
- 保留 SQLite 为权威状态、`status.json` 为可恢复快照的对外行为。
- 不改变视频生成业务流程和外部 Provider 选择。
