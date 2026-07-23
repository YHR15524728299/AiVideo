"""AI Content Factory - tkinter 桌面操作窗口。"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import (
    BooleanVar,
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
from .state_machine import PipelineStage

# 阶段顺序与中文名称（只展示主流程阶段）
STAGES = [
    (PipelineStage.DIRECTION_LOADED, "方向生成"),
    (PipelineStage.TOPICS_GENERATED, "候选选题"),
    (PipelineStage.TOPIC_SELECTED, "选题确定"),
    (PipelineStage.RESEARCHED, "资料研究"),
    (PipelineStage.SCRIPT_GENERATED, "脚本撰写"),
    (PipelineStage.SCRIPT_REVIEWED, "脚本审核"),
    (PipelineStage.CONTENT_PACKAGED, "内容打包"),
    (PipelineStage.AUDIO_GENERATED, "旁白合成"),
    (PipelineStage.STORYBOARD_GENERATED, "视觉分镜"),
    (PipelineStage.KEYFRAMES_GENERATED, "素材生成"),
    (PipelineStage.RENDERED, "视频渲染"),
    (PipelineStage.QA_CHECKED, "质量检查"),
    (PipelineStage.PACKAGED, "发布包生成"),
    (PipelineStage.COMPLETED, "完成"),
]

STAGE_INDEX = {stage: i for i, (stage, _) in enumerate(STAGES)}


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
        self.win.geometry("780x560")
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

                # 只保留免费模型
                free_models: list[dict] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    pricing = item.get("pricing", {})
                    promo = pricing.get("promo", "0") if isinstance(pricing, dict) else "0"
                    comp = pricing.get("completion", "0") if isinstance(pricing, dict) else "0"
                    try:
                        promo_f = float(promo)
                        comp_f = float(comp)
                    except (ValueError, TypeError):
                        continue
                    if promo_f != 0.0 or comp_f != 0.0:
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
            desc = str(m.get("description", ""))
            if keyword and keyword not in model_id.lower() and keyword not in desc.lower():
                continue
            provider = model_id.split("/")[0] if "/" in model_id else ""
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
    """返回当前 uv 环境的 Python 路径。"""
    return sys.executable


class AicfGUI:
    """AI Content Factory 桌面操作窗口。"""

    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("AI Content Factory - 自动成片工具")
        self.root.geometry("960x720")
        self.root.minsize(860, 640)

        # 日志队列：后台线程 -> UI
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.current_process: subprocess.Popen[str] | None = None

        # 字体
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        self.root.option_add("*Font", default_font)

        self._setup_styles()
        self._build_ui()
        self._refresh_job_list()
        self._poll_log_queue()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("EnvOk.TLabel", foreground="#2e7d32", font=("TkDefaultFont", 9, "bold"))
        style.configure("EnvFail.TLabel", foreground="#c62828", font=("TkDefaultFont", 9, "bold"))
        style.configure("Stage.TLabel", padding=(6, 4), relief="flat")
        style.configure("StageActive.TLabel", padding=(6, 4), background="#1976d2", foreground="white", font=("TkDefaultFont", 9, "bold"))
        style.configure("StageDone.TLabel", padding=(6, 4), background="#2e7d32", foreground="white", font=("TkDefaultFont", 9))
        style.configure("StageFail.TLabel", padding=(6, 4), background="#c62828", foreground="white", font=("TkDefaultFont", 9))

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
            lbl = ttk.Label(env_frame, text="未检查", style="EnvFail.TLabel")
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

        ttk.Label(setup_frame, text="内容方向:").grid(row=1, column=0, sticky="nw", pady=(6, 0), padx=(0, 6))
        self.direction_text = scrolledtext.ScrolledText(setup_frame, height=4, wrap="word", font=("Consolas", 10))
        self.direction_text.grid(row=1, column=1, columnspan=3, sticky="nsew", pady=(6, 0))
        default_dir = self._load_default_direction()
        if default_dir:
            self.direction_text.insert("1.0", default_dir)

        setup_frame.columnconfigure(3, weight=1)
        setup_frame.rowconfigure(1, weight=1)

        # ---- 操作按钮 ----
        btn_frame = ttk.Frame(root, padding=(10, 4))
        btn_frame.pack(fill="x")

        self.btn_start = ttk.Button(btn_frame, text="▶ 开始生成", command=self._start_job)
        self.btn_start.pack(side="left", padx=(0, 6))

        self.btn_resume = ttk.Button(btn_frame, text="⏵ 继续/恢复", command=self._resume_job)
        self.btn_resume.pack(side="left", padx=6)

        ttk.Button(btn_frame, text="🔄 刷新状态", command=self._refresh_status).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="📂 打开输出目录", command=self._open_output).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="⏹ 停止", command=self._stop_job).pack(side="left", padx=6)

        # ---- 阶段进度 ----
        stage_frame = ttk.LabelFrame(root, text="流水线进度", padding=8)
        stage_frame.pack(fill="x", padx=10, pady=4)

        self.stage_labels: list[ttk.Label] = []
        cols = 7
        for i, (_, name) in enumerate(STAGES):
            lbl = ttk.Label(stage_frame, text=name, style="Stage.TLabel", anchor="center")
            lbl.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=3)
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
            columns=("status", "stage", "updated"),
            show="headings",
            height=12,
        )
        self.job_tree.heading("status", text="状态")
        self.job_tree.heading("stage", text="当前阶段")
        self.job_tree.heading("updated", text="更新时间")
        self.job_tree.column("status", width=100, anchor="w")
        self.job_tree.column("stage", width=120, anchor="w")
        self.job_tree.column("updated", width=140, anchor="w")
        self.job_tree.pack(fill="both", expand=True)
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)

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

        # ---- 底部状态栏 ----
        self.status_var = StringVar(value="就绪")
        status_bar = ttk.Label(root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 3))
        status_bar.pack(fill="x", padx=10, pady=(0, 8))

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

    def _get_job_dir(self, job_id: str) -> Path:
        return project_root() / "outputs" / job_id

    def _get_status_path(self, job_id: str) -> Path:
        return self._get_job_dir(job_id) / "status.json"

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_buttons_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.btn_start.configure(state=state)
        self.btn_resume.configure(state=state)
        self.running = running

    # ------------------------------------------------------------------
    # 环境检查
    # ------------------------------------------------------------------
    def _run_doctor(self) -> None:
        self._log("正在检查环境...", "info")
        self._set_status("检查环境中...")

        def worker() -> None:
            try:
                result = subprocess.run(
                    [python_executable(), "-m", "aicf", "doctor"],
                    cwd=str(project_root()),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                )
                output = result.stdout + result.stderr
                self.log_queue.put(output + "\n")

                ok = result.returncode == 0
                self.root.after(0, lambda: self._update_env_lights(output, ok))
            except Exception as e:
                self.log_queue.put(f"环境检查失败: {e}\nerror")
                self.root.after(0, lambda: self._set_status("环境检查失败"))

        threading.Thread(target=worker, daemon=True).start()

    def _update_env_lights(self, output: str, ok: bool) -> None:
        # 简单关键词判断
        checks = {
            "openrouter": ("openrouter" in output.lower() and ("available" in output.lower() or "可用" in output or "ok" in output.lower())),
            "dreamina": ("dreamina" in output.lower() or "jimeng" in output.lower() or "即梦" in output) and not ("not found" in output.lower() or "missing" in output.lower()),
            "ffmpeg": ("ffmpeg" in output.lower()) and not ("not found" in output.lower() or "missing" in output.lower()),
            "tts": ("tts" in output.lower() or "edgetts" in output.lower() or "sapi" in output.lower()) and not ("not found" in output.lower() or "missing" in output.lower()),
        }
        for key, lbl in self.env_labels.items():
            if checks.get(key, False):
                lbl.configure(text="✓ 就绪", style="EnvOk.TLabel")
            else:
                lbl.configure(text="✗ 异常", style="EnvFail.TLabel")

        if ok:
            self._set_status("环境检查完成")
            self._log("环境检查通过", "success")
        else:
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

        self._set_buttons_running(True)
        self._set_status("运行中...")
        self._log(f"执行命令: {' '.join(args)}", "info")

        def worker() -> None:
            try:
                env = os.environ.copy()
                if env_extra:
                    env.update(env_extra)
                self.current_process = subprocess.Popen(
                    args,
                    cwd=str(cwd or project_root()),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    bufsize=1,
                )
                assert self.current_process.stdout is not None
                for line in self.current_process.stdout:
                    line = line.rstrip("\n\r")
                    tag = ""
                    low = line.lower()
                    if any(k in low for k in ("error", "失败", "failed", "exception")):
                        tag = "error"
                    elif any(k in low for k in ("passed", "成功", "completed", "ready", "通过")):
                        tag = "success"
                    self.log_queue.put(line + "\n" + tag)
                self.current_process.wait()
                code = self.current_process.returncode
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
        self._set_buttons_running(False)
        self._set_status("就绪")
        self._refresh_status()
        self._refresh_job_list()

    def _start_job(self) -> None:
        job_id = self.job_id_var.get().strip() or self._auto_job_id()
        self.job_id_var.set(job_id)
        self._ensure_direction_file()
        self._log(f"任务 [{job_id}] 开始生成", "info")
        self._run_command_async(
            [python_executable(), "-m", "aicf", "autopilot", "--job", job_id],
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _resume_job(self) -> None:
        job_id = self._current_job_id()
        if not job_id:
            messagebox.showwarning("提示", "请先在历史任务中选择一个任务，或在任务ID框中输入")
            return
        self._log(f"任务 [{job_id}] 继续/恢复", "info")
        self._run_command_async(
            [python_executable(), "-m", "aicf", "resume", "--job", job_id],
            env_extra={"AICF_PROJECT_ROOT": str(project_root())},
        )

    def _stop_job(self) -> None:
        if self.current_process and self.running:
            self.current_process.terminate()
            self._log("已发送停止信号", "info")

    def _current_job_id(self) -> str:
        sel = self.job_tree.selection()
        if sel:
            return str(sel[0])
        return self.job_id_var.get().strip()

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
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
        if cur == "COMPLETED" or data.get("status") == "ready_to_publish":
            self._set_status(f"任务 {job_id} 已完成 ✓")
        elif failed:
            self._set_status(f"任务 {job_id} 在 [{failed}] 失败/等待恢复")
        else:
            self._set_status(f"任务 {job_id} 当前阶段: {cur}")

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

    def _refresh_job_list(self) -> None:
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
                    if st == "ready_to_publish" or cur == "COMPLETED":
                        status_text = "✓ 已完成"
                    elif failed:
                        status_text = "✗ 失败/等待"
                    elif cur and cur != "INIT":
                        status_text = "▶ 进行中"
                    else:
                        status_text = "⏸ 已初始化"
                    stage_text = failed or cur or "-"
                    ts = d.get("updated_at") or d.get("started_at") or ""
                    if ts:
                        updated = str(ts)[:16].replace("T", " ")
                    else:
                        updated = datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    status_text = "状态异常"
            self.job_tree.insert("", "end", iid=job_dir.name, values=(status_text, stage_text, updated))

    def _on_job_select(self, _event: object = None) -> None:
        sel = self.job_tree.selection()
        if sel:
            self.job_id_var.set(str(sel[0]))
            self._refresh_status()

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
    def run(self) -> None:
        # 启动后自动做一次环境检查
        self.root.after(500, self._run_doctor)
        self.root.mainloop()


def launch() -> None:
    """启动桌面窗口入口。"""
    app = AicfGUI()
    app.run()
