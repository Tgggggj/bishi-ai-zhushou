from __future__ import annotations

import argparse
import base64
import ctypes
import html
import http.client
import io
import json
import mimetypes
import os
import queue
import re
import socket
import ssl
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
CLOUD_SYNC_BASE_URL = "https://practice-assistant-tgggggj.netlify.app"
LEGACY_FIXED_PROMPT = "基于以上图片信息，回答图片中的题目。"
DEFAULT_FIXED_PROMPT = (
    "准确识别题干、图形、表格和全部选项。第一行先直接输出最终答案；"
    "选择题必须先写“答案：X（选项内容）”，再用最少必要文字给出解析。"
)
DEFAULT_MOBILE_ANSWER = "暂无答案。请先在桌面端完成一次练习解析。"
LONG_PRESS_SECONDS = 2.5
LONG_PRESS_MOVE_TOLERANCE = 28
VK_RBUTTON = 0x02
OVERLAY_COLOR = "#000000"


def configure_process_dpi_awareness() -> None:
    try:
        user32 = ctypes.windll.user32
        try:
            # Per-monitor V2 keeps Tk, GetCursorPos and ImageGrab in the same physical coordinate space.
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        user32.SetProcessDPIAware()
    except Exception:
        pass


configure_process_dpi_awareness()


class CloudSyncError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


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
STYLES = ["精简答案", "答案 + 解析", "只给思路", "训练教练"]
LANGUAGES = ["Python", "Java", "C++", "JavaScript", "Go"]

DOCUMENT_FORMAT_TEXT = (
    "输出必须是标准中文文档格式，不要使用 Markdown、HTML 或代码围栏。"
    "不要输出 #、**、```、- 等 Markdown 标记。"
    "第一行必须先给最终答案；选择题第一行写“答案：X（选项内容）”。"
    "然后使用以下结构：一、解析；二、关键步骤；"
    "三、参考代码（仅编程题需要）；四、注意事项。"
    "能用一句话解释清楚时不要扩写，无法确定时明确指出不确定点，不要猜测图片中不存在的内容。"
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
        "use_vision_input": True,
        "multi_image_mode": False,
        "sync_base_url": CLOUD_SYNC_BASE_URL,
        "sync_username": "",
        "sync_token": "",
    }
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if loaded.get("fixed_prompt") in {"", LEGACY_FIXED_PROMPT}:
                loaded["fixed_prompt"] = DEFAULT_FIXED_PROMPT
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
    on_delta: Any | None = None,
    on_reset: Any | None = None,
) -> dict[str, Any]:
    api_key = str(settings.get("api_key", "")).strip()
    model = str(settings.get("model", "")).strip()
    if not api_key:
        raise ValueError("请填写 API Key")
    if not model:
        raise ValueError("请填写模型名称")

    prompt_text = json.dumps(messages, ensure_ascii=False)
    configured_max_tokens = int(settings.get("max_tokens") or 1800)
    max_tokens = min(configured_max_tokens, 2200 if "编程题" in prompt_text else 900)
    timeout_seconds = min(55.0, max(20.0, float(settings.get("timeout") or 55)))
    chat_url = resolve_chat_url(str(settings.get("base_url", "")))
    last_error: Exception | None = None

    transient_markers = ("timed out", "unexpected_eof", "eof occurred", "connection reset", "remote end closed")

    for attempt in range(3):
        emitted = False
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(settings.get("temperature") or 0.2),
            "max_tokens": max_tokens if attempt == 0 else min(max_tokens, 700),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if "aliyuncs.com" in chat_url.lower() or "dashscope" in chat_url.lower():
            payload["enable_thinking"] = False
        req = request.Request(
            chat_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "PracticeAssistant/1.0",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=timeout_seconds if attempt == 0 else timeout_seconds + 20) as resp:
                content_type = str(resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    choices = data.get("choices") or []
                    message = choices[0].get("message", {}) if choices else {}
                    content = message.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
                    if content and on_delta:
                        emitted = True
                        on_delta(str(content))
                    return {
                        "content": content or json.dumps(data, ensure_ascii=False, indent=2),
                        "model": data.get("model", model),
                        "usage": data.get("usage", {}),
                    }

                chunks: list[str] = []
                response_model = model
                usage: dict[str, Any] = {}
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event_data = line[5:].strip()
                    if not event_data or event_data == "[DONE]":
                        continue
                    event = json.loads(event_data)
                    response_model = str(event.get("model") or response_model)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    delta = choices[0].get("delta", {}) if choices else {}
                    content = delta.get("content", "")
                    if isinstance(content, list):
                        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                    if content:
                        text = str(content)
                        chunks.append(text)
                        if on_delta:
                            emitted = True
                            on_delta(text)
                if not chunks:
                    raise RuntimeError("模型接口未返回可用内容")
                return {
                    "content": "".join(chunks),
                    "model": response_model,
                    "usage": usage,
                }
            break
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"模型接口返回 {exc.code}: {body[:1200]}") from exc
        except (socket.timeout, TimeoutError) as exc:
            last_error = exc
            if emitted and on_reset:
                on_reset()
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            raise RuntimeError("模型接口读取超时，自动重试后仍未恢复。") from exc
        except (
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            ssl.SSLEOFError,
        ) as exc:
            last_error = exc
            if emitted and on_reset:
                on_reset()
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            raise RuntimeError("模型服务提前断开连接，已自动重连 3 次仍未恢复。") from exc
        except ssl.SSLError as exc:
            last_error = exc
            if emitted and on_reset:
                on_reset()
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            raise RuntimeError("模型接口 SSL 连接被提前断开，自动重试后仍未恢复。") from exc
        except error.URLError as exc:
            last_error = exc
            reason = str(exc.reason)
            if attempt < 2 and any(marker in reason.lower() for marker in transient_markers):
                if emitted and on_reset:
                    on_reset()
                time.sleep(0.35 * (attempt + 1))
                continue
            raise RuntimeError(f"无法连接模型接口: {exc.reason}") from exc
    else:
        raise RuntimeError(f"模型请求失败: {last_error}")

    raise RuntimeError("模型接口未返回可用内容")


def resolve_sync_base_url(settings: dict[str, Any] | None = None) -> str:
    base_url = str((settings or {}).get("sync_base_url") or CLOUD_SYNC_BASE_URL).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("云端同步地址必须是有效的 http(s) 地址")
    return base_url


def cloud_sync_request(
    settings: dict[str, Any],
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str = "",
) -> dict[str, Any]:
    url = f"{resolve_sync_base_url(settings)}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=float(settings.get("timeout") or 90)) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error") or body
        except Exception:
            detail = body
        raise CloudSyncError(str(detail)[:1200], status=int(exc.code or 400)) from exc
    except error.URLError as exc:
        raise RuntimeError(f"无法连接云端同步服务: {exc.reason}") from exc
    return json.loads(text or "{}")


def cloud_send_email_code(settings: dict[str, Any], email: str, purpose: str) -> dict[str, Any]:
    return cloud_sync_request(
        settings,
        "/api/sync/send-code",
        "POST",
        {"email": email, "username": email, "purpose": purpose},
    )


def cloud_register_user(settings: dict[str, Any], email: str, password: str, code: str) -> dict[str, Any]:
    return cloud_sync_request(
        settings,
        "/api/sync/register",
        "POST",
        {"email": email, "username": email, "password": password, "code": code},
    )


def cloud_reset_password(settings: dict[str, Any], email: str, password: str, code: str) -> dict[str, Any]:
    return cloud_sync_request(
        settings,
        "/api/sync/reset-password",
        "POST",
        {"email": email, "username": email, "password": password, "code": code},
    )


def cloud_login_user(settings: dict[str, Any], email: str, password: str) -> dict[str, Any]:
    return cloud_sync_request(
        settings,
        "/api/sync/login",
        "POST",
        {"email": email, "username": email, "password": password},
    )


def cloud_logout_user(settings: dict[str, Any], token: str) -> dict[str, Any]:
    return cloud_sync_request(settings, "/api/sync/logout", "POST", {}, token=token)


def cloud_get_profile(settings: dict[str, Any], token: str) -> dict[str, Any]:
    data = cloud_sync_request(settings, "/api/sync/profile", "GET", token=token)
    return data.get("profile", data)


def cloud_update_current(settings: dict[str, Any], token: str, payload: dict[str, Any]) -> dict[str, Any]:
    return cloud_sync_request(settings, "/api/sync/state", "POST", payload, token=token)


def cloud_prepend_history_item(settings: dict[str, Any], token: str, item: dict[str, Any]) -> dict[str, Any]:
    return cloud_sync_request(settings, "/api/sync/history/add", "POST", {"item": item}, token=token)


def cloud_update_settings(settings: dict[str, Any], token: str, model_settings: dict[str, Any]) -> dict[str, Any]:
    return cloud_sync_request(settings, "/api/sync/settings", "POST", {"settings": model_settings}, token=token)


def cloud_proxy_sync(path: str, method: str, payload: dict[str, Any] | None, headers: Any) -> dict[str, Any]:
    auth = headers.get("Authorization", "") if headers else ""
    token = auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    return cloud_sync_request(
        {"sync_base_url": CLOUD_SYNC_BASE_URL, "timeout": 90},
        path,
        method,
        payload,
        token=token,
    )


def messages_include_images(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def model_rejected_image_input(error_text: str) -> bool:
    lowered = error_text.lower()
    markers = [
        "unknown variant image_url",
        "expected text",
        "image_url",
        "only text",
        "text only",
        "content must be a string",
        "unsupported image",
        "does not support image",
        "multimodal",
        "multi-modal",
        "vision",
    ]
    return any(marker in lowered for marker in markers)


def extract_prompt_text(messages: list[dict[str, Any]]) -> str:
    if len(messages) < 2:
        return ""
    content = messages[1].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text", ""))
    return ""


def replace_user_message_with_text(messages: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    if not messages:
        return [{"role": "user", "content": text}]
    copied = [dict(message) for message in messages]
    if len(copied) == 1:
        copied.append({"role": "user", "content": text})
    else:
        copied[1] = {"role": "user", "content": text}
    return copied


def web_config_to_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_url": config.get("baseUrl", config.get("base_url", "")),
        "api_key": config.get("apiKey", config.get("api_key", "")),
        "model": config.get("model", ""),
        "max_tokens": config.get("maxTokens", config.get("max_tokens", 1800)),
        "temperature": config.get("temperature", 0.2),
        "timeout": config.get("timeout", 90),
    }


def image_to_data_url(image: Image.Image, max_side: int = 2048, quality: int = 90) -> str:
    image = image.convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=0 if quality >= 85 else 2)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def load_image_file(path: str) -> str:
    with Image.open(path) as image:
        return image_to_data_url(image)


def image_from_data_url(data_url: str) -> Image.Image:
    encoded = data_url.split(",", 1)[1] if "," in data_url else data_url
    return Image.open(io.BytesIO(base64.b64decode(encoded))).copy()


def sync_image_payload(images: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not images:
        return []
    item = images[-1]
    data_url = str(item.get("data_url") or item.get("dataUrl") or "")
    if not data_url:
        return []
    with image_from_data_url(data_url) as image:
        compact = image_to_data_url(image, max_side=1200, quality=76)
    return [{
        "id": str(item.get("id") or f"desktop-image-{int(time.time() * 1000)}"),
        "name": str(item.get("name") or "latest-capture.jpg"),
        "dataUrl": compact,
    }]


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


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def virtual_bbox_to_image_bbox(
    bbox: tuple[int, int, int, int],
    virtual_bounds: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    virtual_x, virtual_y, virtual_width, virtual_height = virtual_bounds
    image_width, image_height = image_size
    if virtual_width <= 0 or virtual_height <= 0 or image_width <= 0 or image_height <= 0:
        raise ValueError("无效的虚拟桌面或截图尺寸")
    scale_x = image_width / virtual_width
    scale_y = image_height / virtual_height
    return (
        max(0, min(image_width, round((left - virtual_x) * scale_x))),
        max(0, min(image_height, round((top - virtual_y) * scale_y))),
        max(0, min(image_width, round((right - virtual_x) * scale_x))),
        max(0, min(image_height, round((bottom - virtual_y) * scale_y))),
    )


def is_right_button_down() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)


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
    selector.geometry(f"{width}x{height}{x:+d}{y:+d}")
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
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


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


class SHSTOCKICONINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("iSysImageIndex", ctypes.c_int),
        ("iIcon", ctypes.c_int),
        ("szPath", wintypes.WCHAR * 260),
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
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WH_KEYBOARD_LL = 13

    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000

    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002

    HOTKEY_REGION = 1001
    HOTKEY_LONG_PRESS = 1002
    HOTKEY_FULLSCREEN = 1003
    HOTKEY_OCR = 1004
    HOTKEY_REGION_FALLBACK = 1011
    HOTKEY_LONG_PRESS_FALLBACK = 1012
    HOTKEY_FULLSCREEN_FALLBACK = 1013
    HOTKEY_OCR_FALLBACK = 1014
    HOTKEY_REGION_FUNCTION = 1021
    HOTKEY_LONG_PRESS_FUNCTION = 1022
    HOTKEY_FULLSCREEN_FUNCTION = 1023
    HOTKEY_OCR_FUNCTION = 1024
    HOTKEY_ANALYZE_QUEUE = 1031
    HOTKEY_ANALYZE_QUEUE_FALLBACK = 1032

    CMD_SHOW = 2001
    CMD_WEB = 2002
    CMD_REVIEW = 2003
    CMD_EXIT = 2004

    VK_R = 0x52
    VK_H = 0x48
    VK_F = 0x46
    VK_O = 0x4F
    VK_ENTER = 0x0D
    VK_A = 0x41
    VK_F8 = 0x77
    VK_F9 = 0x78
    VK_F10 = 0x79
    VK_F11 = 0x7A
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_SHIFT = 0x10

    def __init__(self, command_queue: queue.Queue[Any]) -> None:
        self.command_queue = command_queue
        self.thread: threading.Thread | None = None
        self.hwnd: int | None = None
        self.thread_id = 0
        self.ready = threading.Event()
        self._wndproc: Any = None
        self._keyboard_proc_ref: Any = None
        self._keyboard_hook: int | None = None
        self._consumed_keys: set[int] = set()
        self._last_hook_trigger: tuple[int, float] = (0, 0.0)
        self._registered_hotkeys: list[int] = []
        self._tray_added = False
        self._tray_hicon: int | None = None

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
        if not self._install_keyboard_hook():
            self._register_hotkeys(hwnd)
        self._add_tray_icon(hwnd)
        self.ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _install_keyboard_hook(self) -> bool:
        user32 = ctypes.windll.user32
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._keyboard_proc_ref = HOOKPROC(self._keyboard_hook_proc)
        hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._keyboard_proc_ref, None, 0)
        if not hook:
            self.command_queue.put(("status", "全局键盘监听启动失败，正在使用系统热键注册兜底"))
            return False
        self._keyboard_hook = int(hook)
        self.command_queue.put(("status", "全局快捷键监听已启用，可在其他窗口中使用"))
        return True

    def _keyboard_hook_proc(self, code: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        if code < 0:
            return int(user32.CallNextHookEx(None, code, wparam, lparam))

        data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = int(data.vkCode)
        event = int(wparam)
        if event in {self.WM_KEYUP, self.WM_SYSKEYUP} and vk in self._consumed_keys:
            self._consumed_keys.discard(vk)
            return 1
        if event not in {self.WM_KEYDOWN, self.WM_SYSKEYDOWN}:
            return int(user32.CallNextHookEx(None, code, wparam, lparam))

        ctrl = bool(user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
        alt = bool(user32.GetAsyncKeyState(self.VK_MENU) & 0x8000)
        shift = bool(user32.GetAsyncKeyState(self.VK_SHIFT) & 0x8000)
        command = None
        if vk == self.VK_F8:
            command = "hotkey_region"
        elif vk == self.VK_F9:
            command = "hotkey_long_press"
        elif vk == self.VK_F10:
            command = "hotkey_fullscreen"
        elif vk == self.VK_F11:
            command = "hotkey_ocr"
        elif ctrl and (alt or shift):
            command = {
                self.VK_R: "hotkey_region",
                self.VK_H: "hotkey_long_press",
                self.VK_F: "hotkey_fullscreen",
                self.VK_O: "hotkey_ocr",
            }.get(vk)

        if not command:
            return int(user32.CallNextHookEx(None, code, wparam, lparam))

        self._consumed_keys.add(vk)
        last_vk, last_at = self._last_hook_trigger
        now = time.monotonic()
        if vk != last_vk or now - last_at >= 0.45:
            self._last_hook_trigger = (vk, now)
            self.command_queue.put(command)
        return 1

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        if msg == self.WM_HOTKEY:
            mapping = {
                self.HOTKEY_REGION: "hotkey_region",
                self.HOTKEY_LONG_PRESS: "hotkey_long_press",
                self.HOTKEY_FULLSCREEN: "hotkey_fullscreen",
                self.HOTKEY_OCR: "hotkey_ocr",
                self.HOTKEY_REGION_FALLBACK: "hotkey_region",
                self.HOTKEY_LONG_PRESS_FALLBACK: "hotkey_long_press",
                self.HOTKEY_FULLSCREEN_FALLBACK: "hotkey_fullscreen",
                self.HOTKEY_OCR_FALLBACK: "hotkey_ocr",
                self.HOTKEY_REGION_FUNCTION: "hotkey_region",
                self.HOTKEY_LONG_PRESS_FUNCTION: "hotkey_long_press",
                self.HOTKEY_FULLSCREEN_FUNCTION: "hotkey_fullscreen",
                self.HOTKEY_OCR_FUNCTION: "hotkey_ocr",
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
        hotkeys = [
            ("右键长按选区", [
                (self.HOTKEY_REGION, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_R, "Ctrl+Alt+R"),
                (self.HOTKEY_REGION_FALLBACK, self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_R, "Ctrl+Shift+R"),
                (self.HOTKEY_REGION_FUNCTION, self.MOD_NOREPEAT, self.VK_F8, "F8"),
            ]),
            ("长按选区", [
                (self.HOTKEY_LONG_PRESS, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_H, "Ctrl+Alt+H"),
                (self.HOTKEY_LONG_PRESS_FALLBACK, self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_H, "Ctrl+Shift+H"),
                (self.HOTKEY_LONG_PRESS_FUNCTION, self.MOD_NOREPEAT, self.VK_F9, "F9"),
            ]),
            ("全屏截图", [
                (self.HOTKEY_FULLSCREEN, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_F, "Ctrl+Alt+F"),
                (self.HOTKEY_FULLSCREEN_FALLBACK, self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_F, "Ctrl+Shift+F"),
                (self.HOTKEY_FULLSCREEN_FUNCTION, self.MOD_NOREPEAT, self.VK_F10, "F10"),
            ]),
            ("图片转文字", [
                (self.HOTKEY_OCR, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_O, "Ctrl+Alt+O"),
                (self.HOTKEY_OCR_FALLBACK, self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_O, "Ctrl+Shift+O"),
                (self.HOTKEY_OCR_FUNCTION, self.MOD_NOREPEAT, self.VK_F11, "F11"),
            ]),
        ]
        for action, candidates in hotkeys:
            registered_labels: list[str] = []
            failed_labels: list[str] = []
            for hotkey_id, modifiers, vk, label in candidates:
                if user32.RegisterHotKey(hwnd, hotkey_id, modifiers, vk):
                    self._registered_hotkeys.append(hotkey_id)
                    registered_labels.append(label)
                else:
                    failed_labels.append(label)
            if registered_labels:
                self.command_queue.put(("status", f"{action}热键可用: {', '.join(registered_labels)}"))
            else:
                self.command_queue.put(("status", f"{action}热键全部注册失败: {', '.join(failed_labels)}"))

    def _add_tray_icon(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        stock = SHSTOCKICONINFO()
        stock.cbSize = ctypes.sizeof(SHSTOCKICONINFO)
        # SIID_DESKTOPPC + SHGSI_ICON | SHGSI_SMALLICON.
        if shell32.SHGetStockIconInfo(94, 0x000000100 | 0x000000001, ctypes.byref(stock)) == 0:
            self._tray_hicon = int(stock.hIcon)
            nid.hIcon = stock.hIcon
        else:
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
        if self._keyboard_hook:
            user32.UnhookWindowsHookEx(wintypes.HANDLE(self._keyboard_hook))
            self._keyboard_hook = None
        self._keyboard_proc_ref = None
        self._consumed_keys.clear()
        if self._tray_added:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
            self._tray_added = False
        if self._tray_hicon:
            user32.DestroyIcon(wintypes.HANDLE(self._tray_hicon))
            self._tray_hicon = None


@dataclass
class MobileState:
    question: str = ""
    answer: str = DEFAULT_MOBILE_ANSWER
    model: str = ""
    updated_at: str = ""
    image_count: int = 0
    images: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(
        self,
        question: str,
        answer: str,
        model: str,
        image_count: int,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        with self.lock:
            self.question = question
            self.answer = answer
            self.model = model
            self.image_count = image_count
            if images is not None:
                self.images = [dict(item) for item in images[-1:]]
            self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            save_latest_result(
                {
                    "question": self.question,
                    "answer": self.answer,
                    "model": self.model,
                    "updated_at": self.updated_at,
                    "image_count": self.image_count,
                    "images": self.images,
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
                "images": [dict(item) for item in self.images],
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

    def start_ndjson(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def send_stream_event(self, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(raw)
        self.wfile.flush()

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/api/latest":
            self.send_json({"ok": True, **self.shared_state.snapshot()})
            return

        if path.startswith("/api/sync/"):
            try:
                self.send_json(cloud_proxy_sync(path, "GET", None, self.headers))
            except CloudSyncError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=exc.status)
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
                self.send_json(cloud_proxy_sync(path, "POST", payload, self.headers))
                return

            if path == "/api/chat":
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    raise ValueError("messages 必须是数组")
                self.start_ndjson()
                try:
                    result = call_openai_compatible(
                        web_config_to_settings(payload.get("config") or {}),
                        messages,
                        on_delta=lambda chunk: self.send_stream_event({"delta": chunk}),
                        on_reset=lambda: self.send_stream_event({"reset": True}),
                    )
                    self.send_stream_event(
                        {
                            "done": True,
                            "content": clean_markdown_answer(result["content"]),
                            "model": result["model"],
                            "usage": result.get("usage", {}),
                            "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                except Exception as exc:
                    self.send_stream_event({"error": str(exc)})
                return

            if path == "/api/check":
                result = call_openai_compatible(
                    web_config_to_settings(payload.get("config") or {}),
                    [{"role": "user", "content": "请只回复 OK。"}],
                )
                self.send_json({"ok": True, "content": result["content"], "model": result["model"]})
                return

            self.send_error(404)
        except CloudSyncError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=exc.status)
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
        latest_answer = str(latest_result.get("answer") or "")
        if latest_answer.strip().startswith("请求失败"):
            latest_result = {}
        self.mobile_state = MobileState(
            question=str(latest_result.get("question", "")),
            answer=str(latest_result.get("answer") or DEFAULT_MOBILE_ANSWER),
            model=str(latest_result.get("model", "")),
            updated_at=str(latest_result.get("updated_at", "")),
            image_count=int(latest_result.get("image_count") or 0),
            images=list(latest_result.get("images") or [])[-1:],
        )
        self.mobile_server: ThreadingHTTPServer | None = None
        self.mobile_thread: threading.Thread | None = None
        self.mobile_url = ""
        self.web_url = ""
        self.sync_token = str(self.settings.get("sync_token", ""))
        self.sync_username = str(self.settings.get("sync_username", ""))
        self.images: list[dict[str, str]] = []
        self.preview_photos: list[ImageTk.PhotoImage] = []
        self.multi_image = False
        self.busy = False
        self.ui_commands: queue.Queue[Any] = queue.Queue()
        self.win32_controller: Win32BackgroundController | None = None
        self.hidden_to_tray = False
        self.exiting = False
        self.tray_icon_ready = False
        self.reveal_window_after_answer = False
        self.long_press_selector: dict[str, Any] | None = None
        self.stream_answer_buffer = ""

        self.root.title("面试 Copilot - 云端同步桌面版")
        geometry, minimum_size = compact_window_geometry(self.root)
        self.root.geometry(geometry)
        self.root.minsize(*minimum_size)
        self.root.configure(bg="#edf6ff")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.setup_style()
        self.build_ui()
        self.bind_hotkeys()
        self.start_mobile_server()
        self.start_background_controls()
        self.root.after(100, self.process_ui_commands)
        self.root.after(350, self.hide_to_tray)

    def setup_style(self) -> None:
        self.root.option_add("*Font", ("Microsoft YaHei", 9))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#edf6ff")
        style.configure("Panel.TFrame", background="#f8fbff", relief="flat")
        style.configure("TLabel", background="#edf6ff", foreground="#0b1220")
        style.configure("Panel.TLabel", background="#f8fbff", foreground="#0b1220")
        style.configure("Muted.TLabel", background="#f8fbff", foreground="#657389")
        style.configure("Accent.TButton", background="#1677ff", foreground="#ffffff", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#0d63da")])
        style.configure("TButton", padding=(9, 5), background="#ffffff", foreground="#0b1220")
        style.configure("TCombobox", padding=(6, 2))
        style.configure("TCheckbutton", background="#f8fbff", foreground="#101828", padding=(0, 2))

    def build_ui(self) -> None:
        self.init_control_variables()
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=0, minsize=320)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        header = tk.Frame(
            outer,
            bg="#0b1220",
            padx=14,
            pady=10,
            highlightthickness=1,
            highlightbackground="#2a456c",
        )
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="面试 Copilot 工作台",
            bg="#0b1220",
            fg="#ffffff",
            font=("Microsoft YaHei", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="实时截图辅助 · 模拟面试训练 · 题库复盘 · 网页/Windows 云端同步",
            bg="#0b1220",
            fg="#a6c8ff",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        badge_frame = tk.Frame(header, bg="#0b1220")
        badge_frame.grid(row=2, column=0, sticky="w", pady=(7, 0))
        for text in ("Practice mode", "Cloud account", "Live capture"):
            tk.Label(
                badge_frame,
                text=text,
                bg="#13233a",
                fg="#dff3ff",
                padx=8,
                pady=2,
                font=("Microsoft YaHei", 8, "bold"),
            ).pack(side="left", padx=(0, 6))
        ttk.Button(header, text="账号", command=self.open_account_window).grid(row=0, column=1, rowspan=2, padx=(6, 0))
        ttk.Button(header, text="模型", command=self.open_model_settings_window).grid(row=0, column=2, rowspan=2, padx=(6, 0))
        ttk.Button(header, text="同步", command=self.open_web_window).grid(row=0, column=3, rowspan=2, padx=(6, 0))
        ttk.Button(header, text="生成回答", style="Accent.TButton", command=self.analyze).grid(row=0, column=4, rowspan=2, padx=(6, 0))
        hide_button = tk.Button(
            header,
            text="🖥",
            command=self.hide_to_tray,
            bg="#13233a",
            fg="#ffffff",
            activebackground="#203b61",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            width=3,
            font=("Segoe UI Emoji", 13),
            cursor="hand2",
        )
        hide_button.grid(row=0, column=5, rowspan=2, padx=(8, 0))
        hide_button.bind("<Enter>", lambda _event: self.set_status("隐藏到系统托盘"))
        hide_button.bind("<Leave>", lambda _event: self.set_status("就绪"))

        left_shell = ttk.Frame(outer, style="Panel.TFrame", padding=0)
        left_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        left_shell.rowconfigure(0, weight=1)
        left_shell.columnconfigure(0, weight=1)
        left_canvas = tk.Canvas(left_shell, bg="#f8fbff", bd=0, highlightthickness=0)
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

    def init_control_variables(self) -> None:
        if hasattr(self, "base_url_var"):
            return
        self.base_url_var = tk.StringVar(value=str(self.settings.get("base_url", "")))
        self.api_key_var = tk.StringVar(value=str(self.settings.get("api_key", "")))
        self.model_var = tk.StringVar(value=str(self.settings.get("model", "")))
        self.max_tokens_var = tk.StringVar(value=str(self.settings.get("max_tokens", 1800)))
        self.temperature_var = tk.StringVar(value=str(self.settings.get("temperature", 0.2)))
        self.timeout_var = tk.StringVar(value=str(self.settings.get("timeout", 90)))
        self.sync_username_var = tk.StringVar(value=self.sync_username)
        self.sync_password_var = tk.StringVar(value="")
        self.sync_code_var = tk.StringVar(value="")
        self.fixed_prompt_var = tk.StringVar(value=str(self.settings.get("fixed_prompt", DEFAULT_FIXED_PROMPT)))

    def build_left_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="实时采集", style="Panel.TLabel", font=("Microsoft YaHei", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="截图、导入图片或 OCR 后进入右侧面试输入区。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 8))
        help_text = (
            "Ctrl+Alt+R/H: 后台右键长按 2.5 秒选左上角，再右键长按 2.5 秒选右下角，选完自动分析\n"
            "Ctrl+Alt+F: 全屏截图并自动分析\n"
            "Ctrl+Enter: 分析当前题目\n"
            "Ctrl+Alt+O: 图片转文字\n"
            "备用: Ctrl+Shift+R/H/F/O 或 F8/F9/F10/F11\n"
            "Shift+C: 切换模型预设\n"
            "截图和分析会在后台运行，结果同步到移动端/网页端。"
        )
        help_box = tk.Text(parent, height=7, wrap="word", bg="#f3f8ff", fg="#121621", relief="solid", bd=1)
        help_box.insert("1.0", help_text)
        help_box.configure(state="disabled")
        help_box.grid(row=2, column=0, sticky="ew", pady=(0, 10))

        ttk.Label(parent, text="输入来源", style="Panel.TLabel", font=("Microsoft YaHei", 11, "bold")).grid(row=3, column=0, sticky="w")
        shot_buttons = ttk.Frame(parent, style="Panel.TFrame")
        shot_buttons.grid(row=4, column=0, sticky="ew", pady=(5, 5))
        ttk.Button(shot_buttons, text="拖拽截图", command=lambda: self.capture_region(auto_analyze=True, keep_hidden=True)).pack(side="left")
        ttk.Button(shot_buttons, text="右键长按选区", command=lambda: self.capture_long_press_region(auto_analyze=True, keep_hidden=True)).pack(side="left", padx=5)
        ttk.Button(shot_buttons, text="全屏截图", command=lambda: self.capture_screen(auto_analyze=True)).pack(side="left")
        image_buttons = ttk.Frame(parent, style="Panel.TFrame")
        image_buttons.grid(row=5, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(image_buttons, text="添加图片", command=self.add_image_file).pack(side="left")
        ttk.Button(image_buttons, text="图片转文字", command=self.ocr_images_to_question).pack(side="left", padx=5)
        ttk.Button(image_buttons, text="清空图片", command=self.clear_images).pack(side="left")

        self.use_vision_var = tk.BooleanVar(value=bool(self.settings.get("use_vision_input", True)))
        ttk.Checkbutton(
            parent,
            text="优先把截图直接发送给视觉模型",
            variable=self.use_vision_var,
        ).grid(row=6, column=0, sticky="w")
        self.multi_image_var = tk.BooleanVar(value=False)

        self.image_status_var = tk.StringVar(value="未添加图片")
        ttk.Label(parent, textvariable=self.image_status_var, style="Muted.TLabel").grid(row=8, column=0, sticky="w")
        preview_frame = ttk.Frame(parent, style="Panel.TFrame")
        preview_frame.grid(row=9, column=0, sticky="ew", pady=(5, 8))
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

        self.account_status_var = tk.StringVar(value=self.account_status_text())
        self.mobile_url_var = tk.StringVar(value="启动中")
        ttk.Label(parent, textvariable=self.account_status_var, style="Muted.TLabel").grid(row=10, column=0, sticky="w", pady=(8, 0))
        ttk.Label(parent, text="账号、模型、同步入口已收纳在右上角。", style="Muted.TLabel").grid(row=11, column=0, sticky="w", pady=(4, 0))

    def build_right_panel(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent, style="Panel.TFrame")
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure((0, 1, 2), weight=1)
        self.mode_var = tk.StringVar(value=MODES[0])
        self.style_var = tk.StringVar(value=STYLES[0])
        self.language_var = tk.StringVar(value=LANGUAGES[0])
        self.add_combo(controls, "题型", self.mode_var, MODES, 0)
        self.add_combo(controls, "回答", self.style_var, STYLES, 1)
        self.add_combo(controls, "语言", self.language_var, LANGUAGES, 2)

        prompt_frame = ttk.Frame(parent, style="Panel.TFrame")
        prompt_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        prompt_frame.columnconfigure(0, weight=1)
        ttk.Label(prompt_frame, text="固定提示词", style="Panel.TLabel", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Entry(prompt_frame, textvariable=self.fixed_prompt_var).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(prompt_frame, text="恢复默认", command=self.reset_fixed_prompt).grid(row=1, column=1, sticky="e", padx=(5, 0))

        ttk.Label(parent, text="面试输入", style="Panel.TLabel", font=("Microsoft YaHei", 13, "bold")).grid(row=2, column=0, sticky="w", pady=(12, 5))
        self.question_text = tk.Text(
            parent,
            height=8,
            wrap="word",
            relief="solid",
            bd=1,
            bg="#fbfdff",
            fg="#101828",
            insertbackground="#101828",
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
        ttk.Button(action_top, text="分析当前题目", style="Accent.TButton", command=self.analyze).pack(side="left")
        ttk.Button(action_top, text="拖拽截图", command=lambda: self.capture_region(auto_analyze=True, keep_hidden=True)).pack(side="left", padx=(5, 0))
        ttk.Button(action_top, text="右键长按选区", command=lambda: self.capture_long_press_region(auto_analyze=True, keep_hidden=True)).pack(side="left", padx=(5, 0))
        ttk.Button(action_top, text="图片转文字", command=self.ocr_images_to_question).pack(side="left", padx=(5, 0))
        ttk.Button(action_bottom, text="同步到云端", command=self.sync_current_workspace).pack(side="left")
        ttk.Button(action_bottom, text="发布到复盘页", command=self.publish_current_answer).pack(side="left", padx=(5, 0))
        ttk.Button(action_bottom, text="复制答案", command=self.copy_answer).pack(side="left", padx=(5, 0))
        ttk.Button(action_bottom, text="清空", command=self.clear_workspace).pack(side="left", padx=(5, 0))

        ttk.Label(parent, text="AI 回答建议", style="Panel.TLabel", font=("Microsoft YaHei", 13, "bold")).grid(row=5, column=0, sticky="w", pady=(0, 5))
        self.answer_text = tk.Text(
            parent,
            height=11,
            wrap="word",
            relief="solid",
            bd=1,
            bg="#fbfdff",
            fg="#101828",
            insertbackground="#101828",
            padx=10,
            pady=8,
            spacing1=2,
            spacing3=6,
        )
        self.answer_text.grid(row=6, column=0, sticky="nsew")
        self.answer_text.insert("1.0", "配置模型后输入面试题、笔试题或截图内容，开始生成回答建议。")

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
        self.root.bind("<Control-Alt-r>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-R>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-h>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-H>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Alt-f>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Alt-F>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Alt-o>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Control-Alt-O>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Control-Shift-r>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Shift-R>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Shift-h>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Shift-H>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<Control-Shift-f>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Shift-F>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<Control-Shift-o>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Control-Shift-O>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<F8>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<F9>", lambda _event: self.capture_long_press_region(auto_analyze=True, keep_hidden=True))
        self.root.bind("<F10>", lambda _event: self.capture_screen(auto_analyze=True))
        self.root.bind("<F11>", lambda _event: self.ocr_images_to_question())
        self.root.bind("<Shift-C>", lambda _event: self.cycle_preset())
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
            "multi_image_mode": False,
            "sync_base_url": str(self.settings.get("sync_base_url") or CLOUD_SYNC_BASE_URL).strip() or CLOUD_SYNC_BASE_URL,
            "sync_username": self.sync_username.strip().lower(),
            "sync_token": self.sync_token,
        }

    def save_current_settings(self) -> None:
        self.settings = self.current_settings()
        save_settings(self.settings)
        if self.sync_token:
            try:
                cloud_update_settings(self.settings, self.sync_token, self.settings)
                self.set_status("配置已保存并同步")
                return
            except Exception as exc:
                self.set_status(f"配置已保存，云端同步失败: {str(exc)[:80]}")
                return
        self.set_status("配置已保存")

    def apply_cloud_model_settings(self, cloud_settings: dict[str, Any] | None) -> bool:
        if not isinstance(cloud_settings, dict):
            return False
        base_url = str(cloud_settings.get("baseUrl") or cloud_settings.get("base_url") or "").strip()
        api_key = str(cloud_settings.get("apiKey") or cloud_settings.get("api_key") or "").strip()
        model = str(cloud_settings.get("model") or "").strip()
        if not any([base_url, api_key, model]):
            return False
        self.base_url_var.set(base_url)
        self.api_key_var.set(api_key)
        self.model_var.set(model)
        self.max_tokens_var.set(str(cloud_settings.get("maxTokens") or cloud_settings.get("max_tokens") or 1800))
        self.temperature_var.set(str(cloud_settings.get("temperature") if cloud_settings.get("temperature") is not None else 0.2))
        self.timeout_var.set(str(cloud_settings.get("timeout") or 90))
        fixed_prompt = str(cloud_settings.get("fixedPrompt") or cloud_settings.get("fixed_prompt") or "").strip()
        if fixed_prompt == LEGACY_FIXED_PROMPT:
            fixed_prompt = DEFAULT_FIXED_PROMPT
        if fixed_prompt:
            self.fixed_prompt_var.set(fixed_prompt)
        if "useVisionInput" in cloud_settings or "use_vision_input" in cloud_settings:
            self.use_vision_var.set(bool(cloud_settings.get("useVisionInput", cloud_settings.get("use_vision_input"))))
        if "multiImageMode" in cloud_settings or "multi_image_mode" in cloud_settings:
            self.multi_image = False
            if hasattr(self, "multi_image_var"):
                self.multi_image_var.set(False)
        self.settings = self.current_settings()
        save_settings(self.settings)
        return True

    def clear_key(self) -> None:
        self.api_key_var.set("")
        self.save_current_settings()

    def reset_fixed_prompt(self) -> None:
        self.fixed_prompt_var.set(DEFAULT_FIXED_PROMPT)
        self.save_current_settings()

    def make_dialog(self, title: str, width: int = 520, height: int = 360) -> tk.Toplevel:
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title(title)
        dialog.configure(bg="#eaf3ff")
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        try:
            dialog.attributes("-alpha", 0.98)
        except Exception:
            pass
        self.center_dialog(dialog, width, height)
        dialog.after(80, lambda: self.show_centered_dialog(dialog, width, height))
        return dialog

    def show_centered_dialog(self, dialog: tk.Toplevel, width: int, height: int) -> None:
        if not dialog.winfo_exists():
            return
        self.center_dialog(dialog, width, height)
        dialog.deiconify()
        dialog.lift(self.root)
        dialog.focus_force()

    def center_dialog(self, dialog: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        dialog.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(self.root.winfo_width(), 1)
        root_h = max(self.root.winfo_height(), 1)
        screen_w = dialog.winfo_screenwidth()
        screen_h = dialog.winfo_screenheight()
        x = root_x + max((root_w - width) // 2, 0)
        y = root_y + max((root_h - height) // 2, 0)
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.lift(self.root)

    def open_account_window(self) -> None:
        if not hasattr(self, "sync_username_var"):
            return
        dialog = self.make_dialog("账号同步", 620, 300)
        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=14)
        frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        frame.columnconfigure((0, 1, 2), weight=1)
        ttk.Label(frame, text="云端账号", style="Panel.TLabel", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, textvariable=self.account_status_var, style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 12))
        fields = ttk.Frame(frame, style="Panel.TFrame")
        fields.grid(row=2, column=0, columnspan=3, sticky="ew")
        fields.columnconfigure((0, 1, 2), weight=1)
        self.add_small_entry(fields, "邮箱", self.sync_username_var, 0)
        self.add_small_entry(fields, "密码", self.sync_password_var, 1, show="*")
        self.add_small_entry(fields, "验证码", self.sync_code_var, 2)
        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="登录", style="Accent.TButton", command=self.login_sync_account).pack(side="left")
        ttk.Button(actions, text="发送注册验证码", command=self.send_register_code).pack(side="left", padx=6)
        ttk.Button(actions, text="验证码注册", command=self.register_sync_account).pack(side="left")
        ttk.Button(actions, text="发送重置验证码", command=self.send_reset_code).pack(side="left", padx=6)
        ttk.Button(actions, text="重置密码", command=self.reset_sync_password).pack(side="left")
        ttk.Button(actions, text="退出", command=self.logout_sync_account).pack(side="left", padx=6)
        ttk.Label(
            frame,
            text=f"同步服务: {resolve_sync_base_url(self.settings)}",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.show_centered_dialog(dialog, 620, 300)

    def open_model_settings_window(self) -> None:
        if not hasattr(self, "base_url_var"):
            return
        dialog = self.make_dialog("模型配置", 620, 430)
        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=14)
        frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="模型配置", style="Panel.TLabel", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.add_labeled_entry(frame, "API Base URL", self.base_url_var, 1)
        self.add_labeled_entry(frame, "API Key", self.api_key_var, 3, show="*")
        self.add_labeled_entry(frame, "模型", self.model_var, 5)
        small = ttk.Frame(frame, style="Panel.TFrame")
        small.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        small.columnconfigure((0, 1, 2), weight=1)
        self.add_small_entry(small, "最大输出", self.max_tokens_var, 0)
        self.add_small_entry(small, "温度", self.temperature_var, 1)
        self.add_small_entry(small, "超时", self.timeout_var, 2)
        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=8, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="保存配置", style="Accent.TButton", command=self.save_current_settings).pack(side="left")
        ttk.Button(actions, text="测试连接", command=self.test_connection).pack(side="left", padx=6)
        ttk.Button(actions, text="清除 Key", command=self.clear_key).pack(side="left")
        self.show_centered_dialog(dialog, 620, 430)

    def open_web_window(self) -> None:
        dialog = self.make_dialog("网页/移动端", 560, 260)
        frame = ttk.Frame(dialog, style="Panel.TFrame", padding=14)
        frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="网页与同步", style="Panel.TLabel", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="同一个云端账号可以在网页和 Windows 端拉取/推送数据。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 12))
        ttk.Entry(frame, textvariable=self.mobile_url_var).grid(row=2, column=0, sticky="ew")
        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="打开网页版", command=lambda: webbrowser.open(self.web_url)).pack(side="left")
        ttk.Button(actions, text="打开复盘", command=lambda: webbrowser.open(self.mobile_url)).pack(side="left", padx=6)
        ttk.Button(actions, text="复制链接", command=self.copy_mobile_url).pack(side="left")
        sync = ttk.Frame(frame, style="Panel.TFrame")
        sync.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(sync, text="同步当前", style="Accent.TButton", command=self.sync_current_workspace).pack(side="left")
        ttk.Button(sync, text="拉取网页最新", command=self.pull_sync_workspace).pack(side="left", padx=6)
        ttk.Label(frame, text="仅限自我练习和复盘，不用于真实考试或面试。", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.show_centered_dialog(dialog, 560, 260)

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
        code = self.sync_code_var.get().strip() if hasattr(self, "sync_code_var") else ""
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            result = cloud_register_user(self.settings, username, password, code)
            self.set_sync_session(str(result.get("email") or result["username"]), result["token"])
            cloud_update_settings(self.settings, self.sync_token, self.current_settings())
            self.sync_password_var.set("")
            if hasattr(self, "sync_code_var"):
                self.sync_code_var.set("")
            self.set_status("账号已注册并登录，模型配置已同步")
        except Exception as exc:
            messagebox.showerror("注册失败", str(exc))

    def send_register_code(self) -> None:
        self.send_sync_code("register")

    def send_reset_code(self) -> None:
        self.send_sync_code("reset")

    def send_sync_code(self, purpose: str) -> None:
        username = self.sync_username_var.get().strip().lower()
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            cloud_send_email_code(self.settings, username, purpose)
            self.set_status("验证码已发送，请查看邮箱")
        except Exception as exc:
            messagebox.showerror("验证码发送失败", str(exc))

    def reset_sync_password(self) -> None:
        username = self.sync_username_var.get().strip().lower()
        password = self.sync_password_var.get()
        code = self.sync_code_var.get().strip() if hasattr(self, "sync_code_var") else ""
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            result = cloud_reset_password(self.settings, username, password, code)
            self.set_sync_session(str(result.get("email") or result["username"]), result["token"])
            self.sync_password_var.set("")
            if hasattr(self, "sync_code_var"):
                self.sync_code_var.set("")
            self.pull_sync_workspace(silent=True)
            self.set_status("密码已重置并登录，模型配置已拉取")
        except Exception as exc:
            messagebox.showerror("重置失败", str(exc))

    def login_sync_account(self) -> None:
        username = self.sync_username_var.get().strip().lower()
        password = self.sync_password_var.get()
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            result = cloud_login_user(self.settings, username, password)
            self.set_sync_session(str(result.get("email") or result["username"]), result["token"])
            self.sync_password_var.set("")
            self.pull_sync_workspace(silent=True)
            self.set_status("账号已登录，模型配置已拉取")
        except Exception as exc:
            messagebox.showerror("登录失败", str(exc))

    def logout_sync_account(self) -> None:
        previous_token = self.sync_token
        try:
            if previous_token:
                cloud_logout_user(self.settings, previous_token)
        except Exception:
            pass
        self.sync_token = ""
        self.sync_username = ""
        self.sync_username_var.set("")
        self.sync_password_var.set("")
        if hasattr(self, "sync_code_var"):
            self.sync_code_var.set("")
        self.settings = self.current_settings()
        save_settings(self.settings)
        self.update_account_status()
        self.set_status("账号已退出")

    def current_sync_payload(self, answer: str | None = None, model: str | None = None, image_count: int | None = None) -> dict[str, Any]:
        cloud_images = sync_image_payload(self.images)
        return {
            "question": self.question_text.get("1.0", "end").strip(),
            "answer": clean_markdown_answer(self.answer_text.get("1.0", "end").strip() if answer is None else answer),
            "model": self.model_var.get().strip() if model is None else model,
            "imageCount": len(self.images) if image_count is None else image_count,
            "images": cloud_images,
            "mode": self.mode_var.get(),
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def sync_current_workspace(self) -> None:
        if not self.sync_token:
            messagebox.showwarning("未登录", "请先登录或注册账号。")
            return
        try:
            payload = self.current_sync_payload()
            cloud_update_current(self.settings, self.sync_token, payload)
            cloud_update_settings(self.settings, self.sync_token, self.current_settings())
            if payload["answer"]:
                cloud_prepend_history_item(
                    self.settings,
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
            self.set_status("已同步当前内容和模型配置")
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
            "images": sync_image_payload(self.images),
            "mode": mode,
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            cloud_update_current(self.settings, self.sync_token, payload)
            cloud_prepend_history_item(
                self.settings,
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

    def publish_analysis_progress(self, question: str, image_count: int, mode: str) -> None:
        model = self.model_var.get().strip()
        answer = "正在分析，请稍候..."
        local_images = sync_image_payload(self.images)
        self.mobile_state.update(question, answer, model, image_count, local_images)
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
            cloud_update_current(self.settings, self.sync_token, payload)
        except Exception:
            pass

    def pull_sync_workspace(self, silent: bool = False) -> None:
        if not self.sync_token:
            if not silent:
                messagebox.showwarning("未登录", "请先登录或注册账号。")
            return
        try:
            profile = cloud_get_profile(self.settings, self.sync_token)
            settings_pulled = self.apply_cloud_model_settings(profile.get("settings"))
            current = profile.get("current", {})
            question = str(current.get("question", ""))
            answer = clean_markdown_answer(str(current.get("answer", "")))
            model = str(current.get("model", ""))
            image_count = int(current.get("imageCount") or 0)
            cloud_images = list(current.get("images") or [])[-1:]
            if question:
                self.question_text.delete("1.0", "end")
                self.question_text.insert("1.0", question)
            if answer:
                self.set_answer(answer)
            if isinstance(current.get("images"), list):
                self.images = [
                    {
                        "name": str(item.get("name") or "cloud-image.jpg"),
                        "data_url": str(item.get("dataUrl") or item.get("data_url") or ""),
                    }
                    for item in cloud_images
                    if item.get("dataUrl") or item.get("data_url")
                ]
                self.update_image_status()
            if question or answer:
                self.mobile_state.update(question, answer, model, image_count, cloud_images)
            if not silent:
                self.set_status("已拉取账号最新内容和模型配置" if settings_pulled else "已拉取账号最新内容")
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
            if auto_analyze:
                self.root.withdraw()
                self.hidden_to_tray = True
                self.root.update_idletasks()
                time.sleep(0.12)
            image = self.grab_virtual_screen()
            if auto_analyze:
                self.images.clear()
            name = f"screen-{time.strftime('%H%M%S')}.jpg"
            data_url = image_to_data_url(image)
            self.images.append({"name": name, "data_url": data_url})
            self.update_image_status()
            self.set_status(f"已截取最新图片: {name}")
            if auto_analyze:
                self.analyze(force_vision=True)
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))

    def capture_region(self, auto_analyze: bool = True, keep_hidden: bool = True) -> None:
        root_was_visible = self.root.state() != "withdrawn"
        if keep_hidden:
            self.root.withdraw()
            self.hidden_to_tray = True
        x, y, width, height = virtual_screen_bounds(self.root)
        selector = tk.Toplevel(self.root)
        canvas = configure_selector_window(selector, x, y, width, height, alpha=0.16)

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
        if self.long_press_selector and self.long_press_selector.get("active"):
            self.long_press_selector["active"] = False
            self.set_status("已重新开始右键长按选区")

        if keep_hidden:
            self.root.withdraw()
            self.hidden_to_tray = True

        self.long_press_selector = {
            "active": True,
            "points": [],
            "pressed": False,
            "press_pos": None,
            "press_at": 0.0,
            "recorded_current": False,
            "auto_analyze": auto_analyze,
        }
        self.set_status("后台选区已启动：右键长按 2.5 秒记录两个角点")
        self.root.after(40, self.poll_long_press_region)

    def poll_long_press_region(self) -> None:
        state = self.long_press_selector
        if not state or not state.get("active"):
            return

        try:
            down = is_right_button_down()
            pos = cursor_position()
        except Exception as exc:
            state["active"] = False
            self.set_status(f"右键长按监听失败: {exc}")
            return

        if down and not state["pressed"]:
            state["pressed"] = True
            state["press_pos"] = pos
            state["press_at"] = time.monotonic()
            state["recorded_current"] = False

        elif down and state["pressed"] and not state["recorded_current"]:
            start = state.get("press_pos")
            if start is not None:
                if abs(pos[0] - start[0]) > LONG_PRESS_MOVE_TOLERANCE or abs(pos[1] - start[1]) > LONG_PRESS_MOVE_TOLERANCE:
                    state["pressed"] = False
                    state["press_pos"] = None
                    state["recorded_current"] = False
                elif time.monotonic() - float(state.get("press_at") or 0.0) >= LONG_PRESS_SECONDS:
                    self.record_long_press_point(start)

        elif not down and state["pressed"]:
            state["pressed"] = False
            state["press_pos"] = None
            state["recorded_current"] = False

        if state.get("active"):
            self.root.after(40, self.poll_long_press_region)

    def record_long_press_point(self, point: tuple[int, int]) -> None:
        state = self.long_press_selector
        if not state or not state.get("active"):
            return

        points: list[tuple[int, int]] = state["points"]
        if len(points) >= 2:
            return

        points.append((int(point[0]), int(point[1])))
        state["recorded_current"] = True
        self.set_status(f"已记录第 {len(points)} 个角点")

        if len(points) < 2:
            return

        state["active"] = False
        (x1, y1), (x2, y2) = points
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right - left < 8 or bottom - top < 8:
            self.set_status("截图区域过小，已取消")
            return

        self.root.after(
            120,
            lambda: self.capture_bbox(
                (left, top, right, bottom),
                prefix="longpress-region",
                auto_recognize=bool(state.get("auto_analyze", True)),
                restore_main=False,
                reveal_after_answer=False,
            ),
        )

    def capture_bbox(
        self,
        bbox: tuple[int, int, int, int],
        prefix: str = "region",
        auto_recognize: bool = False,
        restore_main: bool = False,
        reveal_after_answer: bool = False,
    ) -> None:
        try:
            image = self.grab_virtual_screen(bbox)
            if restore_main:
                self.root.deiconify()
                self.root.lift()
            if auto_recognize:
                self.images.clear()
            name = f"{prefix}-{time.strftime('%H%M%S')}.jpg"
            data_url = image_to_data_url(image)
            self.images.append({"name": name, "data_url": data_url})
            self.update_image_status()
            self.set_status(f"已截取最新图片: {name}")
            if auto_recognize:
                self.analyze(force_vision=True, reveal_after_answer=reveal_after_answer)
        except Exception as exc:
            if restore_main:
                self.root.deiconify()
                self.root.lift()
            messagebox.showerror("区域截图失败", str(exc))

    def grab_virtual_screen(self, bbox: tuple[int, int, int, int] | None = None) -> Image.Image:
        image = ImageGrab.grab(all_screens=True)
        if bbox is None:
            return image

        crop_box = virtual_bbox_to_image_bbox(
            bbox,
            virtual_screen_bounds(self.root),
            image.size,
        )
        if crop_box[2] - crop_box[0] < 2 or crop_box[3] - crop_box[1] < 2:
            image.close()
            raise RuntimeError("截图区域超出屏幕范围或尺寸过小")
        cropped = image.crop(crop_box)
        image.close()
        return cropped

    def add_image_file(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择练习图片",
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("All files", "*.*")],
        )
        if not paths:
            return
        try:
            latest_path = paths[-1]
            self.images.clear()
            with Image.open(latest_path) as image:
                self.images.append({"name": Path(latest_path).name, "data_url": image_to_data_url(image)})
            self.update_image_status()
            self.set_status("已添加最新图片")
        except Exception as exc:
            messagebox.showerror("图片读取失败", str(exc))

    def analysis_images(self, use_all: bool = False) -> list[dict[str, str]]:
        if not self.images:
            return []
        return [dict(self.images[-1])]

    def update_image_status(self) -> None:
        total = len(self.images)
        if total <= 0:
            self.image_status_var.set("未添加图片。本次不会发送图片。")
        else:
            self.image_status_var.set(f"当前仅保留最新截图 1 张；分析只发送这一张。")
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

        latest_items = self.images[-1:]
        for index, item in enumerate(latest_items, start=1):
            row = ttk.Frame(self.preview_inner, style="Panel.TFrame", padding=(8, 8))
            row.pack(fill="x", padx=4, pady=(4, 0))

            try:
                thumb = image_from_data_url(item["data_url"])
                thumb.thumbnail((118, 76))
                photo = ImageTk.PhotoImage(thumb)
                self.preview_photos.append(photo)
                preview = tk.Label(row, image=photo, bg="#ffffff", bd=1, relief="solid", cursor="hand2")
                preview.pack(side="left")
                preview.bind("<Button-1>", lambda _event, current=dict(item): self.open_image_preview(current))
            except Exception:
                ttk.Label(row, text="[图片预览失败]", style="Panel.TLabel").pack(side="left")

            name = item.get("name") or f"image-{index}"
            ttk.Label(
                row,
                text=f"{index}. {name} · 本次分析会发送",
                style="Panel.TLabel",
                wraplength=190,
            ).pack(side="left", padx=(10, 0), fill="x", expand=True)

        self.preview_inner.update_idletasks()
        self.preview_canvas.configure(scrollregion=self.preview_canvas.bbox("all"))

    def open_image_preview(self, item: dict[str, Any]) -> None:
        try:
            source = image_from_data_url(str(item.get("data_url") or ""))
        except Exception as exc:
            messagebox.showerror("图片预览失败", str(exc))
            return

        dialog = self.make_dialog("截图放大预览", 920, 680)
        dialog.minsize(560, 420)
        toolbar = ttk.Frame(dialog, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text=str(item.get("name") or "截图"), font=("Microsoft YaHei", 10, "bold")).pack(side="left")

        canvas = tk.Canvas(dialog, bg="#10151f", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        state = {"scale": 1.0, "photo": None}

        def render() -> None:
            available_w = max(80, canvas.winfo_width() - 24)
            available_h = max(80, canvas.winfo_height() - 24)
            fit = min(available_w / source.width, available_h / source.height, 1.0)
            scale = max(0.1, min(6.0, fit * float(state["scale"])))
            size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
            resized = source.resize(size, Image.LANCZOS)
            state["photo"] = ImageTk.PhotoImage(resized)
            canvas.delete("all")
            canvas.create_image(canvas.winfo_width() // 2, canvas.winfo_height() // 2, image=state["photo"], anchor="center")

        def zoom(factor: float) -> None:
            state["scale"] = max(0.2, min(8.0, float(state["scale"]) * factor))
            render()

        ttk.Button(toolbar, text="−", width=3, command=lambda: zoom(0.8)).pack(side="right")
        ttk.Button(toolbar, text="+", width=3, command=lambda: zoom(1.25)).pack(side="right", padx=5)
        ttk.Button(toolbar, text="适应窗口", command=lambda: (state.update(scale=1.0), render())).pack(side="right")
        canvas.bind("<Configure>", lambda _event: render())
        canvas.bind("<MouseWheel>", lambda event: zoom(1.15 if event.delta > 0 else 0.87))
        dialog.bind("<Destroy>", lambda _event: source.close())

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
            "综合题": "完整识别题干和选项，先给最终结论，再给关键推理和易错点。",
            "编程题": f"给出算法思路、边界条件、复杂度，并使用 {language} 写出代码。",
            "行测/图推": "逐项检查图形、数量、位置、方向和变化规律，先给答案选项，再说明最关键的排除依据。",
            "模拟面试": "按模拟面试回答组织语言，给出 60 秒版本和展开版本。",
            "复盘讲解": "复盘材料中的问题，指出错因、知识点和下一步训练安排。",
        }
        style_rules = {
            "答案 + 解析": "直接给出答案，再解释关键依据。",
            "只给思路": "只给解题思路和推理过程，不直接跳到结论。",
            "精简答案": "第一行直接给最终答案；选择题先写答案字母和选项内容，随后只保留最关键的解析。",
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

    def build_messages(self, force_vision: bool = False, use_all_images: bool = False) -> list[dict[str, Any]]:
        system_prompt = (
            "你是求职笔试、编程题和模拟面试的练习教练。"
            "你的用途仅限自我练习、复盘和模拟训练。"
            "不要协助正在进行的真实考试、真实笔试、真实面试或任何规避监控的行为；"
            "遇到这类语境时，改为提供学习建议、通用思路和练习方法。"
            "回答使用中文，结构清晰。"
            "必须完整读取图片中的题干、图表、代码和所有选项，禁止根据模糊局部内容臆测。"
            "先输出答案结果，再输出精简解析；选择题答案必须包含选项字母和对应内容。"
            f"{document_format_instruction()}"
        )
        prompt = self.build_prompt()
        use_vision = force_vision or bool(self.use_vision_var.get())
        images = self.analysis_images(use_all=use_all_images)
        if not images or not use_vision:
            return [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": image["data_url"], "detail": "high"}})
        return [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}]

    def ocr_images_to_question(self) -> None:
        if self.busy:
            self.set_status("当前正在处理，请稍后再试")
            return
        if not self.images:
            messagebox.showwarning("缺少图片", "请先截图或添加图片。")
            return
        self.start_ocr(self.analysis_images(), auto_analyze=False)

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
            question_text = self.ocr_images_to_text(images)
            self.root.after(0, lambda: self.finish_ocr(question_text, auto_analyze))
        except Exception as exc:
            self.root.after(0, lambda: self.finish_ocr_error(str(exc)))

    def ocr_images_to_text(self, images: list[dict[str, Any]]) -> str:
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
        return question_text

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
                cloud_update_current(self.settings, self.sync_token, self.current_sync_payload(answer=answer, model=self.model_var.get().strip()))
            except Exception:
                pass
        self.set_answer(answer)
        self.set_status("图片转文字失败")

    def analyze_capture_queue(self) -> None:
        if self.images:
            self.analyze(force_vision=True, use_all_images=False)
        else:
            self.analyze()

    def analyze(self, force_vision: bool = False, reveal_after_answer: bool = False, use_all_images: bool = False) -> None:
        if self.busy:
            if reveal_after_answer:
                self.show_window()
            return
        question = self.question_text.get("1.0", "end").strip()
        if not question and not self.images:
            if reveal_after_answer:
                self.show_window()
            messagebox.showwarning("缺少题目", "请先输入题目或添加图片。")
            return
        use_vision = force_vision or bool(self.use_vision_var.get())
        images_for_analysis = self.analysis_images(use_all=use_all_images)
        if not question and images_for_analysis and not use_vision:
            self.start_ocr(images_for_analysis, auto_analyze=True)
            return
        try:
            self.settings = self.current_settings()
            save_settings(self.settings)
            if self.sync_token:
                try:
                    cloud_update_settings(self.settings, self.sync_token, self.settings)
                except Exception:
                    pass
            messages = self.build_messages(force_vision=force_vision, use_all_images=use_all_images)
            question_snapshot = self.question_text.get("1.0", "end").strip()
            if not question_snapshot and images_for_analysis and use_vision:
                question_snapshot = "见随附图片。"
            image_count = len(images_for_analysis)
            images_snapshot = images_for_analysis
            settings_snapshot = dict(self.settings)
            mode_snapshot = self.mode_var.get()
            self.publish_analysis_progress(question_snapshot, image_count, mode_snapshot)
        except Exception as exc:
            if reveal_after_answer:
                self.show_window()
            messagebox.showerror("配置错误", str(exc))
            return

        self.busy = True
        if not reveal_after_answer:
            self.root.withdraw()
            self.hidden_to_tray = True
        self.reveal_window_after_answer = self.reveal_window_after_answer or reveal_after_answer
        self.stream_answer_buffer = ""
        self.set_answer("")
        self.set_status("解析中...")
        threading.Thread(
            target=self._analyze_worker,
            args=(settings_snapshot, messages, question_snapshot, image_count, mode_snapshot, images_snapshot),
            daemon=True,
        ).start()

    def _analyze_worker(
        self,
        settings: dict[str, Any],
        messages: list[dict[str, Any]],
        question: str,
        image_count: int,
        mode: str,
        images: list[dict[str, Any]],
    ) -> None:
        try:
            result = call_openai_compatible(
                settings,
                messages,
                on_delta=lambda chunk: self.root.after(0, lambda text=chunk: self.append_stream_answer(text)),
                on_reset=lambda: self.root.after(0, self.reset_stream_answer),
            )
            answer = clean_markdown_answer(result["content"])
            model = result["model"]
            self.mobile_state.update(question, answer, model, image_count)
            self.push_sync_result(question, answer, model, image_count, mode)
            self.root.after(0, lambda latest=(images[-1] if images else None): self.finish_answer(answer, f"完成: {model}，已发布到移动端复盘", latest))
        except Exception as exc:
            error_text = str(exc)
            if images and messages_include_images(messages) and model_rejected_image_input(error_text):
                try:
                    self.root.after(
                        0,
                        lambda: (
                            self.set_answer("当前模型接口不支持图片直传，正在自动转文字后重试..."),
                            self.set_status("视觉输入被拒绝，正在 OCR 兜底"),
                        ),
                    )
                    ocr_text = self.ocr_images_to_text(images)
                    original_prompt = extract_prompt_text(messages)
                    fallback_prompt = "\n\n".join(
                        part
                        for part in [
                            original_prompt,
                            "图片文字识别结果：",
                            ocr_text,
                        ]
                        if part
                    )
                    fallback_messages = replace_user_message_with_text(messages, fallback_prompt)
                    result = call_openai_compatible(settings, fallback_messages)
                    answer = clean_markdown_answer(result["content"])
                    model = result["model"]
                    self.root.after(0, lambda text=ocr_text: self.replace_question_text(text))
                    self.mobile_state.update(ocr_text, answer, model, image_count)
                    self.push_sync_result(ocr_text, answer, model, image_count, mode)
                    self.root.after(
                        0,
                        lambda latest=(images[-1] if images else None): self.finish_answer(answer, f"完成: {model}，已用 OCR 兜底发布到移动端复盘", latest),
                    )
                    return
                except Exception as fallback_exc:
                    error_text = f"{error_text}\n自动 OCR 兜底也失败：{fallback_exc}"

            answer = clean_markdown_answer(f"请求失败：{error_text}")
            model = str(settings.get("model", ""))
            self.mobile_state.update(question, answer, model, image_count)
            self.push_sync_result(question, answer, model, image_count, mode)
            self.root.after(0, lambda latest=(images[-1] if images else None): self.finish_answer(answer, "解析失败", latest))

    def retain_latest_image(self, latest_image: dict[str, Any] | None) -> None:
        if not latest_image:
            return
        self.images = [{
            "name": str(latest_image.get("name") or "latest-capture"),
            "data_url": str(latest_image.get("data_url") or latest_image.get("dataUrl") or ""),
        }]
        self.preview_photos.clear()
        self.update_image_status()

    def finish_answer(self, answer: str, status: str, latest_image: dict[str, Any] | None = None) -> None:
        self.busy = False
        self.retain_latest_image(latest_image)
        self.set_answer(clean_markdown_answer(answer))
        self.set_status(status)
        if self.reveal_window_after_answer:
            self.reveal_window_after_answer = False
            self.show_window()

    def publish_current_answer(self) -> None:
        answer = clean_markdown_answer(self.answer_text.get("1.0", "end").strip())
        self.set_answer(answer)
        question = self.question_text.get("1.0", "end").strip()
        self.mobile_state.update(question, answer, self.model_var.get().strip(), len(self.images))
        if self.sync_token:
            try:
                cloud_update_current(self.settings, self.sync_token, self.current_sync_payload())
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
                self.capture_long_press_region(auto_analyze=True, keep_hidden=True)
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
        self.set_multi_image(False)

    def set_multi_image_from_ui(self) -> None:
        self.set_multi_image(False)

    def set_multi_image(self, enabled: bool) -> None:
        self.multi_image = False
        if hasattr(self, "multi_image_var"):
            self.multi_image_var.set(False)
        self.settings = self.current_settings()
        save_settings(self.settings)
        self.update_image_status()
        self.set_status("多图发送已取消：始终只分析最新截图")

    def set_answer(self, text: str) -> None:
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", clean_markdown_answer(text))

    def append_stream_answer(self, chunk: str) -> None:
        self.stream_answer_buffer += chunk
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", self.stream_answer_buffer)
        self.answer_text.see("end")

    def reset_stream_answer(self) -> None:
        self.stream_answer_buffer = ""
        self.answer_text.delete("1.0", "end")

    def replace_question_text(self, text: str) -> None:
        self.question_text.delete("1.0", "end")
        self.question_text.insert("1.0", text)

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
    vision_messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": [{"type": "text", "text": "题目"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}]},
    ]
    assert messages_include_images(vision_messages)
    assert model_rejected_image_input("unknown variant image_url, expected text")
    assert extract_prompt_text(vision_messages) == "题目"
    assert virtual_bbox_to_image_bbox((-1920, 0, 0, 1080), (-1920, 0, 3840, 1080), (3840, 1080)) == (0, 0, 1920, 1080)
    assert virtual_bbox_to_image_bbox((0, 0, 960, 540), (0, 0, 1920, 1080), (3840, 2160)) == (0, 0, 1920, 1080)
    replaced = replace_user_message_with_text(vision_messages, "纯文本")
    assert replaced[0]["role"] == "system"
    assert replaced[1] == {"role": "user", "content": "纯文本"}
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
