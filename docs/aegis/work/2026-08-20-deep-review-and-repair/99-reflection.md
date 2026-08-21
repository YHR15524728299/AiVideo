# 最终复盘

## 结果

任务生命周期修复达到计划定义的 12 项验收标准。最终回归执行 566 项测试且全部
通过，覆盖率为 81.70%；编译、CLI 帮助冒烟和 `git diff --check` 均返回退出码
0。当前分支是 `fix/job-lifecycle-consistency`，实现、测试与文档仍未提交。

## 架构判断

初始问题来自所有权重叠，而非单个条件分支。修复后，Repository、RuntimeLease、
Coordinator、JobService 和 ViewModel 分别拥有业务状态、项目运行权、破坏性
生命周期操作、恢复授权和 GUI 投影。GUI 与 CLI 只保留适配责任，无法通过复制
规则绕过 Owner。

SQLite 继续作为业务状态权威源，`status.json` 降为 Repository 管理的派生快照。
项目生命周期锁把进程检查、状态 CAS、租约和删除清理放在同一协调边界。状态或
进程身份不可确认时关闭危险动作，比猜测“任务未运行”更符合停止与恢复安全要求。

## 兼容取舍

现有 CLI 命令、Job 目录、中文 Job ID、后台 Worker 和交付格式均保留。兼容命令
改为薄适配器，`content-run` 改为受保护的 Worker 内部入口。旧失败记录缺少
`FailureKind` 时要求人工确认，旧死租约也必须先恢复终态；这会减少自动恢复，
但避免用不完整历史数据授权危险操作。

## 计划偏差

实现没有改变视频业务阶段或 Provider。计划中的 `Runtime View` 最终落为
`JobViewModelBuilder` / `JobViewModelPoller`，并补充了
`JobLifecycleCoordinator` 作为停止、强制中断和删除的应用层 Owner。该边界比
把协调逻辑放回 GUI 更窄，也已记录在 ADR 0001。

模块 E 未修改生产代码。静态检查确认没有私有 `atexit` 调用，也没有 GUI 直接
写业务快照。生命周期代码中的少量 `except ...: pass` 位于 best-effort 清理或
取消通知边界：主错误仍被重新抛出或保存，未把失败伪装为成功。

## 剩余边界

自动化证据不能证明外部 Provider 在当前机器真实可用。OpenRouter 仍需要凭据、
网络和实时免费证明；Dreamina 需要可用 CLI 与凭据；EdgeTTS 依赖网络服务，
Windows SAPI 依赖本机组件；FFmpeg/ffprobe 的真实验收还需要实际媒体输入。
M7 仍只生成本地发布包。

## 后续

维护者可审阅并提交当前未提交分支。真实 Provider 端到端验收应在依赖齐备的环境
单独执行，不应以 Fake、注入 transport 或命令合同测试替代。
