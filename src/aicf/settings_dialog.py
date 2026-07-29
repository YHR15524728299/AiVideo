"""集中式设置对话框。

设计原则：
🔍 自动检测项（系统自动探测，只显示状态，失败时可手动指定）：
  - FFmpeg、即梦CLI、可灵CLI、TTS引擎
✏️ 手动配置项（需要用户填写/选择）：
  - OpenRouter API Key、默认模型、默认偏好设置
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import (
    BOTH, DISABLED, END, LEFT, RIGHT, NORMAL, WORD,
    StringVar, BooleanVar, Toplevel, filedialog, messagebox,
)
from tkinter import ttk
import tkinter.scrolledtext as st

from .production_settings import (
    KLING_MODEL_DISPLAY_NAMES, JIMENG_MODEL_DISPLAY_NAMES,
    MOTION_MODE_DISPLAY_NAMES, VIDEO_PROVIDER_DISPLAY_NAMES,
    VOICE_DISPLAY_NAMES, VOICE_GROUP_ORDER, ORIENTATION_DISPLAY_NAMES,
    PLATFORM_DISPLAY_NAMES, ProductionSettings,
)
from .providers.jimeng import detect_jimeng_cli
from .providers.kling import detect_kling_cli
from .providers.tts import discover_ffmpeg_toolchain, KokoroTtsProvider
from .logging_utils import sanitize_error
# 提前导入doctor，避免线程中首次导入
from . import doctor as _doctor_mod


def _safe_path(path: str | Path) -> str:
    """对路径进行脱敏处理。"""
    return sanitize_error(str(path))


def _load_env(path: Path) -> dict[str, str]:
    r: dict[str, str] = {}
    if not path.is_file():
        return r
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        r[k.strip()] = v.strip().strip('"').strip("'")
    return r


def _format_env_value(v: str) -> str:
    """格式化环境变量值，含空格或特殊字符时加双引号。"""
    v = v.strip()
    if not v:
        return '""'
    # 如果含空格、#、=等特殊字符，需要加引号
    if any(c in v for c in (' ', '\t', '#', '=', '"', "'")):
        # 转义内部双引号
        escaped = v.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return v

def _save_env(path: Path, vals: dict[str, str]) -> None:
    lines: list[str] = []
    existing: set[str] = set()
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        for l in lines:
            s = l.strip()
            if s and not s.startswith("#") and "=" in s:
                existing.add(s.split("=", 1)[0].strip())
    new_lines: list[str] = []
    updated: set[str] = set()
    for l in lines:
        s = l.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in vals:
                new_lines.append(f"{k}={_format_env_value(vals[k])}")
                updated.add(k)
                continue
        new_lines.append(l)
    for k in vals:
        if k not in existing and k not in updated:
            if new_lines and new_lines[-1].strip() != "":
                new_lines.append("")
            new_lines.append(f"{k}={_format_env_value(vals[k])}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# 状态指示灯
# ---------------------------------------------------------------------------
class _Dot(ttk.Frame):
    COLORS = {
        "ok": ("● 已就绪", "#22c55e"),
        "warn": ("● 需注意", "#f59e0b"),
        "error": ("● 未配置", "#ef4444"),
        "testing": ("● 检测中", "#3b82f6"),
    }
    def __init__(self, master, status="testing"):
        super().__init__(master)
        self._dot = ttk.Label(self, font=("Segoe UI", 11))
        self._dot.pack(side=LEFT)
        self._txt = ttk.Label(self, font=("Segoe UI", 9))
        self._txt.pack(side=LEFT, padx=(4, 0))
        self.set(status)
    def set(self, status, text=None):
        d, c = self.COLORS.get(status, self.COLORS["error"])
        self._dot.configure(text=d.split()[0], foreground=c)
        self._txt.configure(text=text or d[2:])


# ---------------------------------------------------------------------------
# 自动检测项卡片（全部用pack布局，简单可靠）
# ---------------------------------------------------------------------------
class _AutoCard(ttk.LabelFrame):
    def __init__(self, master, title, desc, icon="🔧", browse_dir=False):
        super().__init__(master, text=f" {icon}  {title} ", padding=10)
        self._browse_dir = browse_dir
        self._cb_apply = None
        self._status = "testing"  # ok / warn / error / testing

        # 顶栏：状态 + 描述 + 按钮
        top = ttk.Frame(self)
        top.pack(fill="x")

        self.dot = _Dot(top, "testing")
        self.dot.pack(side=LEFT)

        mid = ttk.Frame(top)
        mid.pack(side=LEFT, fill="x", expand=True, padx=10)
        ttk.Label(mid, text=desc, foreground="#4b5563", wraplength=420, justify="left").pack(anchor="w")
        self.detail = ttk.Label(mid, text="", foreground="#6b7280", font=("Segoe UI", 8))
        self.detail.pack(anchor="w", pady=(2, 0))

        self.btn_frame = ttk.Frame(top)
        self.btn_frame.pack(side=RIGHT)

        # 手动指定区（默认隐藏）
        self.manual_area = ttk.Frame(self)
        ttk.Label(
            self.manual_area,
            text="自动检测失败，可手动指定位置：",
            foreground="#92400e", font=("Segoe UI", 8)
        ).pack(anchor="w", pady=(0, 4))
        mrow = ttk.Frame(self.manual_area)
        mrow.pack(fill="x")
        self.var = StringVar()
        ttk.Entry(mrow, textvariable=self.var, width=40).pack(side=LEFT, fill="x", expand=True, padx=(0, 4))
        ttk.Button(mrow, text="浏览...", width=7, command=self._browse).pack(side=LEFT, padx=(0, 4))
        ttk.Button(mrow, text="应用", width=8, command=self._apply).pack(side=LEFT)

    def add_btn(self, text, cmd):
        b = ttk.Button(self.btn_frame, text=text, command=cmd)
        b.pack(side=LEFT, padx=(4, 0))
        return b

    def on_apply(self, cb):
        self._cb_apply = cb

    def ok(self, text=""):
        self._status = "ok"
        self.dot.set("ok")
        self.detail.configure(text=text, foreground="#22c55e")
        self.manual_area.pack_forget()

    def warn(self, text=""):
        self._status = "warn"
        self.dot.set("warn")
        self.detail.configure(text=text, foreground="#f59e0b")
        self.manual_area.pack_forget()

    def err(self, text="", show_manual=False):
        self._status = "error"
        self.dot.set("error")
        self.detail.configure(text=text, foreground="#ef4444")
        if show_manual:
            self.manual_area.pack(fill="x", pady=(10, 0))
        else:
            self.manual_area.pack_forget()

    def testing(self, text="检测中..."):
        self._status = "testing"
        self.dot.set("testing")
        self.detail.configure(text=text, foreground="#3b82f6")

    def get_status(self):
        return self._status

    def _browse(self):
        if self._browse_dir:
            p = filedialog.askdirectory(title="选择目录")
        else:
            p = filedialog.askopenfilename(
                title="选择可执行文件",
                filetypes=[("可执行文件", "*.exe *.cmd *.bat"), ("所有文件", "*.*")]
            )
        if p:
            self.var.set(p)

    def _apply(self):
        v = self.var.get().strip()
        if v and self._cb_apply:
            self._cb_apply(v)

    def get_val(self):
        return self.var.get().strip()

    def set_val(self, v):
        self.var.set(v)


# ---------------------------------------------------------------------------
# 总览页
# ---------------------------------------------------------------------------
class _Overview(ttk.Frame):
    def __init__(self, master, on_go):
        super().__init__(master, padding=16)
        self._on_go = on_go
        ttk.Label(self, text="设置总览", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self,
            text="🔍 自动检测项（安装后系统自动识别）  ✏️ 手动配置项（需要你填写）",
            foreground="#6b7280",
        ).pack(anchor="w", pady=(0, 16))

        self.dots = {}
        items = [
            ("ai", "✏️", "AI 大模型 (OpenRouter API Key)", "脚本生成需要", 0),
            ("vjm", "🔍", "即梦 CLI", "文生视频（可选其一）", 1),
            ("vkl", "🔍", "可灵 CLI", "文生视频（可选其一）", 1),
            ("tts", "🔍", "语音合成", "旁白音频", 2),
            ("ff", "🔍", "FFmpeg", "视频渲染必需", 2),
        ]
        for key, icon, title, desc, tab in items:
            row = ttk.Frame(self)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=icon).pack(side=LEFT)
            d = _Dot(row, "testing")
            d.pack(side=LEFT, padx=(8, 0))
            ttk.Label(row, text=title, font=("Segoe UI", 10, "bold")).pack(side=LEFT, padx=(8, 8))
            ttk.Label(row, text=desc, foreground="#6b7280").pack(side=LEFT)
            ttk.Button(row, text="前往配置", command=lambda t=tab: self._on_go(t)).pack(side=RIGHT)
            self.dots[key] = d

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=16)
        self.summary = ttk.Label(self, text="", font=("Segoe UI", 10), wraplength=640, justify="left")
        self.summary.pack(anchor="w")
        ttk.Button(self, text="🔄 重新检测所有自动项", command=lambda: self.event_generate("<<Refresh>>")).pack(anchor="w", pady=(8, 0))

    def set(self, key, status, text=""):
        self.dots[key].set(status, text)

    def set_summary(self, text):
        self.summary.configure(text=text)


# ---------------------------------------------------------------------------
# ✏️ AI 模型页
# ---------------------------------------------------------------------------
class _AIPage(ttk.Frame):
    def __init__(self, master, env_path):
        super().__init__(master, padding=16)
        self._env = _load_env(env_path)

        ttk.Label(self, text="✏️  AI 大模型配置", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(self, text="API Key 需要你手动填写", foreground="#6b7280").pack(anchor="w", pady=(0, 12))

        key_box = ttk.LabelFrame(self, text=" 🔑 OpenRouter API Key（必填） ", padding=12)
        key_box.pack(fill="x", pady=4)
        ttk.Label(
            key_box,
            text="前往 openrouter.ai 免费注册，在「API Keys」页面创建密钥后粘贴到下方。密钥仅保存在本地。",
            foreground="#4b5563", wraplength=580, justify="left",
        ).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(key_box)
        row.pack(fill="x")
        self.key_var = StringVar(value=self._env.get("OPENROUTER_API_KEY", ""))
        self._entry = ttk.Entry(row, textvariable=self.key_var, show="•", width=50)
        self._entry.pack(side=LEFT, fill="x", expand=True, padx=(0, 4))
        self._show_btn = ttk.Button(row, text="显示", width=6, command=self._toggle)
        self._show_btn.pack(side=LEFT, padx=(0, 4))
        self._test_btn = ttk.Button(row, text="测试连接", width=10, command=self._test)
        self._test_btn.pack(side=LEFT, padx=(0, 4))
        ttk.Button(row, text="去注册 →", width=10, command=self._signup).pack(side=LEFT)

        self.keydot = _Dot(key_box)
        self.keydot.pack(anchor="w", pady=(8, 0))
        self._showing = False
        self._update_key_status()
        # 实时更新状态
        self.key_var.trace_add("write", lambda *_: self._update_key_status())

        model_box = ttk.LabelFrame(self, text=" 🧠 默认模型 ", padding=12)
        model_box.pack(fill="x", pady=(12, 4))
        ttk.Label(model_box, text="选择一个免费模型（必须以 :free 结尾）。", foreground="#6b7280").pack(anchor="w", pady=(0, 8))
        self.model_var = StringVar(value=self._env.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free"))
        models = [
            "deepseek/deepseek-chat-v3-0324:free",
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-4-maverick:free",
            "qwen/qwen3-235b-a22b:free",
        ]
        ttk.Combobox(model_box, textvariable=self.model_var, values=models, width=50).pack(fill="x")

    def _toggle(self):
        self._showing = not self._showing
        self._entry.configure(show="" if self._showing else "•")
        self._show_btn.configure(text="隐藏" if self._showing else "显示")

    def _update_key_status(self):
        if self.key_var.get().strip():
            self.keydot.set("ok", "已填写")
        else:
            self.keydot.set("error", "未填写 — 必填")

    def _signup(self):
        import webbrowser
        webbrowser.open("https://openrouter.ai/keys")

    def _test(self):
        self.keydot.set("testing", "测试中...")
        self._test_btn.configure(state=DISABLED)
        def worker():
            try:
                import urllib.request, json as _j
                key = self.key_var.get().strip()
                if not key:
                    self.after(0, lambda: self._done(False, "请先填写 API Key"))
                    return
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/models?limit=1",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = _j.loads(r.read().decode())
                    if "data" in d:
                        self.after(0, lambda: self._done(True, "连接成功，Key 有效"))
                    else:
                        self.after(0, lambda: self._done(False, "返回异常"))
            except Exception as e:
                self.after(0, lambda: self._done(False, f"失败：{str(e)[:80]}"))
        threading.Thread(target=worker, daemon=True).start()

    def _done(self, ok, msg):
        self._test_btn.configure(state=NORMAL)
        self.keydot.set("ok" if ok else "error", msg)

    def collect(self):
        return {
            "OPENROUTER_API_KEY": self.key_var.get().strip(),
            "OPENROUTER_MODEL": self.model_var.get().strip(),
        }

    def key_status(self):
        return ("ok", "已配置") if self.key_var.get().strip() else ("error", "未配置")


# ---------------------------------------------------------------------------
# 🔍 视频生成页
# ---------------------------------------------------------------------------
class _VideoPage(ttk.Frame):
    def __init__(self, master, env_path, on_status_change=None):
        super().__init__(master, padding=16)
        self._env = _load_env(env_path)
        self._on_status_change = on_status_change

        ttk.Label(self, text="🔍  视频生成服务", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self,
            text="系统自动检测 CLI。登录请在终端完成，GUI 不处理密码。即梦和可灵至少配一个。",
            foreground="#6b7280", wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.jm = _AutoCard(self, "即梦（Dreamina / 豆包·Seedance）",
                            "字节跳动文生视频服务，安装并登录后自动检测。", icon="🎬")
        self.jm.pack(fill="x", pady=4)
        self.jm.add_btn("🔄 重新检测", self._detect_jm)
        self.jm.add_btn("📋 登录步骤", self._jm_help)
        self.jm.add_btn("💻 打开终端", self._terminal)
        self.jm.on_apply(lambda p: self._detect_jm(p))
        sv = self._env.get("JIMENG_CLI_EXECUTABLE", "")
        if sv:
            self.jm.set_val(sv)

        self.kl = _AutoCard(self, "可灵（Kling AI）",
                            "快手文生视频服务，npm 安装 CLI 并登录后自动检测。", icon="🎬")
        self.kl.pack(fill="x", pady=4)
        self.kl.add_btn("🔄 重新检测", self._detect_kl)
        self.kl.add_btn("📋 安装/登录", self._kl_help)
        self.kl.add_btn("💻 打开终端", self._terminal)
        self.kl.on_apply(lambda p: self._detect_kl(p))
        skv = self._env.get("KLING_CLI_EXECUTABLE", "")
        if skv:
            self.kl.set_val(skv)

        self.after(200, self._detect_jm)
        self.after(400, self._detect_kl)

    def _notify(self):
        if self._on_status_change:
            self.after(100, self._on_status_change)

    def _detect_jm(self, manual=None):
        self.jm.testing()
        self._notify()
        manual_path = manual if manual is not None else self._env.get("JIMENG_CLI_EXECUTABLE", "")
        def w():
            try:
                if manual_path and manual_path.strip():
                    caps = detect_jimeng_cli(
                        candidates=[[manual_path.strip()]],
                        timeout_seconds=5,
                    )
                else:
                    caps = detect_jimeng_cli(timeout_seconds=5)
                self.after(0, lambda: self._jm_ok(caps))
            except Exception as e:
                msg = str(e)
                import re
                # 匹配路径（不区分大小写，支持.exe/.cmd）
                paths = re.findall(r'([A-Za-z]:\\[^\s:；]+?\.(?:exe|EXE|cmd|CMD))', msg)
                if paths:
                    p = _safe_path(paths[0])
                    self.after(0, lambda: self.jm.warn(f"已找到 CLI，但可能需要登录。终端运行 dreamina login。路径：{p}"))
                else:
                    self.after(0, lambda: self.jm.err("未检测到即梦 CLI。", show_manual=True))
                self.after(0, self._notify)
        threading.Thread(target=w, daemon=True).start()

    def _jm_ok(self, caps):
        safe_path = _safe_path(caps.cli_path) if caps.cli_path else ""
        if caps.cli_path and caps.supports_async_task:
            self.jm.ok(f"路径：{safe_path}")
        elif caps.cli_path:
            self.jm.warn(f"已找到 CLI，但需要登录。终端运行 dreamina login。路径：{safe_path}")
        else:
            self.jm.err("未检测到。请安装后点「重新检测」，或手动指定路径。", show_manual=True)
        self._notify()

    def _detect_kl(self, manual=None):
        self.kl.testing()
        self._notify()
        manual_path = manual if manual is not None else self._env.get("KLING_CLI_EXECUTABLE", "")
        if manual_path and manual_path.strip():
            os.environ["KLING_CLI_EXECUTABLE"] = manual_path.strip()
        def w():
            try:
                caps = detect_kling_cli()
                self.after(0, lambda: self._kl_ok(caps))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: self.kl.err(f"检测失败：{msg[:60]}", show_manual=True))
                self.after(0, self._notify)
        threading.Thread(target=w, daemon=True).start()

    def _kl_ok(self, caps):
        safe_path = _safe_path(caps.cli_path) if caps.cli_path else ""
        if caps.cli_path and caps.supports_async_task:
            self.kl.ok(f"路径：{safe_path}")
        elif caps.cli_path:
            self.kl.warn("已安装但未登录。终端运行 kling login")
        else:
            self.kl.err("未检测到。运行 npm i -g @klingai/cli-cn 安装，然后 kling login。", show_manual=True)
        self._notify()

    def _jm_help(self):
        messagebox.showinfo("即梦登录", "1. 安装即梦 CLI（dreamina.exe）\n2. 点「打开终端」\n3. 运行 dreamina login\n4. 扫码登录\n5. 点「重新检测」")

    def _kl_help(self):
        messagebox.showinfo("可灵安装", "1. 安装 Node.js\n2. 点「打开终端」\n3. npm i -g @klingai/cli-cn\n4. kling login\n5. 点「重新检测」")

    def _terminal(self):
        try:
            subprocess.Popen(["powershell.exe", "-NoExit"], cwd=str(_root()))
        except:
            os.startfile(str(_root()))

    def collect(self):
        r = {}
        v = self.jm.get_val()
        if v:
            r["JIMENG_CLI_EXECUTABLE"] = v
        kv = self.kl.get_val()
        if kv:
            r["KLING_CLI_EXECUTABLE"] = kv
        return r

    def statuses(self):
        def s(card):
            st = card.get_status()
            t = card.detail.cget("text")
            if st == "ok":
                return ("ok", "已就绪")
            elif st == "warn":
                if "未登录" in t or "需要登录" in t:
                    return ("warn", "需登录")
                return ("warn", "部分可用")
            return ("error", "未配置")
        return {"vjm": s(self.jm), "vkl": s(self.kl)}


# ---------------------------------------------------------------------------
# 🔍 TTS / FFmpeg 页
# ---------------------------------------------------------------------------
class _TtsPage(ttk.Frame):
    def __init__(self, master, env_path, on_status_change=None):
        super().__init__(master, padding=16)
        self._env = _load_env(env_path)
        self._on_status_change = on_status_change

        ttk.Label(self, text="🔍  语音合成 & FFmpeg", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(self, text="自动检测，安装对应组件即可。", foreground="#6b7280").pack(anchor="w", pady=(0, 12))

        self.kokoro = _AutoCard(self, "Kokoro 本地 TTS（推荐）",
                                "离线神经网络语音合成，音质最好。", icon="🔊")
        self.kokoro.pack(fill="x", pady=4)
        self.kokoro.add_btn("🔄 重新检测", self._detect)
        self.kokoro.add_btn("📋 说明", lambda: messagebox.showinfo(
            "Kokoro", "本地离线 TTS，音质最好。\n安装：uv add kokoro-onnx misaki[zh] soundfile\n首次使用自动下载模型（约300MB）。"))

        self.edge = _AutoCard(self, "Edge TTS（微软在线）",
                              "微软免费在线语音，需要网络。", icon="🔊")
        self.edge.pack(fill="x", pady=4)
        self.edge.add_btn("🔄 重新检测", self._detect)
        self.edge.add_btn("📋 说明", lambda: messagebox.showinfo("Edge TTS", "在线语音，需要网络。\n安装：uv add edge-tts"))

        self.sapi = _AutoCard(self, "Windows SAPI（系统回退）",
                              "Windows 自带语音，始终可用。", icon="🔊")
        self.sapi.pack(fill="x", pady=4)

        self.ff = _AutoCard(self, "FFmpeg（视频渲染必需）",
                            "音视频处理工具。winget install Gyan.FFmpeg 安装后自动检测。",
                            icon="🎞️", browse_dir=True)
        self.ff.pack(fill="x", pady=4)
        self.ff.add_btn("🔄 重新检测", self._detect)
        self.ff.add_btn("📋 说明", lambda: messagebox.showinfo(
            "FFmpeg", "视频渲染必需。\n安装：winget install Gyan.FFmpeg\n安装后重启本工具。"))
        self.ff.on_apply(self._on_ff_apply)
        sv = self._env.get("AICF_FFMPEG_DIR", "")
        if sv:
            self.ff.set_val(sv)

        self.after(300, self._detect)

    def _on_ff_apply(self, p):
        os.environ["AICF_FFMPEG_DIR"] = p
        self._detect()

    def _notify(self):
        if self._on_status_change:
            self.after(100, self._on_status_change)

    def _detect(self):
        for c in (self.kokoro, self.edge, self.sapi, self.ff):
            c.testing()
        self._notify()
        def w():
            # Kokoro检测
            try:
                k = KokoroTtsProvider()
                if k.available():
                    self.after(0, lambda: self.kokoro.ok("本地模型就绪"))
                else:
                    self.after(0, lambda: self.kokoro.warn("已安装但模型未下载（首次使用自动下载）"))
            except ImportError:
                self.after(0, lambda: self.kokoro.warn("未安装（可选）"))
            except Exception as e:
                self.after(0, lambda: self.kokoro.warn(f"检测异常：{str(e)[:30]}"))
            # Edge TTS检测
            try:
                import importlib.util
                if importlib.util.find_spec("edge_tts"):
                    self.after(0, lambda: self.edge.ok("edge-tts 已安装"))
                else:
                    self.after(0, lambda: self.edge.warn("未安装（可选）"))
            except:
                self.after(0, lambda: self.edge.err("检测失败"))
            # SAPI检测
            if sys.platform == "win32":
                self.after(0, lambda: self.sapi.ok("Windows 系统语音可用"))
            else:
                self.after(0, lambda: self.sapi.err("仅支持 Windows"))
            # FFmpeg检测
            try:
                tc = discover_ffmpeg_toolchain()
                self.after(0, lambda: self.ff.ok(f"已找到：{_safe_path(tc.ffmpeg)}"))
            except Exception as e:
                c = self.ff.get_val()
                if c:
                    self.after(0, lambda: self.ff.err(f"指定目录未找到：{_safe_path(c)}", show_manual=True))
                else:
                    self.after(0, lambda: self.ff.err("未找到。请安装 FFmpeg。", show_manual=True))
            self.after(0, self._notify)
        threading.Thread(target=w, daemon=True).start()

    def collect(self):
        r = {}
        v = self.ff.get_val()
        if v:
            r["AICF_FFMPEG_DIR"] = v
        return r

    def statuses(self):
        # TTS状态：任一高级TTS可用则ok，否则SAPI可用为warn
        k_ok = self.kokoro.get_status() == "ok"
        e_ok = self.edge.get_status() == "ok"
        s_ok = self.sapi.get_status() == "ok"
        if k_ok or e_ok:
            tts_st = ("ok", "已就绪")
        elif s_ok:
            tts_st = ("warn", "仅系统语音")
        else:
            tts_st = ("error", "未配置")
        # FFmpeg状态
        if self.ff.get_status() == "ok":
            ff_st = ("ok", "已就绪")
        else:
            ff_st = ("error", "未找到")
        return {"tts": tts_st, "ff": ff_st}


# ---------------------------------------------------------------------------
# ✏️ 默认偏好页
# ---------------------------------------------------------------------------
class _DefaultsPage(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=16)
        path = _root() / "config" / "default_settings.json"
        self._path = path
        d = {}
        if path.is_file():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except:
                pass

        # 标题区域（用pack）
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="✏️  默认偏好", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(header, text="新建任务时的默认选项，可在主界面修改。", foreground="#6b7280").pack(anchor="w", pady=(4, 0))

        # 表单区域（单独Frame，内部用grid）
        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        r = 0
        def row(label):
            nonlocal r
            ttk.Label(form, text=label, font=("Segoe UI", 10, "bold")).grid(
                row=r, column=0, sticky="w", pady=6, padx=(0, 16))
            r += 1
            return r - 1

        def put_combo(row_idx, var, values, width=25, desc=None):
            cb = ttk.Combobox(form, textvariable=var, values=values, state="readonly", width=width)
            cb.grid(row=row_idx, column=1, sticky="w", pady=6)
            if desc:
                ttk.Label(form, text=desc, foreground="#6b7280", font=("Segoe UI", 8)).grid(
                    row=row_idx+1, column=1, sticky="w", pady=(0, 4))

        self.prov_disp = StringVar(value=VIDEO_PROVIDER_DISPLAY_NAMES.get(d.get("video_provider", "jimeng"), "即梦"))
        put_combo(row("默认视频生成"), self.prov_disp, tuple(VIDEO_PROVIDER_DISPLAY_NAMES.values()))

        self.jm_disp = StringVar(value=JIMENG_MODEL_DISPLAY_NAMES.get(d.get("jimeng_model", "seedance2.0fast"), "Seedance 2.0 极速"))
        put_combo(row("默认即梦模型"), self.jm_disp, tuple(JIMENG_MODEL_DISPLAY_NAMES.values()))

        self.kl_disp = StringVar(value=KLING_MODEL_DISPLAY_NAMES.get(d.get("kling_model", "kling-video-v2_6"), "可灵 2.6 高品质"))
        put_combo(row("默认可灵模型"), self.kl_disp, tuple(KLING_MODEL_DISPLAY_NAMES.values()))

        self.res = StringVar(value=d.get("video_resolution", "720p"))
        put_combo(row("默认分辨率"), self.res, ("720p", "1080p"))

        self.mot_disp = StringVar(value=MOTION_MODE_DISPLAY_NAMES.get(d.get("motion_mode", "video"), "视频模式"))
        put_combo(row("默认动态模式"), self.mot_disp, tuple(MOTION_MODE_DISPLAY_NAMES.values()),
                  desc="视频=动态视频；图片=静帧")

        self.ori_disp = StringVar(value=ORIENTATION_DISPLAY_NAMES.get(d.get("orientation", "portrait"), "竖屏 (9:16)"))
        put_combo(row("默认视频方向"), self.ori_disp, tuple(ORIENTATION_DISPLAY_NAMES.values()))

        vd = d.get("narration_voice", "kokoro:zm_yunyang")
        self.voi_disp = StringVar(value=VOICE_DISPLAY_NAMES.get(vd, "Kokoro·云扬"))
        put_combo(row("默认旁白音色"), self.voi_disp, tuple(VOICE_DISPLAY_NAMES[v] for v in VOICE_GROUP_ORDER))

        plat_row = row("默认导出平台")
        pf = ttk.Frame(form)
        pf.grid(row=plat_row, column=1, sticky="w", pady=6)
        self.pvars = {}
        dp = d.get("selected_platforms", ["douyin"])
        for k, n in PLATFORM_DISPLAY_NAMES.items():
            v = BooleanVar(value=k in dp)
            self.pvars[k] = v
            ttk.Checkbutton(pf, text=n, variable=v).pack(side=LEFT, padx=(0, 12))

    def collect(self):
        def kd(m, disp):
            for k, v in m.items():
                if v == disp:
                    return k
            return next(iter(m))
        plats = [k for k, v in self.pvars.items() if v.get()] or ["douyin"]
        return {
            "video_provider": kd(VIDEO_PROVIDER_DISPLAY_NAMES, self.prov_disp.get()),
            "jimeng_model": kd(JIMENG_MODEL_DISPLAY_NAMES, self.jm_disp.get()),
            "kling_model": kd(KLING_MODEL_DISPLAY_NAMES, self.kl_disp.get()),
            "video_resolution": self.res.get(),
            "motion_mode": kd(MOTION_MODE_DISPLAY_NAMES, self.mot_disp.get()),
            "orientation": kd(ORIENTATION_DISPLAY_NAMES, self.ori_disp.get()),
            "narration_voice": kd(VOICE_DISPLAY_NAMES, self.voi_disp.get()),
            "selected_platforms": plats,
        }


# ---------------------------------------------------------------------------
# ℹ️ 诊断页
# ---------------------------------------------------------------------------
class _AboutPage(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=16)
        ttk.Label(self, text="ℹ️ 关于 & 诊断", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 12))
        self.txt = st.ScrolledText(self, height=18, wrap=WORD, font=("Consolas", 9))
        self.txt.pack(fill=BOTH, expand=True, pady=(0, 8))
        bf = ttk.Frame(self)
        bf.pack(fill="x")
        ttk.Button(bf, text="🔄 重新诊断", command=self._run).pack(side=LEFT)
        ttk.Button(bf, text="📂 数据目录", command=lambda: os.startfile(str(_root() / "data"))).pack(side=LEFT, padx=8)
        ttk.Button(bf, text="📂 配置目录", command=lambda: os.startfile(str(_root() / "config"))).pack(side=LEFT)
        self.after(200, self._run)

    def _run(self):
        self.txt.delete("1.0", END)
        self.txt.insert("1.0", "诊断中...\n")
        def w():
            try:
                import io, contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    _doctor_mod.Doctor().run()
                out = buf.getvalue()
                self.after(0, lambda: (self.txt.delete("1.0", END), self.txt.insert("1.0", out)))
            except Exception as e:
                self.after(0, lambda: self.txt.insert(END, f"\n错误：{e}"))
        threading.Thread(target=w, daemon=True).start()


# ---------------------------------------------------------------------------
# 主对话框
# ---------------------------------------------------------------------------
class SettingsDialog(Toplevel):
    def __init__(self, master, on_saved=None, first_time=False):
        super().__init__(master)
        self.title("设置")
        self.geometry("780x680")
        self.minsize(700, 580)
        self.transient(master)
        self.grab_set()
        self._on_saved = on_saved

        env_path = _root() / ".env"
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill=BOTH, expand=True)

        nb = ttk.Notebook(outer)
        nb.pack(fill=BOTH, expand=True)

        self.ov = _Overview(nb, on_go=lambda i: nb.select(i+1))
        self.ai = _AIPage(nb, env_path)
        self.vp = _VideoPage(nb, env_path, on_status_change=self._refresh_ov)
        self.tp = _TtsPage(nb, env_path, on_status_change=self._refresh_ov)
        self.dp = _DefaultsPage(nb)
        self.ap = _AboutPage(nb)

        nb.add(self.ov, text="📊 总览")
        nb.add(self.ai, text="✏️ AI 大模型")
        nb.add(self.vp, text="🔍 视频生成")
        nb.add(self.tp, text="🔍 语音/FFmpeg")
        nb.add(self.dp, text="✏️ 默认偏好")
        nb.add(self.ap, text="ℹ️ 诊断")

        self.ov.bind("<<Refresh>>", lambda e: self._refresh_all())

        bf = ttk.Frame(outer)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="保存", command=self._save).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(bf, text="关闭", command=self._on_close).pack(side=RIGHT)
        self._status = ttk.Label(bf, text="", foreground="#6b7280")
        self._status.pack(side=LEFT)
        self._dirty = False

        # 监听输入变化，标记dirty
        def mark_dirty(*_):
            self._dirty = True
        # AI页面
        if hasattr(self.ai, 'key_var'):
            self.ai.key_var.trace_add("write", mark_dirty)
        if hasattr(self.ai, 'model_var'):
            self.ai.model_var.trace_add("write", mark_dirty)

        # 定时刷新总览：600ms、1.5s、3s、6s（覆盖检测完成时间）
        self.after(600, self._refresh_ov)
        self.after(1500, self._refresh_ov)
        self.after(3000, self._refresh_ov)
        self.after(6500, self._refresh_ov)
        if first_time:
            self.after(800, self._welcome)

    def _refresh_all(self):
        self.vp._detect_jm()
        self.vp._detect_kl()
        self.tp._detect()
        self.after(800, self._refresh_ov)

    def _refresh_ov(self):
        ai_s, ai_t = self.ai.key_status()
        self.ov.set("ai", ai_s, ai_t)
        for k, (s, t) in self.vp.statuses().items():
            self.ov.set(k, s, t)
        for k, (s, t) in self.tp.statuses().items():
            self.ov.set(k, s, t)
        errs = []
        warns = []
        if ai_s != "ok":
            errs.append("OpenRouter API Key（✏️ 手动填写）")
        vj, _ = self.vp.statuses().get("vjm", ("err", ""))
        vk, _ = self.vp.statuses().get("vkl", ("err", ""))
        if vj != "ok" and vk != "ok":
            errs.append("至少一个视频服务（🔍 安装后自动检测）")
        elif vj == "warn" or vk == "warn":
            warns.append("有视频服务未登录")
        fs, _ = self.tp.statuses().get("ff", ("err", ""))
        if fs != "ok":
            errs.append("FFmpeg（🔍 安装后自动检测）")
        if errs:
            self.ov.set_summary("⚠️ 需要配置：\n  • " + "\n  • ".join(errs))
        elif warns:
            self.ov.set_summary("✅ 基本可用。" + "；".join(warns))
        else:
            self.ov.set_summary("✅ 全部就绪，可以开始了！")

    def _welcome(self):
        ai_s, _ = self.ai.key_status()
        fs, _ = self.tp.statuses().get("ff", ("err", ""))
        vj, _ = self.vp.statuses().get("vjm", ("err", ""))
        vk, _ = self.vp.statuses().get("vkl", ("err", ""))
        miss = []
        if ai_s != "ok": miss.append("API Key")
        if fs != "ok": miss.append("FFmpeg")
        if vj != "ok" and vk != "ok": miss.append("视频服务")
        if miss:
            messagebox.showinfo("欢迎使用",
                "首次使用需完成配置：\n\n"
                "✏️ 手动填写：\n  • OpenRouter API Key\n\n"
                "🔍 安装后自动检测：\n  • FFmpeg\n  • 即梦或可灵 CLI\n\n"
                "点击标签页逐项配置，完成后点「保存」。", parent=self)

    def _save(self):
        env_path = _root() / ".env"
        def_path = _root() / "config" / "default_settings.json"
        env = {}
        env.update(self.ai.collect())
        env.update(self.vp.collect())
        env.update(self.tp.collect())
        env = {k: v for k, v in env.items() if v}
        try:
            _save_env(env_path, env)
            for k, v in env.items():
                os.environ[k] = v
            defaults = self.dp.collect()
            def_path.parent.mkdir(parents=True, exist_ok=True)
            def_path.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")
            self._dirty = False
            self._status.configure(text="✓ 配置已保存，部分设置需重启后生效", foreground="#22c55e")
            if self._on_saved:
                self._on_saved()
            # 保存后刷新总览
            self.after(300, self._refresh_ov)
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=self)

    def _on_close(self):
        if self._dirty:
            if not messagebox.askyesno("未保存的更改", "您有未保存的更改，确定要关闭吗？", parent=self):
                return
        self.destroy()


def open_settings(master, on_saved=None, first_time=False):
    return SettingsDialog(master, on_saved=on_saved, first_time=first_time)


def load_default_settings() -> ProductionSettings:
    p = _root() / "config" / "default_settings.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            allowed = {"selected_platforms", "video_provider", "jimeng_model", "kling_model",
                       "video_resolution", "motion_mode", "narration_voice", "orientation"}
            return ProductionSettings(**{k: v for k, v in data.items() if k in allowed})
        except:
            pass
    return ProductionSettings()
