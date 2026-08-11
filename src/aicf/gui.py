"""AI Content Factory - tkinter 桌面操作窗口。"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import yaml
from datetime import datetime
from pathlib import Path
from threading import Event
from tkinter import (
    BooleanVar,
    Label,
    Menu,
    PhotoImage,
    StringVar,
    Tk,
    Toplevel,
    ttk,
    scrolledtext,
    messagebox,
)
from tkinter import font as tkfont
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .atomic_io import atomic_write_text
from .background_worker import WorkerIdentityError, force_kill_worker, read_worker_record, stop_worker
from .config import load_config
from .database import JobRepository
from .file_lock import lock_is_active
from .job_actions import (
    JobActionState,
    derive_job_actions,
    failed_attention_can_auto_reopen,
    first_available_job_id,
    job_storage_exists,
    should_recover_zombie_job,
    summarize_research_failure,
)
from .production_settings import (
    ProductionSettings,
    MOTION_MODE_DISPLAY_NAMES,
    MOTION_MODE_VALUES,
    ORIENTATION_DISPLAY_NAMES,
    PLATFORM_DISPLAY_NAMES,
    VIDEO_PROVIDER_DISPLAY_NAMES,
    JIMENG_MODEL_DISPLAY_NAMES,
    KLING_MODEL_DISPLAY_NAMES,
    VOICE_DISPLAY_NAMES,
    VOICE_GROUP_ORDER,
)
from .providers.jimeng import detect_jimeng_cli
from .providers.kling import detect_kling_cli
from .secret_store import load_secret, migrate_secret_from_env
from .settings_dialog import open_settings, load_default_settings
from .state_machine import PipelineStage
from .logging_utils import sanitize_error
from .subprocess_utils import silent_run
from .path_utils import project_root, python_executable
from .constants import OPENROUTER_MODELS_URL

# 阶段顺序与中文名称（完整流水线）
STAGES = [
    (PipelineStage.DIRECTION_LOADED, "方向载入"),
    (PipelineStage.DIRECTION_ANALYZED, "方向分析"),
    (PipelineStage.TOPICS_GENERATED, "候选选题"),
    (PipelineStage.TOPIC_SELECTED, "选题确定"),
    (PipelineStage.RESEARCHED, "资料研究"),
    (PipelineStage.SCRIPT_GENERATED, "脚本撰写"),
    (PipelineStage.SCRIPT_REVIEWED, "脚本审核"),
    (PipelineStage.CONTENT_PACKAGED, "内容打包"),
    (PipelineStage.AUDIO_GENERATED, "旁白合成"),
    (PipelineStage.NARRATION_TIMELINE_CREATED, "旁白时间线"),
    (PipelineStage.STORYBOARD_GENERATED, "视觉分镜"),
    (PipelineStage.CLIP_PLAN_CREATED, "片段计划"),
    (PipelineStage.KEYFRAMES_GENERATED, "素材生成"),
    (PipelineStage.VIDEO_CLIPS_GENERATED, "视频片段"),
    (PipelineStage.SUBTITLES_GENERATED, "字幕生成"),
    (PipelineStage.MASTER_TIMELINE_ASSEMBLED, "主时间线"),
    (PipelineStage.RENDERED, "视频渲染"),
    (PipelineStage.QA_CHECKED, "质量检查"),
    (PipelineStage.AUTO_REPAIRED, "自动修复"),
    (PipelineStage.PACKAGED, "发布包生成"),
    (PipelineStage.COMPLETED, "完成"),
]

STAGE_INDEX = {stage: i for i, (stage, _) in enumerate(STAGES)}


def build_production_settings(
    platforms: dict[str, bool],
    *,
    video_provider: str = "jimeng",
    jimeng_model: str = "seedance2.0fast",
    kling_model: str = "kling-video-v2_6",
    video_resolution: str = "720p",
    motion_mode: str = "video",
    narration_voice: str = "kokoro:zm_yunyang",
    orientation: str = "portrait",
) -> ProductionSettings:
    return ProductionSettings(
        selected_platforms=tuple(
            platform for platform, selected in platforms.items() if selected
        ),
        video_provider=video_provider,
        jimeng_model=jimeng_model,
        kling_model=kling_model,
        video_resolution=video_resolution,
        motion_mode=motion_mode,
        narration_voice=narration_voice,
        orientation=orientation,
    )


def final_video_for_job(
    job_dir: str | Path,
    user_output_dir: str | Path | None = None,
) -> Path | None:
    if user_output_dir is not None:
        user_video = Path(user_output_dir) / "最终视频.mp4"
        if user_video.is_file():
            return user_video
    root = Path(job_dir)
    settings = ProductionSettings.load_for_job(root)
    for platform in settings.selected_platforms:
        video = root / "delivery" / platform / "video.mp4"
        if video.is_file():
            return video
    return None


def worker_start_command(job_id: str) -> list[str]:
    return [
        python_executable(),
        "-m",
        "aicf",
        "worker-start",
        "--job",
        job_id,
    ]


# ---------------------------------------------------------------------------
# .env 文件读写
# ---------------------------------------------------------------------------
def _read_env_file() -> str:
    env_path = project_root() / ".env"
    if env_path.is_file():
        return env_path.read_text(encoding="utf-8")
    return ""


def _write_env_file(content: str) -> None:
    env_path = project_root() / ".env"
    atomic_write_text(env_path, content)


def _get_env_value(key: str) -> str:
    """从进程环境、安全凭据存储或非敏感 .env 配置读取值。"""
    val = os.getenv(key, "")
    if val:
        return val
    if key == "OPENROUTER_API_KEY":
        migrate_secret_from_env(project_root() / ".env", key)
        return load_secret(key)
    env_text = _read_env_file()
    m = re.search(rf"^{key}\s*=\s*(.+)$", env_text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("\"'")
    return ""


class _LiteralString(str):
    pass


class _ConfigDumper(yaml.SafeDumper):
    pass


_ConfigDumper.add_representer(
    _LiteralString,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str",
        value,
        style="|",
    ),
)


def update_direction_config(config_path: str | Path, direction: str) -> Path:
    path = Path(config_path)
    data: dict[str, object] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("内容方向配置格式无效")
        data = loaded
    data["direction"] = _LiteralString(direction)
    atomic_write_text(
        path,
        yaml.dump(
            data,
            Dumper=_ConfigDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
    )
    return path


def _set_env_value(key: str, value: str) -> None:
    """在 .env 文件中设置键值，保留已有内容。"""
    env_text = _read_env_file()
    pattern = rf"^{key}\s*=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, env_text, re.MULTILINE):
        env_text = re.sub(pattern, new_line, env_text, flags=re.MULTILINE)
    else:
        if env_text and not env_text.endswith("\n"):
            env_text += "\n"
        env_text += new_line + "\n"
    _write_env_file(env_text)


# ---------------------------------------------------------------------------
# OpenRouter 模型选择对话框
# ---------------------------------------------------------------------------

class ModelSelectionDialog:
    """OpenRouter 免费模型选择窗口。"""

    def __init__(self, parent: Tk) -> None:
        self.parent = parent
        self.models: list[dict] = []
        self.current_model = _get_env_value("OPENROUTER_MODEL")
        self.api_key = _get_env_value("OPENROUTER_API_KEY")

        self.win = Toplevel(parent)
        self.win.title("OpenRouter 模型选择")
        self.win.geometry("780x640")
        self.win.resizable(True, True)
        self.win.transient(parent)
        self.win.grab_set()

        self._build()
        self.win.after_idle(self._bring_to_front)

    def _bring_to_front(self) -> None:
        self.win.update_idletasks()
        x = self.parent.winfo_rootx() + max(
            0, (self.parent.winfo_width() - self.win.winfo_width()) // 2
        )
        y = self.parent.winfo_rooty() + max(
            0, (self.parent.winfo_height() - self.win.winfo_height()) // 2
        )
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(100, lambda: self.win.attributes("-topmost", False))
        self.win.focus_force()

    def _build(self) -> None:
        # ---- 顶部：当前模型 ----
        top = ttk.Frame(self.win, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="当前模型:").pack(side="left")
        self.current_label = ttk.Label(
            top, text=self.current_model or "未设置", foreground="#1976d2", font=("TkDefaultFont", 9, "bold")
        )
        self.current_label.pack(side="left", padx=6)

        ttk.Label(top, text="（仅显示免费模型，价格均为 0）").pack(side="left")

        self.fetch_btn = ttk.Button(top, text="🔄 获取免费模型列表", command=self._fetch_models)
        self.fetch_btn.pack(side="right")

        # ---- 搜索 ----
        search_frame = ttk.Frame(self.win, padding=(10, 0))
        search_frame.pack(fill="x")

        ttk.Label(search_frame, text="搜索:").pack(side="left")
        self.search_var = StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=6)
        ttk.Label(search_frame, text="（按模型名/提供方/描述过滤）").pack(side="left")

        # ---- 模型列表 ----
        list_frame = ttk.Frame(self.win, padding=10)
        list_frame.pack(fill="both", expand=True)

        columns = ("model", "provider", "context", "desc")
        self.tree = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            height=18,
        )
        self.tree.heading("model", text="模型名称")
        self.tree.heading("provider", text="提供方")
        self.tree.heading("context", text="上下文长度")
        self.tree.heading("desc", text="描述")
        self.tree.column("model", width=220, anchor="w")
        self.tree.column("provider", width=100, anchor="w")
        self.tree.column("context", width=90, anchor="e")
        self.tree.column("desc", width=320, anchor="w")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.tree.bind("<Double-1>", lambda _: self._confirm())

        # ---- 加载指示器 ----
        self.loading_var = StringVar(value="")
        loading_label = ttk.Label(self.win, textvariable=self.loading_var)
        loading_label.pack(pady=(0, 4))

        # ---- 底部按钮 ----
        btn_frame = ttk.Frame(self.win, padding=10)
        btn_frame.pack(fill="x")

        self.confirm_btn = ttk.Button(btn_frame, text="✓ 确认选择", command=self._confirm)
        self.confirm_btn.pack(side="right", padx=4)
        ttk.Button(btn_frame, text="取消", command=self.win.destroy).pack(side="right", padx=4)

        # 自动获取
        self.win.after(100, self._fetch_models)

    def _fetch_models(self) -> None:
        if not self.api_key:
            messagebox.showwarning("提示", "未设置 OPENROUTER_API_KEY，请先在 .env 或环境变量中配置")
            return

        self.fetch_btn.configure(state="disabled")
        self.loading_var.set("正在获取 OpenRouter 免费模型列表...")

        def worker() -> None:
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                }
                req = Request(OPENROUTER_MODELS_URL, headers=headers, method="GET")
                with urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                data = payload.get("data", [])
                if not isinstance(data, list):
                    raise ValueError("模型列表格式异常")

                # 只保留免费模型（所有价格字段都为 0）
                free_models: list[dict] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    pricing = item.get("pricing", {})
                    if not isinstance(pricing, dict):
                        continue
                    is_free = True
                    for price_key in ("prompt", "completion", "request", "image", "video"):
                        val = pricing.get(price_key, "0")
                        try:
                            if float(val) != 0.0:
                                is_free = False
                                break
                        except (ValueError, TypeError):
                            is_free = False
                            break
                    if not is_free:
                        continue
                    # 只保留支持文本的模型
                    arch = item.get("architecture", {})
                    if isinstance(arch, dict):
                        modality = arch.get("modality", "")
                        if isinstance(modality, str) and "image" in modality and "text" not in modality:
                            continue
                    free_models.append(item)

                self.models = sorted(
                    free_models,
                    key=lambda m: str(m.get("id", "")),
                )
                self.win.after(0, self._populate)
                msg = f"获取完成，共 {len(self.models)} 个免费模型"
                self.win.after(0, lambda: self.loading_var.set(msg))
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as e:
                self.win.after(0, lambda: self.loading_var.set(f"获取失败: {e}"))
                self.win.after(0, lambda: messagebox.showerror("错误", f"获取模型列表失败:\n{str(e)[:300]}"))
            finally:
                self.win.after(0, lambda: self.fetch_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for m in self.models:
            model_id = m.get("id", "")
            provider = model_id.split("/")[0] if "/" in model_id else ""
            ctx = m.get("context_length", "")
            if isinstance(ctx, (int, float)):
                ctx = f"{int(ctx):,}"
            else:
                ctx = str(ctx)
            desc = str(m.get("description", ""))[:120]
            values = (model_id, provider, ctx, desc)
            iid = self.tree.insert("", "end", values=values)

            # 高亮当前模型
            if model_id == self.current_model:
                self.tree.selection_set(iid)
                self.tree.see(iid)

        self._apply_filter()

    def _apply_filter(self) -> None:
        keyword = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for m in self.models:
            model_id = str(m.get("id", ""))
            provider = model_id.split("/")[0] if "/" in model_id else ""
            desc = str(m.get("description", ""))
            if keyword and (
                keyword not in model_id.lower()
                and keyword not in desc.lower()
                and keyword not in provider.lower()
            ):
                continue
            ctx = m.get("context_length", "")
            if isinstance(ctx, (int, float)):
                ctx = f"{int(ctx):,}"
            else:
                ctx = str(ctx)
            values = (model_id, provider, ctx, desc[:120])
            iid = self.tree.insert("", "end", values=values)
            if model_id == self.current_model:
                self.tree.selection_set(iid)
                self.tree.see(iid)

    def _confirm(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个模型")
            return
        values = self.tree.item(sel[0], "values")
        model_id = str(values[0])
        try:
            _set_env_value("OPENROUTER_MODEL", model_id)
            os.environ["OPENROUTER_MODEL"] = model_id
            self.current_label.configure(text=model_id)
            self.current_model = model_id
            self.win.after(0, lambda: messagebox.showinfo("成功", f"已切换模型为:\n{model_id}\n\n下次启动任务时生效"))
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def get_selected_model(self) -> str:
        return self.current_model


class AicfGUI:
    """AI Content Factory 桌面操作窗口。"""

    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("AI Content Factory - 自动成片工具")
        self._app_icon = PhotoImage(file=str(project_root() / "assets" / "app-icon.png"))
        self.root.iconphoto(True, self._app_icon)
        self.root.geometry("1280x900")
        self.root.minsize(1100, 760)

        # 日志队列：后台线程 -> UI
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, object, object | None]] = queue.Queue()
        self.running = False
        self.current_process: subprocess.Popen[str] | None = None
        self._polling_job_id: str = ""  # 正在运行、需要实时跟踪日志的任务
        self._display_job_id: str = ""  # 进度条当前显示的任务（跟随用户选中项）
        self._user_selected_job: bool = False  # 用户是否手动选中了某个任务（用于判断是否自动切换显示）
        self._logged_stages: set[str] = set()  # 已记录到日志的阶段，避免重复
        self._log_file_offsets: dict[str, int] = {}  # 已读取的日志文件字节位置，用于增量读取
        self._last_refresh_ts: float = 0.0  # 上次刷新任务列表的时间戳
        self._pid_cache: dict[str, tuple[float, bool]] = {}  # PID运行状态缓存，避免重复系统调用
        self._poll_in_progress: bool = False  # 轮询防重入标志
        self._force_refresh_event: Event = Event()  # 强制刷新事件：给后台线程发信号立即刷新
        self._loading_initial_logs: bool = False  # 正在加载初始日志标志，避免重复加载
        self._last_loaded_job_id: str | None = None  # 上次加载日志的任务ID，防抖用

        # 字体
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        self.root.option_add("*Font", default_font)

        self._setup_styles()
        self._build_ui()
        # 加载默认生产设置
        try:
            default_settings = load_default_settings()
            self._apply_production_settings(default_settings)
        except Exception:
            pass
        # 注意：任务列表刷新、日志轮询、状态检测全部由后台线程 + _poll_progress统一处理
        # 不在UI线程做任何文件IO
        self._update_button_states()  # 初始化按钮状态
        # 后台异步检测视频提供商和环境状态（不阻塞UI启动）
        for lbl in self.env_labels.values():
            lbl.configure(text="检测中...", fg="#666666", font=("TkDefaultFont", 9))
        self._set_status("启动中，正在后台检测环境...")
        self._start_async_provider_detection()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("vista")
        style.configure("Stage.TLabel", padding=(6, 4), relief="flat")
        style.configure("StageActive.TLabel", padding=(6, 4), background="#1976d2", foreground="white", font=("TkDefaultFont", 9, "bold"))
        style.configure("StageDone.TLabel", padding=(6, 4), background="#2e7d32", foreground="white", font=("TkDefaultFont", 9))
        style.configure("StageFail.TLabel", padding=(6, 4), background="#c62828", foreground="white", font=("TkDefaultFont", 9))
        style.configure("Treeview", rowheight=24)

    def _build_ui(self) -> None:
        root = self.root

        # ---- 顶部：环境状态 ----
        env_frame = ttk.LabelFrame(root, text="环境状态", padding=8)
        env_frame.pack(fill="x", padx=10, pady=(10, 4))

        self.env_labels: dict[str, Label] = {}
        env_items = [
            ("openrouter", "OpenRouter"),
            ("dreamina", "即梦 CLI"),
            ("kling", "可灵 CLI"),
            ("ffmpeg", "FFmpeg"),
            ("tts", "TTS 语音"),
        ]
        for i, (key, text) in enumerate(env_items):
            tk.Label(env_frame, text=text + ":", bg="#f0f0f0").grid(row=0, column=i * 2, sticky="w", padx=(8, 4))
            lbl = tk.Label(env_frame, text="未检查", fg="#666666", bg="#f0f0f0", font=("TkDefaultFont", 9), cursor="hand2")
            lbl.grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 16))
            lbl.bind("<Button-1>", lambda e, k=key: self._open_settings_for(k))
            self.env_labels[key] = lbl

        ttk.Button(env_frame, text="检查环境", command=self._run_doctor).grid(
            row=0, column=len(env_items) * 2, sticky="e", padx=8
        )
        ttk.Button(env_frame, text="⚙️ 设置", command=self._open_settings).grid(
            row=0, column=len(env_items) * 2 + 1, sticky="e", padx=(0, 8)
        )

        # 当前模型显示 + 选择按钮
        current_model = _get_env_value("OPENROUTER_MODEL") or "未设置"
        tk.Label(env_frame, text="模型:", bg="#f0f0f0").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=(4, 0))
        self.model_label = tk.Label(env_frame, text=current_model, fg="#1976d2", bg="#f0f0f0", font=("TkDefaultFont", 8, "bold"))
        self.model_label.grid(row=1, column=1, columnspan=7, sticky="w", pady=(4, 0))
        ttk.Button(env_frame, text="模型选择", command=self._open_model_selector).grid(
            row=1, column=len(env_items) * 2, sticky="e", padx=8, pady=(4, 0)
        )

        # ---- 任务设置 ----
        setup_frame = ttk.LabelFrame(root, text="任务设置", padding=8)
        setup_frame.pack(fill="x", padx=10, pady=4)

        ttk.Label(setup_frame, text="任务ID:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.job_id_var = StringVar(value=self._auto_job_id())
        self.job_id_entry = ttk.Entry(setup_frame, textvariable=self.job_id_var, width=22)
        self.job_id_entry.grid(row=0, column=1, sticky="w")
        self.job_id_entry.bind("<FocusIn>", self._on_job_id_focus)
        self.job_id_entry.bind("<KeyPress>", self._on_job_id_keypress)

        ttk.Label(setup_frame, text="（留空则自动生成）").grid(row=0, column=2, sticky="w", padx=(4, 0))

        # 方向选择
        ttk.Label(setup_frame, text="视频方向:").grid(
            row=0, column=3, sticky="w", padx=(20, 6)
        )
        self.orientation_var = StringVar(value="portrait")
        orientation_frame = ttk.Frame(setup_frame)
        orientation_frame.grid(row=0, column=4, sticky="w")
        for orient_value, orient_label in ORIENTATION_DISPLAY_NAMES.items():
            ttk.Radiobutton(
                orientation_frame,
                text=orient_label,
                variable=self.orientation_var,
                value=orient_value,
            ).pack(side="left", padx=(0, 10))

        ttk.Label(setup_frame, text="导出平台:").grid(
            row=1, column=0, sticky="w", pady=(6, 0), padx=(0, 6)
        )
        platform_frame = ttk.Frame(setup_frame)
        platform_frame.grid(row=1, column=1, columnspan=4, sticky="w", pady=(6, 0))
        self.platform_vars = {
            "douyin": BooleanVar(value=True),
            "xiaohongshu": BooleanVar(value=False),
            "tiktok": BooleanVar(value=False),
            "youtube_shorts": BooleanVar(value=False),
            "youtube": BooleanVar(value=False),
        }
        for platform, variable in self.platform_vars.items():
            ttk.Checkbutton(
                platform_frame,
                text=PLATFORM_DISPLAY_NAMES[platform],
                variable=variable,
            ).pack(side="left", padx=(0, 10))

        options = ttk.Frame(setup_frame)
        options.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))

        # 快速检测可用的视频生成提供商（仅检查文件存在，不做网络请求）
        self._available_providers = self._quick_detect_providers()
        self.video_provider_var = StringVar(value=self._available_providers[0] if self._available_providers else "")
        self.jimeng_model_var = StringVar(value="seedance2.0fast")
        self.kling_model_var = StringVar(value="kling-video-v2_6")
        self.video_resolution_var = StringVar(value="720p")
        self.motion_mode_display_var = StringVar(value=MOTION_MODE_DISPLAY_NAMES["video"])
        self.narration_voice_var = StringVar(value="kokoro:zm_yunyang")
        self.narration_voice_display_var = StringVar(value=VOICE_DISPLAY_NAMES["kokoro:zm_yunyang"])

        # 视频生成提供商
        ttk.Label(options, text="视频生成:").pack(side="left", padx=(0, 4))
        provider_display_values = tuple(
            VIDEO_PROVIDER_DISPLAY_NAMES.get(p, p) for p in self._available_providers
        )
        initial_provider_display = VIDEO_PROVIDER_DISPLAY_NAMES.get(
            self.video_provider_var.get(), ""
        )
        self.provider_display_var = StringVar(value=initial_provider_display)
        self.provider_combo = ttk.Combobox(
            options,
            textvariable=self.provider_display_var,
            values=provider_display_values,
            state="readonly",
            width=16,
        )
        self.provider_combo.pack(side="left", padx=(0, 8))
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_selected)
        if not self._available_providers:
            self.provider_combo.set("未配置")
            self.provider_combo.configure(state="disabled")

        # 模型（根据提供商动态变化）
        ttk.Label(options, text="模型:").pack(side="left", padx=(0, 4))
        self.model_combo = ttk.Combobox(
            options,
            state="readonly",
            width=20,
        )
        self.model_combo.pack(side="left", padx=(0, 12))
        self._update_model_combo()

        # 分辨率
        ttk.Label(options, text="分辨率:").pack(side="left", padx=(0, 4))
        ttk.Combobox(
            options,
            textvariable=self.video_resolution_var,
            values=("720p", "1080p"),
            state="readonly",
            width=10,
        ).pack(side="left", padx=(0, 12))

        # 动态模式（中文显示）
        ttk.Label(options, text="动态模式:").pack(side="left", padx=(0, 4))
        motion_mode_combo = ttk.Combobox(
            options,
            textvariable=self.motion_mode_display_var,
            values=tuple(MOTION_MODE_DISPLAY_NAMES.values()),
            state="readonly",
            width=12,
        )
        motion_mode_combo.pack(side="left", padx=(0, 12))

        # 旁白音色（中文显示）
        ttk.Label(options, text="旁白音色:").pack(side="left", padx=(0, 4))
        voice_display_values = tuple(VOICE_DISPLAY_NAMES[v] for v in VOICE_GROUP_ORDER)
        voice_combo = ttk.Combobox(
            options,
            textvariable=self.narration_voice_display_var,
            values=voice_display_values,
            state="readonly",
            width=24,
        )
        voice_combo.pack(side="left", padx=(0, 6))
        voice_combo.bind("<<ComboboxSelected>>", self._on_voice_selected)

        # 试听按钮
        self.btn_preview_voice = ttk.Button(
            options, text="🔊 试听", command=self._preview_voice, width=8
        )
        self.btn_preview_voice.pack(side="left", padx=(0, 12))
        self._preview_process: subprocess.Popen[bytes] | None = None

        ttk.Label(setup_frame, text="内容方向:").grid(row=3, column=0, sticky="nw", pady=(6, 0), padx=(0, 6))
        self.direction_text = scrolledtext.ScrolledText(setup_frame, height=4, wrap="word", font=("Consolas", 10))
        self.direction_text.grid(row=3, column=1, columnspan=4, sticky="nsew", pady=(6, 0))
        self._direction_placeholder = "在此输入视频内容方向，例如：\n• 主题：xxx\n• 风格：轻松/科普/情感\n• 目标受众：xxx\n• 关键信息点：..."
        self._direction_has_placeholder = False
        default_dir = self._load_default_direction()
        if default_dir:
            self.direction_text.insert("1.0", default_dir)
        else:
            self._show_direction_placeholder()
        self.direction_text.bind("<FocusIn>", self._on_direction_focus_in)
        self.direction_text.bind("<FocusOut>", self._on_direction_focus_out)

        setup_frame.columnconfigure(4, weight=1)
        setup_frame.rowconfigure(3, weight=1)

        # ---- 操作按钮 ----
        btn_frame = ttk.Frame(root, padding=(10, 4))
        btn_frame.pack(fill="x")

        self.btn_new = ttk.Button(btn_frame, text="＋ 新建任务", command=self._new_job)
        self.btn_new.pack(side="left", padx=(0, 6))

        self.btn_start = ttk.Button(btn_frame, text="▶ 开始生成", command=self._start_job)
        self.btn_start.pack(side="left", padx=(0, 6))

        self.btn_resume = ttk.Button(btn_frame, text="⏵ 继续/恢复", command=self._resume_job)
        self.btn_resume.pack(side="left", padx=6)

        self.btn_retry_research = ttk.Button(
            btn_frame,
            text="🔎 重新搜索资料",
            command=self._retry_research,
        )
        self.btn_retry_research.pack(side="left", padx=6)

        self.btn_research_details = ttk.Button(
            btn_frame,
            text="查看失败详情",
            command=self._show_research_failure_details,
        )
        self.btn_research_details.pack(side="left", padx=6)

        ttk.Button(btn_frame, text="🔄 立即刷新", command=self._refresh_all).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="📂 打开输出目录", command=self._open_output).pack(side="left", padx=6)
        self.btn_open_video = ttk.Button(btn_frame, text="▶ 打开最终视频", command=self._open_final_video)
        self.btn_open_video.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(btn_frame, text="⏹ 停止", command=self._stop_job)
        self.btn_stop.pack(side="left", padx=6)

        # ---- 阶段进度 ----
        stage_frame = ttk.LabelFrame(root, text="流水线进度", padding=8)
        stage_frame.pack(fill="x", padx=10, pady=4)

        self.stage_labels: list[ttk.Label] = []
        cols = 7  # 每行 7 个阶段，21 个阶段共 3 行
        for i, (_, name) in enumerate(STAGES):
            lbl = ttk.Label(stage_frame, text=name, style="Stage.TLabel", anchor="center")
            lbl.grid(row=i // cols, column=i % cols, sticky="ew", padx=2, pady=2)
            stage_frame.columnconfigure(i % cols, weight=1)
            self.stage_labels.append(lbl)

        # ---- 中部：历史任务 + 日志 ----
        middle = ttk.PanedWindow(root, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=10, pady=4)

        # 历史任务列表
        history_frame = ttk.LabelFrame(middle, text="历史任务", padding=4)
        middle.add(history_frame, weight=1)

        self.job_tree = ttk.Treeview(
            history_frame,
            columns=("job_id", "direction", "status", "stage", "updated"),
            show="headings",
            height=12,
        )
        self.job_tree.heading("job_id", text="任务ID")
        self.job_tree.heading("direction", text="内容方向")
        self.job_tree.heading("status", text="状态")
        self.job_tree.heading("stage", text="当前阶段")
        self.job_tree.heading("updated", text="更新时间")
        self.job_tree.column("job_id", width=140, anchor="w")
        self.job_tree.column("direction", width=160, anchor="w")
        self.job_tree.column("status", width=110, anchor="w")
        self.job_tree.column("stage", width=140, anchor="w")
        self.job_tree.column("updated", width=140, anchor="w")
        self.job_tree.tag_configure("selected_row", background="#1976d2", foreground="white")
        self.job_tree.pack(fill="both", expand=True)
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)
        self.job_tree.bind("<Button-3>", self._show_job_context_menu)  # 右键菜单

        # 历史任务右键菜单
        self.job_context_menu = Menu(self.root, tearoff=0)
        self.job_context_menu.add_command(label="📂 打开任务目录", command=self._open_job_dir)
        self.job_context_menu.add_command(label="🔄 强制清理僵尸任务", command=self._force_clean_job)
        self.job_context_menu.add_separator()
        self.job_context_menu.add_command(label="🗑 删除任务", command=self._delete_selected_job)

        # 日志区
        log_frame = ttk.LabelFrame(middle, text="运行日志", padding=4)
        middle.add(log_frame, weight=2)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        # 日志颜色标签 - error用红底白字更醒目
        self.log_text.tag_configure("error", foreground="#ff5252", background="#4a1515", font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("success", foreground="#51cf66")
        self.log_text.tag_configure("info", foreground="#74c0fc")
        self.log_text.tag_configure("warning", foreground="#ffd43b", background="#3d3000")
        self._log_auto_scroll = True
        self._auto_scroll_var = BooleanVar(value=True)

        # 右键菜单
        self._log_menu = Menu(self.log_text, tearoff=0)
        self._log_menu.add_command(label="复制", command=self._copy_log_selection, accelerator="Ctrl+C")
        self._log_menu.add_command(label="全选", command=self._select_all_log, accelerator="Ctrl+A")
        self._log_menu.add_separator()
        self._log_menu.add_command(label="清屏", command=self._clear_log)
        self._log_menu.add_separator()
        self._log_menu.add_checkbutton(label="自动滚动", command=self._toggle_auto_scroll, variable=self._auto_scroll_var)
        self.log_text.bind("<Button-3>", self._show_log_menu)

        # ---- 底部状态栏 ----
        self.status_var = StringVar(value="就绪")
        self.status_bar = tk.Label(
            root, textvariable=self.status_var, relief="sunken", anchor="w",
            padx=6, pady=3, bg="#f5f5f5", fg="#333333",
            font=("TkDefaultFont", 9)
        )
        self.status_bar.pack(fill="x", padx=10, pady=(0, 8))

        # 窗口关闭时停止试听
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _auto_job_id(self) -> str:
        now = datetime.now()
        base = f"VIDEO{now.strftime('%m%d%H%M%S')}"
        return first_available_job_id(base, self._job_id_taken)

    def _job_id_taken(self, job_id: str) -> bool:
        try:
            self._get_repo().get_job(job_id)
            return True
        except KeyError:
            return job_storage_exists(
                self._get_job_dir(job_id),
                project_root() / "outputs" / job_id,
            )

    def _show_direction_placeholder(self):
        """显示内容方向的placeholder提示文字。"""
        self.direction_text.delete("1.0", "end")
        self.direction_text.insert("1.0", self._direction_placeholder)
        self.direction_text.configure(foreground="#9ca3af")
        self._direction_has_placeholder = True

    def _get_direction_content(self) -> str:
        """获取文本框内容，不包含末尾隐式换行，strip后返回。如果是placeholder则返回空。"""
        if self._is_showing_placeholder():
            return ""
        # 使用 "end-1c" 避免Tkinter自动添加的末尾换行符
        return self.direction_text.get("1.0", "end-1c").strip()

    def _is_showing_placeholder(self) -> bool:
        """判断当前是否正在显示placeholder。"""
        if not self._direction_has_placeholder:
            return False
        current = self.direction_text.get("1.0", "end-1c")
        return current == self._direction_placeholder

    def _on_direction_focus_in(self, _event=None):
        """获得焦点时清除placeholder。"""
        if self._is_showing_placeholder():
            self.direction_text.delete("1.0", "end")
            self.direction_text.configure(foreground="#000000")
            self._direction_has_placeholder = False

    def _on_direction_focus_out(self, _event=None):
        """失去焦点时如果为空则显示placeholder。"""
        content = self._get_direction_content()
        if not content:
            self._show_direction_placeholder()

    def _load_default_direction(self) -> str:
        path = project_root() / "config" / "content_direction.yaml"
        try:
            cfg = load_config(path)
            d = cfg.get("direction", "")
            return str(d) if d else ""
        except Exception:
            return ""

    def _log(self, msg: str, tag: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {sanitize_error(msg)}\n{tag}")

    def _detect_log_tag(self, line: str) -> str:
        """根据日志内容自动检测日志级别，返回对应的tag颜色。"""
        line_lower = line.lower()
        # 错误级别：最高优先级
        if any(kw in line for kw in ("ERROR", "Error", "错误", "失败", "Traceback", "Exception", "CRITICAL", "FATAL")):
            return "error"
        if any(kw in line_lower for kw in ("error", "failed", "exception", "traceback")):
            return "error"
        # 警告级别
        if any(kw in line for kw in ("WARNING", "Warning", "警告", "WARN", "重试", "降级", "fallback")):
            return "warning"
        if any(kw in line_lower for kw in ("warning", "warn", "retry", "fallback")):
            return "warning"
        # 成功级别
        if any(kw in line for kw in ("完成", "成功", "✓", "DONE", "COMPLETED", "通过")):
            return "success"
        if any(kw in line_lower for kw in ("success", "completed", "done", "passed")):
            return "success"
        # 信息级别（阶段开始等）
        if any(kw in line for kw in ("开始", "启动", "▶", ">>>", "==>", "阶段")):
            return "info"
        if any(kw in line_lower for kw in ("start", "begin", "stage", "running")):
            return "info"
        return ""

    def _log_raw(self, line: str, tag: str = "", add_timestamp: bool = False) -> None:
        """直接输出日志行，可指定tag或自动检测颜色。"""
        if not tag:
            tag = self._detect_log_tag(line)
        if add_timestamp:
            self._log(line, tag)
        else:
            # 不带时间戳，用于worker.log已有时间戳的情况
            self.log_queue.put(f"{sanitize_error(line)}\n{tag}")

    def _toggle_auto_scroll(self) -> None:
        """切换日志自动滚动开关。"""
        self._log_auto_scroll = self._auto_scroll_var.get()

    def _open_settings_for(self, key: str) -> None:
        """点击环境灯时打开设置对话框。"""
        self._open_settings()

    def _poll_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if "\n" in item:
                    text, tag = item.rsplit("\n", 1)
                else:
                    text, tag = item, ""
                self.log_text.configure(state="normal")
                if tag and tag in ("error", "success", "info", "warning"):
                    self.log_text.insert("end", text, tag)
                else:
                    self.log_text.insert("end", text)
                if self._log_auto_scroll:
                    self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                action, arg1, arg2 = self.ui_queue.get_nowait()
                if action == "env_update":
                    self._update_env_lights(str(arg1), bool(arg2))
                elif action == "set_status":
                    self._set_status(str(arg1))
                elif action == "preview_done":
                    self._on_preview_done()
                elif action == "show_error":
                    messagebox.showerror(str(arg1), str(arg2))
                elif action == "providers_detected":
                    self._on_providers_detected(list(arg1) if arg1 else [])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _on_providers_detected(self, providers: list[str]) -> None:
        """后台检测完成后更新视频提供商下拉框。"""
        self._available_providers = providers
        if providers:
            provider_display_values = tuple(
                VIDEO_PROVIDER_DISPLAY_NAMES.get(p, p) for p in providers
            )
            self.provider_combo.configure(values=provider_display_values, state="readonly")
            current_provider = self._get_selected_provider()
            if current_provider not in providers:
                new_provider = providers[0]
                self.video_provider_var.set(new_provider)
                self.provider_display_var.set(
                    VIDEO_PROVIDER_DISPLAY_NAMES.get(new_provider, new_provider)
                )
                self._refresh_model_combo_options()
        else:
            self.provider_combo.configure(values=[], state="disabled")
            self.provider_combo.set("未配置")
        self._update_button_states()

    def _clear_log(self) -> None:
        """清空运行日志（带确认）。"""
        if not messagebox.askyesno("确认清屏", "确定要清空所有日志吗？此操作不可撤销。", parent=self.root):
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log("日志已清空", "info")

    def _copy_log_selection(self) -> None:
        """复制日志中选中的文本到剪贴板。"""
        try:
            selected = self.log_text.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            # 没有选中文本时复制全部
            content = self.log_text.get("1.0", "end-1c")
            self.root.clipboard_clear()
            self.root.clipboard_append(content)

    def _select_all_log(self) -> None:
        """全选日志内容。"""
        self.log_text.tag_add("sel", "1.0", "end-1c")
        self.log_text.mark_set("insert", "1.0")
        self.log_text.see("insert")

    def _show_log_menu(self, event: object) -> None:
        """在鼠标位置弹出右键菜单。"""
        try:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._log_menu.grab_release()

    def _get_job_dir(self, job_id: str) -> Path:
        try:
            return Path(self._get_repo().get_job(job_id).output_dir)
        except KeyError:
            return project_root() / "data" / "jobs" / job_id

    def _get_status_path(self, job_id: str) -> Path:
        return self._get_job_dir(job_id) / "status.json"

    def _get_repo(self) -> JobRepository:
        return JobRepository(project_root() / "data" / "content.db")

    def _recover_zombie_job(self, job_id: str, data: dict) -> bool:
        """检测并自动恢复僵尸任务（进程已死但状态显示运行中）。
        返回 True 表示成功恢复为可重试状态。"""
        cur = str(data.get("current_stage") or "")
        failed = str(data.get("failed_stage") or "")
        completed = data.get("completed_stages", [])
        if not isinstance(completed, (list, tuple, set)):
            completed = ()
        if not should_recover_zombie_job(
            current_stage=cur,
            failed_stage=failed,
            completed_stages=completed,
        ):
            return False
        # 检查是否真的是僵尸（锁文件失效=进程已死）
        job_dir = self._get_job_dir(job_id)
        lock_path = job_dir / ".autopilot.lock"
        if lock_is_active(lock_path, stale_after=120.0):
            return False  # 进程还在运行，不是僵尸
        # 确认是僵尸：尝试标记为可重试失败
        try:
            stage = PipelineStage(cur)
            repo = self._get_repo()
            # 读取当前状态确认
            status = repo.get_job(job_id)
            if (
                status.current_stage == stage
                and should_recover_zombie_job(
                    current_stage=(
                        status.current_stage.value
                        if status.current_stage is not None
                        else ""
                    ),
                    failed_stage=(
                        status.failed_stage.value
                        if status.failed_stage is not None
                        else ""
                    ),
                    completed_stages=[
                        completed_stage.value
                        for completed_stage in status.completed_stages
                    ],
                )
            ):
                repo.fail_stage(
                    job_id,
                    stage,
                    reason="进程异常退出（可能是程序被关闭、网络中断或电脑休眠），点击「继续/恢复」即可从当前阶段重新开始",
                    retryable=True,
                )
                self._log(
                    f"检测到异常中断的任务 [{job_id}]（阶段：{self._translate_stage(cur)}），"
                    f"已自动标记为可恢复，点击「继续/恢复」即可继续",
                    "warning",
                )
                # 清理过期锁文件
                try:
                    if lock_path.exists():
                        lock_path.unlink()
                except Exception:
                    pass
                return True
        except Exception:
            pass
        return False

    def _collect_production_settings(self) -> ProductionSettings:
        # 将中文显示名转换回内部值
        motion_display = self.motion_mode_display_var.get()
        motion_mode = MOTION_MODE_VALUES.get(motion_display, "video")

        # 获取当前选中的模型
        current_provider = self._get_selected_provider()
        kling_model = self.kling_model_var.get()
        jimeng_model = self.jimeng_model_var.get()

        # 校验：至少选择一个平台
        selected_platforms = {
            platform: bool(variable.get())
            for platform, variable in self.platform_vars.items()
        }
        if not any(selected_platforms.values()):
            messagebox.showwarning("提示", "请至少选择一个发布平台")
            return None  # type: ignore[return-value]

        return build_production_settings(
            selected_platforms,
            video_provider=current_provider or "jimeng",
            jimeng_model=jimeng_model,
            kling_model=kling_model,
            video_resolution=self.video_resolution_var.get(),
            motion_mode=motion_mode,
            narration_voice=self.narration_voice_var.get(),
            orientation=self.orientation_var.get(),
        )

    def _apply_production_settings(self, settings: ProductionSettings) -> None:
        selected = set(settings.selected_platforms)
        for platform, variable in self.platform_vars.items():
            variable.set(platform in selected)
        provider_key = getattr(settings, "video_provider", "jimeng")
        self.video_provider_var.set(provider_key)
        if hasattr(self, "provider_display_var"):
            self.provider_display_var.set(
                VIDEO_PROVIDER_DISPLAY_NAMES.get(provider_key, provider_key)
            )
        self.jimeng_model_var.set(settings.jimeng_model)
        self.kling_model_var.set(getattr(settings, "kling_model", "kling-video-v2_6"))
        if hasattr(self, "model_combo"):
            self._update_model_combo_display()
        self.video_resolution_var.set(settings.video_resolution)
        # 内部值转中文显示
        self.motion_mode_display_var.set(
            MOTION_MODE_DISPLAY_NAMES.get(settings.motion_mode, "视频模式")
        )
        self.narration_voice_var.set(settings.narration_voice)
        self.narration_voice_display_var.set(
            VOICE_DISPLAY_NAMES.get(settings.narration_voice, VOICE_DISPLAY_NAMES["kokoro:zm_yunyang"])
        )
        self.orientation_var.set(getattr(settings, "orientation", "portrait"))

    def _load_job_production_settings(self, job_id: str) -> ProductionSettings:
        settings = ProductionSettings.load_for_job(self._get_job_dir(job_id))
        self._apply_production_settings(settings)
        return settings

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        # 根据状态文本自动设置颜色
        text_lower = text.lower()
        if any(kw in text for kw in ("失败", "错误", "异常", "✗")) or "fail" in text_lower or "error" in text_lower:
            self.status_bar.configure(fg="#c62828")  # 红色
        elif any(kw in text for kw in ("完成", "就绪", "✓", "成功")) or "success" in text_lower or "complete" in text_lower:
            self.status_bar.configure(fg="#2e7d32")  # 绿色
        elif any(kw in text for kw in ("警告", "需处理")) or "warn" in text_lower:
            self.status_bar.configure(fg="#f57c00")  # 橙色
        elif "运行中" in text or "running" in text_lower:
            self.status_bar.configure(fg="#1565c0")  # 蓝色
        else:
            self.status_bar.configure(fg="#333333")  # 默认深灰

    def _set_buttons_running(self, running: bool) -> None:
        self.running = running
        self._update_button_states()

    def _friendly_error(self, error: Exception) -> str:
        """将技术异常转换为用户友好的中文提示。"""
        error_msg = str(error)
        error_lower = error_msg.lower()

        # FFmpeg 相关错误
        if "ffmpeg" in error_lower or "ffprobe" in error_lower:
            if "not found" in error_lower or "未找到" in error_msg:
                return ("未找到FFmpeg。\n\n"
                        "请安装完整版FFmpeg：\n"
                        "  方法1：运行 winget install Gyan.FFmpeg\n"
                        "  方法2：从 https://ffmpeg.org 下载后添加到PATH\n\n"
                        "安装后请重启本工具。")
            return f"音频/视频处理出错：{error_msg}\n\n请确认FFmpeg安装正确。"

        # 网络相关错误
        if any(kw in error_lower for kw in ("401", "unauthorized", "invalid api", "api key")):
            return ("API Key无效或未配置。\n\n"
                    "请点击「⚙️ 设置」按钮，在「AI大模型」页面检查您的OPENROUTER_API_KEY是否正确。")
        if "429" in error_lower or "too many requests" in error_lower or "rate limit" in error_lower:
            return "请求过于频繁，请稍后重试。"
        if "403" in error_lower or "forbidden" in error_lower:
            return "API访问被拒绝，请检查您的API Key权限和账户余额。"
        if any(kw in error_lower for kw in ("timeout", "timed out", "连接超时", "网络")):
            return "网络连接超时，请检查网络连接后重试。"
        if any(kw in error_lower for kw in ("connection", "connect", "dns", "网络错误")):
            return "网络连接失败，请检查网络连接后重试。"

        # TTS 相关错误
        if "kokoro" in error_lower or "tts" in error_lower:
            if "model" in error_lower or "下载" in error_msg:
                return ("语音模型下载失败或未就绪。\n\n"
                        "首次使用Kokoro本地TTS需要下载约300MB的模型文件，\n"
                        "请确保网络通畅后重试。")
            return f"语音生成失败：{error_msg}"

        # CLI 认证错误
        if any(kw in error_lower for kw in ("login", "认证", "auth", "未登录")):
            return ("视频生成工具未登录。\n\n"
                    "请点击「⚙️ 设置」→「视频生成」，根据指引完成登录。")

        # CLI 未找到
        if any(kw in error_lower for kw in ("not found", "未找到", "no such file")):
            if "dreamina" in error_lower or "jimeng" in error_lower or "即梦" in error_msg:
                return ("未找到即梦视频生成工具。\n\n"
                        "请在「⚙️ 设置」→「视频生成」中安装并配置即梦CLI。")
            if "kling" in error_lower or "可灵" in error_msg:
                return ("未找到可灵视频生成工具。\n\n"
                        "请在「⚙️ 设置」→「视频生成」中安装并配置可灵CLI。")

        # 默认：返回简化的错误信息，技术详情放日志
        return f"操作失败：{error_msg}\n\n详细错误信息请查看日志。"

    def _current_job_actions(self) -> JobActionState:
        selected = self.job_tree.selection() if hasattr(self, "job_tree") else ()
        job_id = str(selected[0]) if selected else ""
        if not job_id:
            return derive_job_actions(
                existing_job=False,
                app_has_running_job=bool(self._polling_job_id) or self.running,
            )

        data: dict[str, object] = {}
        try:
            status = self._get_repo().get_job(job_id)
            loaded = status.model_dump(mode="json")
            if isinstance(loaded, dict):
                data = loaded
        except KeyError:
            status_path = self._get_status_path(job_id)
            if status_path.is_file():
                try:
                    loaded = json.loads(status_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (OSError, json.JSONDecodeError):
                    data = {}
        except (OSError, ValueError):
            status_path = self._get_status_path(job_id)
            try:
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        current_stage = str(data.get("current_stage") or "")
        failed_stage = str(data.get("failed_stage") or "")
        recoverable = False
        stages = data.get("stages")
        if failed_stage and isinstance(stages, dict):
            failed_record = stages.get(failed_stage)
            if isinstance(failed_record, dict):
                recoverable = bool(failed_record.get("recoverable"))
            if current_stage == "FAILED_NEEDS_ATTENTION":
                recoverable = recoverable or failed_attention_can_auto_reopen(
                    failed_stage,
                    stages,
                )
        job_dir = self._get_job_dir(job_id)
        job_is_running = self._is_job_really_running(job_dir, data)
        final_video = final_video_for_job(
            job_dir,
            project_root() / "outputs" / job_id,
        )
        research_failure_summary = ""
        if failed_stage == "RESEARCHED":
            evidence_path = job_dir / "research_sources.json"
            if evidence_path.is_file():
                try:
                    evidence = json.loads(
                        evidence_path.read_text(encoding="utf-8")
                    )
                    if isinstance(evidence, list):
                        research_failure_summary = summarize_research_failure(
                            [
                                item for item in evidence
                                if isinstance(item, dict)
                            ]
                        )
                except (OSError, json.JSONDecodeError):
                    pass
        return derive_job_actions(
            existing_job=True,
            current_stage=current_stage,
            failed_stage=failed_stage,
            recoverable=recoverable,
            job_is_running=job_is_running,
            app_has_running_job=bool(self._polling_job_id) or self.running,
            has_final_video=final_video is not None,
            research_failure_summary=research_failure_summary,
        )

    def _show_current_job_guidance(self) -> None:
        self._set_status(self._current_job_actions().guidance)

    def _update_button_states(self) -> None:
        """根据当前运行状态和环境配置更新按钮可用状态。"""
        has_video = bool(self._available_providers)
        has_api = bool(_get_env_value("OPENROUTER_API_KEY"))
        actions = self._current_job_actions()
        can_start = actions.can_start and has_video and has_api

        self.btn_start.configure(state="normal" if can_start else "disabled")
        self.btn_resume.configure(
            state="normal" if actions.can_resume else "disabled"
        )
        self.btn_retry_research.configure(
            state="normal" if actions.can_retry_research else "disabled"
        )
        self.btn_research_details.configure(
            state=(
                "normal"
                if actions.can_view_research_failure
                else "disabled"
            )
        )

        if hasattr(self, "btn_stop"):
            self.btn_stop.configure(
                state="normal" if actions.can_stop else "disabled"
            )

        if hasattr(self, "btn_open_video") and hasattr(self, "job_tree"):
            self.btn_open_video.configure(
                state="normal" if actions.can_open_video else "disabled"
            )

        if not has_api:
            self.btn_start.configure(text="▶ 请先配置API Key")
        elif not has_video:
            self.btn_start.configure(text="▶ 请先配置视频服务")
        elif not actions.can_start and self.job_tree.selection():
            self.btn_start.configure(text="▶ 已有任务不可重复生成")
        else:
            self.btn_start.configure(text="▶ 开始生成")

    # ------------------------------------------------------------------
    # 音色选择与试听
    # ------------------------------------------------------------------
    def _voice_id_from_display(self, display_name: str) -> str:
        """从中文显示名反查内部 voice_id。"""
        for vid, dname in VOICE_DISPLAY_NAMES.items():
            if dname == display_name:
                return vid
        return "kokoro:zm_yunyang"

    def _on_voice_selected(self, _event: object = None) -> None:
        """下拉框选择音色时同步内部 voice_id。"""
        display = self.narration_voice_display_var.get()
        self.narration_voice_var.set(self._voice_id_from_display(display))

    @staticmethod
    def _quick_find_cli(name: str, candidates: list[Path] | None = None) -> str | None:
        """快速查找CLI可执行文件（仅文件系统检查，不做网络请求）。"""
        found = shutil.which(name)
        if found:
            return found
        if candidates:
            for c in candidates:
                try:
                    if c.is_file():
                        return str(c)
                except Exception:
                    pass
        return None

    def _quick_detect_providers(self) -> list[str]:
        """快速检测哪些视频提供商的CLI文件存在（不做网络/登录验证，毫秒级返回）。"""
        providers: list[str] = []
        # 检测即梦CLI文件
        try:
            root = project_root()
            config_path = root / "config" / "jimeng_cli.yaml"
            exe = (os.environ.get("JIMENG_CLI_EXECUTABLE") or "").strip()
            if exe and Path(exe).is_file():
                providers.append("jimeng")
            else:
                local_appdata = os.environ.get("LOCALAPPDATA") or ""
                userprofile = os.environ.get("USERPROFILE") or ""
                jm_candidates = []
                if local_appdata:
                    jm_candidates.append(Path(local_appdata) / "Programs" / "dreamina" / "dreamina.exe")
                if userprofile:
                    jm_candidates.append(Path(userprofile) / "bin" / "dreamina.exe")
                if self._quick_find_cli("dreamina", jm_candidates) or self._quick_find_cli("jimeng"):
                    providers.append("jimeng")
                elif config_path.is_file():
                    try:
                        cfg = load_config(config_path)
                        p = cfg.get("cli_path", "") or ""
                        if p and Path(p).is_file():
                            providers.append("jimeng")
                    except Exception:
                        pass
        except Exception:
            pass
        # 检测可灵CLI文件
        try:
            exe = (os.environ.get("KLING_CLI_EXECUTABLE") or "").strip()
            if exe and Path(exe).is_file():
                providers.append("kling")
            else:
                appdata = os.environ.get("APPDATA") or ""
                kl_candidates = []
                if appdata:
                    kl_candidates.append(Path(appdata) / "npm" / "kling.cmd")
                    kl_candidates.append(Path(appdata) / "TRAE SOLO CN" / "ModularData" / "ai-agent" / "vm" / "tools" / "node" / "kling.cmd")
                if self._quick_find_cli("kling", kl_candidates):
                    providers.append("kling")
        except Exception:
            pass
        return providers

    def _detect_video_providers(self) -> list[str]:
        """完整检测哪些视频生成提供商已配置可用（含网络who_am_i验证登录状态）。"""
        providers: list[str] = []
        # 检测即梦CLI
        try:
            root = project_root()
            config_path = root / "config" / "jimeng_cli.yaml"
            caps = detect_jimeng_cli(config_path=config_path, timeout_seconds=30)
            if caps.supports_async_task:
                providers.append("jimeng")
        except Exception as e:
            print(f"[WARN] 即梦检测失败: {e}", file=sys.stderr)
        # 检测可灵CLI
        try:
            caps = detect_kling_cli(timeout_seconds=30)
            if caps.supports_async_task and caps.cli_path:
                providers.append("kling")
        except Exception as e:
            print(f"[WARN] 可灵检测失败: {e}", file=sys.stderr)
        return providers

    def _start_async_provider_detection(self) -> None:
        """后台线程异步执行完整的提供商检测和环境检查，不阻塞UI启动。"""
        def worker() -> None:
            # 1. 完整检测视频提供商
            providers = self._detect_video_providers()
            self.ui_queue.put(("providers_detected", providers, None))
            # 2. 后台运行doctor检查环境
            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                result = silent_run(
                    [python_executable(), "-m", "aicf", "doctor"],
                    cwd=str(project_root()),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    env=env,
                )
                output = (result.stdout or "") + (result.stderr or "")
                ok = result.returncode == 0
                self.ui_queue.put(("env_update", output, ok))
            except Exception as e:
                self.ui_queue.put(("env_update", f"环境检查异常: {e}", False))
        threading.Thread(target=worker, daemon=True).start()

    def _get_selected_provider(self) -> str:
        """获取当前选中的视频提供商（内部key）。"""
        display = self.provider_display_var.get()
        for key, name in VIDEO_PROVIDER_DISPLAY_NAMES.items():
            if name == display:
                return key
        # fallback: 检查var
        pv = self.video_provider_var.get()
        if pv in VIDEO_PROVIDER_DISPLAY_NAMES:
            return pv
        return self._available_providers[0] if self._available_providers else "jimeng"

    def _on_provider_selected(self, _event: object = None) -> None:
        """切换视频提供商时更新模型下拉框。"""
        provider = self._get_selected_provider()
        self.video_provider_var.set(provider)
        self._refresh_model_combo_options()

    def _refresh_model_combo_options(self) -> None:
        """根据当前选中的提供商刷新模型下拉框的选项列表和事件绑定。"""
        provider = self._get_selected_provider()
        if provider == "kling":
            model_map = KLING_MODEL_DISPLAY_NAMES
            current_var = self.kling_model_var
        else:
            model_map = JIMENG_MODEL_DISPLAY_NAMES
            current_var = self.jimeng_model_var

        display_values = tuple(model_map.values())
        self.model_combo.configure(values=display_values)
        current_display = model_map.get(current_var.get(), current_var.get())
        self.model_combo.set(current_display)

        def on_model_selected(_event: object = None) -> None:
            display = self.model_combo.get()
            # 反查内部key
            for key, name in model_map.items():
                if name == display:
                    current_var.set(key)
                    break
        self.model_combo.bind("<<ComboboxSelected>>", on_model_selected)

    def _update_model_combo_display(self) -> None:
        """根据当前选中的提供商更新模型下拉框显示值（不改变选项列表）。"""
        provider = self._get_selected_provider()
        if provider == "kling":
            display_name = KLING_MODEL_DISPLAY_NAMES.get(
                self.kling_model_var.get(), self.kling_model_var.get()
            )
        else:
            display_name = JIMENG_MODEL_DISPLAY_NAMES.get(
                self.jimeng_model_var.get(), self.jimeng_model_var.get()
            )
        self.model_combo.set(display_name)

    def _update_model_combo(self) -> None:
        """初始化模型下拉框的选项列表。"""
        self._refresh_model_combo_options()

    def _stop_preview(self) -> None:
        """停止正在播放的试听音频。"""
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        if self._preview_process is not None:
            try:
                self._preview_process.terminate()
                self._preview_process.wait(timeout=2)
            except Exception:
                try:
                    self._preview_process.kill()
                except Exception:
                    pass
            self._preview_process = None

    def _preview_voice(self) -> None:
        """生成并播放当前选中音色的试听音频。试听词覆盖多种语调，便于区分音色。"""
        self._stop_preview()
        self.btn_preview_voice.configure(state="disabled", text="生成中...")

        voice_id = self._voice_id_from_display(self.narration_voice_display_var.get())
        # 较长的试听文本，覆盖不同声调/语速/情绪，便于区分音色差异
        preview_text = "然哥哥早上好呀！今天天气真不错，你吃早饭了吗？我们一起出门走走吧。"

        def worker() -> None:
            try:
                from .providers.tts import build_default_tts_service
                import tempfile
                import winsound

                self.ui_queue.put(("set_status", "正在生成试听音频...", None))
                tts = build_default_tts_service()
                tmp_dir = Path(tempfile.gettempdir()) / "aicf_preview"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                # 清理旧的试听文件
                for old in tmp_dir.glob("preview_*.wav"):
                    try:
                        old.unlink()
                    except Exception:
                        pass
                out_path = tmp_dir / f"preview_{voice_id.replace(':', '_')}.wav"
                result = tts.preview(preview_text, out_path, voice_id)
                self.log_queue.put(f"[试听] 使用 {result.provider} 生成音色 {voice_id}\n")

                # 用 winsound 直接异步播放（可被停止，切换音色时自动中断上一个）
                winsound.PlaySound(str(out_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                self.log_queue.put(f"[试听] 正在播放，切换音色后点试听可直接对比\n")
                self.ui_queue.put(("set_status", "试听播放中（切换音色可对比）", None))
            except Exception as error:
                error_msg = str(error)
                error_type = type(error).__name__
                self.log_queue.put(f"[试听] 生成失败: {error_type}: {error_msg}\n")
                self.ui_queue.put(("set_status", "试听失败", None))
                # 转换为用户友好的错误提示
                friendly_msg = self._friendly_error(error)
                self.ui_queue.put(("show_error", "试听失败", friendly_msg))
            finally:
                self.ui_queue.put(("preview_done", None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_preview_done(self) -> None:
        """试听音频生成完成，恢复按钮状态。"""
        self.btn_preview_voice.configure(state="normal", text="🔊 试听")

    # ------------------------------------------------------------------
    # 环境检查
    # ------------------------------------------------------------------
    def _run_doctor(self) -> None:
        self._log("正在检查环境...", "info")
        self._set_status("检查环境中...")

        def worker() -> None:
            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                result = silent_run(
                    [python_executable(), "-m", "aicf", "doctor"],
                    cwd=str(project_root()),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    env=env,
                )
                output = (result.stdout or "") + (result.stderr or "")
                self.log_queue.put(output + "\n")

                ok = result.returncode == 0
                self.ui_queue.put(("env_update", output, ok))
            except Exception as e:
                self.log_queue.put(f"环境检查失败: {e}\nerror")
                self.ui_queue.put(("set_status", "环境检查失败", None))

        threading.Thread(target=worker, daemon=True).start()

    def _update_env_lights(self, output: str, ok: bool) -> None:
        if ok:
            for lbl in self.env_labels.values():
                lbl.configure(text="✓ 就绪", fg="#2e7d32", font=("TkDefaultFont", 9, "bold"))
            self._set_status("环境检查完成")
            self._log("环境检查通过", "success")
        else:
            checks = {
                "openrouter": "openrouter: OK" in output,
                "dreamina": "jimeng: OK" in output,
                "kling": "kling: OK" in output,
                "ffmpeg": "ffmpeg: OK" in output,
                "tts": "tts_strategy: OK" in output,
            }
            for key, lbl in self.env_labels.items():
                if checks.get(key, False):
                    lbl.configure(text="✓ 就绪", fg="#2e7d32", font=("TkDefaultFont", 9, "bold"))
                else:
                    lbl.configure(text="✗ 异常", fg="#c62828", font=("TkDefaultFont", 9, "bold"))
            self._set_status("环境存在问题，请查看日志")
            self._log("环境存在问题", "error")

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------
    def _ensure_direction_file(self) -> Path:
        """将界面输入的方向写入全局 config/content_direction.yaml。"""
        # 如果正在显示placeholder，视为空
        if self._is_showing_placeholder():
            direction = ""
        else:
            direction = self._get_direction_content()
        cfg_path = project_root() / "config" / "content_direction.yaml"
        # 不再使用默认值，调用前应确保direction不为空
        if not direction:
            direction = "请根据用户需求生成AI短视频"
        return update_direction_config(cfg_path, direction)

    def _run_command_async(self, args: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> None:
        """在后台线程运行命令，实时输出日志。"""
        if self.running:
            messagebox.showinfo("提示", "已有任务正在运行，请等待或先停止")
            return
        # 立即设置运行标志，防止快速双击导致重复启动
        self._set_buttons_running(True)
        self._set_status("运行中...")
        self._log(f"执行命令: {' '.join(args)}", "info")
        self._log_file_offsets.clear()  # 清空日志文件偏移，重新读取

        # 提取 job_id，全局轮询器会自动检测并开始跟踪
        try:
            idx = args.index("--job")
            self._polling_job_id = args[idx + 1]
        except (ValueError, IndexError):
            self._polling_job_id = ""

        def worker() -> None:
            proc = None
            try:
                env = os.environ.copy()
                if env_extra:
                    env.update(env_extra)
                # 强制 Python 子进程使用 UTF-8 输出，避免 Windows GBK 编码导致乱码
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                proc = subprocess.Popen(
                    args,
                    cwd=str(cwd or project_root()),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.current_process = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip("\n\r")
                    tag = ""
                    low = line.lower()
                    if any(k in low for k in ("error", "失败", "failed", "exception")):
                        tag = "error"
                    elif any(k in low for k in ("passed", "成功", "completed", "ready", "通过")):
                        tag = "success"
                    self.log_queue.put(line + "\n" + tag)
                proc.wait()
                code = proc.returncode
                if code == 0:
                    self.log_queue.put("命令执行完成\nsuccess")
                else:
                    self.log_queue.put(f"命令退出码: {code}\nerror")
            except Exception as e:
                self.log_queue.put(f"执行异常: {e}\nerror")
            finally:
                self.current_process = None
                self.root.after(0, self._on_command_done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_command_done(self) -> None:
        """命令执行完毕（进程退出），刷新状态让轮询器读取最终结果。"""
        self.current_process = None
        self._set_buttons_running(False)
        # 不立即清除 _polling_job_id/_display_job_id，让全局轮询器读取 status.json 的最终状态
        self._refresh_job_list()
        self._refresh_status_for_job(self._display_job_id)

    def _auto_detect_and_poll(self) -> None:
        """启动时从数据库检测正在运行的任务并自动跟踪。"""
        terminal_states = {"COMPLETED", "INIT", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION"}
        running_jobs: list[tuple[float, Path]] = []
        statuses = self._get_repo().list_jobs()
        for status in statuses:
            job_dir = Path(status.output_dir)
            sp = job_dir / "status.json"
            if not sp.is_file():
                continue
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                cur = data.get("current_stage", "")
                failed = data.get("failed_stage", "")
                # 自动检测并恢复僵尸任务（进程已死但状态显示运行中）
                if cur and cur not in terminal_states and not failed:
                    self._recover_zombie_job(job_dir.name, data)
                    # 恢复后重新读取状态
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    cur = data.get("current_stage", "")
                    failed = data.get("failed_stage", "")
                is_running = bool(
                    cur
                    and cur not in terminal_states
                    and not failed
                    and self._is_job_really_running(job_dir, data)
                )
                if is_running:
                    running_jobs.append((sp.stat().st_mtime, job_dir))
            except Exception:
                pass
        if running_jobs:
            running_jobs.sort(reverse=True)
            job_id = running_jobs[0][1].name
            self._polling_job_id = job_id
            self._display_job_id = job_id
            self._user_selected_job = False
            self._logged_stages.clear()
            self._set_buttons_running(True)
            self._log(f"自动检测到运行中的任务 [{job_id}]，已连接进度跟踪", "info")
            self.job_id_var.set(job_id)
            self.job_tree.selection_set(job_id)
            self.job_tree.focus(job_id)
            self._highlight_selected_job()
        else:
            # 无运行任务，默认显示最新任务的状态
            if statuses:
                self._display_job_id = statuses[0].job_id
                self.job_tree.selection_set(self._display_job_id)
                self.job_tree.focus(self._display_job_id)
                self._highlight_selected_job()
                self._refresh_status_for_job(self._display_job_id)

    def _poll_progress(self) -> None:
        """UI线程只做一件事：从ui_queue取后台线程准备好的数据更新界面，绝对不做文件IO。
        
        架构：
        - _bg_worker_thread：后台守护线程，做所有文件IO、状态检测、日志读取
        - ui_queue：后台线程 → UI线程的消息队列
        - UI线程（本方法）：每100ms处理队列消息，纯控件渲染，不碰文件
        """
        # 处理所有待处理的UI消息
        processed = 0
        while processed < 100:  # 一次最多处理100条，避免UI阻塞
            try:
                msg = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            
            # 兼容两种消息格式：
            # 新格式：(msg_type: str, payload) - 2元组
            # 旧格式：(action: str, arg1, arg2) - 3元组（环境检测等后台操作）
            if len(msg) == 2:
                msg_type, payload = msg
                if msg_type == "job_list":
                    self._apply_job_list_update(payload)
                elif msg_type == "display_stages":
                    self._update_stages_from_status(payload)
                elif msg_type == "log_lines":
                    for line, tag in payload:
                        self._log_raw(line, tag)
                elif msg_type == "running_job":
                    if payload and not self._user_selected_job:
                        job_id = payload
                        if self._polling_job_id != job_id:
                            self._polling_job_id = job_id
                            self._logged_stages.clear()
                            self._log_file_offsets.clear()
                            self._set_buttons_running(True)
                            self._log(f"检测到任务 [{job_id}] 开始运行，正在跟踪进度", "info")
                            self._display_job_id = job_id
                            self.job_id_var.set(job_id)
                    elif not payload and self._polling_job_id:
                        self._polling_job_id = ""
                        self._set_buttons_running(False)
            elif len(msg) == 3:
                # 旧格式3元组：(action, arg1, arg2)
                action, arg1, arg2 = msg
                if action == "env_update":
                    self._update_env_lights(str(arg1), bool(arg2))
                elif action == "set_status":
                    self._set_status(str(arg1))
                elif action == "preview_done":
                    self._on_preview_done()
                elif action == "show_error":
                    messagebox.showerror(str(arg1), str(arg2))
                elif action == "providers_detected":
                    self._on_providers_detected(list(arg1) if arg1 else [])
            processed += 1
        
        # 同时处理日志队列
        self._poll_log_queue_inner()
        
        # 100ms后继续处理（高频但极轻量，只处理内存中的队列消息）
        self.root.after(100, self._poll_progress)
    
    def _poll_log_queue_inner(self) -> None:
        """处理日志队列消息（内联到_poll_progress，不需要单独轮询）。"""
        try:
            while True:
                item = self.log_queue.get_nowait()
                if "\n" in item:
                    text, tag = item.rsplit("\n", 1)
                else:
                    text, tag = item, ""
                self.log_text.configure(state="normal")
                if tag:
                    self.log_text.insert("end", text + "\n", tag)
                else:
                    self.log_text.insert("end", text + "\n")
                if self._log_auto_scroll:
                    self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass

    def _start_background_poll_thread(self) -> None:
        """启动后台轮询线程，所有耗时IO都在这里做，UI线程只负责渲染。"""
        def bg_poll_loop():
            last_list_refresh = 0.0
            last_zombie_check = 0.0
            terminal_states = {"COMPLETED", "INIT", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION"}
            
            while True:
                try:
                    now = time.time()
                    
                    # 1. 检测是否需要立即刷新（用户手动点了立即刷新）
                    force_refresh = self._force_refresh_event.is_set()
                    if force_refresh:
                        self._force_refresh_event.clear()
                    
                    # 2. 检测运行中的任务 + 僵尸恢复（30秒检查一次僵尸）
                    should_check_zombie = now - last_zombie_check >= 30.0
                    if should_check_zombie:
                        last_zombie_check = now
                    
                    newest_running_id = None
                    newest_running_mtime = 0.0
                    
                    try:
                        jobs = list(self._get_repo().list_jobs())
                    except Exception:
                        jobs = []
                    
                    for status in jobs:
                        try:
                            job_dir = Path(status.output_dir)
                            sp = job_dir / "status.json"
                            if not sp.is_file():
                                continue
                            data = json.loads(sp.read_text(encoding="utf-8"))
                            cur = data.get("current_stage", "")
                            failed = data.get("failed_stage", "")
                            
                            # 僵尸恢复检查（节流）
                            if should_check_zombie and cur and cur not in terminal_states and not failed:
                                try:
                                    if self._recover_zombie_job(job_dir.name, data):
                                        data = json.loads(sp.read_text(encoding="utf-8"))
                                        cur = data.get("current_stage", "")
                                        failed = data.get("failed_stage", "")
                                except Exception:
                                    pass
                            
                            # 轻量检查：只看锁文件+worker记录，不做系统调用
                            is_running = bool(
                                cur
                                and cur not in terminal_states
                                and not failed
                                and lock_is_active(job_dir / ".autopilot.lock", stale_after=120.0)
                            )
                            if is_running:
                                # 再检查worker记录
                                try:
                                    from .background_worker import read_worker_record
                                    record = read_worker_record(job_dir)
                                    is_running = record is not None and record.finished_at is None
                                except Exception:
                                    is_running = False
                            
                            if is_running:
                                mtime = sp.stat().st_mtime
                                if mtime > newest_running_mtime:
                                    newest_running_mtime = mtime
                                    newest_running_id = job_dir.name
                        except Exception:
                            pass
                    
                    # 通知UI线程运行任务变化
                    if newest_running_id != self._polling_job_id:
                        self.ui_queue.put(("running_job", newest_running_id))
                    
                    # 3. 跟踪运行中任务的日志（增量读取）
                    if newest_running_id:
                        try:
                            job_dir = self._get_job_dir(newest_running_id)
                            log_lines: list[tuple[str, str]] = []
                            
                            # worker.log增量读取
                            for wlog_path in [
                                job_dir / "_work" / "runtime" / "worker.log",
                                job_dir / "worker.log",
                                job_dir / "logs" / "worker.log",
                            ]:
                                if wlog_path.is_file():
                                    cache_key = f"{newest_running_id}/worker.log"
                                    last_pos = self._log_file_offsets.get(cache_key, 0)
                                    try:
                                        size = wlog_path.stat().st_size
                                        if size < last_pos:
                                            last_pos = 0
                                        if size > last_pos:
                                            with open(wlog_path, "r", encoding="utf-8", errors="replace") as f:
                                                f.seek(last_pos)
                                                content = f.read()
                                            self._log_file_offsets[cache_key] = size
                                            for line in content.splitlines():
                                                line = line.rstrip()
                                                if line:
                                                    tag = self._get_log_tag(line)
                                                    log_lines.append((line, tag))
                                    except Exception:
                                        pass
                                    break
                            
                            # 阶段日志增量读取
                            sp = job_dir / "status.json"
                            if sp.is_file():
                                try:
                                    data = json.loads(sp.read_text(encoding="utf-8"))
                                    stages = data.get("stages", {})
                                    if isinstance(stages, dict):
                                        for stage_key, stage_info in stages.items():
                                            if not isinstance(stage_info, dict):
                                                continue
                                            log_rel = stage_info.get("log_path")
                                            if not isinstance(log_rel, str) or not log_rel:
                                                continue
                                            log_path = None
                                            for base_dir in [job_dir / "_work" / "runtime", job_dir, job_dir / "logs"]:
                                                candidate = base_dir / log_rel
                                                if candidate.is_file():
                                                    log_path = candidate
                                                    break
                                            if log_path is None:
                                                continue
                                            cache_key = f"{newest_running_id}/{log_rel}"
                                            last_pos = self._log_file_offsets.get(cache_key, 0)
                                            try:
                                                fsize = log_path.stat().st_size
                                                if fsize < last_pos:
                                                    last_pos = 0
                                                if fsize > last_pos:
                                                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                                                        f.seek(last_pos)
                                                        new_content = f.read()
                                                    self._log_file_offsets[cache_key] = fsize
                                                    for line in new_content.splitlines():
                                                        line = line.rstrip()
                                                        if line:
                                                            tag = self._get_log_tag(line)
                                                            log_lines.append((line, tag))
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                            
                            if log_lines:
                                self.ui_queue.put(("log_lines", log_lines))
                        except Exception:
                            pass
                    
                    # 4. 刷新显示中任务的进度条状态
                    display_id = self._display_job_id
                    if display_id:
                        try:
                            sp = self._get_status_path(display_id)
                            if sp.is_file():
                                data = json.loads(sp.read_text(encoding="utf-8"))
                                self.ui_queue.put(("display_stages", data))
                        except Exception:
                            pass
                    
                    # 5. 刷新任务列表（每8秒一次，或强制刷新）
                    if force_refresh or now - last_list_refresh >= 8.0:
                        last_list_refresh = now
                        try:
                            job_list_data = []
                            for status in jobs:
                                try:
                                    job_dir = Path(status.output_dir)
                                    sp = job_dir / "status.json"
                                    status_text = "未开始"
                                    stage_text = "-"
                                    updated = "-"
                                    direction_text = "-"
                                    
                                    # 读取方向信息
                                    direction_file = job_dir / "direction.json"
                                    if direction_file.is_file():
                                        try:
                                            ddata = json.loads(direction_file.read_text(encoding="utf-8"))
                                            series_name = ddata.get("series_name", "")
                                            core_direction = ddata.get("core_direction", "")
                                            if series_name:
                                                direction_text = series_name
                                            elif core_direction:
                                                direction_text = core_direction[:20] + "..." if len(core_direction) > 20 else core_direction
                                        except Exception:
                                            pass
                                    
                                    if sp.is_file():
                                        try:
                                            d = json.loads(sp.read_text(encoding="utf-8"))
                                            st = d.get("status", "")
                                            cur = d.get("current_stage", "")
                                            failed = d.get("failed_stage", "")
                                            # 轻量检查运行状态
                                            is_running = bool(
                                                cur
                                                and cur not in terminal_states
                                                and not failed
                                                and lock_is_active(job_dir / ".autopilot.lock", stale_after=120.0)
                                            )
                                            if is_running:
                                                try:
                                                    from .background_worker import read_worker_record
                                                    record = read_worker_record(job_dir)
                                                    is_running = record is not None and record.finished_at is None
                                                except Exception:
                                                    is_running = False
                                            
                                            if cur == "FAILED_NEEDS_ATTENTION":
                                                status_text = "✗ 需人工处理"
                                            elif cur == "FAILED_RETRYABLE":
                                                status_text = "⚠ 可重试失败"
                                            elif st == "ready_to_publish" or cur == "COMPLETED":
                                                status_text = "✓ 已完成"
                                            elif failed:
                                                status_text = "✗ 失败/等待"
                                            elif cur and cur != "INIT":
                                                status_text = "▶ 进行中" if is_running else "⚠ 异常中断"
                                            else:
                                                status_text = "○ 待启动"
                                            
                                            raw = failed or cur or ""
                                            stage_text = self._translate_stage(raw) if raw else "-"
                                            ts = d.get("updated_at") or d.get("started_at") or ""
                                            if ts:
                                                updated = str(ts)[:16].replace("T", " ")
                                            else:
                                                updated = datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                                        except Exception:
                                            status_text = "状态异常"
                                    
                                    job_list_data.append({
                                        "job_id": status.job_id,
                                        "direction": direction_text,
                                        "status": status_text,
                                        "stage": stage_text,
                                        "updated": updated,
                                    })
                                except Exception:
                                    pass
                            self.ui_queue.put(("job_list", job_list_data))
                        except Exception:
                            pass
                    
                    # 休眠2秒，后台线程不占CPU
                    time.sleep(2.0)
                except Exception:
                    # 后台线程任何异常都不崩溃，休眠后继续
                    time.sleep(3.0)
        
        import threading
        self._bg_thread = threading.Thread(target=bg_poll_loop, daemon=True, name="AICF-BG-Poll")
        self._bg_thread.start()

    def _apply_job_list_update(self, job_list_data: list[dict]) -> None:
        """UI线程：应用后台线程准备好的任务列表数据（纯控件操作，无IO）。"""
        current_selection = self.job_tree.selection()
        self.job_tree.delete(*self.job_tree.get_children())
        for item in job_list_data:
            self.job_tree.insert(
                "", "end", iid=item["job_id"],
                values=(item["job_id"], item["direction"], item["status"], item["stage"], item["updated"]),
            )
        # 恢复之前选中的任务（如果还存在）
        selection_restored = False
        if current_selection:
            existing = [item for item in current_selection if self.job_tree.exists(item)]
            if existing:
                self.job_tree.selection_set(existing)
                self.job_tree.focus(existing[0])
                selection_restored = True
        self._highlight_selected_job()
        # 自动选中第一个任务（仅当没有选中项且用户没有手动选择过时），不触发日志重载
        if not selection_restored and not self.job_tree.selection() and not self._user_selected_job:
            children = self.job_tree.get_children()
            if children:
                first_job = children[0]
                self.job_tree.selection_set(first_job)
                self.job_tree.focus(first_job)
                # 标记为正在加载初始日志，避免重复
                self._loading_initial_logs = True
                self._on_job_select()
                self._loading_initial_logs = False
        self._update_button_states()

    def _get_log_tag(self, line: str) -> str:
        """根据日志内容判断标签颜色。"""
        line_lower = line.lower()
        if any(kw in line for kw in [" ERROR ", "错误", "失败", "Exception", "Traceback", "CRITICAL"]):
            return "error"
        if any(kw in line for kw in [" WARNING ", "警告", "Retry", "timeout", "超时"]):
            return "warning"
        if any(kw in line for kw in [" ✓ ", "成功", "完成", "passed", "SUCCESS"]):
            return "success"
        return ""

    def _update_display_job_stages(self) -> None:
        """根据 _display_job_id 更新进度条阶段颜色、状态文字。"""
        sp = self._get_status_path(self._display_job_id)
        if not sp.is_file():
            return
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            self._update_stages_from_status(data)
            cur = data.get("current_stage", "")
            failed = data.get("failed_stage", "")
            completed = data.get("completed_stages", [])
            stage_name = self._translate_stage(cur) if cur else ""
            is_running = self._is_job_display_running(self._display_job_id, data)

            # 状态栏文字
            if self._polling_job_id and self._polling_job_id != self._display_job_id:
                # 用户在看历史任务，但有其他任务在后台运行
                run_stage = ""
                rsp = self._get_status_path(self._polling_job_id)
                if rsp.is_file():
                    try:
                        rdata = json.loads(rsp.read_text(encoding="utf-8"))
                        rc = rdata.get("current_stage", "")
                        run_stage = self._translate_stage(rc) if rc else ""
                    except Exception:
                        pass
                self._set_status(f"后台运行: {run_stage} | 查看: {self._display_job_id}")
            elif cur == "FAILED_NEEDS_ATTENTION":
                self._set_status("失败，需人工处理")
            elif cur == "FAILED_RETRYABLE":
                failed_name = self._translate_stage(failed) if failed else stage_name
                self._set_status(f"[{failed_name}] 可重试失败，点击继续/恢复")
            elif failed:
                failed_name = self._translate_stage(failed)
                self._set_status(f"运行中: {stage_name}（{failed_name} 失败，等待恢复）")
            elif cur == "COMPLETED" or data.get("status") == "ready_to_publish":
                self._set_status("完成 ✓")
            elif is_running:
                self._set_status(f"运行中: {stage_name}")
            else:
                self._set_status(f"任务 [{self._display_job_id}] {stage_name}")

            # 运行中任务的阶段完成日志
            if self._polling_job_id == self._display_job_id:
                for s in completed:
                    if s not in self._logged_stages:
                        s_name = self._translate_stage(s)
                        self._log(f"✓ {s_name} 完成", "success")
                        self._logged_stages.add(s)
                if (
                    cur
                    and cur not in self._logged_stages
                    and cur not in ("COMPLETED", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION")
                ):
                    self._log(f"→ 进入阶段: {stage_name}", "info")
                    self._logged_stages.add(cur)
        except Exception:
            pass

    def _tail_running_job_logs(self) -> None:
        """增量读取运行中任务的日志文件并输出到日志区。"""
        if not self._polling_job_id:
            return
        sp = self._get_status_path(self._polling_job_id)
        if not sp.is_file():
            return
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            self._tail_log_files(data)
        except Exception:
            pass

    def _is_job_display_running(self, job_id: str, data: dict) -> bool:
        """判断一个任务是否真正在运行（用于显示状态）。"""
        cur = data.get("current_stage", "")
        failed = data.get("failed_stage", "")
        terminal = {"COMPLETED", "INIT", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION"}
        if not cur or cur in terminal or failed:
            return False
        job_dir = self._get_job_dir(job_id)
        return self._is_job_really_running(job_dir, data)

    def _sync_display_after_refresh(self) -> None:
        """刷新任务列表后，确保选中项和显示状态同步。"""
        sel = self.job_tree.selection()
        if sel:
            job_id = str(sel[0])
            if self._display_job_id != job_id:
                self._display_job_id = job_id
        elif self._display_job_id and self.job_tree.exists(self._display_job_id):
            self.job_tree.selection_set(self._display_job_id)
            self.job_tree.focus(self._display_job_id)
            self._highlight_selected_job()

    def _refresh_status_for_job(self, job_id: str) -> None:
        """刷新指定任务的阶段进度显示。"""
        sp = self._get_status_path(job_id)
        if not sp.is_file():
            return
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            self._update_stages_from_status(data)
        except Exception:
            pass

    def _new_job(self) -> None:
        """退出历史任务查看状态，准备一个不会覆盖旧结果的新任务。"""
        for item in self.job_tree.selection():
            self.job_tree.selection_remove(item)
        self.job_tree.focus("")
        self._highlight_selected_job()
        self._display_job_id = ""
        self._user_selected_job = False
        self.job_id_var.set(self._auto_job_id())
        self._reset_stages()
        try:
            self._apply_production_settings(load_default_settings())
        except Exception:
            pass
        self.direction_text.delete("1.0", "end")
        default_direction = self._load_default_direction()
        if default_direction:
            self.direction_text.insert("1.0", default_direction)
            self.direction_text.configure(foreground="#111827")
            self._direction_has_placeholder = False
        else:
            self._show_direction_placeholder()
        self.job_id_entry.focus_set()
        self._update_button_states()
        self._show_current_job_guidance()

    def _on_job_id_keypress(self, event: object) -> None:
        """用户编辑任务 ID 时切换到新任务状态，避免自动刷新覆盖输入。"""
        keysym = str(getattr(event, "keysym", ""))
        char = str(getattr(event, "char", ""))
        state = int(getattr(event, "state", 0))
        navigation_keys = {
            "Left",
            "Right",
            "Home",
            "End",
            "Tab",
            "Return",
            "Escape",
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
        }
        is_control_shortcut = bool(state & 0x4) and keysym.lower() not in {
            "v",
            "x",
        }
        is_edit = (
            bool(char)
            or keysym in {"BackSpace", "Delete"}
            or (bool(state & 0x4) and keysym.lower() in {"v", "x"})
        )
        if keysym in navigation_keys or is_control_shortcut or not is_edit:
            return
        self._enter_job_id_edit_mode()

    def _on_job_id_focus(self, _event: object) -> None:
        """输入框获得焦点即退出历史任务查看，兼容粘贴和输入法。"""
        self._enter_job_id_edit_mode()

    def _enter_job_id_edit_mode(self) -> None:
        selected = self.job_tree.selection()
        if not selected:
            return
        for item in selected:
            self.job_tree.selection_remove(item)
        self.job_tree.focus("")
        self._highlight_selected_job()
        self._display_job_id = ""
        self._user_selected_job = False
        self._reset_stages()
        self.root.after_idle(self._update_button_states)

    def _start_job(self) -> None:
        self._logged_stages.clear()
        self._log_file_offsets.clear()
        job_id = self.job_id_var.get().strip() or self._auto_job_id()
        # 校验任务ID：禁止Windows文件非法字符和控制字符，允许中文、字母、数字、下划线、短横线等
        import re
        _INVALID_JOB_ID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
        if (
            not job_id
            or job_id != job_id.strip()
            or job_id in {".", ".."}
            or _INVALID_JOB_ID.search(job_id)
        ):
            messagebox.showwarning(
                "任务ID无效",
                "任务ID不能为空，不能包含前后空格，不能是 . 或 ..，\n"
                "且不能包含以下Windows文件非法字符：< > : \" / \\ | ? *"
            )
            return
        if self._job_id_taken(job_id):
            messagebox.showwarning(
                "任务ID已存在",
                f"任务 [{job_id}] 已存在，为避免覆盖原结果，本次未启动。\n\n"
                "如需继续旧任务，请点击“继续/恢复”；"
                "如需制作新视频，请点击“新建任务”。",
            )
            self._refresh_job_list()
            if self.job_tree.exists(job_id):
                self.job_tree.selection_set(job_id)
                self.job_tree.focus(job_id)
                self._on_job_select()
            return
        # 校验内容方向不为空
        if self._is_showing_placeholder() or not self._get_direction_content():
            messagebox.showwarning("请填写内容方向", "请在「内容方向」文本框中填写视频的主题、风格和关键信息，\n这将帮助AI生成更符合您预期的视频内容。")
            self.direction_text.focus_set()
            return
        self.job_id_var.set(job_id)
        # 新任务启动，显示切到新任务
        self._display_job_id = job_id
        self._user_selected_job = False
        # 清空日志并准备实时跟踪
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_file_offsets.clear()
        self._polling_job_id = job_id
        try:
            repo = self._get_repo()
            settings = self._collect_production_settings()
            if settings is None:
                return
            self._ensure_direction_file()
            repo.create_job(job_id, self._get_job_dir(job_id))
            settings.save_for_job(self._get_job_dir(job_id))
        except ValueError as error:
            messagebox.showerror("生产设置无效", str(error))
            return
        self._log(f"任务 [{job_id}] 开始生成", "info")
        self._run_command_async(
            worker_start_command(job_id),
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _resume_job(self) -> None:
        self._logged_stages.clear()
        self._log_file_offsets.clear()
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先在历史任务中选择一个任务，或在任务ID框中输入")
            return
        self._display_job_id = job_id
        self._user_selected_job = False
        self._load_job_production_settings(job_id)
        # 清空日志并准备实时跟踪
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._polling_job_id = job_id
        self._log(f"任务 [{job_id}] 继续/恢复", "info")
        self._run_command_async(
            worker_start_command(job_id),
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _retry_research(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择资料研究失败的任务")
            return
        try:
            status = self._get_repo().get_job(job_id)
        except KeyError:
            messagebox.showerror("无法重试", f"任务 [{job_id}] 不存在")
            return
        if (
            status.current_stage.value != "FAILED_RETRYABLE"
            or status.failed_stage is None
            or status.failed_stage.value != "RESEARCHED"
        ):
            messagebox.showwarning("无法重试", "当前任务不是资料研究失败状态")
            return
        marker_path = self._get_job_dir(job_id) / "research_retry_request.json"
        atomic_write_text(
            marker_path,
            json.dumps(
                {"reason": "user_retry"},
                ensure_ascii=False,
                indent=2,
            ),
        )
        self._log(f"任务 [{job_id}] 重新搜索资料", "info")
        self._resume_job()

    def _show_research_failure_details(self) -> None:
        actions = self._current_job_actions()
        if not actions.can_view_research_failure:
            messagebox.showwarning("提示", "当前任务没有资料研究失败详情")
            return
        messagebox.showinfo(
            "资料研究失败详情",
            actions.guidance
            + "\n\n重新搜索会避开已经确认失效的网址，"
            "并保留已完成的方向分析和选题。",
        )

    def _stop_job(self) -> None:
        job_id = self._polling_job_id or self._current_job_id()
        record = read_worker_record(self._get_job_dir(job_id)) if job_id else None
        if record and record.finished_at is None:
            if not messagebox.askyesno(
                "确认停止",
                "确定要停止当前正在运行的任务吗？\n已生成的部分资源会被保留。",
            ):
                return
            pid = record.pid
            self._log(f"正在停止任务 (PID={pid})...", "info")
            try:
                stop_worker(self._get_job_dir(job_id))
            except WorkerIdentityError as error:
                self._log(f"正常停止失败: {error}", "warning")
                # 身份校验失败时，提供强制清理选项
                if messagebox.askyesno(
                    "强制清理",
                    "检测到Worker进程异常（可能已崩溃或PID被复用）。\n"
                    "是否强制清理任务状态？\n\n"
                    "这将标记任务为停止状态并允许创建新任务。",
                    icon="warning",
                ):
                    try:
                        # 清理.autopilot.lock
                        lock_file = self._get_job_dir(job_id) / ".autopilot.lock"
                        if lock_file.exists():
                            lock_file.unlink()
                        # 强制清理worker记录
                        force_kill_worker(self._get_job_dir(job_id))
                        # 更新status.json - 标记为可恢复的失败状态
                        sp = self._get_job_dir(job_id) / "status.json"
                        if sp.is_file():
                            try:
                                d = json.loads(sp.read_text(encoding="utf-8"))
                                # 正确识别真正失败的阶段（避免把状态名当成阶段名）
                                terminal_states = {"COMPLETED", "INIT", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION"}
                                failed_stage = d.get("current_stage", "UNKNOWN")
                                if failed_stage in terminal_states:
                                    # 当前是状态名，需要找真正失败的阶段
                                    stages = d.get("stages", {})
                                    completed = set(d.get("completed_stages", []))
                                    # 找第一个started但未completed的阶段
                                    found = None
                                    for stage_enum in [s[0].value for s in STAGES]:
                                        if stage_enum in stages and stage_enum not in completed:
                                            stage_data = stages[stage_enum]
                                            if isinstance(stage_data, dict) and stage_data.get("started_at"):
                                                found = stage_enum
                                                break
                                    if found:
                                        failed_stage = found
                                    elif d.get("failed_stage") and d["failed_stage"] not in terminal_states:
                                        failed_stage = d["failed_stage"]
                                d["failed_stage"] = failed_stage
                                d["current_stage"] = "FAILED_RETRYABLE"
                                d["last_error"] = "任务被用户强制停止，可点击继续/恢复"
                                # 标记当前失败阶段为可恢复
                                stages = d.get("stages", {})
                                if isinstance(stages, dict) and failed_stage in stages:
                                    stages[failed_stage]["recoverable"] = True
                                atomic_write_text(sp, json.dumps(d, ensure_ascii=False, indent=2))
                            except Exception:
                                pass
                        self._log("已强制清理任务状态，可以点击继续/恢复", "info")
                        messagebox.showinfo("清理完成", "任务状态已清理，可以点击「继续/恢复」从断点继续，或创建新任务。")
                        self._refresh_job_list()
                        self._update_button_states()
                    except Exception as e:
                        self._log(f"强制清理失败: {e}", "error")
                        messagebox.showerror("清理失败", str(e))
                return
            except OSError as error:
                self._log(f"停止Worker失败: {error}", "error")
                messagebox.showerror("停止失败", str(error))
                return
            self._log("已发送停止信号", "info")

    def _current_job_id(self) -> str:
        sel = self.job_tree.selection()
        if sel:
            return str(sel[0])
        return self.job_id_var.get().strip()

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def _refresh_all(self) -> None:
        """立即刷新：给后台线程发信号，不做任何IO（UI线程纯操作）。"""
        self._set_status("正在刷新...")
        self._force_refresh_event.set()  # 通知后台线程立即刷新
        self._set_status("刷新请求已发送")

    def _refresh_status(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            self._reset_stages()
            return
        status_path = self._get_status_path(job_id)
        if not status_path.is_file():
            self._reset_stages()
            self._set_status(f"任务 {job_id} 尚无状态文件")
            return
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"读取状态失败: {e}", "error")
            return
        self._update_stages_from_status(data)
        cur = data.get("current_stage", "")
        failed = data.get("failed_stage", "")
        job_dir = self._get_job_dir(job_id)
        is_running = self._is_job_really_running(job_dir, data)
        if cur == "COMPLETED" or data.get("status") == "ready_to_publish":
            self._set_status(f"任务 {job_id} 已完成 ✓")
        elif cur == "FAILED_NEEDS_ATTENTION":
            self._set_status(f"任务 {job_id} 失败，需人工处理")
        elif cur == "FAILED_RETRYABLE":
            self._set_status(f"任务 {job_id} 可重试失败，点击继续/恢复")
        elif failed:
            failed_name = self._translate_stage(failed)
            self._set_status(f"任务 {job_id} 在 [{failed_name}] 失败/等待恢复")
        elif cur and cur != "INIT" and not is_running:
            self._set_status(f"任务 {job_id} 异常中断，点击继续/恢复")
        else:
            stage_name = self._translate_stage(cur) if cur else "-"
            self._set_status(f"任务 {job_id} 当前阶段: {stage_name}")

    def _reset_stages(self) -> None:
        for lbl in self.stage_labels:
            lbl.configure(style="Stage.TLabel")

    def _update_stages_from_status(self, data: dict) -> None:
        completed = set(data.get("completed_stages", []))
        current = data.get("current_stage", "")
        failed = data.get("failed_stage", "")
        self._reset_stages()
        for i, (stage, _) in enumerate(STAGES):
            sname = stage.value if hasattr(stage, "value") else str(stage)
            if sname == failed:
                self.stage_labels[i].configure(style="StageFail.TLabel")
            elif sname in completed or sname == PipelineStage.COMPLETED.value:
                self.stage_labels[i].configure(style="StageDone.TLabel")
            elif sname == current:
                self.stage_labels[i].configure(style="StageActive.TLabel")

    def _translate_stage(self, stage_value: str) -> str:
        """将 PipelineStage 值转为中文名称。"""
        for stage, name in STAGES:
            if stage.value == stage_value:
                return name
        return stage_value

    def _is_job_really_running(self, job_dir: Path, data: dict, *, light_check: bool = False) -> bool:
        """按统一 PID/时间/心跳协议判断任务是否运行，不修改锁文件。
        
        判断逻辑：
        1. 锁文件是否活跃（120秒内心跳）
        2. Worker记录是否存在且未标记为finished
        3. Worker进程PID是否真实存在且身份匹配
        
        Args:
            light_check: 轻量检查模式，只检查锁文件和worker记录，不做完整进程身份验证（用于列表刷新避免UI卡顿）
        """
        del data
        # 首先检查锁文件
        if not lock_is_active(
            job_dir / ".autopilot.lock",
            stale_after=120.0,
        ):
            return False
        
        # 锁文件活跃的情况下，检查worker记录
        from .background_worker import _identity_matches, read_worker_record
        
        record = read_worker_record(job_dir)
        if record is None or record.finished_at is not None:
            return False
        
        if light_check or not record.pid:
            # 轻量检查：锁文件活跃 + 记录未完成，即视为运行中（列表刷新用）
            return True
        
        # 完整检查：验证进程PID真实存在且身份匹配（带缓存避免重复系统调用）
        from .process_identity import get_process_identity
        
        # 简单的PID存在性检查缓存（5秒内有效）
        cache_key = f"pid_check_{record.pid}"
        now_ts = time.time()
        cached = self._pid_cache.get(cache_key)
        if cached is not None and now_ts - cached[0] < 5.0:
            return cached[1]
        
        try:
            identity = get_process_identity(record.pid)
            result = _identity_matches(record, identity)
        except Exception:
            result = False
        
        self._pid_cache[cache_key] = (now_ts, result)
        return result

    def _highlight_selected_job(self) -> None:
        """给历史任务列表中的当前选中项加显式高亮。"""
        selected = set(self.job_tree.selection())
        for item in self.job_tree.get_children():
            if item in selected:
                self.job_tree.item(item, tags=("selected_row",))
            else:
                self.job_tree.item(item, tags=())

    def _refresh_job_list(self) -> None:
        current_selection = self.job_tree.selection()
        self.job_tree.delete(*self.job_tree.get_children())
        for status in self._get_repo().list_jobs():
            job_dir = Path(status.output_dir)
            sp = job_dir / "status.json"
            status_text = "未开始"
            stage_text = "-"
            updated = "-"
            direction_text = "-"
            
            # 读取内容方向信息
            dp = job_dir / "direction.json"
            if dp.is_file():
                try:
                    direction_data = json.loads(dp.read_text(encoding="utf-8"))
                    series_name = direction_data.get("series_name", "")
                    core_direction = direction_data.get("core_direction", "")
                    if series_name:
                        direction_text = series_name
                    elif core_direction:
                        # 核心方向较长，截断显示
                        direction_text = core_direction[:20] + "..." if len(core_direction) > 20 else core_direction
                except Exception:
                    direction_text = "-"
            
            if sp.is_file():
                try:
                    d = json.loads(sp.read_text(encoding="utf-8"))
                    st = d.get("status", "")
                    cur = d.get("current_stage", "")
                    failed = d.get("failed_stage", "")
                    # 列表刷新用轻量检查：避免对每个任务做完整进程身份验证导致UI卡死
                    is_really_running = self._is_job_really_running(job_dir, d, light_check=True)
                    # 失败状态优先显示
                    if cur == "FAILED_NEEDS_ATTENTION":
                        status_text = "✗ 需人工处理"
                    elif cur == "FAILED_RETRYABLE":
                        status_text = "⚠ 可重试失败"
                    elif st == "ready_to_publish" or cur == "COMPLETED":
                        status_text = "✓ 已完成"
                    elif failed:
                        status_text = "✗ 失败/等待"
                    elif cur and cur != "INIT":
                        if is_really_running:
                            status_text = "▶ 进行中"
                        else:
                            # 进程已死但状态未更新（僵尸任务）
                            status_text = "⚠ 异常中断"
                    else:
                        status_text = "⏸ 已初始化"
                    # 翻译阶段名称为中文
                    raw = failed or cur or ""
                    stage_text = self._translate_stage(raw) if raw else "-"
                    ts = d.get("updated_at") or d.get("started_at") or ""
                    if ts:
                        updated = str(ts)[:16].replace("T", " ")
                    else:
                        updated = datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    status_text = "状态异常"

            self.job_tree.insert(
                "", "end", iid=status.job_id,
                values=(status.job_id, direction_text, status_text, stage_text, updated),
            )
        if current_selection:
            existing = [item for item in current_selection if self.job_tree.exists(item)]
            if existing:
                self.job_tree.selection_set(existing)
                self.job_tree.focus(existing[0])
        self._highlight_selected_job()

    def _read_tail_lines(self, file_path: Path, max_lines: int) -> list[str]:
        """高效读取文件最后N行，避免加载整个大文件。"""
        try:
            with open(file_path, "rb") as f:
                # 从文件末尾向前找换行符
                block_size = 8192
                f.seek(0, 2)  # 移动到文件末尾
                file_size = f.tell()
                blocks: list[bytes] = []
                lines_found = 0
                block_end = file_size
                
                while block_end > 0 and lines_found <= max_lines:
                    block_start = max(0, block_end - block_size)
                    f.seek(block_start)
                    block = f.read(block_end - block_start)
                    blocks.append(block)
                    lines_found += block.count(b'\n')
                    block_end = block_start
                
                # 反向拼接，按行分割，取最后max_lines行
                content = b''.join(reversed(blocks))
                lines = content.decode('utf-8', errors='replace').splitlines()
                return lines[-max_lines:] if len(lines) > max_lines else lines
        except Exception:
            return []

    def _load_job_logs(self, job_id: str) -> None:
        """加载指定任务的完整日志到日志面板（用于查看历史任务）。"""
        job_dir = self._get_job_dir(job_id)
        if not job_dir.is_dir():
            return
        
        # 清空当前日志
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        # 重置日志偏移
        for key in list(self._log_file_offsets.keys()):
            if key.startswith(f"{job_id}/"):
                del self._log_file_offsets[key]
        
        self._log(f"{'='*60}", "info")
        self._log(f"任务 [{job_id}] 日志", "info")
        self._log(f"{'='*60}", "info")
        
        # 读取status.json获取阶段信息
        sp = self._get_status_path(job_id)
        data = {}
        if sp.is_file():
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        # 读取worker.log - 兼容多个可能的位置
        worker_log_candidates = [
            job_dir / "_work" / "runtime" / "worker.log",
            job_dir / "worker.log",
            job_dir / "logs" / "worker.log",
        ]
        worker_log = None
        for candidate in worker_log_candidates:
            if candidate.is_file():
                worker_log = candidate
                break
        if worker_log is not None:
            try:
                # 只读最后500行，避免大文件卡死UI
                lines = self._read_tail_lines(worker_log, 500)
                self._log_file_offsets[f"{job_id}/worker.log"] = worker_log.stat().st_size
                self._log("--- Worker 主日志（最近500行） ---", "info")
                for line in lines:
                    if line.strip():
                        self._log_raw(line)
            except Exception as e:
                self._log(f"读取worker.log失败: {e}", "error")
        
        # 读取各阶段日志 - 兼容 _work/runtime/ 和 根目录/logs/ 两种路径
        runtime_dir_candidates = [
            job_dir / "_work" / "runtime",
            job_dir,
            job_dir / "logs",
        ]
        stages_info = data.get("stages", {})
        if isinstance(stages_info, dict):
            for stage_key, stage_info in stages_info.items():
                if not isinstance(stage_info, dict):
                    continue
                log_rel = stage_info.get("log_path")
                if not isinstance(log_rel, str) or not log_rel:
                    continue
                # 在多个候选目录中查找日志文件
                log_path = None
                for base_dir in runtime_dir_candidates:
                    candidate = base_dir / log_rel
                    if candidate.is_file():
                        log_path = candidate
                        break
                if log_path is None:
                    continue
                try:
                    # 每个阶段日志只读最后200行
                    lines = self._read_tail_lines(log_path, 200)
                    self._log_file_offsets[f"{job_id}/{log_rel}"] = log_path.stat().st_size
                    stage_name = self._translate_stage(stage_key)
                    self._log(f"--- 阶段: {stage_name}（最近200行） ---", "info")
                    for line in lines:
                        if line.strip():
                            self._log_raw(line)
                except Exception:
                    pass
        
        # 如果任务失败，显示错误摘要
        failed_stage = data.get("failed_stage", "")
        error_msg = ""
        if failed_stage and isinstance(stages_info, dict):
            stage_info = stages_info.get(failed_stage, {})
            if isinstance(stage_info, dict):
                error_msg = stage_info.get("error", "")
        
        if error_msg:
            self._log("", "")
            self._log(f"{'!'*60}", "error")
            self._log(f"任务失败于阶段: {failed_stage}", "error")
            self._log(f"错误信息: {sanitize_error(error_msg)}", "error")
            self._log(f"{'!'*60}", "error")
        
        # 滚动到顶部方便查看
        self.log_text.see("1.0")

    def _on_job_select(self, _event: object = None) -> None:
        sel = self.job_tree.selection()
        if sel:
            self._highlight_selected_job()
            job_id = str(sel[0])
            # 防抖：如果选中的是同一个任务且已经加载过日志，跳过重复IO
            if job_id == getattr(self, '_last_loaded_job_id', None) and not self._loading_initial_logs:
                self.job_id_var.set(job_id)
                self._update_button_states()
                self._show_current_job_guidance()
                return
            self._last_loaded_job_id = job_id
            self.job_id_var.set(job_id)
            # 用户手动选中任务，进度条显示该任务的状态
            self._display_job_id = job_id
            self._user_selected_job = True
            # 注意：_load_job_production_settings、_refresh_status_for_job、_load_job_logs 都是IO操作
            # 这里会有短暂阻塞，但用户主动点击时可以接受
            self._load_job_production_settings(job_id)
            self._refresh_status_for_job(job_id)
            sp = self._get_status_path(job_id)
            if sp.is_file():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    job_dir = self._get_job_dir(job_id)
                    is_running = self._is_job_display_running(job_id, data)
                    if is_running:
                        self._set_buttons_running(True)
                        # 正在运行的任务：先加载已有日志，然后继续实时跟踪
                        self._polling_job_id = ""  # 先清空，避免_tail重复追加
                        self._log_file_offsets.clear()
                        self._load_job_logs(job_id)
                        # 设置偏移为当前文件末尾，避免重复读取 - 兼容多路径
                        worker_log_candidates = [
                            job_dir / "_work" / "runtime" / "worker.log",
                            job_dir / "worker.log",
                            job_dir / "logs" / "worker.log",
                        ]
                        for wlog in worker_log_candidates:
                            if wlog.is_file():
                                self._log_file_offsets[f"{job_id}/worker.log"] = wlog.stat().st_size
                                break
                        stages_info = data.get("stages", {})
                        runtime_dir_candidates = [
                            job_dir / "_work" / "runtime",
                            job_dir,
                            job_dir / "logs",
                        ]
                        if isinstance(stages_info, dict):
                            for stage_info in stages_info.values():
                                if isinstance(stage_info, dict):
                                    log_rel = stage_info.get("log_path")
                                    if isinstance(log_rel, str) and log_rel:
                                        for base_dir in runtime_dir_candidates:
                                            log_path = base_dir / log_rel
                                            if log_path.is_file():
                                                self._log_file_offsets[f"{job_id}/{log_rel}"] = log_path.stat().st_size
                                                break
                        self._polling_job_id = job_id
                    else:
                        self._set_buttons_running(self._polling_job_id != "")
                        # 如果是已完成/失败的任务，加载完整历史日志
                        self._polling_job_id = ""
                        self._load_job_logs(job_id)
                except Exception:
                    pass
            self._update_button_states()
            self._show_current_job_guidance()

    def _open_output(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            # 没有选中任务时，打开jobs根目录
            path = project_root() / "data" / "jobs"
        else:
            path = self._get_job_dir(job_id)
            if not path.is_dir():
                path = project_root() / "data" / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # type: ignore[attr-defined]

    def _open_final_video(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择任务")
            return
        video = final_video_for_job(self._get_job_dir(job_id))
        if video is None:
            messagebox.showwarning("提示", "所选任务尚无最终视频，请等待任务完成")
            return
        os.startfile(str(video))  # type: ignore[attr-defined]

    def _show_job_context_menu(self, event: object) -> None:
        """右键点击历史任务时弹出上下文菜单。"""
        item = self.job_tree.identify_row(event.y)  # type: ignore[attr-defined]
        if item:
            self.job_tree.selection_set(item)
            self.job_tree.focus(item)
            self._on_job_select()
            try:
                self.job_context_menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]
            finally:
                self.job_context_menu.grab_release()

    def _delete_selected_job(self) -> None:
        """删除选中任务的数据库记录、工作文件和用户交付文件。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择要删除的任务")
            return
        job_dir = self._get_job_dir(job_id)
        # 如果任务正在运行（PID真实存在），不允许删除
        sp = job_dir / "status.json"
        is_running = False
        if sp.is_file():
            try:
                d = json.loads(sp.read_text(encoding="utf-8"))
                is_running = self._is_job_really_running(job_dir, d)
            except Exception:
                pass
        if is_running:
            messagebox.showwarning("提示", "任务正在运行中，请先停止后再删除")
            return
        # 收集要删除的路径：任务目录本身（包含outputs）
        locations = [str(job_dir)] if job_dir.exists() else []
        location_text = "\n".join(locations) if locations else "任务文件已不存在，仅清理列表记录"
        confirm = messagebox.askyesno(
            "确认删除",
            f"确定要删除任务 [{job_id}] 吗？\n\n"
            f"将清理：\n{location_text}\n\n此操作不可恢复。",
        )
        if not confirm:
            return
        try:
            self._get_repo().delete_job(job_id)
            cleanup_errors: list[str] = []
            if job_dir.is_dir():
                try:
                    shutil.rmtree(job_dir)
                except OSError as error:
                    cleanup_errors.append(f"{job_dir}: {error}")
            self._log(f"已删除任务: {job_id}", "info")
            self._refresh_job_list()
            self._reset_stages()
            self._display_job_id = ""
            self._user_selected_job = False
            self._update_button_states()
            if cleanup_errors:
                self._set_status(f"任务 [{job_id}] 已从列表删除，部分文件需手工清理")
                messagebox.showwarning(
                    "任务记录已删除",
                    "任务已从列表中移除，但以下文件未能自动删除：\n\n"
                    + "\n".join(cleanup_errors),
                )
            else:
                self._set_status(f"任务 [{job_id}] 已彻底删除")
        except Exception as e:
            messagebox.showerror("错误", f"删除失败: {e}")

    def _open_job_dir(self) -> None:
        """打开选中任务的目录。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择一个任务")
            return
        job_dir = self._get_job_dir(job_id)
        if not job_dir.is_dir():
            messagebox.showwarning("提示", f"任务目录不存在: {job_dir}")
            return
        try:
            os.startfile(str(job_dir))
        except Exception as e:
            messagebox.showerror("错误", f"打开目录失败: {e}")

    def _force_clean_job(self) -> None:
        """右键菜单直接强制清理僵尸任务。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择一个任务")
            return
        job_dir = self._get_job_dir(job_id)
        if not job_dir.is_dir():
            messagebox.showwarning("提示", f"任务目录不存在: {job_dir}")
            return
        
        # 检查是否真的是僵尸任务
        sp = job_dir / "status.json"
        if sp.is_file():
            try:
                d = json.loads(sp.read_text(encoding="utf-8"))
                is_running = self._is_job_really_running(job_dir, d)
                if is_running:
                    if not messagebox.askyesno(
                        "任务正在运行",
                        "检测到任务进程真实存在且正在运行！\n"
                        "强制清理可能导致进程残留，是否仍要继续？",
                        icon="warning",
                    ):
                        return
            except Exception:
                pass
        
        confirm = messagebox.askyesno(
            "强制清理僵尸任务",
            f"将强制清理任务 [{job_id}] 的运行状态，\n"
            "清理后可以点击「继续/恢复」从断点继续。\n\n"
            "是否继续？",
            icon="warning",
        )
        if not confirm:
            return
        
        try:
            # 清理.autopilot.lock
            lock_file = job_dir / ".autopilot.lock"
            if lock_file.exists():
                lock_file.unlink()
            # 强制清理worker记录
            force_kill_worker(job_dir)
            # 更新status.json - 标记为可恢复
            if sp.is_file():
                try:
                    d = json.loads(sp.read_text(encoding="utf-8"))
                    # 正确识别真正失败的阶段（避免把状态名当成阶段名）
                    terminal_states = {"COMPLETED", "INIT", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION"}
                    failed_stage = d.get("current_stage", "UNKNOWN")
                    if failed_stage in terminal_states:
                        stages = d.get("stages", {})
                        completed = set(d.get("completed_stages", []))
                        found = None
                        for stage_enum in [s[0].value for s in STAGES]:
                            if stage_enum in stages and stage_enum not in completed:
                                stage_data = stages[stage_enum]
                                if isinstance(stage_data, dict) and stage_data.get("started_at"):
                                    found = stage_enum
                                    break
                        if found:
                            failed_stage = found
                        elif d.get("failed_stage") and d["failed_stage"] not in terminal_states:
                            failed_stage = d["failed_stage"]
                    d["failed_stage"] = failed_stage
                    d["current_stage"] = "FAILED_RETRYABLE"
                    d["last_error"] = "用户手动强制清理，可点击继续/恢复"
                    stages = d.get("stages", {})
                    if isinstance(stages, dict) and failed_stage in stages:
                        stages[failed_stage]["recoverable"] = True
                    atomic_write_text(sp, json.dumps(d, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            self._log(f"已强制清理任务 [{job_id}]", "info")
            messagebox.showinfo("清理完成", "任务状态已清理，可以点击「继续/恢复」从断点继续。")
            self._refresh_job_list()
            self._update_button_states()
        except Exception as e:
            self._log(f"强制清理失败: {e}", "error")
            messagebox.showerror("清理失败", str(e))

    def _tail_log_files(self, data: dict) -> None:
        """增量读取当前及已完成阶段的日志文件，追加到运行日志窗口。"""
        job_id = self._polling_job_id
        if not job_id:
            return
        job_dir = self._get_job_dir(job_id)
        stages_info = data.get("stages", {})
        if not isinstance(stages_info, dict):
            return
        
        # worker.log - 兼容多个位置
        worker_log_candidates = [
            job_dir / "_work" / "runtime" / "worker.log",
            job_dir / "worker.log",
            job_dir / "logs" / "worker.log",
        ]
        worker_log = None
        for candidate in worker_log_candidates:
            if candidate.is_file():
                worker_log = candidate
                break
        if worker_log is not None:
            cache_key = f"{job_id}/worker.log"
            last_pos = self._log_file_offsets.get(cache_key, 0)
            try:
                size = worker_log.stat().st_size
                if size < last_pos:
                    last_pos = 0
                if size > last_pos:
                    with worker_log.open(
                        "r", encoding="utf-8", errors="replace"
                    ) as handle:
                        handle.seek(last_pos)
                        content = handle.read()
                    self._log_file_offsets[cache_key] = size
                    for line in content.splitlines():
                        if line.strip():
                            self._log_raw(line.rstrip())
            except OSError:
                pass
        
        # 读取所有有 log_path 的阶段日志 - 兼容多个目录位置
        runtime_dir_candidates = [
            job_dir / "_work" / "runtime",
            job_dir,
            job_dir / "logs",
        ]
        for stage_key, stage_info in stages_info.items():
            if not isinstance(stage_info, dict):
                continue
            log_rel = stage_info.get("log_path")
            if not isinstance(log_rel, str) or not log_rel:
                continue
            log_path = None
            for base_dir in runtime_dir_candidates:
                candidate = base_dir / log_rel
                if candidate.is_file():
                    log_path = candidate
                    break
            if log_path is None:
                continue
            cache_key = f"{job_id}/{log_rel}"
            last_pos = self._log_file_offsets.get(cache_key, 0)
            try:
                fsize = log_path.stat().st_size
                if fsize < last_pos:
                    last_pos = 0  # 文件被截断或重新创建
                if fsize > last_pos:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        new_content = f.read()
                    self._log_file_offsets[cache_key] = fsize
                    if new_content.strip():
                        for line in new_content.strip().splitlines():
                            line = line.rstrip()
                            if line:
                                self._log_raw(line)
            except Exception:
                pass

    def _open_model_selector(self) -> None:
        """打开 OpenRouter 模型选择窗口。"""
        dialog = ModelSelectionDialog(self.root)
        self.root.after(500, lambda: self._sync_model_label(dialog))

    def _open_settings(self, *, first_time: bool = False) -> None:
        """打开集中设置对话框。
        
        Args:
            first_time: 是否是首次启动引导模式
        """
        def on_saved() -> None:
            # 设置保存后，刷新环境状态和模型标签
            self._run_doctor()
            current_model = _get_env_value("OPENROUTER_MODEL") or "未设置"
            self.model_label.configure(text=current_model)
            # 重新检测可用的视频提供商
            self._available_providers = self._detect_video_providers()
            if self._available_providers:
                provider_display_values = tuple(
                    VIDEO_PROVIDER_DISPLAY_NAMES.get(p, p) for p in self._available_providers
                )
                self.provider_combo.configure(values=provider_display_values, state="readonly")
                # 如果当前选中的provider不再可用，切换到第一个可用的
                current_provider = self._get_selected_provider()
                if current_provider not in self._available_providers:
                    new_provider = self._available_providers[0]
                    self.video_provider_var.set(new_provider)
                    self.provider_display_var.set(
                        VIDEO_PROVIDER_DISPLAY_NAMES.get(new_provider, new_provider)
                    )
                    self._refresh_model_combo_options()
            else:
                self.provider_combo.configure(values=[], state="disabled")
                self.provider_combo.set("未配置")
            # 更新按钮状态
            self._update_button_states()

        open_settings(self.root, on_saved=on_saved, first_time=first_time)

    def _needs_initial_setup(self) -> bool:
        """检查是否需要初始配置（首次使用）。仅做快速本地检查，不做网络请求。"""
        # 检查 API Key
        api_key = _get_env_value("OPENROUTER_API_KEY")
        if not api_key:
            return True
        # 检查 FFmpeg
        try:
            from .providers.tts import discover_ffmpeg_toolchain
            discover_ffmpeg_toolchain()
        except Exception:
            return True
        # 检查视频服务CLI文件是否存在（至少一个）
        quick_providers = self._quick_detect_providers()
        if not quick_providers:
            return True
        return False

    def _sync_model_label(self, dialog: ModelSelectionDialog) -> None:
        """同步模型标签（模型选择窗口关闭后更新）。"""
        if dialog.win.winfo_exists():
            self.root.after(300, lambda: self._sync_model_label(dialog))
        else:
            model = _get_env_value("OPENROUTER_MODEL") or "未设置"
            self.model_label.configure(text=model)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        self._stop_preview()
        self.root.destroy()

    def run(self) -> None:
        # 启动后台IO轮询线程（所有文件IO、状态检测、日志读取都在后台）
        self._start_background_poll_thread()
        # 启动UI消息轮询（每100ms处理队列消息，纯控件渲染，无IO）
        self.root.after(100, self._poll_progress)
        # 延迟检查是否需要初始配置（等后台环境检测完成后）
        self.root.after(3000, self._check_first_run)
        self.root.mainloop()

    def _check_first_run(self) -> None:
        """检查是否是首次启动，如果缺少关键配置则自动打开设置面板。"""
        try:
            if self._needs_initial_setup():
                self._open_settings(first_time=True)
        except Exception:
            # 如果检测过程出错，不要阻断启动，让用户手动点击设置
            pass


def launch() -> None:
    """启动桌面窗口入口。"""
    # GUI 入口点：显式确保环境已初始化
    from .secret_store import load_runtime_secrets
    from .path_utils import load_project_env
    load_project_env(override=False)
    load_runtime_secrets()
    
    app = AicfGUI()
    
    # 启动后延迟运行健康检查（避免阻塞窗口显示）
    app.root.after(500, lambda: _run_startup_health_check(app))
    
    app.run()


def _run_startup_health_check(app: AicfGUI) -> None:
    """在GUI启动后运行健康检查，有问题则提示用户。"""
    import threading
    
    def _check():
        try:
            from .preflight import run_preflight_checks
            result = run_preflight_checks(check_model_reachability=True, check_ffmpeg=True)
            
            if not result.ok:
                # 有严重错误，在主线程显示对话框
                errors = result.errors()
                warnings = result.warnings()
                msg_parts = []
                if errors:
                    msg_parts.append(f"检测到 {len(errors)} 个严重问题，可能导致任务失败：\n")
                    for err in errors:
                        msg_parts.append(f"• [{err.category}] {err.message}")
                        if err.fix_hint:
                            msg_parts.append(f"  → {err.fix_hint}")
                if warnings:
                    if msg_parts:
                        msg_parts.append("")
                    msg_parts.append(f"警告 ({len(warnings)} 项)：")
                    for warn in warnings:
                        msg_parts.append(f"• [{warn.category}] {warn.message}")
                
                msg_parts.append("\n建议打开设置面板检查配置。")
                
                # 在主线程显示
                full_msg = "\n".join(msg_parts)
                app.root.after(0, lambda: messagebox.showwarning(
                    "系统健康检查",
                    full_msg,
                ))
        except Exception as e:
            # 健康检查本身失败了，不要阻断启动
            app._log(f"健康检查执行失败: {e}", "debug")
    
    # 在后台线程运行，避免阻塞UI
    threading.Thread(target=_check, daemon=True).start()
