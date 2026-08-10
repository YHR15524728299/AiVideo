# 内容产物路径与提供商路由修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让内容验收始终读取 `outputs/<job_id>`，并且素材阶段只初始化任务明确选择的视频提供商。

**Architecture:** Autopilot 分离内部任务目录与内容产物目录，内容哈希只从显式 `content_output_root` 读取。素材适配器改为按需工厂：进入素材阶段后读取任务冻结的 `ProductionSettings.video_provider`，仅构建 `jimeng` 或 `kling` 中被选择的一项，不做静默降级。

**Tech Stack:** Python 3.11、pytest、SQLite、Pydantic

---

### Task 1: 分离内容产物目录

**Files:**
- Modify: `src/aicf/autopilot.py`
- Modify: `src/aicf/cli.py`
- Test: `tests/test_autopilot_full_chain.py`

- [ ] 新增失败用例：数据库任务目录为 `data/jobs/<id>`，M2 产物位于 `outputs/<id>`，内容验收必须成功。
- [ ] 为 Autopilot 增加显式 `content_output_root`，`_ensure_content` 的脚本与内容包哈希只读取该目录。
- [ ] 在 `build_autopilot` 中传入项目 `outputs` 根目录。
- [ ] 运行全链测试确认通过。

### Task 2: 只加载选中提供商

**Files:**
- Modify: `src/aicf/cli.py`
- Modify: `src/aicf/autopilot.py`
- Test: `tests/test_cli_and_logging.py`
- Test: `tests/test_autopilot_full_chain.py`

- [ ] 新增失败用例：选择 `jimeng` 时不得调用 `build_kling_adapter`。
- [ ] `build_m4_asset_runner(provider)` 只构建指定适配器，不允许静默切换。
- [ ] Autopilot 进入 `KEYFRAMES_GENERATED` 时读取冻结设置并按需创建素材 Runner。
- [ ] 独立 `asset-run` 命令也从视觉计划目录读取任务设置后选择提供商。
- [ ] 运行 CLI 与全链测试确认通过。

### Task 3: 回归与恢复

**Files:**
- Runtime state: `data/jobs/260806`
- Runtime artifacts: `outputs/260806`

- [ ] 运行全量测试和 80% 覆盖率门禁。
- [ ] 合并到 `main`，确认未跟踪 `config/` 不进入提交。
- [ ] 备份 `260806` 当前状态。
- [ ] 重开错误的 `CONTENT_PACKAGED` 失败，复用已审核脚本与内容包。
- [ ] 确认进入旁白/分镜后不出现可灵探测，素材阶段选择即梦。
- [ ] 推送 `main` 并清理临时分支。
