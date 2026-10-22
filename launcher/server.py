"""本地管理台 HTTP 服务：静态页面 + JSON API + SSE 实时日志。

只监听 127.0.0.1，并做基本的 Origin 校验，避免被外部网页调用。
"""
from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import env_check
from bot_manager import bot_manager
from config_store import (
    APP_NAME,
    APP_VERSION,
    DEFAULTS,
    ROOT,
    ConfigError,
    get_api_key,
    load_config,
    load_settings,
    resolved_values,
    save_config,
    save_settings,
    validate_values,
)


def get_web_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "launcher" / "web"
    return Path(__file__).resolve().parent / "web"


WEB_DIR = get_web_dir()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    """可直接展示给用户的前端错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------- 配置校验缓存（/api/state 每 2 秒轮询，避免重复 import） ----------------
_summary_cache: dict = {"ts": 0.0, "data": None}
SUMMARY_TTL = 2.0


def _config_summary() -> dict:
    now = time.time()
    if _summary_cache["data"] is not None and now - _summary_cache["ts"] < SUMMARY_TTL:
        return _summary_cache["data"]

    data = load_config()
    fields = data["fields"]
    values = resolved_values()
    errors = validate_values(values)
    api_key_set = bool(get_api_key())
    if not api_key_set:
        errors = ["API Key 未配置"]

    summary = {
        "loaded": True,
        "api_key_set": api_key_set,
        "api_key_masked": fields["api_key"]["masked"],
        "model": fields["model"]["value"],
        "base_url": fields["base_url"]["value"],
        "valid": not errors,
        "errors": errors[:6],
    }
    _summary_cache.update({"ts": now, "data": summary})
    return summary


def _invalidate_config_cache() -> None:
    _summary_cache.update({"ts": 0.0, "data": None})


# ---------------- 业务处理 ----------------
def _onebot_status() -> dict:
    """探测 OneBot WebSocket 端口是否可达（bot 只有连上它才能收到消息）。"""
    values = resolved_values()
    url = str(values.get("napcat_ws_url") or "ws://127.0.0.1:3001")
    try:
        port = urlparse(url).port or 3001
    except ValueError:
        port = 3001
    reachable = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            reachable = True
    except OSError:
        reachable = False
    return {"reachable": reachable, "host": "127.0.0.1", "port": port, "url": url}


def _state() -> dict:
    return {
        "ok": True,
        "version": APP_VERSION,
        "bot": bot_manager.status(),
        "onebot": _onebot_status(),
        "server": {"host": "127.0.0.1", "port": _state_port[0]},
        "config": _config_summary(),
    }


_state_port = [0]


def _config_payload() -> dict:
    data = load_config()
    fields = {}
    for key, item in data["fields"].items():
        fields[key] = {"value": item["value"], "from": item.get("from", "")}
        if key == "api_key":
            fields[key] = {
                "value": "",
                "set": item.get("set", False),
                "masked": item.get("masked", ""),
                "from": item.get("from", ""),
            }
    return {
        "ok": True,
        "fields": fields,
        "defaults": {key: DEFAULTS[key] for key in DEFAULTS},
        "errors": data.get("errors", []),
    }


def _save_config(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象")
    try:
        result = save_config(payload)
    except ConfigError as exc:
        raise ApiError(str(exc), 400) from exc
    _invalidate_config_cache()
    warnings = list(result.get("warnings", []))
    if bot_manager.is_running():
        warnings.append("bot 正在运行，部分配置需要重启 bot 后生效")
    bot_manager.log("配置已保存", "INFO")
    return {"ok": True, "saved": True, "warnings": warnings}


def _probe_chat(
    base: str, headers: dict, model: str, timeout: float = 25.0
) -> tuple[bool, str, str | None]:
    """用一次最小 chat 请求验证模型是否真的可用。返回 (ok, served_model, error)。"""
    payload_body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload_body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        return False, "", f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, "", str(exc)
    served = ""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict):
            served = str(data.get("model") or "")
    except ValueError:
        pass
    return True, served, None


def _test_llm(payload: dict) -> dict:
    values = resolved_values()
    key = str(payload.get("api_key") or values.get("api_key") or "").strip()
    base = str(payload.get("base_url") or values.get("base_url") or "").strip().rstrip("/")
    model = str(payload.get("model") or values.get("model") or "").strip()
    if not key:
        raise ApiError("API Key 为空，无法测试")
    if not base.startswith(("http://", "https://")):
        raise ApiError("Base URL 必须以 http:// 或 https:// 开头")
    if not model:
        raise ApiError("模型名称为空")

    headers = {"Authorization": f"Bearer {key}"}
    start = time.time()

    # 1) 优先 /models：同时验证连通性与鉴权
    try:
        request = urllib.request.Request(f"{base}/models", headers=headers)
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        models = [
            str(item.get("id"))
            for item in data.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ][:30]
        latency = int((time.time() - start) * 1000)
        if not models or model in models:
            return {
                "ok": True,
                "latency_ms": latency,
                "models": models,
                "model_ok": True,
                "served_model": model,
                "warning": "",
            }

        # 模型不在列表里：部分服务商用别名/路由，实测一次避免误报
        probe_ok, served, probe_error = _probe_chat(base, headers, model)
        if probe_ok:
            return {
                "ok": True,
                "latency_ms": latency,
                "models": models,
                "model_ok": True,
                "served_model": served or model,
                "warning": (
                    f"模型 {model} 不在 /models 列表中，但实测可用"
                    f"（服务端返回 {served or model}）"
                ),
            }
        return {
            "ok": True,
            "latency_ms": latency,
            "models": models,
            "model_ok": False,
            "served_model": "",
            "warning": f"连接成功，但模型 {model} 实测不可用：{probe_error}",
        }
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        if exc.code not in (404, 405):
            raise ApiError(f"鉴权或接口异常（HTTP {exc.code}）：{body}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"连接失败：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise ApiError(f"连接超时或被拒绝：{exc}") from exc

    # 2) /models 不可用时，用一次最小 chat 请求验证
    probe_ok, served, probe_error = _probe_chat(base, headers, model)
    latency = int((time.time() - start) * 1000)
    if probe_ok:
        return {
            "ok": True,
            "latency_ms": latency,
            "models": [],
            "model_ok": True,
            "served_model": served or model,
            "warning": "",
        }
    raise ApiError(f"测试请求失败：{probe_error}")


def _env_checks(refresh: bool) -> dict:
    return {"ok": True, "checks": env_check.check_all(refresh=refresh)}


# ---------------- HTTP Handler ----------------
class ManagerHandler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"

    # ---- 基础输出 ----
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"ok": False, "error": "文件读取失败"}, 500)
            return
        content_type = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return data

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return True

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 静默访问日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._dispatch_get()
        except ApiError as exc:
            self._send_json({"ok": False, "error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if not self._origin_ok():
                raise ApiError("非法来源请求", 403)
            self._dispatch_post()
        except ApiError as exc:
            self._send_json({"ok": False, "error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, 500)

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/api/ping":
            self._send_json({"ok": True, "app": APP_NAME, "version": APP_VERSION})
            return
        if path == "/api/state":
            self._send_json(_state())
            return
        if path == "/api/config":
            self._send_json(_config_payload())
            return
        if path == "/api/logs":
            fallback = int(query.get("tail", ["400"])[0] or 400)
            level = (query.get("level", ["all"])[0] or "all").upper()
            lines = bot_manager.snapshot(tail=fallback)
            if level in ("INFO", "WARN", "ERROR", "DEBUG"):
                lines = [line for line in lines if line["level"] == level]
            self._send_json({"ok": True, "lines": lines})
            return
        if path == "/api/logs/stream":
            self._stream_logs()
            return
        if path == "/api/logs/download":
            lines = bot_manager.snapshot(tail=5000)
            text = "\n".join(
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(line['ts']))} "
                f"[{line['level']}] [{line['source']}] {line['text']}"
                for line in lines
            )
            self._send_text(text or "（暂无日志）", content_type="text/plain; charset=utf-8")
            return
        if path == "/api/env":
            refresh = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self._send_json(_env_checks(refresh))
            return
        if path == "/api/settings":
            self._send_json({"ok": True, "settings": load_settings(), "root": str(ROOT)})
            return
        if path == "/api/napcat/status":
            self._send_json({"ok": True, "running": env_check._napcat_process is not None})
            return

        # 静态资源
        self._serve_static(path)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel.startswith("api/"):
            raise ApiError("接口不存在", 404)
        candidate = (WEB_DIR / rel).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            raise ApiError("非法路径", 403) from None
        if candidate.is_file():
            self._send_file(candidate)
            return
        if "." not in Path(rel).name:
            # SPA 回退
            index = WEB_DIR / "index.html"
            if index.is_file():
                self._send_file(index)
                return
        raise ApiError("资源不存在", 404)

    def _dispatch_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        payload = self._read_json()

        if path == "/api/config":
            self._send_json(_save_config(payload))
            return
        if path == "/api/config/test":
            self._send_json(_test_llm(payload))
            return
        if path == "/api/bot/start":
            try:
                result = bot_manager.start()
            except RuntimeError as exc:
                raise ApiError(str(exc), 400) from exc
            self._send_json({"ok": True, **result})
            return
        if path == "/api/bot/stop":
            self._send_json({"ok": True, **bot_manager.stop()})
            return
        if path == "/api/bot/restart":
            try:
                result = bot_manager.restart()
            except RuntimeError as exc:
                raise ApiError(str(exc), 400) from exc
            self._send_json({"ok": True, **result})
            return
        if path == "/api/bot/auto-restart":
            bot_manager.set_auto_restart(bool(payload.get("enabled")))
            self._send_json({"ok": True, "enabled": bool(payload.get("enabled"))})
            return
        if path == "/api/logs/clear":
            bot_manager.clear()
            self._send_json({"ok": True})
            return
        if path == "/api/action/open":
            target = str(payload.get("target") or "")
            try:
                env_check.open_target(target)
            except (ValueError, OSError) as exc:
                raise ApiError(f"打开失败：{exc}") from exc
            self._send_json({"ok": True})
            return
        if path == "/api/settings":
            settings = load_settings()
            if "auto_restart" in payload:
                bot_manager.set_auto_restart(bool(payload["auto_restart"]))
            for key in ("napcat_command", "auto_open", "theme", "port"):
                if key in payload:
                    settings[key] = payload[key]
            save_settings(settings)
            self._send_json({"ok": True, "settings": load_settings()})
            return
        if path == "/api/napcat/start":
            try:
                self._send_json({"ok": True, **env_check.start_napcat()})
            except RuntimeError as exc:
                raise ApiError(str(exc), 400) from exc
            return
        if path == "/api/napcat/stop":
            self._send_json({"ok": True, **env_check.stop_napcat()})
            return
        if path == "/api/shutdown":
            stopped = bot_manager.stop()["stopped"]
            self._send_json({"ok": True, "stopped_bot": stopped})
            threading.Timer(0.5, self.server.shutdown).start()
            return

        raise ApiError("接口不存在", 404)

    # ---- SSE ----
    def _stream_logs(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        subscriber = bot_manager.subscribe()
        shutdown_event = getattr(self.server, "shutdown_event", None)
        try:
            snapshot = {"type": "snapshot", "lines": bot_manager.snapshot(300)}
            self._sse_write(snapshot)
            while True:
                if shutdown_event is not None and shutdown_event.is_set():
                    break
                try:
                    line = subscriber.get(timeout=15)
                    self._sse_write({"type": "append", "line": line})
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bot_manager.unsubscribe(subscriber)

    def _sse_write(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.shutdown_event = threading.Event()

    def shutdown(self) -> None:  # noqa: D401 - 兼容标准接口
        self.shutdown_event.set()
        super().shutdown()


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ManagerServer:
    server = ManagerServer((host, port), ManagerHandler)
    _state_port[0] = server.server_address[1]
    return server
