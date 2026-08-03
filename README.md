# AI Content Factory

方向驱动 AI 自动成片系统的 M0-M5 增量实现。当前包含环境检查、项目基础工程、配置加载、Pydantic 数据合同、SQLite Job 状态、可恢复状态机、UTF-8 脱敏日志、文件缓存、OpenRouter 结构化调用、内容编排、TTS、字幕，以及 FFmpeg 竖屏集成渲染与 ffprobe 交付验收。

## 当前范围

- 已完成 M0：Git/Python/FFmpeg/ffprobe/即梦/OpenRouter/TTS 检测与 `doctor`。
- 已完成 M1：配置、合同、SQLite、状态机、日志、缓存、恢复与测试。
- 已完成 M2：OpenRouter HTTP JSON Schema 调用、确定性缓存、指数退避重试、Token usage 聚合，以及 direction/research/script/review/package 内容引擎。
- 已完成 TTS Provider 基础能力：优先调用 EdgeTTS，失败时自动回退 Windows SAPI，并记录实际 Provider 与降级原因。
- 已完成真实旁白批量合成、时间线、SRT/ASS 字幕、双遍 loudnorm 音量标准化。
- 已完成 1080x1920、30fps、H.264/yuv420p、AAC 48kHz 双声道的 FFmpeg 集成渲染，并由 ffprobe 自动断言交付参数与时长。
- 已完成从 direction 到 `ready_to_publish` 的内容编排；审核不通过时停在 `needs_revision`，不会生成发布包。
- M0-M6 已具备代码与自动化测试；真实作业 `M2REAL001` 当前等待可用的 Dreamina CLI/凭据后继续。
- M7 当前只生成本地发布包，不会调用任何平台上传或发布 API。
- `run_autopilot.ps1` 仍只执行基础就绪检查，不会伪装成完整成片流程。

## 安装

在 PowerShell 中运行：

```powershell
Set-Location "<项目目录>\ai_content_factory"
.\scripts\bootstrap.ps1
```

当前机器的默认 Python 缺少内置 `venv` 模块，`bootstrap.ps1` 会安全回退到 `virtualenv`，不会修改其他工程。

## 配置

主要输入是 `config\content_direction.yaml`，只有 `direction` 必填。其余字段缺失时使用代码默认值。

复制 `.env.example` 为 `.env` 仅用于非敏感配置参考。GUI 保存的 OpenRouter
API Key 使用 Windows DPAPI 加密，并存放在当前 Windows 用户的应用数据目录，
不会写入项目源码、`.env` 或 YAML 配置。也可以通过进程环境变量临时提供：

```powershell
$env:OPENROUTER_API_KEY = "..."
$env:JIMENG_CLI_EXECUTABLE = "真实即梦CLI命令"
```

系统不会打印完整 API Key、Token 或 Cookie。`OpenRouterClient` 在每次聊天调用（包括读取本地缓存前）先实时请求 `https://openrouter.ai/api/v1/models`：只有目录中存在所选模型、`pricing` 至少包含 `prompt` 与 `completion`，且目录返回的全部价格字段均可解析为 0 时，才允许 M2 继续。目录请求失败、响应格式无效、模型缺失、价格字段不完整或任一价格非零都会失败关闭并阻断 M2；不会以 `:free` 后缀、旧缓存或离线目录作为免费证明。

模型目录请求使用独立的 `model_catalog_transport`，测试可注入该 transport，不访问真实 OpenRouter。免费证明通过后，聊天请求调用 `https://openrouter.ai/api/v1/chat/completions`，通过 `response_format.type=json_schema` 请求结构化结果；仅对聊天请求的 408/409/429、5xx、网络错误和超时重试。缓存键包含阶段、输入、模型与 prompt 版本，缓存命中不重复计算 usage，但仍必须重新通过实时免费证明。

### TTS Provider 与自动降级

`TtsService` 严格按 `edge_tts -> windows_sapi` 顺序尝试：

1. EdgeTTS 使用 `zh-CN-XiaoxiaoNeural` 生成语音，再由 FFmpeg 转为 WAV。
2. EdgeTTS 因依赖、网络、服务或转码失败时，自动回退到 Windows SAPI 的 `Microsoft Huihui Desktop`。
3. 成功后在音频旁生成 `<音频名>.tts.json`，记录 `provider`、`degraded` 和 `degradation_reason`。未降级时原因是 `null`。
4. 两个 Provider 都失败时删除不完整输出并汇总全部失败原因。

真实冒烟测试：

```powershell
.\.venv\Scripts\python.exe -m aicf tts-smoke --output outputs\tts_smoke.wav
```

命令输出实际 Provider；发生回退时同时输出降级原因。`doctor` 会报告 EdgeTTS、Windows SAPI 及当前选择策略。

## M2 内容产物

`ContentOrchestrator` 接受注入的结构化客户端，因此生产环境可用 `OpenRouterClient`，测试使用 Fake 客户端且不访问网络。成功流程依次执行 direction、research、script、review、package，并在 Job 输出目录生成：

- `direction.json`、`topic.json`、`research.json`
- `script.json`、`script.md`、`review.json`
- `package.json`、`publish.json`
- `usage.json`、`manifest.json`

`publish.json.status` 为 `ready_to_publish`。审核失败时仅保留审核前产物、`usage.json` 和 `manifest.json`，状态为 `needs_revision`。

## 已验证命令

```powershell
.\scripts\doctor.ps1
.\scripts\test.ps1
.\.venv\Scripts\python.exe -m aicf tts-smoke --output outputs\tts_smoke.wav
.\.venv\Scripts\python.exe -m aicf batch-synthesize --script outputs\M2E2E001\script.json --output-dir outputs\M2E2E001\audio
.\.venv\Scripts\python.exe -m aicf render --audio outputs\M2E2E001\audio\voiceover.wav --subtitles outputs\M2E2E001\audio\subtitles.ass --output outputs\M2E2E001\final\integration_sample.mp4 --duration 10.166 --title "AI视频不稳定，先别怪模型"
.\.venv\Scripts\python.exe -m aicf init-job --job JOB001
.\.venv\Scripts\python.exe -m aicf status --job JOB001
.\.venv\Scripts\python.exe -m aicf resume --job JOB001
.\.venv\Scripts\python.exe -m aicf retry --job JOB001 --stage DIRECTION_LOADED
```

直接运行模块时，工作目录应为项目根目录。也可设置 `AICF_PROJECT_ROOT` 指定数据库和输出根目录。

## Job 与恢复

Job 状态同时写入：

- `data\content.db`
- `outputs\<job_id>\status.json`

每个阶段记录开始/完成时间、调用次数、重试次数、错误、日志路径、可恢复性和恢复命令。成功阶段保留在 `completed_stages`，失败不会清除已完成结果。

所有临时文件/目录到正式路径的原子替换统一由 `aicf.atomic_io.atomic_replace` 执行。仅 Windows 的共享冲突 `WinError 5/32` 使用指数短重试，总等待不超过 1 秒；其他系统或错误立即原样抛出。

## 测试

当前可收集 293 项自动化测试。最近一次全量执行结果为 292 passed、1 skipped。
测试遵循测试先行的红-绿流程，覆盖：

- 唯一必填方向与默认值
- 时长边界校验
- Pydantic 数据合同与分数边界
- 合法/非法状态转换
- SQLite 与 `status.json` 双写
- 可恢复失败
- 缓存命中与输入变更失效
- 环境检查与敏感信息隐藏
- EdgeTTS 优先、SAPI 自动回退、实际 Provider 与降级原因记录
- EdgeTTS/SAPI Provider 命令执行与 TTS Smoke CLI
- CLI 初始化、状态和恢复
- 中文路径与 UTF-8 脱敏日志
- OpenRouter `/models` 实时免费证明、失败关闭、可注入目录 transport
- OpenRouter JSON Schema 请求、缓存、重试、解析与 usage
- 五个 M2 内容引擎的 Pydantic 合同校验
- 注入 Fake 客户端的 direction-to-publish 集成流程
- 审核失败阻断 package/publish
- FFmpeg 渲染命令合同、渲染清单、ffprobe JSON 解析与竖屏交付断言
- Windows `os.replace` 的 WinError 5/32 故障注入、指数短重试、1 秒上限与非目标错误立即抛出

```powershell
uv run --extra dev pytest
uv run --extra dev pytest --cov=aicf --cov-report=term-missing
```

当前整体覆盖率为 60.87%，尚未达到工程配置的 80% 质量门槛。主要缺口集中在
Tkinter GUI 和设置窗口，发布前仍需补充窗口生命周期、异步检测、登录流程和
配置保存的自动化测试。

## 已知环境结果

- Python 3.10.11：可用；自带 `venv` 缺失，已使用 `virtualenv`。
- Git 2.53.0：可用。
- FFmpeg/ffprobe 6.1.1：可用。
- EdgeTTS：由 `doctor` 运行时检测，真实调用需要网络。
- Windows SAPI：作为 EdgeTTS 失败时的自动回退，默认中文语音为 `Microsoft Huihui Desktop`。
- Dreamina CLI：`config/jimeng_cli.yaml` 使用 `dreamina` 命令名，由 `JIMENG_CLI_EXECUTABLE` 或 PATH 在运行环境解析，不包含用户名绝对路径。
- OpenRouter：自动测试通过注入 transport，不进行网络调用；真实 M2 调用需设置 `OPENROUTER_API_KEY`，并能实时访问 `/models` 证明所选模型全部价格为 0。
- 全量测试已连续执行两次，均为 234 passed；真实 TTS/Dreamina Smoke 仍以当前外部环境执行结果为准。
