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
from datetime import datetime
from pathlib import Path
from tkinter import (
    BooleanVar,
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

from .config import load_config
from .file_lock import lock_is_active
from .production_settings import (
    ProductionSettings,
    MOTION_MODE_DISPLAY_NAMES,
    MOTION_MODE_VALUES,
    ORIENTATION_DISPLAY_NAMES,
    PLATFORM_DISPLAY_NAMES,
    VOICE_DISPLAY_NAMES,
    VOICE_GROUP_ORDER,
)
from .state_machine import PipelineStage

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
    jimeng_model: str,
    video_resolution: str,
    motion_mode: str,
    narration_voice: str,
    orientation: str,
) -> ProductionSettings:
    return ProductionSettings(
        selected_platforms=tuple(
            platform for platform, selected in platforms.items() if selected
        ),
        jimeng_model=jimeng_model,
        video_resolution=video_resolution,
        motion_mode=motion_mode,
        narration_voice=narration_voice,
        orientation=orientation,
    )


def final_video_for_job(job_dir: str | Path) -> Path | None:
    root = Path(job_dir)
    settings = ProductionSettings.load_for_job(root)
    for platform in settings.selected_platforms:
        video = root / "delivery" / platform / "video.mp4"
        if video.is_file():
            return video
    return None


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
    env_path.write_text(content, encoding="utf-8")


def _get_env_value(key: str) -> str:
    """从 .env 或系统环境变量读取值。"""
    val = os.getenv(key, "")
    if val:
        return val
    env_text = _read_env_file()
    m = re.search(rf"^{key}\s*=\s*(.+)$", env_text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("\"'")
    return ""


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
OR_MODELS_URL = "https://openrouter.ai/api/v1/models"


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
                req = Request(OR_MODELS_URL, headers=headers, method="GET")
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


def project_root() -> Path:
    configured = os.getenv("AICF_PROJECT_ROOT")
    return Path(configured) if configured else Path.cwd()


def python_executable() -> str:
    """返回当前 uv 环境的 Python 路径（始终用 python.exe，避免 pythonw.exe 无控制台输出）。"""
    exe = sys.executable
    if exe.endswith("pythonw.exe"):
        exe = exe.replace("pythonw.exe", "python.exe")
    return exe


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
        self._polling_job_id: str = ""  # 当前轮询进度的任务 ID
        self._logged_stages: set[str] = set()  # 已记录到日志的阶段，避免重复
        self._poll_count: int = 0  # 轮询计数器，用于定期刷新任务列表
        self._log_file_offsets: dict[str, int] = {}  # 已读取的日志文件字节位置，用于增量读取

        # 字体
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        self.root.option_add("*Font", default_font)

        self._setup_styles()
        self._build_ui()
        self._refresh_job_list()
        self._poll_log_queue()
        self._poll_ui_queue()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("vista")
        style.configure("EnvIdle.TLabel", foreground="#666666", font=("TkDefaultFont", 9))
        style.configure("EnvOk.TLabel", foreground="#2e7d32", font=("TkDefaultFont", 9, "bold"))
        style.configure("EnvFail.TLabel", foreground="#c62828", font=("TkDefaultFont", 9, "bold"))
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

        self.env_labels: dict[str, ttk.Label] = {}
        env_items = [
            ("openrouter", "OpenRouter"),
            ("dreamina", "Dreamina CLI"),
            ("ffmpeg", "FFmpeg"),
            ("tts", "TTS 语音"),
        ]
        for i, (key, text) in enumerate(env_items):
            ttk.Label(env_frame, text=text + ":").grid(row=0, column=i * 2, sticky="w", padx=(8, 4))
            lbl = ttk.Label(env_frame, text="未检查", style="EnvIdle.TLabel")
            lbl.grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 16))
            self.env_labels[key] = lbl

        ttk.Button(env_frame, text="检查环境", command=self._run_doctor).grid(
            row=0, column=len(env_items) * 2, sticky="e", padx=8
        )

        # 当前模型显示 + 选择按钮
        current_model = _get_env_value("OPENROUTER_MODEL") or "未设置"
        ttk.Label(env_frame, text="模型:").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=(4, 0))
        self.model_label = ttk.Label(env_frame, text=current_model, foreground="#1976d2", font=("TkDefaultFont", 8, "bold"))
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
        self.jimeng_model_var = StringVar(value="seedance2.0fast")
        self.video_resolution_var = StringVar(value="720p")
        self.motion_mode_display_var = StringVar(value=MOTION_MODE_DISPLAY_NAMES["video"])
        self.narration_voice_var = StringVar(value="kokoro:zm_yunyang")
        self.narration_voice_display_var = StringVar(value=VOICE_DISPLAY_NAMES["kokoro:zm_yunyang"])
        
        # 即梦模型
        ttk.Label(options, text="即梦模型:").pack(side="left", padx=(0, 4))
        ttk.Combobox(
            options,
            textvariable=self.jimeng_model_var,
            values=(
                "seedance2.0fast",
                "seedance2.0",
                "seedance2.0_vip",
                "seedance2.0fast_vip",
                "seedance2.0mini",
            ),
            state="readonly",
            width=18,
        ).pack(side="left", padx=(0, 12))
        
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
        default_dir = self._load_default_direction()
        if default_dir:
            self.direction_text.insert("1.0", default_dir)

        setup_frame.columnconfigure(4, weight=1)
        setup_frame.rowconfigure(3, weight=1)

        # ---- 操作按钮 ----
        btn_frame = ttk.Frame(root, padding=(10, 4))
        btn_frame.pack(fill="x")

        self.btn_start = ttk.Button(btn_frame, text="▶ 开始生成", command=self._start_job)
        self.btn_start.pack(side="left", padx=(0, 6))

        self.btn_resume = ttk.Button(btn_frame, text="⏵ 继续/恢复", command=self._resume_job)
        self.btn_resume.pack(side="left", padx=6)

        ttk.Button(btn_frame, text="🔄 刷新状态", command=self._refresh_all).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="📂 打开输出目录", command=self._open_output).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="打开最终视频", command=self._open_final_video).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="⏹ 停止", command=self._stop_job).pack(side="left", padx=6)

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
            columns=("job_id", "status", "stage", "updated"),
            show="headings",
            height=12,
        )
        self.job_tree.heading("job_id", text="任务ID")
        self.job_tree.heading("status", text="状态")
        self.job_tree.heading("stage", text="当前阶段")
        self.job_tree.heading("updated", text="更新时间")
        self.job_tree.column("job_id", width=100, anchor="w")
        self.job_tree.column("status", width=90, anchor="w")
        self.job_tree.column("stage", width=110, anchor="w")
        self.job_tree.column("updated", width=130, anchor="w")
        self.job_tree.tag_configure("selected_row", background="#1976d2", foreground="white")
        self.job_tree.pack(fill="both", expand=True)
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)
        self.job_tree.bind("<Button-3>", self._show_job_context_menu)  # 右键菜单

        # 历史任务右键菜单
        self.job_context_menu = Menu(self.root, tearoff=0)
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
        self.log_text.tag_configure("error", foreground="#ff6b6b")
        self.log_text.tag_configure("success", foreground="#51cf66")
        self.log_text.tag_configure("info", foreground="#74c0fc")

        # 右键菜单
        self._log_menu = Menu(self.log_text, tearoff=0)
        self._log_menu.add_command(label="清屏", command=self._clear_log)
        self.log_text.bind("<Button-3>", self._show_log_menu)

        # ---- 底部状态栏 ----
        self.status_var = StringVar(value="就绪")
        status_bar = ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 3))
        status_bar.pack(fill="x", padx=10, pady=(0, 8))

        # 窗口关闭时停止试听
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _auto_job_id(self) -> str:
        now = datetime.now()
        return f"VIDEO{now.strftime('%m%d%H%M')}"

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
        self.log_queue.put(f"[{ts}] {msg}\n{tag}")

    def _poll_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if "\n" in item:
                    text, tag = item.rsplit("\n", 1)
                else:
                    text, tag = item, ""
                self.log_text.configure(state="normal")
                if tag and tag in ("error", "success", "info"):
                    self.log_text.insert("end", text, tag)
                else:
                    self.log_text.insert("end", text)
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
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _clear_log(self) -> None:
        """清空运行日志。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _show_log_menu(self, event: object) -> None:
        """在鼠标位置弹出右键菜单。"""
        try:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._log_menu.grab_release()

    def _get_job_dir(self, job_id: str) -> Path:
        return project_root() / "outputs" / job_id

    def _get_status_path(self, job_id: str) -> Path:
        return self._get_job_dir(job_id) / "status.json"

    def _collect_production_settings(self) -> ProductionSettings:
        # 将中文显示名转换回内部值
        motion_display = self.motion_mode_display_var.get()
        motion_mode = MOTION_MODE_VALUES.get(motion_display, "balanced")
        
        return build_production_settings(
            {
                platform: bool(variable.get())
                for platform, variable in self.platform_vars.items()
            },
            jimeng_model=self.jimeng_model_var.get(),
            video_resolution=self.video_resolution_var.get(),
            motion_mode=motion_mode,
            narration_voice=self.narration_voice_var.get(),
            orientation=self.orientation_var.get(),
        )

    def _apply_production_settings(self, settings: ProductionSettings) -> None:
        selected = set(settings.selected_platforms)
        for platform, variable in self.platform_vars.items():
            variable.set(platform in selected)
        self.jimeng_model_var.set(settings.jimeng_model)
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

    def _set_buttons_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.btn_start.configure(state=state)
        self.btn_resume.configure(state=state)
        self.running = running

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

    def _stop_preview(self) -> None:
        """停止正在播放的试听音频。"""
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
        """生成并播放当前选中音色的试听音频（试听词：然哥哥早上好）。"""
        self._stop_preview()
        self.btn_preview_voice.configure(state="disabled", text="生成中...")

        voice_id = self._voice_id_from_display(self.narration_voice_display_var.get())
        preview_text = "然哥哥早上好"

        def worker() -> None:
            try:
                from .providers.tts import build_default_tts_service
                import tempfile

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

                # 用系统默认播放器播放
                os.startfile(str(out_path))  # type: ignore[attr-defined]
                self.log_queue.put(f"[试听] 正在播放: {out_path}\n")
                self.ui_queue.put(("set_status", "试听播放中", None))
            except Exception as error:
                self.log_queue.put(f"[试听] 生成失败: {type(error).__name__}: {error}\n")
                self.ui_queue.put(("set_status", "试听失败", None))
                messagebox.showerror("试听失败", f"生成试听音频失败：\n{error}")
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
                result = subprocess.run(
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
                lbl.configure(text="✓ 就绪", style="EnvOk.TLabel")
            self._set_status("环境检查完成")
            self._log("环境检查通过", "success")
        else:
            checks = {
                "openrouter": "openrouter: OK" in output,
                "dreamina": "jimeng: OK" in output,
                "ffmpeg": "ffmpeg: OK" in output,
                "tts": "tts_strategy: OK" in output,
            }
            for key, lbl in self.env_labels.items():
                if checks.get(key, False):
                    lbl.configure(text="✓ 就绪", style="EnvOk.TLabel")
                else:
                    lbl.configure(text="✗ 异常", style="EnvFail.TLabel")
            self._set_status("环境存在问题，请查看日志")
            self._log("环境存在问题", "error")

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------
    def _ensure_direction_file(self) -> Path:
        """将界面输入的方向写入全局 config/content_direction.yaml。"""
        direction = self.direction_text.get("1.0", "end").strip()
        cfg_path = project_root() / "config" / "content_direction.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if not direction:
            direction = "AI短视频自动生成"
        content = f"direction: |\n"
        for line in direction.splitlines():
            content += f"  {line}\n"
        cfg_path.write_text(content, encoding="utf-8")
        return cfg_path

    def _run_command_async(self, args: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> None:
        """在后台线程运行命令，实时输出日志。"""
        if self.running:
            messagebox.showinfo("提示", "已有任务正在运行，请等待或先停止")
            return
        # 立即设置运行标志，防止快速双击导致重复启动
        self.running = True

        self.btn_start.configure(state="disabled")
        self.btn_resume.configure(state="disabled")
        self._set_status("运行中...")
        self._log(f"执行命令: {' '.join(args)}", "info")
        self._log_file_offsets.clear()  # 清空日志文件偏移，重新读取

        # 提取 job_id 用于轮询进度
        try:
            idx = args.index("--job")
            self._polling_job_id = args[idx + 1]
        except (ValueError, IndexError):
            self._polling_job_id = ""
        if self._polling_job_id:
            self._poll_progress()  # 启动轮询

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
        self._polling_job_id = ""
        self._logged_stages.clear()
        self._set_buttons_running(False)
        self._set_status("就绪")
        self._refresh_status()
        self._refresh_job_list()

    def _poll_progress(self) -> None:
        """每 3 秒读取 status.json 更新阶段进度条、状态文字和日志。"""
        if not self._polling_job_id:
            return
        sp = self._get_status_path(self._polling_job_id)
        if sp.is_file():
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                self._update_stages_from_status(data)
                cur = data.get("current_stage", "")
                failed = data.get("failed_stage", "")
                completed = data.get("completed_stages", [])
                # 找到当前阶段的中文名
                stage_name = self._translate_stage(cur) if cur else ""
                if cur == "FAILED_NEEDS_ATTENTION":
                    self._set_status("失败，需人工处理")
                elif cur == "FAILED_RETRYABLE":
                    failed_name = self._translate_stage(failed) if failed else stage_name
                    self._set_status(f"[{failed_name}] 可重试失败，点击继续/恢复")
                elif failed:
                    failed_name = self._translate_stage(failed)
                    self._set_status(f"运行中: {stage_name}（{failed_name} 失败，等待恢复）")
                elif cur == "COMPLETED" or data.get("status") == "ready_to_publish":
                    self._set_status("完成 ✓")
                else:
                    self._set_status(f"运行中: {stage_name}")

                # 将新完成的阶段写入日志
                for s in completed:
                    if s not in self._logged_stages:
                        s_name = self._translate_stage(s)
                        self._log(f"✓ {s_name} 完成", "success")
                        self._logged_stages.add(s)
                # 当前阶段变化时也记录（排除失败状态和完成状态）
                if (
                    cur
                    and cur not in self._logged_stages
                    and cur not in ("COMPLETED", "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION")
                ):
                    self._log(f"→ 进入阶段: {stage_name}", "info")
                    self._logged_stages.add(cur)

                # 增量读取日志文件
                self._tail_log_files(data)
            except Exception:
                pass
        # 每 3 次轮询刷新一次任务列表
        self._poll_count += 1
        if self._poll_count % 3 == 0:
            self._refresh_job_list()
        self.root.after(3000, self._poll_progress)

    def _start_job(self) -> None:
        self._logged_stages.clear()
        job_id = self.job_id_var.get().strip() or self._auto_job_id()
        self.job_id_var.set(job_id)
        self._ensure_direction_file()
        try:
            self._collect_production_settings().freeze_for_job(
                self._get_job_dir(job_id)
            )
        except ValueError as error:
            messagebox.showerror("生产设置无效", str(error))
            return
        self._log(f"任务 [{job_id}] 开始生成", "info")
        self._run_command_async(
            [python_executable(), "-m", "aicf", "autopilot", "--job", job_id],
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _resume_job(self) -> None:
        self._logged_stages.clear()
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先在历史任务中选择一个任务，或在任务ID框中输入")
            return
        self._load_job_production_settings(job_id)
        self._log(f"任务 [{job_id}] 继续/恢复", "info")
        self._run_command_async(
            [python_executable(), "-m", "aicf", "resume", "--job", job_id],
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _stop_job(self) -> None:
        if self.current_process and self.running:
            pid = self.current_process.pid
            self._log(f"正在停止任务 (PID={pid})...", "info")
            try:
                # Windows: 使用 taskkill /T /F 终止整个进程树
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                try:
                    self.current_process.terminate()
                except Exception:
                    pass
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
        self._refresh_job_list()
        self._refresh_status()

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

    def _is_job_really_running(self, job_dir: Path, data: dict) -> bool:
        """按统一 PID/时间/心跳协议判断任务是否运行，不修改锁文件。"""
        del data
        return lock_is_active(
            job_dir / ".autopilot.lock",
            stale_after=120.0,
        )

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
        outputs = project_root() / "outputs"
        if not outputs.is_dir():
            return
        jobs = sorted(outputs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for job_dir in jobs:
            if not job_dir.is_dir():
                continue
            sp = job_dir / "status.json"
            status_text = "未开始"
            stage_text = "-"
            updated = "-"
            if sp.is_file():
                try:
                    d = json.loads(sp.read_text(encoding="utf-8"))
                    st = d.get("status", "")
                    cur = d.get("current_stage", "")
                    failed = d.get("failed_stage", "")
                    is_really_running = self._is_job_really_running(job_dir, d)
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
                "", "end", iid=job_dir.name,
                values=(job_dir.name, status_text, stage_text, updated),
            )
        if current_selection:
            existing = [item for item in current_selection if self.job_tree.exists(item)]
            if existing:
                self.job_tree.selection_set(existing)
                self.job_tree.focus(existing[0])
        self._highlight_selected_job()

    def _on_job_select(self, _event: object = None) -> None:
        sel = self.job_tree.selection()
        if sel:
            self._highlight_selected_job()
            job_id = str(sel[0])
            self.job_id_var.set(job_id)
            self._load_job_production_settings(job_id)
            self._refresh_status()
            # 如果选中的任务正在运行中，自动启动进度轮询
            sp = self._get_status_path(job_id)
            if sp.is_file():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    cur = data.get("current_stage", "")
                    failed = data.get("failed_stage", "")
                    job_dir = self._get_job_dir(job_id)
                    # 判断是否正在运行：不是终态、不是失败态、且进程真的在运行
                    terminal_states = {
                        "COMPLETED", "INIT",
                        "FAILED_RETRYABLE", "FAILED_NEEDS_ATTENTION",
                    }
                    is_running = bool(
                        cur
                        and cur not in terminal_states
                        and not failed
                        and self._is_job_really_running(job_dir, data)
                    )
                    if is_running and self._polling_job_id != job_id:
                        self._logged_stages.clear()
                        self._polling_job_id = job_id
                        self._set_buttons_running(True)
                        self._log(f"已连接到运行中的任务 [{job_id}]", "info")
                        self._poll_progress()
                    elif not is_running:
                        # 任务未运行，确保按钮可用（特别是恢复按钮）
                        self._set_buttons_running(False)
                        if self._polling_job_id == job_id:
                            self._polling_job_id = ""
                except Exception:
                    pass

    def _open_output(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            path = project_root() / "outputs"
        else:
            path = self._get_job_dir(job_id)
            if not path.is_dir():
                path = project_root() / "outputs"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # type: ignore[attr-defined]

    def _open_final_video(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择任务")
            return
        video = final_video_for_job(self._get_job_dir(job_id))
        if video is None:
            messagebox.showwarning("提示", "所选平台尚无最终视频")
            return
        os.startfile(str(video))  # type: ignore[attr-defined]

    def _show_job_context_menu(self, event: object) -> None:
        """右键点击历史任务时弹出上下文菜单。"""
        item = self.job_tree.identify_row(event.y)  # type: ignore[attr-defined]
        if item:
            self.job_tree.selection_set(item)
            self.job_tree.focus(item)
            self._highlight_selected_job()
            self.job_id_var.set(str(item))
            self._refresh_status()
            try:
                self.job_context_menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]
            finally:
                self.job_context_menu.grab_release()

    def _delete_selected_job(self) -> None:
        """删除选中的历史任务目录。"""
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先选择要删除的任务")
            return
        job_dir = self._get_job_dir(job_id)
        if not job_dir.is_dir():
            messagebox.showwarning("提示", f"任务目录不存在: {job_dir}")
            return
        # 如果任务正在运行，不允许删除
        if self._polling_job_id == job_id and self.running:
            messagebox.showwarning("提示", "任务正在运行中，请先停止后再删除")
            return
        confirm = messagebox.askyesno(
            "确认删除",
            f"确定要删除任务 [{job_id}] 吗？\n\n目录: {job_dir}\n\n此操作不可恢复。",
        )
        if not confirm:
            return
        try:
            shutil.rmtree(job_dir)
            self._log(f"已删除任务: {job_id}", "info")
            self._refresh_job_list()
            self._reset_stages()
            self._set_status("就绪")
        except Exception as e:
            messagebox.showerror("错误", f"删除失败: {e}")

    def _tail_log_files(self, data: dict) -> None:
        """增量读取当前及已完成阶段的日志文件，追加到运行日志窗口。"""
        job_id = self._polling_job_id
        if not job_id:
            return
        job_dir = self._get_job_dir(job_id)
        stages_info = data.get("stages", {})
        if not isinstance(stages_info, dict):
            return
        # 读取所有有 log_path 的阶段日志
        for stage_key, stage_info in stages_info.items():
            if not isinstance(stage_info, dict):
                continue
            log_rel = stage_info.get("log_path")
            if not isinstance(log_rel, str) or not log_rel:
                continue
            log_path = job_dir / log_rel
            if not log_path.is_file():
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
                                self._log(line, "")
            except Exception:
                pass

    def _open_model_selector(self) -> None:
        """打开 OpenRouter 模型选择窗口。"""
        dialog = ModelSelectionDialog(self.root)
        self.root.after(500, lambda: self._sync_model_label(dialog))

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
        # 启动后自动做一次环境检查
        self.root.after(500, self._run_doctor)
        self.root.mainloop()


def launch() -> None:
    """启动桌面窗口入口。"""
    app = AicfGUI()
    app.run()
