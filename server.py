from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import unquote, urlparse

from sync_store import SyncError, handle_sync_get, handle_sync_post


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATIC_DIR = ROOT / "static"
MAX_BODY_BYTES = 24 * 1024 * 1024


def json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def create_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    for offset in range(20):
        candidate = port + offset if port else 0
        try:
            server = ThreadingHTTPServer((host, candidate), PracticeAssistantHandler)
            return server, int(server.server_address[1])
        except OSError:
            if port == 0 or offset == 19:
                raise
    raise RuntimeError("No available local port found")


def resolve_chat_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("缺少 API Base URL")

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API Base URL 必须是 http(s) 地址")

    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def call_openai_compatible(
    config: dict[str, Any],
    messages: list[dict[str, Any]],
    on_delta: Any | None = None,
    on_reset: Any | None = None,
) -> dict[str, Any]:
    base_url = str(config.get("baseUrl", "")).strip()
    api_key = str(config.get("apiKey", "")).strip()
    model = str(config.get("model", "")).strip()
    timeout = min(55.0, max(20.0, float(config.get("timeout") or 55)))
    temperature = float(config.get("temperature") if config.get("temperature") not in {"", None} else 0.2)
    prompt_text = json.dumps(messages, ensure_ascii=False)
    max_tokens = min(int(config.get("maxTokens") or 1800), 2200 if "编程题" in prompt_text else 900)

    if not api_key:
        raise ValueError("缺少 API Key")
    if not model:
        raise ValueError("缺少模型名称")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if "aliyuncs.com" in base_url.lower() or "dashscope" in base_url.lower():
        payload["enable_thinking"] = False

    req = request.Request(
        resolve_chat_url(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            content_type = str(resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            else:
                chunks: list[str] = []
                response_model = model
                usage: dict[str, Any] = {}
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = json.loads(raw)
                    response_model = str(event.get("model") or response_model)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    delta = (event.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if isinstance(delta, list):
                        delta = "".join(str(part.get("text") or "") for part in delta if isinstance(part, dict))
                    if delta:
                        text = str(delta)
                        chunks.append(text)
                        if on_delta:
                            on_delta(text)
                if not chunks:
                    raise RuntimeError("模型接口未返回可用内容")
                return {
                    "content": "".join(chunks),
                    "model": response_model,
                    "usage": usage,
                    "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"模型接口返回 {exc.code}: {body[:1200]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"无法连接模型接口: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("模型接口请求超时") from exc

    choices = data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    if content and on_delta:
        on_delta(str(content))

    return {
        "content": content or json.dumps(data, ensure_ascii=False, indent=2),
        "model": data.get("model", model),
        "usage": data.get("usage", {}),
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class PracticeAssistantHandler(BaseHTTPRequestHandler):
    server_version = "PracticeAssistant/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        code, body = json_bytes(payload, status)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/api/health":
            self.send_json({"ok": True, "service": "local", "time": time.time()})
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

        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()

        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or target.is_dir():
            self.send_error(404)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

            if self.path == "/api/chat":
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    raise ValueError("messages 必须是数组")
                self.start_ndjson()
                try:
                    result = call_openai_compatible(
                        payload.get("config") or {},
                        messages,
                        on_delta=lambda chunk: self.send_stream_event({"delta": chunk}),
                        on_reset=lambda: self.send_stream_event({"reset": True}),
                    )
                    self.send_stream_event({"done": True, **result})
                except Exception as exc:
                    self.send_stream_event({"error": str(exc)})
                return

            if self.path == "/api/check":
                result = call_openai_compatible(
                    payload.get("config") or {},
                    [{"role": "user", "content": "请只回复 OK。"}],
                )
                self.send_json({"ok": True, "content": result["content"], "model": result["model"]})
                return

            self.send_error(404)
        except SyncError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except Exception as exc:  # noqa: BLE001 - return readable local errors to the UI
            self.send_json({"ok": False, "error": str(exc)}, status=400)


def main() -> None:
    parser = argparse.ArgumentParser(description="BYO API practice assistant")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server, port = create_server(args.host, args.port)
    url = f"http://{args.host}:{port}"
    print(f"Local practice assistant: {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
