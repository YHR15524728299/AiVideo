# 扁平交付与锁屏后台Worker实施计划

## Goal

让任务在Windows锁屏后继续运行并临时阻止系统睡眠；GUI关闭不终止任务；任务完成后在任务根目录只显示五个最终交付文件；上传GitHub时排除全部用户运行数据。

## Architecture

- `aicf.background_worker`拥有后台进程、电源请求、PID记录和终态退出。
- `aicf.delivery_view`拥有五文件扁平交付、白名单清理与现有任务迁移。
- CLI提供 `worker-start`、`worker-run`、`worker-status`、`finalize-delivery`。
- GUI只调用 `worker-start`并读取数据库、PID记录和日志，不持有流水线子进程。
- M6继续拥有完整技术交付目录；`delivery_view`仅生成用户可见根部视图，不改变M6验证合同。

## Tech Stack

Python 3.11、Windows `SetThreadExecutionState`、`subprocess.Popen`、SQLite Job状态、pytest、Git。

## Baseline/Authority Refs

- `docs/superpowers/specs/2026-08-03-flat-delivery-background-worker-design.md`
- `src/aicf/autopilot.py`
- `src/aicf/gui.py`
- `src/aicf/engines/m6_engine.py`
- `.gitignore`

## Compatibility Boundary

- `python -m aicf autopilot --job`行为保持不变。
- M6的 `delivery/publish_manifest.json`和完整QA仍保留在内部工作区，供恢复与重新验证。
- 现有数据库和Job ID不迁移。
- 非Windows平台电源控制为空实现，但后台Worker与扁平交付可运行。
- 用户主动注销、关机或强制睡眠不在支持范围内。

## Verification

- `pytest`覆盖电源请求释放、重复Worker、陈旧PID、后台终态、五文件交付与迁移。
- GUI相关测试验证启动命令改为 `worker-start`且关闭窗口不终止Worker。
- 当前任务迁移后根部只显示五个用户文件和内部 `_work`目录。
- 完整测试、`doctor`、`git diff --check`、密钥/路径/媒体扫描。
- 推送前显式检查提交文件列表，禁止运行目录。

## 任务一：后台Worker与电源请求

**Files**
- Create: `src/aicf/background_worker.py`
- Modify: `src/aicf/cli.py`
- Create: `tests/test_background_worker.py`

**Why**
普通GUI子进程随窗口生命周期耦合，无法保证锁屏与关闭GUI后的连续运行。

**Impact/Compatibility**
新增独立入口，不改变现有 `autopilot`调用。

**Steps**
1. 写失败测试：Windows电源请求在正常、异常路径均释放；重复PID不重启；陈旧PID可替换。
2. 运行 `pytest tests/test_background_worker.py -q`确认失败。
3. 实现 `SleepInhibitor`、运行记录、独立Popen启动与Worker终态。
4. 增加CLI子命令并运行目标测试。
5. 提交 `feat(worker): add lock-screen background execution`。

## 任务二：扁平交付与安全清理

**Files**
- Create: `src/aicf/delivery_view.py`
- Modify: `src/aicf/autopilot.py`
- Create: `tests/test_delivery_view.py`

**Why**
最终视频位于多层目录，且用户任务目录混有大量技术产物。

**Impact/Compatibility**
内部 `delivery/`保留为权威技术交付；任务根部新增五文件用户视图。完成后把内部产物迁入 `_work/`，数据库快照仍可访问。

**Steps**
1. 写失败测试：五文件命名、原子复制、白名单清理、拒绝版删除、不可再生素材保留。
2. 运行目标测试确认失败。
3. 实现 `finalize_user_delivery`和 `migrate_completed_job`。
4. 在Autopilot `COMPLETED`前调用最终化。
5. 提交 `feat(delivery): expose five-file task output`。

## 任务三：GUI解耦

**Files**
- Modify: `src/aicf/gui.py`
- Modify: `tests/test_gui_settings.py`

**Why**
GUI当前持有Popen、关闭窗口会终止任务。

**Impact/Compatibility**
普通诊断命令仍使用现有异步命令；开始/恢复任务改用Worker入口。停止按钮根据PID记录终止Worker树。

**Steps**
1. 写失败测试验证开始/恢复命令和关闭行为。
2. 运行目标测试确认失败。
3. 替换任务启动、停止、关闭逻辑。
4. 验证GUI可重新打开并识别运行任务。
5. 提交 `feat(gui): detach jobs from window lifecycle`。

## 任务四：迁移当前任务

**Files**
- Runtime only: `outputs/FED_RATE_20260731/`

**Why**
立即解决最终文件难找的问题。

**Impact/Compatibility**
删除已确认拒绝的图片拼接版和可再生预览/临时帧；真实视频素材迁到 `_work/assets/`；根部生成五个用户文件。

**Steps**
1. 运行迁移预检并打印将保留、移动和删除的路径。
2. 执行迁移。
3. 验证五个根部文件哈希与内部交付一致。
4. 验证状态仍为 `COMPLETED`。
5. 不提交任何运行文件。

## 任务五：验证与GitHub

**Files**
- Modify: `.gitignore`（仅在扫描发现缺口时）
- Modify: `README.md`

**Why**
防止用户数据、媒体和凭据进入远端。

**Steps**
1. 运行完整 `pytest`、`doctor`、后台Worker集成测试和交付结构测试。
2. 扫描已跟踪文件和待提交差异中的密钥、本机绝对路径、媒体、数据库、日志与缓存。
3. 显式暂存源码、测试和文档；检查 `git diff --staged --name-only`。
4. 推送当前 `main`到 `origin/main`；不创建PR，不上传运行目录。
5. 读取远端提交哈希确认同步。

## Repair Track

- 根因：GUI拥有任务进程生命周期；M6技术交付目录同时被当作用户交付界面。
- 修复所有者：后台生命周期归 `background_worker`；用户交付视图归 `delivery_view`。
- 验证：目标测试、完整回归、当前任务迁移与远端文件检查。

## Retirement Track

- 退役GUI关闭时终止流水线的路径。
- 退役用户需要进入 `delivery/youtube/`查找最终视频的路径。
- 保留M6内部完整交付目录作为验证合同，未来当所有恢复与QA消费者迁移到 `_work`后再评估进一步收缩。

## Risks

- Windows API调用失败时必须失败关闭，不伪称阻止睡眠。
- 迁移不能破坏数据库中已记录的manifest哈希；内部manifest保留不动。
- GitHub CLI缺失不阻塞直接 `git push`，但推送前必须确认现有origin与认证可用。
