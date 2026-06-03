from __future__ import annotations

import argparse
import base64
import ctypes
import html
import io
import json
import mimetypes
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib import error, request
from urllib.parse import unquote, urlparse
import webbrowser

from PIL import Image, ImageGrab, ImageTk

from sync_store import (
    SyncError,
    get_profile,
    handle_sync_get,
    handle_sync_post,
    login_user,
    prepend_history_item,
    register_user,
    update_current,
)


APP_NAME = "BYO Practice Assistant Desktop"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC_DIR = ROOT / "static"
APP_DIR = Path(os.environ.get("BYO_APP_DATA_DIR", Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent))
CONFIG_PATH = APP_DIR / "desktop_settings.json"
LATEST_RESULT_PATH = APP_DIR / "latest_result.json"
MAX_BODY_BYTES = 24 * 1024 * 1024
DEFAULT_FIXED_PROMPT = "基于以上图片信息，回答图片中的题目。"
DEFAULT_MOBILE_ANSWER = "暂无答案。请先在桌面端完成一次练习解析。"
LONG_PRESS_SECONDS = 2
OVERLAY_COLOR = "#000000"


WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$imagePath = $env:BYO_OCR_IMAGE
$langTag = $env:BYO_OCR_LANG
if (-not $imagePath) {
    throw '缺少 OCR 图片路径。'
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]

function Await-WinRT($Operation, [type]$ResultType) {
    $method = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    if ($task.Exception) {
        throw $task.Exception
    }
    return $task.Result
}

$file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) ([Windows.Storage.StorageFile])
$stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
if ($langTag) {
    $lang = [Windows.Globalization.Language]::new($langTag)
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    }
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) {
    throw '系统 OCR 不可用。请在 Windows 设置中安装中文 OCR/语言包。'
}

$result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) {
    $lines += $line.Text
}
$lines -join "`n"
"""


PRESETS = [
    ("OpenAI", "https://api.openai.com/v1", "gpt-4.1-mini"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("SiliconFlow", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-VL-72B-Instruct"),
]

MODES = ["综合题", "编程题", "行测/图推", "模拟面试", "复盘讲解"]
STYLES = ["答案 + 解析", "只给思路", "精简答案", "训练教练"]
LANGUAGES = ["Python", "Java", "C++", "JavaScript", "Go"]

DOCUMENT_FORMAT_TEXT = (
    "输出必须是标准中文文档格式，不要使用 Markdown、HTML 或代码围栏。"
    "不要输出 #、**、```、- 等 Markdown 标记。"
    "默认使用以下结构：一、答案；二、解析；三、关键步骤；"
    "四、参考代码（仅编程题需要）；五、注意事项。"
    "编程题代码用纯文本缩进展示，不要使用 Markdown 代码块。"
)


def load_settings() -> dict[str, Any]:
    defaults = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4.1-mini",
        "max_tokens": 1800,
        "temperature": 0.2,
        "timeout": 90,
        "fixed_prompt": DEFAULT_FIXED_PROMPT,
        "use_vision_input": False,
        "sync_username": "",
        "sync_token": "",
    }
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**defaults, **loaded}
    except Exception:
        return defaults
    return defaults


def save_settings(settings: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_latest_result() -> dict[str, Any]:
    try:
        if LATEST_RESULT_PATH.exists():
            data = json.loads(LATEST_RESULT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def save_latest_result(payload: dict[str, Any]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        LATEST_RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def resolve_chat_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("请填写 API Base URL")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API Base URL 必须是 http(s) 地址")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def call_openai_compatible(
    settings: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    api_key = str(settings.get("api_key", "")).strip()
    model = str(settings.get("model", "")).strip()
    if not api_key:
        raise ValueError("请填写 API Key")
    if not model:
        raise ValueError("请填写模型名称")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(settings.get("temperature") or 0.2),
        "max_tokens": int(settings.get("max_tokens") or 1800),
    }
    req = request.Request(
        resolve_chat_url(str(settings.get("base_url", ""))),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=float(settings.get("timeout") or 90)) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"模型接口返回 {exc.code}: {body[:1200]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"无法连接模型接口: {exc.reason}") from exc

    choices = data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return {
        "content": content or json.dumps(data, ensure_ascii=False, indent=2),
        "model": data.get("model", model),
        "usage": data.get("usage", {}),
    }


def web_config_to_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_url": config.get("baseUrl", config.get("base_url", "")),
        "api_key": config.get("apiKey", config.get("api_key", "")),
        "model": config.get("model", ""),
        "max_tokens": config.get("maxTokens", config.get("max_tokens", 1800)),
        "temperature": config.get("temperature", 0.2),
        "timeout": config.get("timeout", 90),
    }


def image_to_data_url(image: Image.Image, max_side: int = 1700) -> str:
    image = image.convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def load_image_file(path: str) -> str:
    with Image.open(path) as image:
        return image_to_data_url(image)


def image_from_data_url(data_url: str) -> Image.Image:
    encoded = data_url.split(",", 1)[1] if "," in data_url else data_url
    return Image.open(io.BytesIO(base64.b64decode(encoded))).copy()


def prepare_ocr_image(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    longest = max(image.size)
    if longest <= 0:
        return image
    scale = 1.0
    if longest < 1200:
        scale = min(3.0, 1200 / longest)
    elif longest > 2600:
        scale = 2600 / longest
    if abs(scale - 1.0) > 0.01:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
    return image


def normalize_ocr_text(text: str) -> str:
    normalized: list[str] = []
    previous_blank = False
    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
        is_blank = not line
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return "\n".join(normalized).strip()


def document_format_instruction() -> str:
    return DOCUMENT_FORMAT_TEXT


def clean_markdown_answer(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[a-zA-Z0-9_+\-.]*\n?", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = text.replace("`", "")

    cleaned_lines: list[str] = []
    previous_blank = False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned_lines.append(line)
        previous_blank = blank
    return "\n".join(cleaned_lines).strip()


def ocr_image_text(image: Image.Image, lang: str = "zh-Hans-CN", timeout: float = 35) -> str:
    tmp_path = ""
    try:
        tmp = tempfile.NamedTemporaryFile(prefix="byo-ocr-", suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        prepare_ocr_image(image).save(tmp_path, format="PNG")

        env = os.environ.copy()
        env["BYO_OCR_IMAGE"] = tmp_path
        env["BYO_OCR_LANG"] = lang
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_OCR_SCRIPT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            details = normalize_ocr_text(completed.stderr or completed.stdout)
            raise RuntimeError(details or "Windows OCR 调用失败。")
        return normalize_ocr_text(completed.stdout)
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def virtual_screen_bounds(root: tk.Tk) -> tuple[int, int, int, int]:
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        x = int(user32.GetSystemMetrics(76))
        y = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width > 0 and height > 0:
            return x, y, width, height
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def work_area_bounds(root: tk.Tk) -> tuple[int, int, int, int]:
    try:
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 0 and height > 0:
                return int(rect.left), int(rect.top), width, height
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def compact_window_geometry(root: tk.Tk) -> tuple[str, tuple[int, int]]:
    x, y, work_width, work_height = work_area_bounds(root)
    min_width = min(900, max(760, work_width - 40))
    min_height = min(620, max(560, work_height - 40))
    width = min(1040, max(min_width, work_width - 80))
    height = min(700, max(min_height, work_height - 80))
    left = x + max(0, (work_width - width) // 2)
    top = y + max(0, (work_height - height) // 2)
    return f"{width}x{height}+{left}+{top}", (min_width, min_height)


def create_server(host: str, port: int, handler_type: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    for offset in range(20):
        candidate = port + offset
        try:
            server = ThreadingHTTPServer((host, candidate), handler_type)
            return server, int(server.server_address[1])
        except OSError:
            if offset == 19:
                raise
    raise RuntimeError("No available local port")


def configure_selector_window(
    selector: tk.Toplevel,
    x: int,
    y: int,
    width: int,
    height: int,
    alpha: float = 0.16,
    cursor: str | None = "cross",
) -> tk.Canvas:
    selector.overrideredirect(True)
    selector.attributes("-topmost", True)
    selector.geometry(f"{width}x{height}+{x}+{y}")
    selector.configure(bg=OVERLAY_COLOR)
    selector.attributes("-alpha", alpha)

    canvas_options: dict[str, Any] = {"bg": OVERLAY_COLOR, "highlightthickness": 0}
    if cursor:
        canvas_options["cursor"] = cursor
    canvas = tk.Canvas(selector, **canvas_options)
    canvas.pack(fill="both", expand=True)
    return canvas


LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class Win32BackgroundController:
    WM_HOTKEY = 0x0312
    WM_COMMAND = 0x0111
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_USER = 0x0400
    WM_TRAY = WM_USER + 20
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONDBLCLK = 0x0203

    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000

    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002

    HOTKEY_REGION = 1001
    HOTKEY_LONG_PRESS = 1002
    HOTKEY_FULLSCREEN = 1003
    HOTKEY_OCR = 1004

    CMD_SHOW = 2001
    CMD_WEB = 2002
    CMD_REVIEW = 2003
    CMD_EXIT = 2004

    VK_R = 0x52
    VK_H = 0x48
    VK_F = 0x46
    VK_O = 0x4F

    def __init__(self, command_queue: queue.Queue[Any]) -> None:
        self.command_queue = command_queue
        self.thread: threading.Thread | None = None
        self.hwnd: int | None = None
        self.thread_id = 0
        self.ready = threading.Event()
        self._wndproc: Any = None
        self._registered_hotkeys: list[int] = []
        self._tray_added = False

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="win32-hotkeys-tray", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        user32 = ctypes.windll.user32
        if self.hwnd:
            user32.PostMessageW(wintypes.HWND(self.hwnd), self.WM_CLOSE, 0, 0)
        elif self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        if self.thread:
            self.thread.join(timeout=2.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HANDLE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.LoadIconW.restype = wintypes.HANDLE
        ctypes.windll.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self.thread_id = int(kernel32.GetCurrentThreadId())
        self._wndproc = WNDPROC(self._window_proc)
        class_name = f"BYOPracticeAssistantTray{os.getpid()}"
        hinstance = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, class_name, "BYO Practice Assistant", 0, 0, 0, 0, 0, None, None, hinstance, None)
        if not hwnd:
            self.command_queue.put(("status", "后台热键窗口创建失败"))
            self.ready.set()
            return

        self.hwnd = int(hwnd)
        self._register_hotkeys(hwnd)
        self._add_tray_icon(hwnd)
        self.ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        if msg == self.WM_HOTKEY:
            mapping = {
                self.HOTKEY_REGION: "hotkey_region",
                self.HOTKEY_LONG_PRESS: "hotkey_long_press",
                self.HOTKEY_FULLSCREEN: "hotkey_fullscreen",
                self.HOTKEY_OCR: "hotkey_ocr",
            }
            command = mapping.get(int(wparam))
            if command:
                self.command_queue.put(command)
            return 0

        if msg == self.WM_TRAY:
            event = int(lparam)
            if event == self.WM_RBUTTONUP:
                self._show_tray_menu(hwnd)
            elif event == self.WM_LBUTTONDBLCLK:
                self.command_queue.put("show_window")
            return 0

        if msg == self.WM_COMMAND:
            command_id = int(wparam) & 0xFFFF
            if command_id == self.CMD_SHOW:
                self.command_queue.put("show_window")
            elif command_id == self.CMD_WEB:
                self.command_queue.put("open_web")
            elif command_id == self.CMD_REVIEW:
                self.command_queue.put("open_mobile")
            elif command_id == self.CMD_EXIT:
                self.command_queue.put("exit_app")
            return 0

        if msg == self.WM_CLOSE:
            self._cleanup(hwnd)
            user32.DestroyWindow(hwnd)
            return 0

        if msg == self.WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _register_hotkeys(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        modifiers = self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT
        hotkeys = [
            (self.HOTKEY_REGION, self.VK_R, "Ctrl+Alt+R 区域截图"),
            (self.HOTKEY_LONG_PRESS, self.VK_H, "Ctrl+Alt+H 长按选区"),
            (self.HOTKEY_FULLSCREEN, self.VK_F, "Ctrl+Alt+F 全屏截图"),
            (self.HOTKEY_OCR, self.VK_O, "Ctrl+Alt+O 图片转文字"),
        ]
        for hotkey_id, vk, label in hotkeys:
            if user32.RegisterHotKey(hwnd, hotkey_id, modifiers, vk):
                self._registered_hotkeys.append(hotkey_id)
            else:
                self.command_queue.put(("status", f"全局热键注册失败: {label}"))

    def _add_tray_icon(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32512))
        nid.szTip = "练习助手 - 托盘运行"
        self._tray_added = bool(shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)))

    def _show_tray_menu(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, self.MF_STRING, self.CMD_SHOW, "显示窗口")
        user32.AppendMenuW(menu, self.MF_STRING, self.CMD_WEB, "打开网页版")
        user32.AppendMenuW(menu, self.MF_STRING, self.CMD_REVIEW, "打开复盘")
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, self.CMD_EXIT, "退出程序")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
        user32.DestroyMenu(menu)

    def _cleanup(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        for hotkey_id in self._registered_hotkeys:
            user32.UnregisterHotKey(hwnd, hotkey_id)
        self._registered_hotkeys.clear()
        if self._tray_added:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
            self._tray_added = False


@dataclass
class MobileState:
    question: str = ""
    answer: str = DEFAULT_MOBILE_ANSWER
    model: str = ""
    updated_at: str = ""
    image_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, question: str, answer: str, model: str, image_count: int) -> None:
        with self.lock:
            self.question = question
            self.answer = answer
            self.model = model
            self.image_count = image_count
            self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            save_latest_result(
                {
                    "question": self.question,
                    "answer": self.answer,
                    "model": self.model,
                    "updated_at": self.updated_at,
                    "image_count": self.image_count,
                }
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "question": self.question,
                "answer": self.answer,
                "model": self.model,
                "updated_at": self.updated_at,
                "image_count": self.image_count,
            }


class MobileReviewHandler(BaseHTTPRequestHandler):
    shared_state: MobileState

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大，请减少图片数量或压缩截图")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/api/latest":
            self.send_json({"ok": True, **self.shared_state.snapshot()})
            return

        if path.startswith("/api/sync/"):
            try:
                result = handle_sync_get(path, self.headers)
                if result is None:
                    self.send_error(404)
                else:
                    self.send_json(result)
            except SyncError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=exc.status)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/mobile":
            page = self.render_page()
            raw = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or target.is_dir():
            self.send_error(404)
            return

        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            path = unquote(self.path.split("?", 1)[0])
            if path.startswith("/api/sync/"):
                result = handle_sync_post(path, payload, self.headers)
                if result is None:
                    self.send_error(404)
                else:
                    self.send_json(result)
                return

            if path == "/api/chat":
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    raise ValueError("messages 必须是数组")
                result = call_openai_compatible(web_config_to_settings(payload.get("config") or {}), messages)
                self.send_json(
                    {
                        "ok": True,
                        "content": clean_markdown_answer(result["content"]),
                        "model": result["model"],
                        "usage": result.get("usage", {}),
                        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                return

            if path == "/api/check":
                result = call_openai_compatible(
                    web_config_to_settings(payload.get("config") or {}),
                    [{"role": "user", "content": "请只回复 OK。"}],
                )
                self.send_json({"ok": True, "content": result["content"], "model": result["model"]})
                return

            self.send_error(404)
        except SyncError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def render_page(self) -> str:
        snap = self.shared_state.snapshot()
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>练习复盘</title>
  <style>
    * {{ box-sizing: border-box; letter-spacing: 0; }}
    body {{ margin: 0; padding: 16px; background: #f5f6f8; color: #16181d; font-family: system-ui, -apple-system, "Microsoft YaHei", sans-serif; }}
    header {{ margin-bottom: 14px; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    .meta {{ color: #69707a; font-size: 13px; }}
    section {{ margin-top: 12px; padding: 14px; border: 1px solid #dfe3e8; border-radius: 8px; background: white; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; }}
    pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; line-height: 1.65; font-size: 15px; }}
    .answer-doc {{ font-family: system-ui, -apple-system, "Microsoft YaHei", sans-serif; line-height: 1.76; }}
    .notice {{ color: #b53636; font-size: 13px; }}
  </style>
</head>
<body>
  <header>
    <h1>练习复盘</h1>
    <div class="meta" id="meta">{html.escape(snap["model"] or "未解析")} · {html.escape(snap["updated_at"] or "等待更新")}</div>
    <div class="notice">仅限自我练习和复盘，不用于真实考试或面试。</div>
  </header>
  <section>
    <h2>题目</h2>
    <pre id="question">{html.escape(snap["question"] or "暂无题目文本。")}</pre>
  </section>
  <section>
    <h2>答案</h2>
    <pre class="answer-doc" id="answer">{html.escape(snap["answer"])}</pre>
  </section>
  <script>
    async function refresh() {{
      const res = await fetch('/api/latest');
      const data = await res.json();
      if (!data.ok) return;
      document.getElementById('question').textContent = data.question || '暂无题目文本。';
      document.getElementById('answer').textContent = data.answer || '暂无答案。';
      document.getElementById('meta').textContent = (data.model || '未解析') + ' · ' + (data.updated_at || '等待更新');
    }}
    setInterval(refresh, 2000);
  </script>
</body>
</html>"""


class DesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = load_settings()
        latest_result = load_latest_result()
        self.mobile_state = MobileState(
            question=str(latest_result.get("question", "")),
            answer=str(latest_result.get("answer") or DEFAULT_MOBILE_ANSWER),
            model=str(latest_result.get("model", "")),
            updated_at=str(latest_result.get("updated_at", "")),
            image_count=int(latest_result.get("image_count") or 0),
        )
        self.mobile_server: ThreadingHTTPServer | None = None
        self.mobile_thread: threading.Thread | None = None
        self.mobile_url = ""
        self.web_url = ""
        self.sync_token = str(self.settings.get("sync_token", ""))
        self.sync_username = str(self.settings.get("sync_username", ""))
        self.images: list[dict[str, str]] = []
        self.preview_photos: list[ImageTk.PhotoImage] = []
        self.multi_image = True
        self.busy = False
        self.ui_commands: queue.Queue[Any] = queue.Queue()
        self.win32_controller: Win32BackgroundController | None = None
        self.hidden_to_tray = False
        self.exiting = False
        self.tray_icon_ready = False

        self.root.title("练习助手 - 桌面版")
        geometry, minimum_size = compact_window_geometry(self.root)
        self.root.geometry(geometry)
        self.root.minsize(*minimum_size)
        self.root.configure(bg="#f5f6f8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.setup_style()
        self.build_ui()
        self.bind_hotkeys()
        self.start_mobile_server()
        self.start_background_controls()
        self.root.after(100, self.process_ui_commands)

    def setup_style(self) -> None:
        self.root.option_add("*Font", ("Microsoft YaHei", 9))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f6f8")
        style.configure("Panel.TFrame", background="#ffffff", relief="flat")
        style.configure("TLabel", background="#f5f6f8", foreground="#16181d")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#16181d")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#69707a")
        style.configure("Accent.TButton", background="#0e7c66", foreground="#ffffff", padding=(10, 5))
        style.map("Accent.TButton", background=[("active", "#08624f")])
        style.configure("TButton", padding=(8, 4))
        style.configure("TCombobox", padding=(6, 2))
        style.configure("TCheckbutton", background="#ffffff", foreground="#16181d", padding=(0, 2))

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=0, minsize=320)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="练习助手 - 桌面版", font=("Microsoft YaHei", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="原生窗口 · 自带 API · 移动端复盘", foreground="#c95642").grid(row=1, column=0, sticky="w")
        ttk.Button(header, text="解析", style="Accent.TButton", command=self.analyze).grid(row=0, column=1, rowspan=2, padx=(6, 0))

        left_shell = ttk.Frame(outer, style="Panel.TFrame", padding=0)
        left_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        left_shell.rowconfigure(0, weight=1)
        left_shell.columnconfigure(0, weight=1)
        left_canvas = tk.Canvas(left_shell, bg="#ffffff", bd=0, highlightthickness=0)
        left_scrollbar = ttk.Scrollbar(left_shell, orient="vertical", command=left_canvas.yview)
        left = ttk.Frame(left_canvas, style="Panel.TFrame", padding=10)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar.grid(row=0, column=1, sticky="ns")
        left.columnconfigure(0, weight=1)

        def update_left_scrollregion(_event: tk.Event | None = None) -> None:
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def resize_left_inner(event: tk.Event) -> None:
            left_canvas.itemconfigure(left_window, width=event.width)

        def scroll_left(event: tk.Event) -> None:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                left_canvas.yview_scroll(delta, "units")

        def enable_left_scroll(_event: tk.Event) -> None:
            self.root.bind_all("<MouseWheel>", scroll_left)

        def disable_left_scroll(_event: tk.Event) -> None:
            self.root.unbind_all("<MouseWheel>")

        left.bind("<Configure>", update_left_scrollregion)
        left_canvas.bind("<Configure>", resize_left_inner)
        left_canvas.bind("<Enter>", enable_left_scroll)
        left_canvas.bind("<Leave>", disable_left_scroll)
        left.bind("<Enter>", enable_left_scroll)
        left.bind("<Leave>", disable_left_scroll)

        right = ttk.Frame(outer, style="Panel.TFrame", padding=10)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        right.rowconfigure(6, weight=1)

        self.build_left_panel(left)
        self.build_right_panel(right)

    def build_left_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="截图热键", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=0, column=0, sticky="w")
        help_text = (
            "Ctrl+Enter: 解析当前题目 / 已添加截图\n"
            "Ctrl+Alt+R: 区域截图 / 自动解析\n"
            "Ctrl+Alt+H: 长按选区截图 / 自动解析\n"
            "Ctrl+Alt+F: 全屏截图 / 自动解析\n"
            "Ctrl+Alt+O: 图片转文字\n"
            "Shift+C: 切换模型预设\n"
            "Shift+S: 多图练习模式开关\n"
            "Ctrl+L: 复制移动端复盘链接\n\n"
            "点 X 会隐藏到托盘，后台仍可用全局热键。\n"
            "退出请使用托盘菜单。"
        )
        help_box = tk.Text(parent, height=6, wrap="word", bg="#f9faf7", fg="#16181d", relief="solid", bd=1)
        help_box.insert("1.0", help_text)
        help_box.configure(state="disabled")
        help_box.grid(row=1, column=0, sticky="ew", pady=(5, 7))

        ttk.Label(parent, text="截图/图片", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=2, column=0, sticky="w")
        shot_buttons = ttk.Frame(parent, style="Panel.TFrame")
        shot_buttons.grid(row=3, column=0, sticky="ew", pady=(5, 5))
        ttk.Button(shot_buttons, text="区域截图", command=lambda: self.capture_region(auto_analyze=True, keep_hidden=False)).pack(side="left")
        ttk.Button(shot_buttons, text="长按选区", command=lambda: self.capture_long_press_region(auto_analyze=True, keep_hidden=False)).pack(side="left", padx=5)
        ttk.Button(shot_buttons, text="全屏截图", command=lambda: self.capture_screen(auto_analyze=True)).pack(side="left")
        image_buttons = ttk.Frame(parent, style="Panel.TFrame")
        image_buttons.grid(row=4, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(image_buttons, text="添加图片", command=self.add_image_file).pack(side="left")
        ttk.Button(image_buttons, text="图片转文字", command=self.ocr_images_to_question).pack(side="left", padx=5)
        ttk.Button(image_buttons, text="清空图片", command=self.clear_images).pack(side="left")

        self.use_vision_var = tk.BooleanVar(value=bool(self.settings.get("use_vision_input", False)))
        ttk.Checkbutton(
            parent,
            text="解析时同时发送图片给模型（仅视觉模型）",
            variable=self.use_vision_var,
        ).grid(row=5, column=0, sticky="w")

        self.image_status_var = tk.StringVar(value="未添加图片")
        ttk.Label(parent, textvariable=self.image_status_var, style="Muted.TLabel").grid(row=6, column=0, sticky="w")
        preview_frame = ttk.Frame(parent, style="Panel.TFrame")
        preview_frame.grid(row=7, column=0, sticky="ew", pady=(5, 8))
        preview_frame.columnconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            preview_frame,
            height=110,
            bg="#ffffff",
            relief="solid",
            bd=1,
            highlightthickness=0,
        )
        self.preview_scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview_canvas.yview)
        self.preview_inner = ttk.Frame(self.preview_canvas, style="Panel.TFrame")
        self.preview_inner_id = self.preview_canvas.create_window((0, 0), window=self.preview_inner, anchor="nw")
        self.preview_canvas.configure(yscrollcommand=self.preview_scrollbar.set)
        self.preview_canvas.grid(row=0, column=0, sticky="ew")
        self.preview_scrollbar.grid(row=0, column=1, sticky="ns")
        self.preview_inner.bind(
            "<Configure>",
            lambda _event: self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all")),
        )
        self.preview_canvas.bind(
            "<Configure>",
            lambda event: self.preview_canvas.itemconfigure(self.preview_inner_id, width=event.width),
        )
        self.render_image_previews()

        ttk.Label(parent, text="模型配置", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=8, column=0, sticky="w")
        form = ttk.Frame(parent, style="Panel.TFrame")
        form.grid(row=9, column=0, sticky="ew", pady=(5, 7))
        form.columnconfigure(0, weight=1)

        self.base_url_var = tk.StringVar(value=str(self.settings.get("base_url", "")))
        self.api_key_var = tk.StringVar(value=str(self.settings.get("api_key", "")))
        self.model_var = tk.StringVar(value=str(self.settings.get("model", "")))
        self.max_tokens_var = tk.StringVar(value=str(self.settings.get("max_tokens", 1800)))
        self.temperature_var = tk.StringVar(value=str(self.settings.get("temperature", 0.2)))
        self.timeout_var = tk.StringVar(value=str(self.settings.get("timeout", 90)))

        self.add_labeled_entry(form, "API Base URL", self.base_url_var, 0)
        self.add_labeled_entry(form, "API Key", self.api_key_var, 2, show="*")
        self.add_labeled_entry(form, "模型", self.model_var, 4)

        small = ttk.Frame(form, style="Panel.TFrame")
        small.grid(row=6, column=0, sticky="ew", pady=(5, 0))
        small.columnconfigure((0, 1, 2), weight=1)
        self.add_small_entry(small, "最大输出", self.max_tokens_var, 0)
        self.add_small_entry(small, "温度", self.temperature_var, 1)
        self.add_small_entry(small, "超时", self.timeout_var, 2)

        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.grid(row=10, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(buttons, text="保存配置", command=self.save_current_settings).pack(side="left")
        ttk.Button(buttons, text="测试连接", command=self.test_connection).pack(side="left", padx=5)
        ttk.Button(buttons, text="清除 Key", command=self.clear_key).pack(side="left")

        ttk.Label(parent, text="账号同步", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=11, column=0, sticky="w")
        account = ttk.Frame(parent, style="Panel.TFrame")
        account.grid(row=12, column=0, sticky="ew", pady=(5, 7))
        account.columnconfigure((0, 1), weight=1)
        self.sync_username_var = tk.StringVar(value=self.sync_username)
        self.sync_password_var = tk.StringVar(value="")
        self.add_small_entry(account, "邮箱", self.sync_username_var, 0)
        self.add_small_entry(account, "密码", self.sync_password_var, 1, show="*")
        account_buttons = ttk.Frame(parent, style="Panel.TFrame")
        account_buttons.grid(row=13, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(account_buttons, text="登录", command=self.login_sync_account).pack(side="left")
        ttk.Button(account_buttons, text="注册", command=self.register_sync_account).pack(side="left", padx=5)
        ttk.Button(account_buttons, text="退出", command=self.logout_sync_account).pack(side="left")
        sync_buttons = ttk.Frame(parent, style="Panel.TFrame")
        sync_buttons.grid(row=14, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(sync_buttons, text="同步当前", command=self.sync_current_workspace).pack(side="left")
        ttk.Button(sync_buttons, text="拉取网页最新", command=self.pull_sync_workspace).pack(side="left", padx=5)
        self.account_status_var = tk.StringVar(value=self.account_status_text())
        ttk.Label(parent, textvariable=self.account_status_var, style="Muted.TLabel").grid(row=15, column=0, sticky="w")

        ttk.Label(parent, text="网页/移动端", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=16, column=0, sticky="w", pady=(8, 0))
        self.mobile_url_var = tk.StringVar(value="启动中")
        url_entry = ttk.Entry(parent, textvariable=self.mobile_url_var)
        url_entry.grid(row=17, column=0, sticky="ew", pady=(5, 5))
        mobile_buttons = ttk.Frame(parent, style="Panel.TFrame")
        mobile_buttons.grid(row=18, column=0, sticky="ew")
        ttk.Button(mobile_buttons, text="打开网页版", command=lambda: webbrowser.open(self.web_url)).pack(side="left")
        ttk.Button(mobile_buttons, text="打开复盘", command=lambda: webbrowser.open(self.mobile_url)).pack(side="left", padx=5)
        ttk.Button(mobile_buttons, text="复制链接", command=self.copy_mobile_url).pack(side="left")
        ttk.Label(parent, text="仅限自我练习和复盘，不用于真实考试或面试。", style="Muted.TLabel").grid(row=19, column=0, sticky="w", pady=(7, 0))

    def build_right_panel(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent, style="Panel.TFrame")
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure((0, 1, 2), weight=1)
        self.mode_var = tk.StringVar(value=MODES[0])
        self.style_var = tk.StringVar(value=STYLES[0])
        self.language_var = tk.StringVar(value=LANGUAGES[0])
        self.fixed_prompt_var = tk.StringVar(value=str(self.settings.get("fixed_prompt", DEFAULT_FIXED_PROMPT)))
        self.add_combo(controls, "题型", self.mode_var, MODES, 0)
        self.add_combo(controls, "回答", self.style_var, STYLES, 1)
        self.add_combo(controls, "语言", self.language_var, LANGUAGES, 2)

        prompt_frame = ttk.Frame(parent, style="Panel.TFrame")
        prompt_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        prompt_frame.columnconfigure(0, weight=1)
        ttk.Label(prompt_frame, text="固定提示词", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(prompt_frame, textvariable=self.fixed_prompt_var).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(prompt_frame, text="恢复默认", command=self.reset_fixed_prompt).grid(row=1, column=1, sticky="e", padx=(5, 0))

        ttk.Label(parent, text="题目", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=2, column=0, sticky="w", pady=(9, 5))
        self.question_text = tk.Text(
            parent,
            height=8,
            wrap="word",
            relief="solid",
            bd=1,
            bg="#fbfcfd",
            fg="#16181d",
            insertbackground="#16181d",
            padx=9,
            pady=7,
            spacing1=2,
            spacing3=4,
        )
        self.question_text.grid(row=3, column=0, sticky="nsew")

        action_bar = ttk.Frame(parent, style="Panel.TFrame")
        action_bar.grid(row=4, column=0, sticky="ew", pady=7)
        action_top = ttk.Frame(action_bar, style="Panel.TFrame")
        action_bottom = ttk.Frame(action_bar, style="Panel.TFrame")
        action_top.pack(fill="x")
        action_bottom.pack(fill="x", pady=(4, 0))
        ttk.Button(action_top, text="解析", style="Accent.TButton", command=self.analyze).pack(side="left")
        ttk.Button(action_top, text="区域截图", command=lambda: self.capture_region(auto_analyze=True, keep_hidden=False)).pack(side="left", padx=(5, 0))
        ttk.Button(action_top, text="长按选区", command=lambda: self.capture_long_press_region(auto_analyze=True, keep_hidden=False)).pack(side="left", padx=(5, 0))
        ttk.Button(action_top, text="图片转文字", command=self.ocr_images_to_question).pack(side="left", padx=(5, 0))
        ttk.Button(action_bottom, text="发布到移动端复盘", command=self.publish_current_answer).pack(side="left")
        ttk.Button(action_bottom, text="复制答案", command=self.copy_answer).pack(side="left", padx=(5, 0))
        ttk.Button(action_bottom, text="清空", command=self.clear_workspace).pack(side="left", padx=(5, 0))

        ttk.Label(parent, text="答案", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=5, column=0, sticky="w", pady=(0, 5))
        self.answer_text = tk.Text(
            parent,
            height=11,
            wrap="word",
            relief="solid",
            bd=1,
            bg="#fbfcfd",
            fg="#16181d",
            insertbackground="#16181d",
            padx=10,
            pady=8,
            spacing1=2,
            spacing3=6,
        )
        self.answer_text.grid(row=6, column=0, sticky="nsew")
        self.answer_text.insert("1.0", "配置模型后输入题目开始练习。")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(parent, textvariable=self.status_var, style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(5, 0))

    def add_labeled_entry(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=var, show=show).grid(row=row + 1, column=0, sticky="ew", pady=(3, 5))

    def add_small_entry(self, parent: ttk.Frame, label: str, var: tk.StringVar, column: int, show: str | None = None) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
        ttk.Label(frame, text=label, style="Panel.TLabel").pack(anchor="w")
        ttk.Entry(frame, textvariable=var, width=10, show=show).pack(fill="x", pady=(3, 0))

    def add_combo(self, parent: ttk.Frame, label: str, var: tk.StringVar, values: list[str], column: int) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
        ttk.Label(frame, text=label, style="Panel.TLabel").pack(anchor="w")
        combo = ttk.Combobox(frame, textvariable=var, values=values, state="readonly")
        combo.pack(fill="x", pady=(3, 0))

    def bind_hotkeys(self) -> None:
        self.root.bind("<Control-Return>", lambda _event: self.analyze())
        self.root.bind("<Control-Alt-r>", lambda _event: self.capture_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-R>", lambda _event: self.capture_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-h>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-H>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-f>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Alt-F>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Alt-o>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Control-Alt-O>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Shift-C>", lambda _event: self.cycle_preset())
        self.root.bind("<Shift-S>", lambda _event: self.toggle_multi_image())
        self.root.bind("<Control-l>", lambda _event: self.copy_mobile_url())

    def current_settings(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "max_tokens": int(float(self.max_tokens_var.get() or 1800)),
            "temperature": float(self.temperature_var.get() or 0.2),
            "timeout": float(self.timeout_var.get() or 90),
            "fixed_prompt": self.fixed_prompt_var.get().strip() or DEFAULT_FIXED_PROMPT,
            "use_vision_input": bool(self.use_vision_var.get()),
            "sync_username": self.sync_username.strip().lower(),
            "sync_token": self.sync_token,
        }

    def save_current_settings(self) -> None:
        self.settings = self.current_settings()
        save_settings(self.settings)
        self.set_status("配置已保存")

    def clear_key(self) -> None:
        self.api_key_var.set("")
        self.save_current_settings()

    def reset_fixed_prompt(self) -> None:
        self.fixed_prompt_var.set(DEFAULT_FIXED_PROMPT)
        self.save_current_settings()

    def account_status_text(self) -> str:
        return f"已登录邮箱: {self.sync_username}" if self.sync_token and self.sync_username else "未登录邮箱"

    def update_account_status(self) -> None:
        if hasattr(self, "account_status_var"):
            self.account_status_var.set(self.account_status_text())

    def set_sync_session(self, username: str, token: str) -> None:
        self.sync_username = username.strip().lower()
        self.sync_token = token
        self.sync_username_var.set(self.sync_username)
        self.settings = self.current_settings()
        save_settings(self.settings)
        self.update_account_status()

    def register_sync_account(self) -> None:
        username = self.sync_username_var.get().strip().lower()
        password = self.sync_password_var.get()
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            result = register_user(username, password)
            self.set_sync_session(str(result.get("email") or result["username"]), result["token"])
            self.sync_password_var.set("")
            self.set_status("账号已注册并登录")
        except Exception as exc:
            messagebox.showerror("注册失败", str(exc))

    def login_sync_account(self) -> None:
        username = self.sync_username_var.get().strip().lower()
        password = self.sync_password_var.get()
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            result = login_user(username, password)
            self.set_sync_session(str(result.get("email") or result["username"]), result["token"])
            self.sync_password_var.set("")
            self.set_status("账号已登录")
            self.pull_sync_workspace(silent=True)
        except Exception as exc:
            messagebox.showerror("登录失败", str(exc))

    def logout_sync_account(self) -> None:
        self.sync_token = ""
        self.sync_username = ""
        self.sync_username_var.set("")
        self.sync_password_var.set("")
        self.settings = self.current_settings()
        save_settings(self.settings)
        self.update_account_status()
        self.set_status("账号已退出")

    def current_sync_payload(self, answer: str | None = None, model: str | None = None, image_count: int | None = None) -> dict[str, Any]:
        return {
            "question": self.question_text.get("1.0", "end").strip(),
            "answer": clean_markdown_answer(self.answer_text.get("1.0", "end").strip() if answer is None else answer),
            "model": self.model_var.get().strip() if model is None else model,
            "imageCount": len(self.images) if image_count is None else image_count,
            "mode": self.mode_var.get(),
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def sync_current_workspace(self) -> None:
        if not self.sync_token:
            messagebox.showwarning("未登录", "请先登录或注册账号。")
            return
        try:
            payload = self.current_sync_payload()
            update_current(self.sync_token, payload)
            if payload["answer"]:
                prepend_history_item(
                    self.sync_token,
                    {
                        "id": f"desktop-{int(time.time() * 1000)}",
                        "createdAt": payload["updatedAt"],
                        "mode": payload["mode"],
                        "question": payload["question"],
                        "imageCount": payload["imageCount"],
                        "answer": payload["answer"],
                        "model": payload["model"],
                    },
                )
            self.set_status("已同步当前内容")
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc))

    def push_sync_result(self, question: str, answer: str, model: str, image_count: int, mode: str) -> None:
        if not self.sync_token:
            return
        payload = {
            "question": question,
            "answer": answer,
            "model": model,
            "imageCount": image_count,
            "mode": mode,
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            update_current(self.sync_token, payload)
            prepend_history_item(
                self.sync_token,
                {
                    "id": f"desktop-{int(time.time() * 1000)}",
                    "createdAt": payload["updatedAt"],
                    "mode": mode,
                    "question": question,
                    "imageCount": image_count,
                    "answer": answer,
                    "model": model,
                },
            )
        except Exception:
            return

    def pull_sync_workspace(self, silent: bool = False) -> None:
        if not self.sync_token:
            if not silent:
                messagebox.showwarning("未登录", "请先登录或注册账号。")
            return
        try:
            profile = get_profile(self.sync_token)
            current = profile.get("current", {})
            question = str(current.get("question", ""))
            answer = clean_markdown_answer(str(current.get("answer", "")))
            model = str(current.get("model", ""))
            image_count = int(current.get("imageCount") or 0)
            if question:
                self.question_text.delete("1.0", "end")
                self.question_text.insert("1.0", question)
            if answer:
                self.set_answer(answer)
            if question or answer:
                self.mobile_state.update(question, answer, model, image_count)
            if not silent:
                self.set_status("已拉取账号最新内容")
        except Exception as exc:
            if not silent:
                messagebox.showerror("拉取失败", str(exc))

    def test_connection(self) -> None:
        self.save_current_settings()
        self.set_status("正在测试连接...")
        threading.Thread(target=self._test_connection_worker, daemon=True).start()

    def _test_connection_worker(self) -> None:
        try:
            result = call_openai_compatible(self.settings, [{"role": "user", "content": "请只回复 OK。"}])
            self.after_status(f"连接可用: {result['model']}")
        except Exception as exc:
            self.after_error(str(exc))

    def capture_screen(self, auto_analyze: bool = True) -> None:
        try:
            image = ImageGrab.grab(all_screens=True)
            if not self.multi_image:
                self.images.clear()
            name = f"screen-{time.strftime('%H%M%S')}.jpg"
            data_url = image_to_data_url(image)
            self.images.append({"name": name, "data_url": data_url})
            self.update_image_status()
            self.set_status(f"已添加全屏截图: {name}")
            if auto_analyze:
                self.ocr_latest_image_and_analyze(data_url)
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))

    def capture_region(self, auto_analyze: bool = True, keep_hidden: bool = True) -> None:
        root_was_visible = self.root.state() != "withdrawn"
        x, y, width, height = virtual_screen_bounds(self.root)
        selector = tk.Toplevel(self.root)
        canvas = configure_selector_window(selector, x, y, width, height, alpha=0.16)
        canvas.create_text(
            24,
            24,
            text="拖拽选择截图区域，松开鼠标完成；Esc 取消",
            fill="#ffffff",
            anchor="nw",
            font=("Microsoft YaHei", 16, "bold"),
        )

        state: dict[str, int | None] = {"start_x": None, "start_y": None, "rect": None}

        def on_press(event: tk.Event) -> None:
            state["start_x"] = int(event.x)
            state["start_y"] = int(event.y)
            if state["rect"] is not None:
                canvas.delete(int(state["rect"]))
            state["rect"] = canvas.create_rectangle(
                event.x,
                event.y,
                event.x,
                event.y,
                outline="#00d18f",
                width=3,
            )

        def on_drag(event: tk.Event) -> None:
            if state["start_x"] is None or state["start_y"] is None or state["rect"] is None:
                return
            canvas.coords(int(state["rect"]), int(state["start_x"]), int(state["start_y"]), event.x, event.y)

        def close_selector(status: str | None = None, restore_root: bool = False) -> None:
            try:
                selector.grab_release()
            except Exception:
                pass
            selector.destroy()
            if restore_root and root_was_visible and not keep_hidden:
                self.root.deiconify()
                self.root.lift()
            if status:
                self.set_status(status)

        def on_release(event: tk.Event) -> None:
            if state["start_x"] is None or state["start_y"] is None:
                close_selector("区域截图已取消", restore_root=True)
                return

            left = x + min(int(state["start_x"]), int(event.x))
            top = y + min(int(state["start_y"]), int(event.y))
            right = x + max(int(state["start_x"]), int(event.x))
            bottom = y + max(int(state["start_y"]), int(event.y))

            if right - left < 8 or bottom - top < 8:
                close_selector("截图区域过小，已取消", restore_root=True)
                return

            close_selector()
            self.root.after(180, lambda: self.capture_bbox((left, top, right, bottom), auto_recognize=auto_analyze, restore_main=False))

        selector.bind("<ButtonPress-1>", on_press)
        selector.bind("<B1-Motion>", on_drag)
        selector.bind("<ButtonRelease-1>", on_release)
        selector.bind("<Escape>", lambda _event: close_selector("区域截图已取消", restore_root=True))
        selector.focus_force()
        selector.grab_set()
        self.set_status("请拖拽选择截图区域")

    def capture_long_press_region(self, auto_analyze: bool = True, keep_hidden: bool = True) -> None:
        root_was_visible = self.root.state() != "withdrawn"
        self.root.withdraw()
        self.root.update_idletasks()
        x, y, width, height = virtual_screen_bounds(self.root)
        selector = tk.Toplevel(self.root)
        canvas = configure_selector_window(selector, x, y, width, height, alpha=0.01, cursor=None)
        hint = tk.Toplevel(self.root)
        hint.overrideredirect(True)
        hint.attributes("-topmost", True)
        hint.configure(bg="#fff8d8")
        hint.geometry(f"+{x + max(20, width - 520)}+{y + 18}")
        hint_var = tk.StringVar(value="长按选区：请长按第 1 个点 2 秒；Esc 取消")
        tk.Label(
            hint,
            textvariable=hint_var,
            bg="#fff8d8",
            fg="#16181d",
            padx=12,
            pady=8,
            font=("Microsoft YaHei", 11, "bold"),
        ).pack()
        instruction = canvas.create_text(
            24,
            24,
            text="第一次长按左键 2 秒记录左上角；第二次长按 2 秒记录右下角；Esc 取消",
            fill="#ffffff",
            anchor="nw",
            font=("Microsoft YaHei", 16, "bold"),
        )
        progress = canvas.create_text(
            24,
            56,
            text="等待第 1 个点",
            fill="#00d18f",
            anchor="nw",
            font=("Microsoft YaHei", 14, "bold"),
        )

        state: dict[str, Any] = {
            "points": [],
            "press_pos": None,
            "press_at": 0.0,
            "after_id": None,
            "recorded": False,
            "rect": None,
        }

        def point_label(index: int) -> str:
            return "左上角" if index == 0 else "右下角"

        def close_selector(status: str | None = None, restore_root: bool = True) -> None:
            after_id = state.get("after_id")
            if after_id is not None:
                try:
                    selector.after_cancel(after_id)
                except Exception:
                    pass
            try:
                selector.grab_release()
            except Exception:
                pass
            try:
                hint.destroy()
            except Exception:
                pass
            selector.destroy()
            if restore_root and root_was_visible and not keep_hidden:
                self.root.deiconify()
                self.root.lift()
            if status:
                self.set_status(status)

        def draw_point(px: int, py: int, index: int) -> None:
            color = "#00d18f" if index == 0 else "#ffd166"
            canvas.create_oval(px - 8, py - 8, px + 8, py + 8, outline=color, width=3)
            canvas.create_text(px + 12, py - 12, text=point_label(index), fill=color, anchor="nw", font=("Microsoft YaHei", 12, "bold"))

        def update_rect() -> None:
            points: list[tuple[int, int]] = state["points"]
            if len(points) != 2:
                return
            (x1, y1), (x2, y2) = points
            if state["rect"] is not None:
                canvas.delete(int(state["rect"]))
            state["rect"] = canvas.create_rectangle(x1, y1, x2, y2, outline="#00d18f", width=3)

        def record_point() -> None:
            if state["press_pos"] is None:
                return
            px, py = state["press_pos"]
            points: list[tuple[int, int]] = state["points"]
            index = len(points)
            points.append((px, py))
            state["recorded"] = True
            draw_point(px, py, index)

            if len(points) == 1:
                hint_var.set("左上角已记录。请长按第 2 个点 2 秒。")
                canvas.itemconfigure(progress, text="左上角已记录。请长按第 2 个点 2 秒。")
                return

            update_rect()
            left = x + min(points[0][0], points[1][0])
            top = y + min(points[0][1], points[1][1])
            right = x + max(points[0][0], points[1][0])
            bottom = y + max(points[0][1], points[1][1])

            if right - left < 8 or bottom - top < 8:
                close_selector("截图区域过小，已取消")
                return

            hint_var.set("区域已确认，正在转文字并解析...")
            canvas.itemconfigure(progress, text="区域已确认，正在转文字并解析...")
            selector.after(
                180,
                lambda: (
                    close_selector(restore_root=False),
                    self.capture_bbox(
                        (left, top, right, bottom),
                        prefix="longpress-region",
                        auto_recognize=auto_analyze,
                        restore_main=False,
                    ),
                ),
            )

        def on_press(event: tk.Event) -> None:
            if len(state["points"]) >= 2:
                return
            state["press_pos"] = (int(event.x), int(event.y))
            state["press_at"] = time.monotonic()
            state["recorded"] = False
            label = point_label(len(state["points"]))
            hint_var.set(f"正在长按记录{label}，保持 2 秒...")
            canvas.itemconfigure(progress, text=f"正在长按记录{label}，保持 2 秒...")
            state["after_id"] = selector.after(int(LONG_PRESS_SECONDS * 1000), record_point)

        def on_motion(event: tk.Event) -> None:
            if state["press_pos"] is None:
                return
            px, py = state["press_pos"]
            if abs(int(event.x) - px) > 28 or abs(int(event.y) - py) > 28:
                after_id = state.get("after_id")
                if after_id is not None:
                    selector.after_cancel(after_id)
                    state["after_id"] = None
                hint_var.set("移动过多，本次长按已取消。请重新长按。")
                canvas.itemconfigure(progress, text="移动过多，本次长按已取消。请重新长按。")
                state["press_pos"] = None

        def on_release(_event: tk.Event) -> None:
            after_id = state.get("after_id")
            if after_id is not None:
                try:
                    selector.after_cancel(after_id)
                except Exception:
                    pass
                state["after_id"] = None
            if state["press_pos"] is not None and not state["recorded"]:
                elapsed = time.monotonic() - float(state["press_at"])
                hint_var.set(f"长按不足 2 秒（{elapsed:.1f}s），请重新长按。")
                canvas.itemconfigure(progress, text=f"长按不足 2 秒（{elapsed:.1f}s），请重新长按。")
            state["press_pos"] = None

        selector.bind("<ButtonPress-1>", on_press)
        selector.bind("<B1-Motion>", on_motion)
        selector.bind("<ButtonRelease-1>", on_release)
        selector.bind("<Escape>", lambda _event: close_selector("长按选区已取消"))
        selector.focus_force()
        selector.grab_set()
        self.set_status("长按选区：请长按第 1 个点 2 秒")

    def capture_bbox(
        self,
        bbox: tuple[int, int, int, int],
        prefix: str = "region",
        auto_recognize: bool = False,
        restore_main: bool = False,
    ) -> None:
        try:
            try:
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                image = ImageGrab.grab(bbox=bbox)
            if restore_main:
                self.root.deiconify()
                self.root.lift()
            if not self.multi_image:
                self.images.clear()
            name = f"{prefix}-{time.strftime('%H%M%S')}.jpg"
            data_url = image_to_data_url(image)
            self.images.append({"name": name, "data_url": data_url})
            self.update_image_status()
            self.set_status(f"已添加区域截图: {name}")
            if auto_recognize:
                self.ocr_latest_image_and_analyze(data_url)
        except Exception as exc:
            if restore_main:
                self.root.deiconify()
                self.root.lift()
            messagebox.showerror("区域截图失败", str(exc))

    def add_image_file(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择练习图片",
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("All files", "*.*")],
        )
        if not paths:
            return
        try:
            if not self.multi_image:
                self.images.clear()
            for path in paths:
                with Image.open(path) as image:
                    self.images.append({"name": Path(path).name, "data_url": image_to_data_url(image)})
            self.update_image_status()
            self.set_status(f"已添加 {len(paths)} 张图片")
        except Exception as exc:
            messagebox.showerror("图片读取失败", str(exc))

    def update_image_status(self) -> None:
        self.image_status_var.set(f"已添加 {len(self.images)} 张图片")
        self.render_image_previews()

    def render_image_previews(self) -> None:
        if not hasattr(self, "preview_inner"):
            return
        for child in self.preview_inner.winfo_children():
            child.destroy()
        self.preview_photos.clear()

        if not self.images:
            ttk.Label(
                self.preview_inner,
                text="暂无图片。添加后会在这里显示全部缩略图。",
                style="Muted.TLabel",
            ).pack(fill="x", padx=10, pady=12)
            self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all"))
            return

        for index, item in enumerate(self.images, start=1):
            row = ttk.Frame(self.preview_inner, style="Panel.TFrame", padding=(8, 8))
            row.pack(fill="x", padx=4, pady=(4, 0))

            try:
                thumb = image_from_data_url(item["data_url"])
                thumb.thumbnail((118, 76))
                photo = ImageTk.PhotoImage(thumb)
                self.preview_photos.append(photo)
                tk.Label(row, image=photo, bg="#ffffff", bd=1, relief="solid").pack(side="left")
            except Exception:
                ttk.Label(row, text="[图片预览失败]", style="Panel.TLabel").pack(side="left")

            name = item.get("name") or f"image-{index}"
            ttk.Label(
                row,
                text=f"{index}. {name}",
                style="Panel.TLabel",
                wraplength=190,
            ).pack(side="left", padx=(10, 0), fill="x", expand=True)

        self.preview_inner.update_idletasks()
        self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all"))

    def clear_images(self) -> None:
        self.images.clear()
        self.preview_photos.clear()
        self.render_image_previews()
        self.image_status_var.set("未添加图片")
        self.set_status("图片已清空")

    def build_prompt(self) -> str:
        question = self.question_text.get("1.0", "end").strip()
        mode = self.mode_var.get()
        style = self.style_var.get()
        language = self.language_var.get()
        fixed_prompt = self.fixed_prompt_var.get().strip()
        mode_rules = {
            "综合题": "识别题型，先给结论，再给关键推理和易错点。",
            "编程题": f"给出算法思路、边界条件、复杂度，并使用 {language} 写出代码。",
            "行测/图推": "先描述题干或图片中的关键信息，再逐步排除选项并说明依据。",
            "模拟面试": "按模拟面试回答组织语言，给出 60 秒版本和展开版本。",
            "复盘讲解": "复盘材料中的问题，指出错因、知识点和下一步训练安排。",
        }
        style_rules = {
            "答案 + 解析": "直接给出答案，再解释关键依据。",
            "只给思路": "只给解题思路和推理过程，不直接跳到结论。",
            "精简答案": "用最短可用答案回答，保留必要公式、代码或选项。",
            "训练教练": "指出薄弱点、改进动作和下一题练习方向。",
        }
        return "\n".join(
            [
                f"场景：{mode}",
                f"回答风格：{style_rules.get(style, style)}",
                f"专项要求：{mode_rules.get(mode, '')}",
                f"固定提示词：{fixed_prompt or DEFAULT_FIXED_PROMPT}",
                f"输出格式：{document_format_instruction()}",
                "材料：",
                question or "见随附图片。",
            ]
        )

    def build_messages(self) -> list[dict[str, Any]]:
        system_prompt = (
            "你是求职笔试、编程题和模拟面试的练习教练。"
            "你的用途仅限自我练习、复盘和模拟训练。"
            "不要协助正在进行的真实考试、真实笔试、真实面试或任何规避监控的行为；"
            "遇到这类语境时，改为提供学习建议、通用思路和练习方法。"
            "回答使用中文，结构清晰。"
            f"{document_format_instruction()}"
        )
        prompt = self.build_prompt()
        if not self.images or not self.use_vision_var.get():
            return [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in self.images:
            content.append({"type": "image_url", "image_url": {"url": image["data_url"]}})
        return [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]

    def ocr_images_to_question(self) -> None:
        if self.busy:
            self.set_status("当前正在处理，请稍后再试")
            return
        if not self.images:
            messagebox.showwarning("缺少图片", "请先截图或添加图片。")
            return
        self.start_ocr([dict(item) for item in self.images], auto_analyze=False)

    def ocr_latest_image_and_analyze(self, image_data_url: str) -> None:
        if self.busy:
            self.set_status("当前正在处理，请稍后再试")
            return
        self.start_ocr([{"name": "latest-capture", "data_url": image_data_url}], auto_analyze=True)

    def start_ocr(self, images: list[dict[str, Any]], auto_analyze: bool) -> None:
        self.busy = True
        self.set_answer("正在将图片转文字...")
        self.set_status("图片转文字中...")
        threading.Thread(
            target=self._ocr_worker,
            args=(images, auto_analyze),
            daemon=True,
        ).start()

    def _ocr_worker(self, images: list[dict[str, Any]], auto_analyze: bool) -> None:
        try:
            parts: list[str] = []
            for index, item in enumerate(images, start=1):
                image = image_from_data_url(item["data_url"])
                try:
                    text = ocr_image_text(image)
                finally:
                    image.close()
                name = item.get("name") or f"image-{index}"
                if text:
                    parts.append(f"【图片 {index}：{name}】\n{text}")
                else:
                    parts.append(f"【图片 {index}：{name}】\n[未识别到文字]")
            question_text = "\n\n".join(parts).strip()
            if not question_text:
                raise RuntimeError("未识别到可用文字。")
            self.root.after(0, lambda: self.finish_ocr(question_text, auto_analyze))
        except Exception as exc:
            self.root.after(0, lambda: self.finish_ocr_error(str(exc)))

    def finish_ocr(self, question_text: str, auto_analyze: bool) -> None:
        self.busy = False
        self.question_text.delete("1.0", "end")
        self.question_text.insert("1.0", question_text)
        if auto_analyze:
            self.set_status("图片转文字完成，正在解析...")
            self.analyze()
        else:
            self.set_answer("图片文字已写入题目区域。点击“解析”即可发送给文本模型。")
            self.set_status("图片转文字完成")

    def finish_ocr_error(self, error_text: str) -> None:
        self.busy = False
        question = self.question_text.get("1.0", "end").strip()
        answer = clean_markdown_answer(f"图片转文字失败：{error_text}")
        self.mobile_state.update(question, answer, self.model_var.get().strip(), len(self.images))
        if self.sync_token:
            try:
                update_current(self.sync_token, self.current_sync_payload(answer=answer, model=self.model_var.get().strip()))
            except Exception:
                pass
        self.set_answer(answer)
        self.set_status("图片转文字失败")

    def analyze(self) -> None:
        if self.busy:
            return
        question = self.question_text.get("1.0", "end").strip()
        if not question and not self.images:
            messagebox.showwarning("缺少题目", "请先输入题目或添加图片。")
            return
        if not question and self.images and not self.use_vision_var.get():
            self.start_ocr([dict(item) for item in self.images], auto_analyze=True)
            return
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            messages = self.build_messages()
            question_snapshot = self.question_text.get("1.0", "end").strip()
            image_count = len(self.images)
            settings_snapshot = dict(self.settings)
            mode_snapshot = self.mode_var.get()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self.busy = True
        self.set_answer("正在请求模型...")
        self.set_status("解析中...")
        threading.Thread(
            target=self._analyze_worker,
            args=(settings_snapshot, messages, question_snapshot, image_count, mode_snapshot),
            daemon=True,
        ).start()

    def _analyze_worker(
        self,
        settings: dict[str, Any],
        messages: list[dict[str, Any]],
        question: str,
        image_count: int,
        mode: str,
    ) -> None:
        try:
            result = call_openai_compatible(settings, messages)
            answer = clean_markdown_answer(result["content"])
            model = result["model"]
            self.mobile_state.update(question, answer, model, image_count)
            self.push_sync_result(question, answer, model, image_count, mode)
            self.root.after(0, lambda: self.finish_answer(answer, f"完成: {model}，已发布到移动端复盘"))
        except Exception as exc:
            answer = clean_markdown_answer(f"请求失败：{exc}")
            model = str(settings.get("model", ""))
            self.mobile_state.update(question, answer, model, image_count)
            self.push_sync_result(question, answer, model, image_count, mode)
            self.root.after(0, lambda: self.finish_answer(answer, "解析失败"))

    def finish_answer(self, answer: str, status: str) -> None:
        self.busy = False
        self.set_answer(clean_markdown_answer(answer))
        self.set_status(status)

    def publish_current_answer(self) -> None:
        answer = clean_markdown_answer(self.answer_text.get("1.0", "end").strip())
        self.set_answer(answer)
        question = self.question_text.get("1.0", "end").strip()
        self.mobile_state.update(question, answer, self.model_var.get().strip(), len(self.images))
        if self.sync_token:
            try:
                update_current(self.sync_token, self.current_sync_payload())
            except Exception:
                pass
        self.set_status("已发布到移动端复盘")

    def copy_answer(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(clean_markdown_answer(self.answer_text.get("1.0", "end").strip()))
        self.set_status("答案已复制")

    def copy_mobile_url(self) -> None:
        if not self.web_url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.web_url)
        self.set_status("网页版链接已复制")

    def clear_workspace(self) -> None:
        self.question_text.delete("1.0", "end")
        self.set_answer("")
        self.clear_images()

    def start_mobile_server(self) -> None:
        MobileReviewHandler.shared_state = self.mobile_state
        server, port = create_server("0.0.0.0", 8786, MobileReviewHandler)
        self.mobile_server = server
        self.mobile_url = f"http://{local_ip()}:{port}/mobile"
        self.web_url = f"http://{local_ip()}:{port}/"
        self.mobile_url_var.set(self.web_url)
        self.mobile_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.mobile_thread.start()

    def start_background_controls(self) -> None:
        try:
            self.win32_controller = Win32BackgroundController(self.ui_commands)
            self.win32_controller.start()
            self.win32_controller.ready.wait(1.0)
            self.tray_icon_ready = bool(self.win32_controller._tray_added)
            if not self.tray_icon_ready:
                self.set_status("托盘图标未就绪，全局热键仍会尝试注册")
        except Exception as exc:
            self.win32_controller = None
            self.tray_icon_ready = False
            self.set_status(f"托盘/全局热键启动失败: {exc}")

    def process_ui_commands(self) -> None:
        while True:
            try:
                command = self.ui_commands.get_nowait()
            except queue.Empty:
                break

            if isinstance(command, tuple) and command and command[0] == "status":
                self.set_status(str(command[1]))
            elif command == "hotkey_region":
                self.capture_region(auto_analyze=True, keep_hidden=True)
            elif command == "hotkey_long_press":
                self.capture_long_press_region(auto_analyze=True, keep_hidden=True)
            elif command == "hotkey_fullscreen":
                self.capture_screen(auto_analyze=True)
            elif command == "hotkey_ocr":
                self.ocr_images_to_question()
            elif command == "show_window":
                self.show_window()
            elif command == "open_web":
                if self.web_url:
                    webbrowser.open(self.web_url)
            elif command == "open_mobile":
                if self.mobile_url:
                    webbrowser.open(self.mobile_url)
            elif command == "exit_app":
                self.exit_app()

        if not self.exiting:
            self.root.after(100, self.process_ui_commands)

    def hide_to_tray(self) -> None:
        self.hidden_to_tray = True
        self.root.withdraw()
        if self.tray_icon_ready:
            self.set_status("已隐藏到托盘，后台全局热键仍可使用")
        else:
            self.set_status("窗口已隐藏，托盘图标未成功创建")

    def show_window(self) -> None:
        self.hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit_app(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        if self.win32_controller:
            self.win32_controller.stop()
            self.win32_controller = None
            self.tray_icon_ready = False
        if self.mobile_server:
            self.mobile_server.shutdown()
            self.mobile_server.server_close()
            self.mobile_server = None
        self.root.destroy()

    def cycle_var(self, var: tk.StringVar, values: list[str]) -> None:
        current = var.get()
        index = values.index(current) if current in values else -1
        var.set(values[(index + 1) % len(values)])

    def cycle_preset(self) -> None:
        current = self.model_var.get()
        index = next((i for i, item in enumerate(PRESETS) if item[2] == current), -1)
        _name, base_url, model = PRESETS[(index + 1) % len(PRESETS)]
        self.base_url_var.set(base_url)
        self.model_var.set(model)
        self.set_status(f"已切换模型: {model}")

    def toggle_multi_image(self) -> None:
        self.multi_image = not self.multi_image
        self.set_status(f"多图练习模式: {'开启' if self.multi_image else '关闭'}")

    def set_answer(self, text: str) -> None:
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", clean_markdown_answer(text))

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def after_status(self, text: str) -> None:
        self.root.after(0, lambda: self.set_status(text))

    def after_error(self, text: str) -> None:
        self.root.after(0, lambda: self.set_status(f"失败: {text}"))

    def on_close(self) -> None:
        if self.exiting:
            self.root.destroy()
            return
        self.hide_to_tray()


def self_test() -> None:
    assert resolve_chat_url("https://example.com/v1") == "https://example.com/v1/chat/completions"
    assert image_to_data_url(Image.new("RGB", (8, 8), "#ffffff")).startswith("data:image/jpeg;base64,")
    print("desktop self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
