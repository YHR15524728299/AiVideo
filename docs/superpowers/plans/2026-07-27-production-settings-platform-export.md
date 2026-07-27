# 生产设置与按需平台导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将产品改为单条连续旁白驱动全片、即梦仅生成无文字动态画面，并由用户按平台勾选需要的最终视频，同时可微调即梦模型与分辨率。

**Architecture:** 新增任务级 `ProductionSettings` 作为唯一生产配置，创建任务时固化到 `production_settings.json`，恢复任务始终读取该文件。母版只渲染一次，平台导出器根据勾选模板复制或转码母版并生成对应发布清单；即梦模型、分辨率和动态模式从任务设置传入素材阶段。旁白仍在 M3 一次性生成，增加可插拔 ASR 验收接口，禁止逐镜头生成语音。

**Tech Stack:** Python 3.11、Pydantic、tkinter、FFmpeg、Dreamina CLI、pytest

---

### Task 1: 任务级生产设置

**Files:**
- Create: `src/aicf/production_settings.py`
- Test: `tests/test_production_settings.py`

- [ ] 定义平台模板、即梦模型、分辨率、动态模式和旁白音色的强类型配置。
- [ ] 验证至少选择一个平台，并验证模型与分辨率组合。
- [ ] 实现 `save_for_job()` 与 `load_for_job()`，旧任务缺少文件时使用兼容默认值。
- [ ] 运行 `uv run pytest tests/test_production_settings.py -q`，预期全部通过。

### Task 2: 按需平台导出

**Files:**
- Create: `src/aicf/platform_export.py`
- Modify: `src/aicf/engines/m6_engine.py`
- Test: `tests/test_platform_export.py`
- Test: `tests/test_m6_delivery.py`

- [ ] 为抖音、小红书、TikTok、YouTube Shorts 定义文件名、尺寸、帧率和码率模板。
- [ ] 仅导出 `selected_platforms` 中的平台，禁止遍历全部支持平台。
- [ ] 同规格平台复用母版内容，必要时使用 FFmpeg 按模板转码。
- [ ] 发布清单仅列出实际导出的文件和平台文案。
- [ ] 运行平台导出与 M6 测试，预期全部通过。

### Task 3: 即梦生产参数透传

**Files:**
- Modify: `src/aicf/m4_asset_runner.py`
- Modify: `src/aicf/providers/jimeng.py`
- Modify: `src/aicf/cli.py`
- Test: `tests/test_m4_asset_runner.py`
- Test: `tests/test_dreamina_protocol.py`

- [ ] 从 `production_settings.json` 读取即梦模型、视频分辨率和动态模式。
- [ ] 图片与视频命令显式携带兼容的分辨率参数。
- [ ] `economy`、`balanced`、`full_motion` 映射到明确动态覆盖率，不再把视频失败永久改写成全图片计划。
- [ ] 运行 Dreamina 与 M4 测试，预期全部通过。

### Task 4: 连续旁白与可懂度验收

**Files:**
- Create: `src/aicf/voice_validation.py`
- Modify: `src/aicf/autopilot.py`
- Modify: `src/aicf/providers/tts.py`
- Test: `tests/test_voice_validation.py`
- Test: `tests/test_autopilot_full_chain.py`

- [ ] 保持整条脚本一次性合成为 `voiceover.wav`，视觉镜头不得各自生成旁白。
- [ ] 定义旁白验收结果，检查语种、关键数字和关键短语。
- [ ] ASR 不可用时记录明确警告但不伪造通过；ASR 可用且关键内容缺失时停止进入视觉生成。
- [ ] 运行旁白与全链测试，预期全部通过。

### Task 5: GUI 生产与导出设置

**Files:**
- Modify: `src/aicf/gui.py`
- Test: `tests/test_gui_settings.py`

- [ ] 在任务设置区加入四个平台复选框，默认仅勾选抖音。
- [ ] 加入即梦模型、分辨率、动态模式和旁白音色选择框。
- [ ] 点击开始时写入任务级 `production_settings.json`，再启动 autopilot。
- [ ] 历史任务恢复时读取固化设置，不受当前界面值影响。
- [ ] 增加“打开最终视频”按钮，优先打开已勾选平台的第一个成片。
- [ ] 运行 GUI 设置测试，预期全部通过。

### Task 6: 全量验证

**Files:**
- Modify: `README.md`

- [ ] 更新新管线、平台勾选、模型与分辨率说明。
- [ ] 运行 `uv run pytest -q`，预期零失败。
- [ ] 运行 `uv run python -m aicf doctor`，确认关键环境检查结果可读。
- [ ] 启动 `uv run python -m aicf ui`，确认窗口能打开并显示新增设置。
