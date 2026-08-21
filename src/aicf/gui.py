"""AI Content Factory - tkinter 桌面操作窗口。"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import yaml
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable
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
from .config import load_config
from .database import JobRepository
from .job_actions import (
    JobActionState,
    first_available_job_id,
    job_storage_exists,
)
from .job_lifecycle import (
    FORCE_INTERRUPT_REASON,
    JobLifecycleCoordinator,
    JobLifecycleOutcome,
)
from .job_service import JobService, ResearchResumeStrategy
from .job_view_model import (
    HealthIssue,
    HealthStatus,
    JobViewModel,
    JobViewModelBuilder,
    JobViewModelPoller,
    fail_closed_with_issue,
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


def worker_start_command(
    job_id: str,
    research_strategy: ResearchResumeStrategy | None = None,
) -> list[str]:
    command = [
        python_executable(),
        "-m",
        "aicf",
        "worker-start",
        "--job",
        job_id,
    ]
    if research_strategy is not None:
        command.extend(["--research-strategy", research_strategy.value])
    return command


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


@dataclass(frozen=True)
class GuiPreferences:
    production_settings: ProductionSettings
    default_direction: str
    api_key: str
    model: str

    @property
    def api_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class RuntimeBootstrap:
    preferences: GuiPreferences
    providers: tuple[str, ...]


@dataclass(frozen=True)
class UiMessage:
    """后台到Tk线程的唯一不可变消息协议。"""

    generation: int
    kind: str
    payload: object = None


def load_api_identity() -> tuple[str, str]:
    """读取API凭据和模型；调用方必须在后台线程执行。"""
    return (
        _get_env_value("OPENROUTER_API_KEY"),
        _get_env_value("OPENROUTER_MODEL") or "未设置",
    )


def load_gui_preferences() -> GuiPreferences:
    """读取GUI启动配置；调用方必须在后台线程执行。"""
    direction_config = load_config(
        project_root() / "config" / "content_direction.yaml"
    )
    direction = str(direction_config.direction or "")
    api_key, model = load_api_identity()
    return GuiPreferences(
        production_settings=load_default_settings(),
        default_direction=direction,
        api_key=api_key,
        model=model or "未设置",
    )


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

    def __init__(
        self,
        parent: Tk,
        *,
        current_model: str,
        api_key: str,
    ) -> None:
        self.parent = parent
        self.models: list[dict] = []
        self.current_model = current_model
        self.api_key = api_key

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
        self.ui_queue: queue.Queue[UiMessage] = queue.Queue()
        self._ui_message_generation = 0
        self._handled_ui_generation = 0
        self._ui_message_lock = Lock()
        self._command_queue: queue.Queue[
            tuple[
                str,
                Callable[[], Any],
                Callable[[Any], None] | None,
                Callable[[Exception], None] | None,
            ]
        ] = queue.Queue()
        self.running = False
        self.current_process: subprocess.Popen[str] | None = None
        self._polling_job_id: str = ""  # 正在运行、需要实时跟踪日志的任务
        self._display_job_id: str = ""  # 进度条当前显示的任务（跟随用户选中项）
        self._user_selected_job: bool = False  # 用户是否手动选中了某个任务（用于判断是否自动切换显示）
        self._logged_stages: set[str] = set()  # 已记录到日志的阶段，避免重复
        self._log_file_offsets: dict[str, int] = {}  # 已读取的日志文件字节位置，用于增量读取
        self._force_refresh_event: Event = Event()  # 强制刷新事件：给后台线程发信号立即刷新
        self._loading_initial_logs: bool = False  # 正在加载初始日志标志，避免重复加载
        self._last_loaded_job_id: str | None = None  # 上次加载日志的任务ID，防抖用
        self._applied_view_generation = 0
        self._api_configured = False
        self._configured_model = "加载中..."
        self._cached_api_key = ""
        self._job_view_model = JobViewModel(
            generation=0,
            selected_job_id="",
            health=HealthStatus.UNKNOWN,
            actions=JobActionState(
                can_start=False,
                can_resume=False,
                can_stop=False,
                can_open_video=False,
                guidance="任务状态正在后台加载，已安全禁用操作。",
            ),
        )

        # 字体
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        self.root.option_add("*Font", default_font)

        self._setup_styles()
        self._build_ui()
        # 注意：任务列表刷新、日志轮询、状态检测全部由后台线程 + _poll_progress统一处理
        # 不在UI线程做任何文件IO
        self._update_button_states()  # 初始化按钮状态
        # 后台异步检测视频提供商和环境状态（不阻塞UI启动）
        for lbl in self.env_labels.values():
            lbl.configure(text="检测中...", fg="#666666", font=("TkDefaultFont", 9))
        self._set_status("启动中，正在后台检测环境...")

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
        current_model = self._configured_model
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

        # 环境与提供商检测必须等mainloop启动后在后台执行。
        self._available_providers: list[str] = []
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
        # UI线程只查内存快照；最终冲突检查由后台创建命令完成。
        existing = {job.job_id for job in self._job_view_model.jobs}
        return first_available_job_id(base, existing.__contains__)

    def _job_id_taken(self, job_id: str) -> bool:
        return any(job.job_id == job_id for job in self._job_view_model.jobs)

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
        job = next(
            (item for item in self._job_view_model.jobs if item.job_id == job_id),
            None,
        )
        if job is not None and job.job_dir:
            return Path(job.job_dir)
        return project_root() / "data" / "jobs" / job_id

    def _get_repo(self) -> JobRepository:
        return JobRepository(project_root() / "data" / "content.db")

    def _get_lifecycle_coordinator(self) -> JobLifecycleCoordinator:
        return JobLifecycleCoordinator(project_root(), self._get_repo())

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

    def _apply_gui_preferences(self, preferences: GuiPreferences) -> None:
        """把后台读取的启动配置应用到控件和内存状态。"""
        self._apply_production_settings(preferences.production_settings)
        self._cached_api_key = preferences.api_key
        self._api_configured = preferences.api_configured
        self._configured_model = preferences.model
        self.model_label.configure(text=preferences.model)
        if self._is_showing_placeholder() and preferences.default_direction:
            self.direction_text.delete("1.0", "end")
            self.direction_text.insert("1.0", preferences.default_direction)
            self.direction_text.configure(foreground="#111827")
            self._direction_has_placeholder = False
        self._update_button_states()

    def _preferences_failed(self, error: Exception) -> None:
        message = f"启动配置读取失败: {sanitize_error(error)}"
        self._api_configured = False
        self._configured_model = "读取失败"
        self.model_label.configure(text=self._configured_model)
        self._log(message, "error")
        self._set_status("启动配置读取失败，请查看日志")
        self._update_button_states()

    def _load_gui_preferences_async(self) -> None:
        self._submit_io_command(
            "load_gui_preferences",
            load_gui_preferences,
            on_success=self._apply_gui_preferences,
            on_error=self._preferences_failed,
        )

    def _refresh_api_identity_async(self) -> None:
        def apply_identity(identity: tuple[str, str]) -> None:
            api_key, model = identity
            self._cached_api_key = api_key
            self._api_configured = bool(api_key)
            self._configured_model = model
            self.model_label.configure(text=model)
            self._update_button_states()

        self._submit_io_command(
            "load_api_identity",
            load_api_identity,
            on_success=apply_identity,
            on_error=self._preferences_failed,
        )

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
        """只读取后台发布的内存模型；选中项未同步时保持fail-closed。"""
        selected = self.job_tree.selection() if hasattr(self, "job_tree") else ()
        job_id = str(selected[0]) if selected else ""
        model = self._job_view_model
        if model.selected_job_id == job_id:
            return model.actions
        return JobActionState(
            can_start=False,
            can_resume=False,
            can_stop=False,
            can_open_video=False,
            guidance="任务状态正在后台刷新，已安全禁用操作。",
        )

    def _show_current_job_guidance(self) -> None:
        self._set_status(self._current_job_actions().guidance)

    def _update_button_states(self) -> None:
        """根据当前运行状态和环境配置更新按钮可用状态。"""
        has_video = bool(self._available_providers)
        has_api = self._api_configured
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
                except OSError as error:
                    logging.getLogger(__name__).debug(
                        "cli_candidate_probe_failed path=%s error=%s",
                        c,
                        sanitize_error(error),
                    )
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
                    except Exception as error:
                        logging.getLogger(__name__).warning(
                            "jimeng_config_probe_failed: %s",
                            sanitize_error(error),
                        )
        except Exception as error:
            logging.getLogger(__name__).warning(
                "jimeng_quick_probe_failed: %s", sanitize_error(error)
            )
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
        except Exception as error:
            logging.getLogger(__name__).warning(
                "kling_quick_probe_failed: %s", sanitize_error(error)
            )
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
            self._publish_ui("providers_detected", tuple(providers))
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
                self._publish_ui("env_update", (output, ok))
            except Exception as e:
                self._publish_ui(
                    "env_update",
                    (f"环境检查异常: {sanitize_error(e)}", False),
                )
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
        except Exception as error:
            logging.getLogger(__name__).debug(
                "preview_stop_failed: %s", sanitize_error(error)
            )
        if self._preview_process is not None:
            try:
                self._preview_process.terminate()
                self._preview_process.wait(timeout=2)
            except Exception:
                try:
                    self._preview_process.kill()
                except Exception as error:
                    logging.getLogger(__name__).debug(
                        "preview_kill_failed: %s", sanitize_error(error)
                    )
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

                self._publish_ui("set_status", "正在生成试听音频...")
                tts = build_default_tts_service()
                tmp_dir = Path(tempfile.gettempdir()) / "aicf_preview"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                # 清理旧的试听文件
                for old in tmp_dir.glob("preview_*.wav"):
                    try:
                        old.unlink()
                    except OSError as error:
                        logging.getLogger(__name__).debug(
                            "preview_cleanup_failed path=%s error=%s",
                            old,
                            sanitize_error(error),
                        )
                out_path = tmp_dir / f"preview_{voice_id.replace(':', '_')}.wav"
                result = tts.preview(preview_text, out_path, voice_id)
                self.log_queue.put(f"[试听] 使用 {result.provider} 生成音色 {voice_id}\n")

                # 用 winsound 直接异步播放（可被停止，切换音色时自动中断上一个）
                winsound.PlaySound(str(out_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                self.log_queue.put(f"[试听] 正在播放，切换音色后点试听可直接对比\n")
                self._publish_ui("set_status", "试听播放中（切换音色可对比）")
            except Exception as error:
                error_msg = str(error)
                error_type = type(error).__name__
                self.log_queue.put(f"[试听] 生成失败: {error_type}: {error_msg}\n")
                self._publish_ui("set_status", "试听失败")
                # 转换为用户友好的错误提示
                friendly_msg = self._friendly_error(error)
                self._publish_ui("show_error", ("试听失败", friendly_msg))
            finally:
                self._publish_ui("preview_done")

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
                self._publish_ui("env_update", (output, ok))
            except Exception as e:
                self.log_queue.put(f"环境检查失败: {e}\nerror")
                self._publish_ui("set_status", "环境检查失败")

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
                self._publish_ui("command_done")

        threading.Thread(target=worker, daemon=True).start()

    def _submit_io_command(
        self,
        name: str,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """提交慢IO；回调只会由UI消息循环异步执行。"""
        self._command_queue.put((name, operation, on_success, on_error))

    def _publish_ui(self, kind: str, payload: object = None) -> UiMessage:
        """按入队顺序分配generation并发布不可变消息。"""
        with self._ui_message_lock:
            self._ui_message_generation += 1
            message = UiMessage(self._ui_message_generation, kind, payload)
            self.ui_queue.put(message)
            return message

    def _start_background_command_thread(self) -> None:
        """串行执行GUI命令，避免回调线程访问SQLite、文件或进程。"""
        def command_loop() -> None:
            while True:
                name, operation, on_success, on_error = self._command_queue.get()
                try:
                    value = operation()
                except Exception as error:
                    logging.getLogger(__name__).exception(
                        "gui_background_command_failed name=%s", name
                    )
                    self._publish_ui("io_command_error", (on_error, error))
                else:
                    self._publish_ui("io_command_result", (on_success, value))
                finally:
                    self._command_queue.task_done()

        self._command_thread = threading.Thread(
            target=command_loop,
            daemon=True,
            name="AICF-GUI-Commands",
        )
        self._command_thread.start()

    def _on_command_done(self) -> None:
        """命令结束后只通知后台刷新，UI线程不读取状态源。"""
        self.current_process = None
        self._set_buttons_running(False)
        self._force_refresh_event.set()

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
                message = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if not isinstance(message, UiMessage):
                raise TypeError("ui_queue只接受UiMessage")
            if message.generation <= self._handled_ui_generation:
                processed += 1
                continue
            self._handled_ui_generation = message.generation
            payload = message.payload
            if message.kind == "job_view_model":
                self._apply_job_view_model(payload)
            elif message.kind == "log_lines":
                for line, tag in payload:
                    self._log_raw(line, tag)
            elif message.kind == "io_command_result":
                callback, value = payload
                if callback is not None:
                    callback(value)
            elif message.kind == "io_command_error":
                callback, error = payload
                if callback is not None:
                    callback(error)
            elif message.kind == "env_update":
                output, ok = payload
                self._update_env_lights(str(output), bool(ok))
            elif message.kind == "set_status":
                self._set_status(str(payload))
            elif message.kind == "preview_done":
                self._on_preview_done()
            elif message.kind == "show_error":
                title, detail = payload
                messagebox.showerror(str(title), str(detail))
            elif message.kind == "providers_detected":
                self._on_providers_detected(list(payload) if payload else [])
            elif message.kind == "command_done":
                self._on_command_done()
            elif message.kind == "startup_health_warning":
                messagebox.showwarning("系统健康检查", str(payload))
            processed += 1
        
        # 同时处理日志队列
        self._poll_log_queue_inner()
        
        # 100ms后继续处理（高频但极轻量，只处理内存中的队列消息）
        self.root.after(100, self._poll_progress)

    def _apply_job_view_model(self, model: JobViewModel) -> None:
        """仅接受更新generation，并把不可变模型交给纯渲染路径。"""
        if model.generation <= self._applied_view_generation:
            return
        self._job_view_model = model
        self._applied_view_generation = model.generation
        self._render_job_view_model(model)

    def _render_job_view_model(self, model: JobViewModel) -> None:
        rows = [
            {
                "job_id": job.row.job_id,
                "direction": job.row.direction,
                "status": job.row.status,
                "stage": self._translate_stage(job.row.stage),
                "updated": job.row.updated,
            }
            for job in model.jobs
            if job.row is not None
        ]
        self._apply_job_list_update(rows)
        self._polling_job_id = model.running_job_id
        selected = model.selected_job()
        if selected is not None:
            self._update_stages_from_status(selected.stage_payload())
        self._set_buttons_running(bool(model.running_job_id))
        self._show_current_job_guidance()
    
    def _poll_log_queue_inner(self) -> None:
        """处理日志队列消息（内联到_poll_progress，不需要单独轮询）。"""
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
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

    def _start_background_poll_thread(self) -> None:
        """后台生成单一ViewModel；UI线程不再自行读取状态源。"""
        def bg_poll_loop() -> None:
            poller = JobViewModelPoller(
                lambda: JobViewModelBuilder(
                    repository=self._get_repo(),
                    project_root=project_root(),
                    final_video_probe=final_video_for_job,
                ),
                logger=logging.getLogger(__name__),
            )
            while True:
                model = self._poll_view_model_once(poller)
                self._publish_ui("job_view_model", model)
                self._force_refresh_event.wait(2.0)
                self._force_refresh_event.clear()

        self._bg_thread = threading.Thread(
            target=bg_poll_loop,
            daemon=True,
            name="AICF-ViewModel-Poll",
        )
        self._bg_thread.start()

    def _poll_view_model_once(
        self,
        poller: JobViewModelPoller,
    ) -> JobViewModel:
        model = poller.next(
            selected_job_id=self._display_job_id,
            app_running=self.running,
        )
        if model.health is HealthStatus.UNKNOWN:
            return model
        try:
            self._queue_incremental_worker_log(model)
        except Exception as error:
            from .logging_utils import log_state_exception

            affected_job_id = model.running_job_id or model.selected_job_id
            model = fail_closed_with_issue(
                model,
                HealthIssue(
                    source="log",
                    message=str(error),
                    job_id=affected_job_id,
                ),
            )
            log_state_exception(
                logging.getLogger(__name__),
                event="gui_worker_log_read_failed",
                source="log",
                error=error,
                job_id=affected_job_id,
            )
        return model

    def _queue_incremental_worker_log(self, model: JobViewModel) -> None:
        job_id = model.running_job_id or model.selected_job_id
        selected = next((job for job in model.jobs if job.job_id == job_id), None)
        if selected is None:
            return
        log_path = (
            project_root()
            / "data"
            / "jobs"
            / job_id
            / "_work"
            / "runtime"
            / "worker.log"
        )
        if not log_path.is_file():
            return
        cache_key = f"{job_id}/worker.log"
        last_pos = self._log_file_offsets.get(cache_key, 0)
        size = log_path.stat().st_size
        if size < last_pos:
            last_pos = 0
        if size == last_pos:
            return
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(last_pos)
            content = stream.read()
        self._log_file_offsets[cache_key] = size
        lines = tuple(
            (line, self._get_log_tag(line))
            for line in content.splitlines()
            if line.strip()
        )
        if lines:
            self._publish_ui("log_lines", lines)

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
        self.direction_text.delete("1.0", "end")
        self._show_direction_placeholder()

        def apply_defaults(
            result: tuple[ProductionSettings, str],
        ) -> None:
            if self._display_job_id or self._user_selected_job:
                return
            settings, default_direction = result
            self._apply_production_settings(settings)
            if default_direction:
                self.direction_text.delete("1.0", "end")
                self.direction_text.insert("1.0", default_direction)
                self.direction_text.configure(foreground="#111827")
                self._direction_has_placeholder = False

        self._submit_io_command(
            "load_new_job_defaults",
            load_gui_preferences,
            on_success=lambda preferences: apply_defaults(
                (
                    preferences.production_settings,
                    preferences.default_direction,
                )
            ),
            on_error=self._preferences_failed,
        )
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
            settings = self._collect_production_settings()
            if settings is None:
                return
        except ValueError as error:
            messagebox.showerror("生产设置无效", str(error))
            return
        direction = self._get_direction_content()
        job_dir = project_root() / "data" / "jobs" / job_id
        output_dir = project_root() / "outputs" / job_id

        def create_job() -> str:
            repo = self._get_repo()
            try:
                repo.get_job(job_id)
            except KeyError:
                if job_storage_exists(job_dir, output_dir):
                    return "exists"
            else:
                return "exists"
            update_direction_config(
                project_root() / "config" / "content_direction.yaml",
                direction,
            )
            repo.create_job(job_id, job_dir)
            settings.save_for_job(job_dir)
            return "created"

        def created(result: object) -> None:
            if result == "exists":
                messagebox.showwarning(
                    "任务ID已存在",
                    f"任务 [{job_id}] 已存在，为避免覆盖原结果，本次未启动。\n\n"
                    "如需继续旧任务，请点击“继续/恢复”；"
                    "如需制作新视频，请点击“新建任务”。",
                )
                self._force_refresh_event.set()
                return
            self._log(f"任务 [{job_id}] 开始生成", "info")
            self._run_command_async(
                worker_start_command(job_id),
                env_extra={"AICF_PROJECT_ROOT": str(project_root())},
            )

        self._set_status("正在创建任务...")
        self._submit_io_command(
            "create_job",
            create_job,
            on_success=created,
            on_error=lambda error: messagebox.showerror(
                "生产设置无效", sanitize_error(error)
            ),
        )

    def _resume_job(
        self,
        research_strategy: ResearchResumeStrategy | None = None,
    ) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先在历史任务中选择一个任务，或在任务ID框中输入")
            return

        def prepare_worker(
            resume_job_id: str,
            selected_strategy: ResearchResumeStrategy | None,
        ) -> tuple[str, ResearchResumeStrategy | None, ProductionSettings]:
            return (
                resume_job_id,
                selected_strategy,
                ProductionSettings.load_for_job(self._get_job_dir(resume_job_id)),
            )

        def resume() -> object:
            repo = self._get_repo()
            if research_strategy is ResearchResumeStrategy.RETRY_SOURCES:
                status = repo.get_job(job_id)
                if (
                    status.current_stage.value != "FAILED_RETRYABLE"
                    or status.failed_stage is None
                    or status.failed_stage.value != "RESEARCHED"
                ):
                    raise ValueError("当前任务不是资料研究失败状态")
            return JobService(repo).resume_job(
                job_id,
                start=prepare_worker,
                research_strategy=research_strategy,
            )

        def resumed(outcome: object) -> None:
            if not outcome.started:
                messagebox.showwarning(
                    "无法直接恢复",
                    outcome.reason
                    + (
                        f"\n\n请执行：{outcome.recovery_command}"
                        if outcome.recovery_command
                        else ""
                    ),
                )
                return
            resume_job_id, selected_strategy, settings = outcome.value
            self._logged_stages.clear()
            self._log_file_offsets.clear()
            self._display_job_id = resume_job_id
            self._user_selected_job = False
            self._apply_production_settings(settings)
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self._polling_job_id = resume_job_id
            self._log(f"任务 [{resume_job_id}] 继续/恢复", "info")
            self._run_command_async(
                worker_start_command(resume_job_id, selected_strategy),
                env_extra={"AICF_PROJECT_ROOT": str(project_root())},
            )

        self._set_status("正在后台校验恢复状态...")
        self._submit_io_command(
            "resume_job",
            resume,
            on_success=resumed,
            on_error=lambda error: messagebox.showerror(
                "无法恢复",
                (
                    f"任务 [{job_id}] 不存在"
                    if isinstance(error, KeyError)
                    else sanitize_error(error)
                ),
            ),
        )

    def _retry_research(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择资料研究失败的任务")
            return
        self._log(f"任务 [{job_id}] 重新搜索资料", "info")
        self._resume_job(ResearchResumeStrategy.RETRY_SOURCES)

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
        if not job_id:
            return
        if not messagebox.askyesno(
            "确认停止",
            "确定要停止当前正在运行的任务吗？\n已生成的部分资源会被保留。",
        ):
            return
        self._log(f"正在停止任务 [{job_id}]...", "info")

        def force_after_failure(error: BaseException) -> None:
            self._log(f"正常停止失败: {error}", "warning")
            if messagebox.askyesno(
                "强制清理",
                "无法协作停止当前Worker。\n"
                "是否由生命周期协调器验证进程并强制清理？",
                icon="warning",
            ):
                self._submit_io_command(
                    "force_interrupt",
                    lambda: self._get_lifecycle_coordinator().force_interrupt(
                        job_id,
                        FORCE_INTERRUPT_REASON,
                    ),
                    on_success=force_finished,
                    on_error=lambda force_error: (
                        self._log(f"强制清理失败: {force_error}", "error"),
                        messagebox.showerror("清理失败", str(force_error)),
                    ),
                )

        def force_finished(result: object) -> None:
            if result.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR:
                detail = result.repair_reason or "运行时清理尚未完成"
                self._log(
                    f"任务状态已提交，运行时清理待重试: {detail}",
                    "warning",
                )
                messagebox.showwarning(
                    "清理待重试",
                    "任务中断状态已提交，但运行时清理尚未完成；"
                    f"可再次执行强制清理。\n\n{detail}",
                )
            else:
                self._log("已强制清理任务状态，可以点击继续/恢复", "info")
                messagebox.showinfo(
                    "清理完成",
                    "任务状态已清理，可以点击「继续/恢复」从断点继续，"
                    "或创建新任务。",
                )
            self._force_refresh_event.set()
            self._update_button_states()

        self._submit_io_command(
            "request_stop",
            lambda: self._get_lifecycle_coordinator().request_stop(job_id),
            on_success=lambda _result: self._log("已发送停止信号", "info"),
            on_error=force_after_failure,
        )

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
        """从内存ViewModel重绘；状态源读取由后台轮询负责。"""
        job_id = self._current_job_id()
        if not job_id:
            self._reset_stages()
            return
        job = next(
            (item for item in self._job_view_model.jobs if item.job_id == job_id),
            None,
        )
        if job is None:
            self._reset_stages()
            self._set_status(f"任务 {job_id} 状态正在后台加载")
            return
        self._update_stages_from_status(job.stage_payload())
        cur = job.current_stage
        failed = job.failed_stage
        if cur == "COMPLETED":
            self._set_status(f"任务 {job_id} 已完成 ✓")
        elif cur == "FAILED_NEEDS_ATTENTION":
            self._set_status(f"任务 {job_id} 失败，需人工处理")
        elif cur == "FAILED_RETRYABLE":
            self._set_status(f"任务 {job_id} 可重试失败，点击继续/恢复")
        elif failed:
            failed_name = self._translate_stage(failed)
            self._set_status(f"任务 {job_id} 在 [{failed_name}] 失败/等待恢复")
        elif cur and cur != "INIT" and not job.running:
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

    def _highlight_selected_job(self) -> None:
        """给历史任务列表中的当前选中项加显式高亮。"""
        selected = set(self.job_tree.selection())
        for item in self.job_tree.get_children():
            if item in selected:
                self.job_tree.item(item, tags=("selected_row",))
            else:
                self.job_tree.item(item, tags=())

    def _read_tail_lines(self, file_path: Path, max_lines: int) -> list[str]:
        """高效读取文件最后N行，避免加载整个大文件。"""
        with open(file_path, "rb") as f:
            block_size = 8192
            f.seek(0, 2)
            file_size = f.tell()
            blocks: list[bytes] = []
            lines_found = 0
            block_end = file_size

            while block_end > 0 and lines_found <= max_lines:
                block_start = max(0, block_end - block_size)
                f.seek(block_start)
                block = f.read(block_end - block_start)
                blocks.append(block)
                lines_found += block.count(b"\n")
                block_end = block_start

            content = b"".join(reversed(blocks))
            lines = content.decode("utf-8", errors="replace").splitlines()
            return lines[-max_lines:] if len(lines) > max_lines else lines

    def _request_job_logs(self, job_id: str) -> None:
        """异步加载历史日志，保持原有分段、颜色和顶部定位体验。"""
        job_dir = self._get_job_dir(job_id)

        def collect_logs() -> tuple[list[tuple[str, str]], dict[str, int]]:
            lines: list[tuple[str, str]] = [
                ("=" * 60, "info"),
                (f"任务 [{job_id}] 日志", "info"),
                ("=" * 60, "info"),
            ]
            offsets: dict[str, int] = {}
            status_data: dict[str, Any] = {}
            status_path = job_dir / "status.json"
            if status_path.is_file():
                loaded = json.loads(status_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    status_data = loaded

            worker_log = next(
                (
                    path
                    for path in (
                        job_dir / "_work" / "runtime" / "worker.log",
                        job_dir / "worker.log",
                        job_dir / "logs" / "worker.log",
                    )
                    if path.is_file()
                ),
                None,
            )
            if worker_log is not None:
                lines.append(("--- Worker 主日志（最近500行） ---", "info"))
                lines.extend(
                    (line, self._get_log_tag(line))
                    for line in self._read_tail_lines(worker_log, 500)
                    if line.strip()
                )
                offsets[f"{job_id}/worker.log"] = worker_log.stat().st_size

            stages = status_data.get("stages", {})
            if isinstance(stages, dict):
                for stage_key, stage_info in stages.items():
                    if not isinstance(stage_info, dict):
                        continue
                    log_rel = stage_info.get("log_path")
                    if not isinstance(log_rel, str) or not log_rel:
                        continue
                    log_path = next(
                        (
                            base / log_rel
                            for base in (
                                job_dir / "_work" / "runtime",
                                job_dir,
                                job_dir / "logs",
                            )
                            if (base / log_rel).is_file()
                        ),
                        None,
                    )
                    if log_path is None:
                        continue
                    lines.append(
                        (
                            f"--- 阶段: {self._translate_stage(stage_key)}"
                            "（最近200行） ---",
                            "info",
                        )
                    )
                    lines.extend(
                        (line, self._get_log_tag(line))
                        for line in self._read_tail_lines(log_path, 200)
                        if line.strip()
                    )
                    offsets[f"{job_id}/{log_rel}"] = log_path.stat().st_size
            return lines, offsets

        def apply_logs(result: tuple[list[tuple[str, str]], dict[str, int]]) -> None:
            if self._display_job_id != job_id:
                return
            lines, offsets = result
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self._log_file_offsets.update(offsets)
            for line, tag in lines:
                self._log_raw(line, tag)
            self.log_text.see("1.0")

        self._submit_io_command(
            "load_job_logs",
            collect_logs,
            on_success=apply_logs,
            on_error=lambda error: self._log(
                f"读取任务日志失败: {sanitize_error(error)}", "error"
            ),
        )

    def _on_job_select(self, _event: object = None) -> None:
        sel = self.job_tree.selection()
        if sel:
            self._highlight_selected_job()
            job_id = str(sel[0])
            self._last_loaded_job_id = job_id
            self.job_id_var.set(job_id)
            self._display_job_id = job_id
            self._user_selected_job = True
            self._log_file_offsets.clear()
            self._update_button_states()
            self._set_status("任务状态正在后台刷新...")
            self._request_job_logs(job_id)
            self._force_refresh_event.set()

    def _open_output(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            # 没有选中任务时，打开jobs根目录
            path = project_root() / "data" / "jobs"
        else:
            path = self._get_job_dir(job_id)
        self._submit_io_command(
            "open_output",
            lambda: (
                path.mkdir(parents=True, exist_ok=True),
                os.startfile(str(path)),  # type: ignore[attr-defined]
            ),
            on_error=lambda error: messagebox.showerror(
                "错误", f"打开目录失败: {sanitize_error(error)}"
            ),
        )

    def _open_final_video(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择任务")
            return
        job = next(
            (item for item in self._job_view_model.jobs if item.job_id == job_id),
            None,
        )
        if job is None or not job.final_video_path:
            messagebox.showwarning("提示", "所选任务尚无最终视频，请等待任务完成")
            return
        video = job.final_video_path
        self._submit_io_command(
            "open_final_video",
            lambda: os.startfile(video),  # type: ignore[attr-defined]
            on_error=lambda error: messagebox.showerror(
                "错误", f"打开视频失败: {sanitize_error(error)}"
            ),
        )

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
        job = next(
            (item for item in self._job_view_model.jobs if item.job_id == job_id),
            None,
        )
        if job is None or job.health is HealthStatus.UNKNOWN:
            messagebox.showwarning("提示", "任务状态尚未确认，请刷新后再删除")
            return
        if job.running:
            messagebox.showwarning("提示", "任务正在运行中，请先停止后再删除")
            return
        confirm = messagebox.askyesno(
            "确认删除",
            f"确定要删除任务 [{job_id}] 吗？\n\n"
            f"将清理：\n{job_dir}\n\n此操作不可恢复。",
        )
        if not confirm:
            return

        def deleted(result: object) -> None:
            cleanup_errors = list(result.cleanup_errors)
            self._log(f"已删除任务: {job_id}", "info")
            self._force_refresh_event.set()
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

        self._submit_io_command(
            "delete_job",
            lambda: self._get_lifecycle_coordinator().delete_job(job_id),
            on_success=deleted,
            on_error=lambda error: messagebox.showerror(
                "错误", f"删除失败: {sanitize_error(error)}"
            ),
        )

    def _open_job_dir(self) -> None:
        """打开选中任务的目录。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择一个任务")
            return
        job_dir = self._get_job_dir(job_id)
        self._submit_io_command(
            "open_job_dir",
            lambda: os.startfile(str(job_dir)),  # type: ignore[attr-defined]
            on_error=lambda error: messagebox.showerror(
                "错误", f"打开目录失败: {sanitize_error(error)}"
            ),
        )

    def _force_clean_job(self) -> None:
        """右键菜单直接强制清理僵尸任务。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择一个任务")
            return
        confirm = messagebox.askyesno(
            "强制清理僵尸任务",
            f"将强制清理任务 [{job_id}] 的运行状态，\n"
            "清理后可以点击「继续/恢复」从断点继续。\n\n"
            "是否继续？",
            icon="warning",
        )
        if not confirm:
            return

        def cleaned(result: object) -> None:
            if result.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR:
                detail = result.repair_reason or "运行时清理尚未完成"
                self._log(
                    f"任务状态已提交，运行时清理待重试: {detail}",
                    "warning",
                )
                messagebox.showwarning(
                    "清理待重试",
                    "任务中断状态已提交，但运行时清理尚未完成；"
                    f"可再次执行强制清理。\n\n{detail}",
                )
                self._force_refresh_event.set()
                self._update_button_states()
                return
            self._log(f"已强制清理任务 [{job_id}]", "info")
            messagebox.showinfo("清理完成", "任务状态已清理，可以点击「继续/恢复」从断点继续。")
            self._force_refresh_event.set()
            self._update_button_states()

        def clean_failed(error: BaseException) -> None:
            self._log(f"强制清理失败: {error}", "error")
            messagebox.showerror("清理失败", str(error))

        self._submit_io_command(
            "force_clean_job",
            lambda: self._get_lifecycle_coordinator().force_interrupt(
                job_id,
                FORCE_INTERRUPT_REASON,
            ),
            on_success=cleaned,
            on_error=clean_failed,
        )

    def _open_model_selector(self) -> None:
        """打开 OpenRouter 模型选择窗口。"""
        dialog = ModelSelectionDialog(
            self.root,
            current_model=self._configured_model,
            api_key=self._cached_api_key,
        )
        self.root.after(500, lambda: self._sync_model_label(dialog))

    def _open_settings(self, *, first_time: bool = False) -> None:
        """打开集中设置对话框。
        
        Args:
            first_time: 是否是首次启动引导模式
        """
        def on_saved() -> None:
            # 设置保存后，刷新环境状态和模型标签
            self._run_doctor()
            self._refresh_api_identity_async()
            # 提供商探测包含文件和进程IO，继续走既有后台检测消息协议。
            self._start_async_provider_detection()

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
            self._refresh_api_identity_async()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        self._stop_preview()
        self.root.destroy()

    def _load_runtime_bootstrap(self) -> RuntimeBootstrap:
        """后台初始化环境、密钥、偏好和本地provider探测。"""
        from .path_utils import load_project_env
        from .secret_store import load_runtime_secrets

        load_project_env(override=False)
        load_runtime_secrets()
        return RuntimeBootstrap(
            preferences=load_gui_preferences(),
            providers=tuple(self._quick_detect_providers()),
        )

    def _apply_runtime_bootstrap(self, bootstrap: RuntimeBootstrap) -> None:
        self._apply_gui_preferences(bootstrap.preferences)
        self._on_providers_detected(list(bootstrap.providers))
        self._start_async_provider_detection()
        _run_startup_health_check(self)

    def _start_background_services(self) -> None:
        """此回调只会在Tk mainloop已开始调度后执行。"""
        self._start_background_command_thread()
        self._start_background_poll_thread()
        self._submit_io_command(
            "runtime_bootstrap",
            self._load_runtime_bootstrap,
            on_success=self._apply_runtime_bootstrap,
            on_error=self._preferences_failed,
        )

    def run(self) -> None:
        # after(0)中的初始化只能在mainloop开始后执行；调用run前不加载环境、
        # 密钥或provider。
        self.root.after(0, self._start_background_services)
        # 启动UI消息轮询（每100ms处理队列消息，纯控件渲染，无IO）
        self.root.after(100, self._poll_progress)
        # 延迟检查是否需要初始配置（等后台环境检测完成后）
        self.root.after(3000, self._check_first_run)
        self.root.mainloop()

    def _check_first_run(self) -> None:
        """后台检查首次配置；UI回调本身不执行文件或进程探测。"""
        self._submit_io_command(
            "check_first_run",
            self._needs_initial_setup,
            on_success=lambda needed: (
                self._open_settings(first_time=True) if needed else None
            ),
            on_error=lambda error: logging.getLogger(__name__).warning(
                "check_first_run_failed: %s", sanitize_error(error)
            ),
        )


def launch() -> None:
    """启动桌面窗口入口。"""
    app = AicfGUI()
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
                
                full_msg = "\n".join(msg_parts)
                app._publish_ui("startup_health_warning", full_msg)
        except Exception as e:
            # 健康检查本身失败了，不要阻断启动
            app._log(f"健康检查执行失败: {e}", "debug")
    
    # 在后台线程运行，避免阻塞UI
    threading.Thread(target=_check, daemon=True).start()
