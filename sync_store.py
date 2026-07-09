from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any


def resolve_app_dir() -> Path:
    override = os.environ.get("BYO_APP_DATA_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = resolve_app_dir()
SYNC_PATH = Path(os.environ.get("BYO_SYNC_STORE_PATH", APP_DIR / "sync_store.json"))
MAX_SYNC_ITEMS = 200
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_LOCK = threading.Lock()


class SyncError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def default_store() -> dict[str, Any]:
    return {"users": {}, "sessions": {}}


def load_store() -> dict[str, Any]:
    if not SYNC_PATH.exists():
        return default_store()
    try:
        loaded = json.loads(SYNC_PATH.read_text(encoding="utf-8"))
    except Exception:
        return default_store()
    loaded.setdefault("users", {})
    loaded.setdefault("sessions", {})
    return loaded


def save_store(store: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = SYNC_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(SYNC_PATH)


def clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise SyncError("请填写有效邮箱地址。")
    return email


def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < 4:
        raise SyncError("密码至少 4 位。")
    if len(password) > 128:
        raise SyncError("密码过长。")
    return password


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    _salt, candidate = hash_password(password, salt_hex)
    return hmac.compare_digest(candidate, digest_hex)


def empty_profile(username: str) -> dict[str, Any]:
    return {
        "username": username,
        "email": username,
        "current": {
            "question": "",
            "answer": "",
            "model": "",
            "imageCount": 0,
            "images": [],
            "mode": "",
            "updatedAt": "",
        },
        "history": [],
        "bank": [],
        "settings": empty_model_settings(),
    }


def empty_model_settings() -> dict[str, Any]:
    return {
        "baseUrl": "",
        "apiKey": "",
        "model": "",
        "maxTokens": 1800,
        "temperature": 0.2,
        "timeout": 90,
        "fixedPrompt": "",
        "useVisionInput": True,
        "multiImageMode": False,
        "updatedAt": "",
    }


def normalize_model_settings(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("settings") or payload.get("config") or payload
    settings = empty_model_settings()
    settings["baseUrl"] = str(source.get("baseUrl") or source.get("base_url") or "").strip()
    settings["apiKey"] = str(source.get("apiKey") or source.get("api_key") or "").strip()
    settings["model"] = str(source.get("model") or "").strip()
    settings["maxTokens"] = int(float(source.get("maxTokens", source.get("max_tokens", 1800)) or 1800))
    settings["temperature"] = float(source.get("temperature", 0.2) if source.get("temperature", 0.2) is not None else 0.2)
    settings["timeout"] = float(source.get("timeout") or 90)
    settings["fixedPrompt"] = str(source.get("fixedPrompt") or source.get("fixed_prompt") or "").strip()
    settings["useVisionInput"] = bool(source.get("useVisionInput", source.get("use_vision_input", True)))
    settings["multiImageMode"] = bool(source.get("multiImageMode", source.get("multi_image_mode", False)))
    settings["updatedAt"] = str(source.get("updatedAt") or source.get("updated_at") or now_text())
    return settings


def ensure_profile(user: dict[str, Any], username: str) -> dict[str, Any]:
    profile = user.setdefault("profile", empty_profile(username))
    profile.setdefault("username", username)
    profile.setdefault("email", username)
    profile.setdefault("current", empty_profile(username)["current"])
    profile.setdefault("history", [])
    profile.setdefault("bank", [])
    profile.setdefault("settings", empty_model_settings())
    return profile


def issue_session(store: dict[str, Any], username: str) -> str:
    token = secrets.token_urlsafe(32)
    store["sessions"][token] = {"username": username, "createdAt": now_text()}
    return token


def register_user(username: str, password: str) -> dict[str, Any]:
    username = clean_email(username)
    password = validate_password(password)
    with _LOCK:
        store = load_store()
        if username in store["users"]:
            raise SyncError("账号已存在。")
        salt, digest = hash_password(password)
        store["users"][username] = {
            "passwordSalt": salt,
            "passwordHash": digest,
            "createdAt": now_text(),
            "profile": empty_profile(username),
        }
        token = issue_session(store, username)
        save_store(store)
    return {"ok": True, "token": token, "username": username, "email": username}


def login_user(username: str, password: str) -> dict[str, Any]:
    username = clean_email(username)
    password = validate_password(password)
    with _LOCK:
        store = load_store()
        user = store["users"].get(username)
        if not user or not verify_password(password, user.get("passwordSalt", ""), user.get("passwordHash", "")):
            raise SyncError("账号或密码错误。", status=401)
        token = issue_session(store, username)
        save_store(store)
    return {"ok": True, "token": token, "username": username, "email": username}


def logout_token(token: str) -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        store["sessions"].pop(token, None)
        save_store(store)
    return {"ok": True}


def username_for_token(store: dict[str, Any], token: str) -> str:
    session = store.get("sessions", {}).get(token or "")
    if not session:
        raise SyncError("请先登录账号。", status=401)
    username = session.get("username", "")
    if username not in store.get("users", {}):
        raise SyncError("账号不存在，请重新登录。", status=401)
    return username


def get_bearer_token(headers: Any) -> str:
    auth = headers.get("Authorization", "") if headers else ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def get_profile(token: str) -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        username = username_for_token(store, token)
        profile = ensure_profile(store["users"][username], username)
        return json.loads(json.dumps(profile, ensure_ascii=False))


def update_current(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    images = normalize_sync_images(payload.get("images"))
    current = {
        "question": str(payload.get("question", "")),
        "answer": str(payload.get("answer", "")),
        "model": str(payload.get("model", "")),
        "imageCount": int(payload.get("imageCount") or payload.get("image_count") or 0),
        "images": images,
        "mode": str(payload.get("mode", "")),
        "updatedAt": str(payload.get("updatedAt") or payload.get("updated_at") or now_text()),
    }
    with _LOCK:
        store = load_store()
        username = username_for_token(store, token)
        profile = ensure_profile(store["users"][username], username)
        profile["current"] = current
        save_store(store)
    return {"ok": True, "current": current}


def normalize_sync_images(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for raw in value[-1:]:
        if not isinstance(raw, dict):
            continue
        data_url = str(raw.get("dataUrl") or raw.get("data_url") or "")
        if not data_url.startswith(("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")):
            continue
        if len(data_url) > 3_000_000:
            raise SyncError("同步图片过大，请重新截图或降低图片尺寸。", status=413)
        result.append({
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or "image")[:180],
            "dataUrl": data_url,
        })
    return result


def replace_collection(token: str, name: str, items: list[Any]) -> dict[str, Any]:
    if name not in {"history", "bank"}:
        raise SyncError("未知同步集合。")
    clean_items = items[:MAX_SYNC_ITEMS] if isinstance(items, list) else []
    with _LOCK:
        store = load_store()
        username = username_for_token(store, token)
        profile = ensure_profile(store["users"][username], username)
        profile[name] = clean_items
        save_store(store)
    return {"ok": True, name: clean_items}


def prepend_history_item(token: str, item: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        username = username_for_token(store, token)
        profile = ensure_profile(store["users"][username], username)
        history = profile.setdefault("history", [])
        item_id = item.get("id")
        if item_id:
            history = [entry for entry in history if entry.get("id") != item_id]
        history.insert(0, item)
        profile["history"] = history[:MAX_SYNC_ITEMS]
        save_store(store)
        return {"ok": True, "history": profile["history"]}


def update_settings(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = normalize_model_settings(payload)
    with _LOCK:
        store = load_store()
        username = username_for_token(store, token)
        profile = ensure_profile(store["users"][username], username)
        profile["settings"] = settings
        save_store(store)
    return {"ok": True, "settings": settings}


def handle_sync_get(path: str, headers: Any) -> dict[str, Any] | None:
    token = get_bearer_token(headers)
    if path == "/api/sync/me":
        if not token:
            raise SyncError("未登录。", status=401)
        profile = get_profile(token)
        return {"ok": True, "username": profile["username"], "email": profile.get("email", profile["username"])}
    if path == "/api/sync/state":
        profile = get_profile(token)
        return {"ok": True, "current": profile.get("current", {})}
    if path == "/api/sync/history":
        profile = get_profile(token)
        return {"ok": True, "history": profile.get("history", [])}
    if path == "/api/sync/bank":
        profile = get_profile(token)
        return {"ok": True, "bank": profile.get("bank", [])}
    if path == "/api/sync/settings":
        profile = get_profile(token)
        return {"ok": True, "settings": profile.get("settings", empty_model_settings())}
    if path == "/api/sync/profile":
        profile = get_profile(token)
        return {"ok": True, "profile": profile}
    return None


def handle_sync_post(path: str, payload: dict[str, Any], headers: Any) -> dict[str, Any] | None:
    token = get_bearer_token(headers)
    if path == "/api/sync/register":
        return register_user(str(payload.get("email") or payload.get("username") or ""), str(payload.get("password", "")))
    if path == "/api/sync/login":
        return login_user(str(payload.get("email") or payload.get("username") or ""), str(payload.get("password", "")))
    if path == "/api/sync/logout":
        return logout_token(token)
    if path == "/api/sync/state":
        return update_current(token, payload)
    if path == "/api/sync/history":
        items = payload.get("history", payload.get("items", []))
        return replace_collection(token, "history", items if isinstance(items, list) else [])
    if path == "/api/sync/history/add":
        item = payload.get("item", payload)
        if not isinstance(item, dict):
            raise SyncError("历史记录必须是对象。")
        return prepend_history_item(token, item)
    if path == "/api/sync/bank":
        items = payload.get("bank", payload.get("items", []))
        return replace_collection(token, "bank", items if isinstance(items, list) else [])
    if path == "/api/sync/settings":
        return update_settings(token, payload)
    return None
