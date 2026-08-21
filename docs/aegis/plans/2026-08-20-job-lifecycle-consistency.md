# 任务生命周期与GUI状态一致性修复计划

## Goal

让任务停止、崩溃、恢复、重启和并发启动拥有单一状态所有者、明确合同和自动化证据，杜绝误杀进程、状态双写漂移、按钮误导和UI线程阻塞。

## Architecture

- `JobRepository` 是Pipeline业务状态和快照同步的唯一写入Owner。
- Worker运行事实由独立的运行时模型表达，不再依靠PipelineStage猜测进程状态。
- GUI和CLI共享应用层恢复用例，不直接拼接恢复策略。
- GUI后台线程生成不可变ViewModel，UI线程只渲染。
- 项目级租约保证同一时间仅一个Worker获得运行权。

## Tech Stack

Python 3.11、SQLite、Pydantic、tkinter、pytest、Windows进程身份与文件锁。

## Baseline/Authority Refs

- `docs/aegis/baseline/2026-08-20-deep-review-baseline.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- `README.md`
- 当前全量测试：`2 failed, 418 passed`

## Compatibility Boundary

- 保留现有CLI命令、Job目录结构、中文Job ID和后台Worker能力。
- 不改变视频业务阶段、Provider和交付格式。
- SQLite继续作为权威源；`status.json`仅由Repository生成。
- 不允许通过兼容代码保留GUI直接写快照的旧路径。

## Architecture Integrity Lens

- Invariant：一个Job状态只有Repository可写；一个项目只允许一个活跃Worker租约。
- Canonical owner：Repository负责业务状态；Runtime View负责运行事实；Resume Use Case负责恢复。
- Responsibility overlap：当前GUI、CLI、Worker各自推导恢复和终态，必须收口。
- Higher-level simplification：以组合运行视图替代散落的字符串集合和布尔判断。
- Retirement：删除GUI直接写快照、重复强制恢复分支和调用私有`atexit._run_exitfuncs()`路径。
- Verdict：先修Owner和合同，再调整GUI展示。

## Plan Pressure Test

- Owner / contract / retirement：已明确，旧双写路径必须删除。
- Architecture integrity：Repository和应用层用例是更高层正确Owner。
- Verification scope：单元、故障注入、并发、全量回归和CLI冒烟。
- Task executability：每个模块可独立红绿验证。
- Pressure result：proceed。

## Plan-Time Complexity Check

- Target files：`gui.py`超过3000行，混合IO、状态派生和控件更新。
- Add-in-place risk：继续向`gui.py`添加分支会扩大认知和竞态风险。
- Better boundary：新增`job_runtime.py`、`job_service.py`、`job_view_model.py`，GUI仅适配。
- Recommendation：新增Owner文件并分切片实施，不在GUI内部继续堆叠逻辑。

## 验收标准

1. `pytest -q`零失败。
2. `pytest --cov=aicf --cov-report=term-missing`覆盖率不低于80%。
3. `compileall -q src tests`退出码0。
4. `git diff --check`退出码0。
5. PID身份不匹配时绝不调用`taskkill`。
6. `taskkill`失败时不得写入已停止终态。
7. GUI不得直接修改业务状态快照。
8. SQLite和快照的版本、阶段、失败原因保持一致。
9. GUI和CLI对同一失败状态产生相同恢复决策。
10. 不同Job并发启动时仅一个获得全局租约。
11. 状态读取失败时fail-closed，开始/恢复按钮不被错误启用。
12. UI线程不读取SQLite、状态文件、日志或进程身份。

## 模块A：Worker终止安全与全局租约

**Files**

- 修改：`src/aicf/background_worker.py`
- 修改：`src/aicf/worker_stop_ipc.py`
- 新增：`src/aicf/job_runtime.py`
- 修改：`tests/test_background_worker.py`
- 修改：`tests/test_worker_stop_ipc.py`
- 新增：`tests/test_job_runtime.py`

**Why**

避免PID复用误杀、终止失败误报成功和多Job并发抢占资源。

**Repair Track**

- 强制停止也必须校验完整进程身份。
- 终止命令返回非零或二次探测仍存活时，不提交`finished_at`。
- 使用项目级租约记录Job ID、instance ID、进程身份和心跳。
- Worker退出按instance ID条件释放租约。

**Retirement Track**

- 删除对`atexit._run_exitfuncs()`私有API的依赖。
- 身份不匹配时只封存旧记录，不杀进程。

**Steps**

1. 写PID复用、taskkill失败和不同Job并发抢占的失败测试。
2. 运行相关测试确认RED。
3. 实现`RuntimeLease`和安全终止协议。
4. 运行模块测试确认GREEN。
5. 运行全量测试并记录证据。

## 模块B：Repository单一状态写入

**Files**

- 修改：`src/aicf/database.py`
- 修改：`src/aicf/gui.py`
- 修改：`tests/test_m0_m1.py`
- 新增：`tests/test_job_interruption.py`

**Why**

消除数据库与`status.json`各说各话，保证强制清理是原子、可恢复、可审计的业务操作。

**Repair Track**

- 新增`mark_interrupted(job_id, expected_stage, reason, worker_instance_id)`。
- 在SQLite事务内验证前置条件、记录失败阶段并递增版本。
- 事务成功后统一同步快照；失败不得提前修改快照。
- GUI只调用Repository方法，不写`status.json`。

**Retirement Track**

- 删除`_stop_job()`和`_force_clean_job()`中的重复JSON写入。
- 删除不存在的`PipelineStage.UNKNOWN`回退。

**Steps**

1. 写Repository失败时快照不变、成功时版本一致的测试。
2. 运行测试确认RED。
3. 实现原子中断方法并迁移GUI调用。
4. 运行模块测试确认GREEN。
5. 搜索确认GUI无业务状态快照写入。

## 模块C：恢复用例与按钮合同

**Files**

- 新增：`src/aicf/job_service.py`
- 修改：`src/aicf/job_actions.py`
- 修改：`src/aicf/cli.py`
- 修改：`src/aicf/gui.py`
- 修改：`tests/test_job_actions.py`
- 新增：`tests/test_job_service.py`

**Why**

保证按钮显示、CLI行为和Repository允许的状态转换完全一致。

**Repair Track**

- 定义`ResumeMode`：断点继续、重试失败阶段、自动重开、需人工确认。
- `derive_job_actions()`返回动作和模式，不直接编码启动命令。
- GUI和CLI都调用`JobService.resume_job()`。
- 研究失败是否允许普通恢复以产品合同为准，测试和实现必须一致。

**Retirement Track**

- 删除GUI“任何失败都直接worker-start”的路径。
- 删除CLI与GUI各自维护的自动重开规则。

**Steps**

1. 写状态组合到按钮和数据库结果的参数化测试。
2. 运行测试确认RED，并明确现有2个失败测试的正确合同。
3. 实现`JobService`和`ResumeMode`。
4. 迁移GUI/CLI并运行相关测试确认GREEN。
5. 运行全量测试确认无合同漂移。

## 模块D：后台ViewModel与异常可见性

**Files**

- 新增：`src/aicf/job_view_model.py`
- 修改：`src/aicf/gui.py`
- 修改：`src/aicf/logging_utils.py`
- 新增：`tests/test_job_view_model.py`
- 修改：`tests/test_gui_settings.py`

**Why**

保证GUI秒开和可响应；状态未知时安全禁用操作，而不是伪装成空闲。

**Repair Track**

- 后台线程聚合Repository、Worker、锁、快照和日志信息。
- 生成带generation的不可变`JobViewModel`。
- UI线程只根据ViewModel设置控件。
- 数据库、文件或进程探测失败形成`UNKNOWN/DEGRADED`并限流记录日志。
- UI忽略旧generation消息。

**Retirement Track**

- 删除`_current_job_actions()`中的同步IO。
- 删除关键状态路径的`except Exception: pass`。

**Steps**

1. 写状态未知fail-closed、旧generation忽略、慢IO不阻塞UI的测试。
2. 运行测试确认RED。
3. 实现ViewModel聚合与后台消息协议。
4. 迁移按钮和列表更新并确认GREEN。
5. 运行GUI冒烟并记录人工检查结果。

## 模块E：计划核对与回归修正

**Files**

- 更新：`README.md`
- 更新：`docs/HANDOFF_LATEST.md`
- 更新：`docs/aegis/work/2026-08-20-deep-review-and-repair/20-checkpoint.md`
- 更新：`docs/aegis/work/2026-08-20-deep-review-and-repair/90-evidence.md`
- 更新：`docs/aegis/work/2026-08-20-deep-review-and-repair/99-reflection.md`

**Steps**

1. 对照12条验收标准逐条标记证据。
2. 运行相关测试、全量测试、覆盖率、编译和diff检查。
3. 对失败或偏差回到对应模块修正，不以更新测试掩盖错误合同。
4. 检查无GUI直接状态写入、无私有atexit调用、无未记录静默异常。
5. 汇总剩余外部环境风险和用户可见变化。

## Risks

- 全局租约属于新增持久化/并发边界，必须兼容旧Worker记录。
- GUI拆分可能影响启动与选择任务体验，需保持现有控件行为。
- 停止语义必须区分“已请求”“已确认退出”和“身份不明”。
- 测试更新只能反映明确合同，不能为了转绿修改期望。

## Rollback

每个模块保持独立提交边界；若某模块验证失败，可回退该模块而不恢复已退休的危险双写或误杀路径。
