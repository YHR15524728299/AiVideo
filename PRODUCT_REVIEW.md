# AI Content Factory 产品验收与代码健康报告

**评审角色：** 产品经理 / 发布验收  
**评审范围：** 全部源码、配置、测试、启动入口、环境诊断、GUI关键流程、敏感信息与工程清洁度  
**评审日期：** 2026-07-30  
**发布结论：** 有条件不通过

项目核心流水线、外部服务检测和自动化测试当前可正常运行，但 GUI 异步线程安全和
自动化覆盖率尚未达到发布门槛。建议先完成报告中的两个发布阻断项，再标记为稳定版。

## 验收摘要

| 维度 | 结果 | 说明 |
|---|---|---|
| 自动化功能测试 | 通过 | 293 项收集，292 passed，1 skipped |
| 覆盖率门槛 | 未通过 | 总覆盖率 60.87%，工程门槛为 80% |
| 环境诊断 | 通过 | Python、FFmpeg、OpenRouter、即梦、可灵、TTS 均就绪 |
| GUI启动 | 通过 | 实测约 1.69 秒 |
| 密钥安全 | 已整改 | 明文密钥已移出工程，改用 Windows DPAPI 加密存储 |
| 工程清洁 | 已整改 | 清理缓存并补充忽略规则；未发现历史输出和调试脚本残留 |
| 发布稳定性 | 有风险 | 设置窗口仍存在后台线程直接调用 Tkinter 的竞态风险 |

## 功能模块

| 模块 | 主要职责 | 当前状态 |
|---|---|---|
| GUI与设置 | 新建、恢复、停止任务，服务登录与配置 | 可用，异步线程需加固 |
| 内容流水线 | 方向、选题、研究、脚本、审核、打包 | 自动化测试通过 |
| 素材生成 | 即梦、可灵图片和视频生成 | 环境检测通过 |
| 音频字幕 | Kokoro、EdgeTTS、SAPI、字幕与时间线 | 自动化测试通过 |
| 视频渲染 | FFmpeg渲染、质量检测、自动修复 | 自动化测试通过 |
| 交付打包 | 抖音、小红书、YouTube Shorts、TikTok、YouTube | 自动化测试通过 |
| 状态恢复 | SQLite、状态机、锁、断点续跑 | 自动化测试通过 |
| 安全与诊断 | 脱敏、密钥存储、doctor环境检查 | 已整改并通过烟测 |

## 本次修复

### 费用安全边界

**Symptom：** OpenRouter 免费模型验证存在进程级缓存，目录验证失败时可能使用旧结果继续请求。  
**Source：** Fail Closed / Single Source of Truth。  
**Consequence：** 模型价格或账户状态变化后仍可能继续调用，破坏费用保护。  
**Remedy：** 已移除共享验证缓存，恢复每次调用实时验证；目录不可用、模型缺失或价格非零时立即阻断。相关测试 20 项通过。

### 内容配置保护

**Symptom：** GUI 开始任务时会重写 `config/content_direction.yaml`，只保留方向字段。  
**Source：** Shotgun Surgery / Data Ownership。  
**Consequence：** 用户设置的视频时长、预算、风格和平台可能被静默删除。  
**Remedy：** 已改为仅更新 `direction`，保留所有其他 YAML 字段，并采用原子写入。

### 密钥移出工程

**Symptom：** 项目 `.env` 中曾保存一枚真实 OpenRouter API Key。  
**Source：** Secrets Management / Least Exposure。  
**Consequence：** 项目被复制、压缩或同步时可能泄露账号资源。  
**Remedy：** 已将密钥迁移到当前 Windows 用户的 DPAPI 加密凭据文件，并从项目 `.env` 删除；GUI 保存密钥时不再写入工程文件。

> 由于该密钥曾以明文落盘，仍建议在 OpenRouter 后台轮换一次密钥。

### 日志脱敏

**Symptom：** GUI 内存日志未统一经过脱敏函数。  
**Source：** Defense in Depth。  
**Consequence：** 外部 CLI 异常可能把 Token、Cookie 或用户路径显示在界面并被复制。  
**Remedy：** GUI `_log()` 已统一调用 `sanitize_error()`，并增加回归测试。

### M2与M6稳定性

已修复平台集合、失败恢复、TTS诊断、交付平台文件集合、探针参数兼容和黑帧阻断。
相关 M2/诊断测试 143 passed、1 skipped，M6 测试 16 passed。

## 发布阻断

### GUI后台线程直接访问Tkinter

**级别：** 高  
**Symptom：** `src/aicf/settings_dialog.py` 中多个工作线程直接调用 `self.after()`，
位置包括第 350–364、438–450、476–483、661–693、839–842 行。  
**Source：** Dependency Disorder / Concurrency Boundary。  
**Consequence：** 快速关闭窗口或重复检测时，可能出现 `main thread is not in main loop`、
`invalid command name`、状态被旧结果覆盖或窗口无响应。  
**Remedy：** 工作线程只写入 `queue.Queue`，所有控件读取和更新统一由主线程轮询；
每轮检测增加 generation ID，丢弃迟到结果。

### GUI自动化覆盖不足

**级别：** 高  
**Symptom：** 总覆盖率 60.87%，`gui.py` 约 10%，`settings_dialog.py` 约 13%，
低于 `pyproject.toml` 设置的 80% 门槛。  
**Source：** Test Pyramid / Humble Object。  
**Consequence：** 登录、窗口关闭、重复点击、配置保存和后台检测的回归无法被自动发现。  
**Remedy：** 拆出可测试的纯逻辑层，并增加窗口生命周期、登录、配置迁移、任务冲突和
异步检测测试；覆盖率达到 80% 后再发布稳定版。

## 其他风险

### 自动重试范围过宽

`src/aicf/autopilot.py` 第 315–324 行仍将广义 `OSError` 视为可重试，可能把文件缺失、
权限或磁盘错误当成网络波动，导致用户额外等待 10/20/40 秒。应仅重试明确的网络超时、
HTTP 429/5xx 和已知外部服务暂态错误。

### 新建与恢复任务语义不清

`src/aicf/gui.py` 第 1809–1837 行没有在“开始生成”前阻止复用已存在的 Job ID。
用户可能以为创建新任务，实际恢复旧任务并继续使用旧配置。建议“开始生成”遇到已有
Job ID 时阻止，并要求使用“继续/恢复”入口。

### 免费模型判断存在重复规则

GUI 模型选择器在 `src/aicf/gui.py` 第 325–330 行自行判断价格，运行时则在
`src/aicf/providers/openrouter.py` 第 260–326 行执行更严格验证。应抽取共享函数，
避免界面允许选择、运行时却拒绝。

## 工程清洁

已完成：

- 删除项目源码和测试目录中的 `__pycache__` 与 pytest 缓存。
- 补充 `.mypy_cache`、`.ruff_cache`、临时文件和补丁残留忽略规则。
- `.env` 不再含真实密钥。
- `config/jimeng_cli.yaml` 不再包含 Windows 用户绝对路径。
- 未发现日志、历史视频输出、调试脚本、备份文件、数据库残留或重复源码。
- `.venv` 为运行依赖，已保留，不属于垃圾文件。

## 健康评分

**Mode：** Health Dashboard  
**Scope：** 整个 `ai_content_factory` 工程  
**Composite Score：** 85/100

| 维度 | 分数 | 主要问题 |
|---|---:|---|
| Code Quality | 90 | 重试异常分类和模型验证规则仍有重复 |
| Architecture | 85 | GUI线程边界不清，窗口代码体积较大 |
| Tech Debt | 88 | Job新建/恢复语义和异常吞噬需收敛 |
| Test Quality | 75 | 覆盖率未达门槛，GUI测试明显不足 |

## 发布建议

当前版本适合继续内部试用，不建议直接标记为“稳定发布”。完成 GUI 主线程队列改造和
覆盖率门槛后，再执行一次真实端到端任务：创建任务、生成脚本、调用视频服务、合成音频、
渲染、自动质检、导出四个平台发布包。该真实任务通过后可进入稳定版候选。
