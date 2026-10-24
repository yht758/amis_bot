"""本地管理台 HTTP 服务：静态页面 + JSON API + SSE 实时日志。

只监听 127.0.0.1，并做基本的 Origin 校验，避免被外部网页调用。
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import env_check
import instance_config
import napcat_update
from instance_manager import instance_manager
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

# ---------------- 运行模式 ----------------
# local  : 管理本机 python run.py 子进程（Windows 桌面默认）
# remote : 通过 ssh.exe 管理云服务器上的 systemd 服务 im-bridge（本机管理台连服务器）
# server : 管理台自身就跑在服务器上，直接用 systemctl / journalctl / docker 管理
def _detect_mode() -> str:
    raw = str(os.environ.get("IM_BRIDGE_MANAGER_MODE") or load_settings().get("mode") or "local").lower()
    return raw if raw in ("local", "remote", "server") else "local"


MODE = _detect_mode()
REMOTE_MODE = MODE == "remote"
SERVER_MODE = MODE == "server"
remote_mod = None
service_mod = None

if REMOTE_MODE:
    try:
        import remote as remote_mod
        from remote import RemoteBotManager
    except ImportError:
        raise SystemExit(
            "remote 模式需要 remote.py（作者自用的远程管理扩展，未包含在开源版）。\n"
            "如果你只想管理本机，请把 data/launcher_settings.json 里的 mode 改回 local。"
        )

    bot_manager = RemoteBotManager()
    try:
        # 把 config_store 的读写路径切到远程缓存，并拉一次服务器配置
        remote_mod.activate_config_cache()
    except Exception as exc:  # 拉取失败不应阻塞管理台启动
        print(f"[warn] 远程配置拉取失败：{exc}")
elif SERVER_MODE:
    try:
        import service_manager as service_mod
    except ImportError:
        raise SystemExit(
            "server 模式需要 service_manager.py（作者自用的云端管理扩展，未包含在开源版）。\n"
            "如果你只想管理本机，请把 data/launcher_settings.json 里的 mode 改回 local。"
        )

    bot_manager = service_mod.SystemdBotManager()
else:
    from bot_manager import bot_manager


# ---------------- 访问口令（服务器模式） ----------------
# 管理台暴露在公网时必须带口令：首次访问 /?token=xxx 种下 Cookie，之后免输。
AUTH_TOKEN = (os.environ.get("IM_BRIDGE_MANAGER_TOKEN") or "").strip()
COOKIE_NAME = "im_bridge_mgr"
AUTH_FILE = ROOT / "data" / "manager_token.txt"
PUBLIC_PATHS = {"/api/ping"}

# 访问日志：记录来源 IP 与请求结果，便于事后审计（token 一律脱敏）
ACCESS_LOG = ROOT / "logs" / "manager_access.log"
ACCESS_LOG_MAX_BYTES = 5 * 1024 * 1024
_TOKEN_PATTERN = re.compile(r"(token=)[^&\s\"]+")
# 前端高频轮询路径，写日志会迅速膨胀，默认不记录
_QUIET_PATHS = ("/api/state", "/api/logs/stream")


def _load_or_create_token() -> str:
    if AUTH_TOKEN:
        return AUTH_TOKEN
    try:
        if AUTH_FILE.is_file():
            token = AUTH_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
    except OSError:
        pass
    import secrets

    token = secrets.token_urlsafe(18)
    try:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(token + "\n", encoding="utf-8")
        try:
            AUTH_FILE.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass
    return token


if SERVER_MODE:
    AUTH_TOKEN = _load_or_create_token()
    print(f"[auth] 管理台访问口令：{AUTH_TOKEN}")


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
    if REMOTE_MODE:
        return remote_mod.onebot_status()
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


def _normalize_state(status: dict) -> dict:
    """统一成前端要的 state 字段（BotManager 与 SystemdBotManager 结构不同）。"""
    status = dict(status or {})
    if status.get("running"):
        state = "running"
    elif status.get("exit_code") not in (None, 0):
        state = "error"
    else:
        state = "stopped"
    status["state"] = state
    return status


_systemd_managers: dict[str, object] = {}
_systemd_lock = threading.Lock()


def _systemd_for(instance_id: str):
    """每个实例对应一个 systemd 单元。

    default 沿用 ``im-bridge.service``（无 --instance），其余实例走模板单元
    ``im-bridge@<id>.service``（ExecStart 带 --instance %i）。
    这样 bot 由 systemd 保活，管理台重启/更新不会带走任何实例。
    """
    with _systemd_lock:
        manager = _systemd_managers.get(instance_id)
        if manager is None:
            unit = "im-bridge" if instance_id == "default" else f"im-bridge@{instance_id}"
            manager = service_mod.SystemdBotManager(unit=unit)
            _systemd_managers[instance_id] = manager
        return manager


def _instance_backend(instance_id: str):
    """状态 / 日志 / 启停的提供者。

    server 模式下所有实例都由 systemd 托管，这里统一走 service_manager；
    本地桌面模式才落到 Python 子进程。
    """
    if SERVER_MODE:
        return _systemd_for(instance_id)
    return instance_manager.require_process(instance_id)


def _instance_status(instance_id: str) -> dict:
    return _normalize_state(_instance_backend(instance_id).status())


def _instance_lifecycle(instance_id: str, action: str) -> dict:
    return getattr(_instance_backend(instance_id), action)()


def _bulk_lifecycle(action: str) -> dict:
    """批量启停。云端每个实例都是独立 systemd 单元，逐个下发。"""
    if not SERVER_MODE:
        return getattr(instance_manager, action)()

    started: list[str] = []
    stopped: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for instance in instance_config.list_instances():
        instance_id = instance["id"]
        if action == "start_all" and not instance.get("enabled", True):
            skipped.append(instance_id)
            continue
        try:
            backend = _systemd_for(instance_id)
            if action == "start_all":
                if backend.is_running():
                    skipped.append(instance_id)
                    continue
                backend.start()
                started.append(instance_id)
            else:
                if not backend.is_running():
                    skipped.append(instance_id)
                    continue
                backend.stop()
                stopped.append(instance_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": instance_id, "error": str(exc)})

    return {
        "started": started,
        "stopped": stopped,
        "skipped": skipped,
        "failed": failed,
        "affected": len(started) + len(stopped),
    }


def _state(instance_id: str = "default") -> dict:
    if REMOTE_MODE:
        target = remote_mod.read_deploy_env()["host"]
    elif SERVER_MODE:
        target = f"systemd://{socket.gethostname()}"
    else:
        target = "127.0.0.1"
    try:
        bot_status = _instance_status(instance_id)
    except Exception as exc:  # noqa: BLE001 - 实例缺失时仍要返回管理台自身状态
        bot_status = {"state": "unknown", "error": str(exc)}
    return {
        "ok": True,
        "version": APP_VERSION,
        "mode": MODE,
        "target": target,
        "hostname": socket.gethostname(),
        "instance": instance_id,
        "bot": bot_status,
        "onebot": _onebot_status(),
        "server": {"host": LISTEN_HOST, "port": _state_port[0]},
        "config": _config_summary(),
    }


LISTEN_HOST = os.environ.get("IM_BRIDGE_MANAGER_HOST") or ("0.0.0.0" if SERVER_MODE else "127.0.0.1")
LISTEN_PORT = int(os.environ.get("IM_BRIDGE_MANAGER_PORT") or 8765)


_state_port = [0]


def _config_payload(instance_id: str = "default") -> dict:
    # default 实例直接用根 config.json；其他实例读自己的配置文件
    config_path = None
    if instance_id != "default":
        instance = instance_config.require_instance(instance_id)
        config_path = instance_config.config_path_of(instance)

    data = load_config(config_path=config_path)
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


def _create_instance(payload: dict) -> dict:
    """创建实例：复制根配置作为模板，密钥仍走共享 .env。"""
    try:
        instance = instance_config.create_instance(
            instance_id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            bot_qq=str(payload.get("bot_qq") or ""),
            persona=str(payload.get("persona") or ""),
            onebot_ws_url=str(payload.get("onebot_ws_url") or ""),
        )
    except instance_config.InstanceError as exc:
        raise ApiError(str(exc), 400) from exc
    return {"ok": True, "instance": instance}


def _save_instance_config(instance_id: str, payload: dict) -> dict:
    """保存某实例的配置；default 写根 config.json，其余写自身配置文件。"""
    instance = instance_config.require_instance(instance_id)
    config_path = None if instance_id == "default" else instance_config.config_path_of(instance)
    data = load_config(config_path=config_path)
    current = {key: item["value"] for key, item in data["fields"].items()}
    current.pop("api_key", None)
    result = save_config(payload, current_values=current, config_path=config_path)
    return {"ok": True, **result}


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

    if REMOTE_MODE:
        try:
            pushed = remote_mod.push_config()
            warnings.append(
                "已同步到服务器：" + ("、".join(pushed) if pushed else "无变更文件")
                + "。重启 bot 后生效。"
            )
            bot_manager.log("配置已推送到服务器", "INFO")
        except remote_mod.RemoteError as exc:
            warnings.append(f"⚠️ 推送服务器失败：{exc}（改动只存在本地缓存）")
            bot_manager.log(f"配置推送失败：{exc}", "ERROR")

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
    if REMOTE_MODE:
        return {"ok": True, "checks": remote_mod.env_checks(refresh=refresh)}
    if SERVER_MODE:
        return {"ok": True, "checks": bot_manager.env_checks(refresh=refresh)}
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
        if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            return True
        if SERVER_MODE:
            # 服务器模式：允许同源访问（用公网 IP 或域名打开页面）
            host_header = (self.headers.get("Host") or "").split(":")[0].strip()
            if host_header and parsed.hostname == host_header:
                return True
        return False

    # ---- 访问口令 ----
    def _client_token(self, query: dict) -> str:
        token = (query.get("token") or [""])[0].strip()
        if token:
            return token
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value.strip()
        return (self.headers.get("X-Auth-Token") or "").strip()

    def _authorized(self, query: dict) -> bool:
        if not AUTH_TOKEN:
            return True
        import secrets

        got = self._client_token(query)
        return bool(got) and secrets.compare_digest(got, AUTH_TOKEN)

    def _deny_auth(self) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>需要访问口令</title>"
            "<div style='font-family:system-ui;max-width:560px;margin:12vh auto;padding:0 24px;line-height:1.8'>"
            "<h2>需要访问口令</h2>"
            "<p>管理台开启了口令保护。请在地址后追加 <code>?token=你的口令</code> 访问一次，"
            "浏览器会记住登录状态。</p>"
            "<p style='color:#888;font-size:13px'>口令查看方式：在服务器执行 "
            "<code>cat /opt/im-bridge/data/manager_token.txt</code></p></div>"
        )
        data = body.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """记录访问日志：时间、来源 IP、请求行、状态码。

        两条硬性约束：
        1. URL 里的 ``token=`` 一律替换为 ``***``，避免日志泄露管理台口令；
        2. 前端轮询（/api/state、日志流）不记录，否则一天能写几十万行。
        """
        try:
            message = fmt % args if args else str(fmt)
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响请求处理
            message = str(fmt)

        if any(path in message for path in _QUIET_PATHS):
            return
        message = _TOKEN_PATTERN.sub(r"\1***", message)

        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{self.client_address[0]} {message}\n"
        )
        try:
            ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            if ACCESS_LOG.exists() and ACCESS_LOG.stat().st_size > ACCESS_LOG_MAX_BYTES:
                backup = ACCESS_LOG.with_suffix(".log.1")
                try:
                    if backup.exists():
                        backup.unlink()
                    ACCESS_LOG.rename(backup)
                except OSError:
                    pass
            with ACCESS_LOG.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

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

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            if not self._origin_ok():
                raise ApiError("非法来源请求", 403)
            self._dispatch_mutation("PATCH")
        except ApiError as exc:
            self._send_json({"ok": False, "error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if not self._origin_ok():
                raise ApiError("非法来源请求", 403)
            self._dispatch_mutation("DELETE")
        except ApiError as exc:
            self._send_json({"ok": False, "error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务端异常：{exc}"}, 500)

    def _dispatch_mutation(self, method: str) -> None:
        """PATCH / DELETE 只处理实例资源，其余路径返回 405。"""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._deny_auth()
            return

        match = re.fullmatch(r"/api/instances/([^/]+)", path)
        if not match:
            raise ApiError(f"{method} 不支持该路径：{path}", 405)
        instance_id = match.group(1)

        if method == "PATCH":
            payload = self._read_json()
            try:
                instance = instance_config.update_instance(instance_id, payload)
            except instance_config.InstanceError as exc:
                raise ApiError(str(exc), 400) from exc
            self._send_json({"ok": True, "instance": instance})
            return

        # DELETE：运行中的实例不允许删，避免子进程变孤儿
        process = instance_manager.process(instance_id)
        if process.is_running():
            raise ApiError("实例正在运行，请先停止再删除", 400)
        keep_backup = (query.get("keep_backup") or ["1"])[0] != "0"
        try:
            result = instance_config.delete_instance(instance_id, keep_backup=keep_backup)
        except instance_config.InstanceError as exc:
            raise ApiError(str(exc), 400) from exc
        self._send_json({"ok": True, **result})

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if not self._authorized(query) and path not in PUBLIC_PATHS:
            self._deny_auth()
            return
        if AUTH_TOKEN and (query.get("token") or [""])[0].strip() and not path.startswith("/api/"):
            # 口令正确：种 Cookie 后跳转到干净地址，避免口令留在地址栏/历史里
            self.send_response(302)
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={AUTH_TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
            self.send_header("Location", path or "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/ping":
            self._send_json({"ok": True, "app": APP_NAME, "version": APP_VERSION})
            return

        # ---- 多实例 ----
        if path == "/api/instances":
            items = instance_manager.list_instances()
            if SERVER_MODE:
                # 云端每个实例都是独立 systemd 单元，状态以 systemctl 为准
                for item in items:
                    try:
                        item["status"] = _instance_status(item["id"])
                    except Exception as exc:  # noqa: BLE001
                        item["status"] = {"state": "unknown", "error": str(exc)}
            self._send_json({"ok": True, "instances": items})
            return
        if path == "/api/instances/start-all":
            self._send_json({"ok": True, **_bulk_lifecycle("start_all")})
            return
        if path == "/api/instances/stop-all":
            self._send_json({"ok": True, **_bulk_lifecycle("stop_all")})
            return
        instance_match = re.fullmatch(r"/api/instances/([^/]+)(/[a-z-]+)?", path)
        if instance_match:
            instance_id = instance_match.group(1)
            suffix = instance_match.group(2) or ""
            try:
                if suffix == "":
                    payload = instance_manager.status(instance_id)
                    self._send_json({"ok": True, **payload})
                    return
                if suffix == "/config":
                    self._send_json(_config_payload(instance_id))
                    return
                if suffix == "/state":
                    self._send_json(_state(instance_id))
                    return
                if suffix == "/logs":
                    tail = int((query.get("tail") or ["300"])[0])
                    lines = _instance_backend(instance_id).snapshot(tail)
                    self._send_json({"ok": True, "lines": lines})
                    return
                if suffix == "/logs/stream":
                    self._stream_instance_logs(instance_id)
                    return
            except instance_config.InstanceError as exc:
                raise ApiError(str(exc), 404) from exc
            raise ApiError(f"未知的实例接口：{path}", 404)

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
            if REMOTE_MODE:
                self._send_json({"ok": True, **remote_mod.napcat_status()})
                return
            if SERVER_MODE:
                self._send_json({"ok": True, **bot_manager.napcat_status()})
                return
            self._send_json({"ok": True, "running": env_check._napcat_process is not None})
            return
        if path == "/api/napcat/update/status":
            if REMOTE_MODE or SERVER_MODE:
                self._send_json({
                    "ok": True, "unsupported": True, "state": "unsupported", "message": "",
                    "hint": "当前模式下不支持在线更新 NapCat，请在服务器执行 docker pull",
                })
                return
            refresh = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self._send_json(napcat_update.get_status(refresh=refresh))
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
        if not self._authorized(parse_qs(parsed.query)):
            self._deny_auth()
            return
        payload = self._read_json()

        # ---- 多实例 ----
        if path == "/api/instances":
            self._send_json(_create_instance(payload))
            return
        if path == "/api/instances/start-all":
            self._send_json({"ok": True, **_bulk_lifecycle("start_all")})
            return
        if path == "/api/instances/stop-all":
            self._send_json({"ok": True, **_bulk_lifecycle("stop_all")})
            return
        instance_match = re.fullmatch(r"/api/instances/([^/]+)(/[a-z-]+)?", path)
        if instance_match:
            instance_id = instance_match.group(1)
            suffix = instance_match.group(2) or ""
            try:
                if suffix == "/start":
                    self._send_json({"ok": True, **_instance_lifecycle(instance_id, "start")})
                    return
                if suffix == "/stop":
                    self._send_json({"ok": True, **_instance_lifecycle(instance_id, "stop")})
                    return
                if suffix == "/restart":
                    self._send_json({"ok": True, **_instance_lifecycle(instance_id, "restart")})
                    return
                if suffix == "/config":
                    self._send_json(_save_instance_config(instance_id, payload))
                    return
            except instance_config.InstanceError as exc:
                raise ApiError(str(exc), 404) from exc
            except RuntimeError as exc:
                raise ApiError(str(exc), 400) from exc
            raise ApiError(f"未知的实例接口：{path}", 404)

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
            if SERVER_MODE:
                raise ApiError("服务器模式不支持「打开本地文件」，请用 SSH / 宝塔文件管理")
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
            for key in ("mode", "napcat_command", "auto_open", "theme", "port", "napcat_update_mirror"):
                if key in payload:
                    settings[key] = payload[key]
            save_settings(settings)
            mode_changed = MODE != str(settings.get("mode") or "local").lower()
            self._send_json({"ok": True, "settings": load_settings(), "restart_required": mode_changed})
            return
        if path == "/api/napcat/start":
            if SERVER_MODE:
                try:
                    self._send_json({"ok": True, **bot_manager.napcat_start()})
                except RuntimeError as exc:
                    raise ApiError(str(exc), 400) from exc
                return
            if REMOTE_MODE:
                try:
                    self._send_json({"ok": True, **remote_mod.napcat_action("start")})
                except remote_mod.RemoteError as exc:
                    raise ApiError(str(exc), 400) from exc
                return
            try:
                self._send_json({"ok": True, **env_check.start_napcat()})
            except RuntimeError as exc:
                raise ApiError(str(exc), 400) from exc
            return
        if path == "/api/napcat/stop":
            if SERVER_MODE:
                try:
                    self._send_json({"ok": True, **bot_manager.napcat_stop()})
                except RuntimeError as exc:
                    raise ApiError(str(exc), 400) from exc
                return
            if REMOTE_MODE:
                try:
                    self._send_json({"ok": True, **remote_mod.napcat_action("stop")})
                except remote_mod.RemoteError as exc:
                    raise ApiError(str(exc), 400) from exc
                return
            self._send_json({"ok": True, **env_check.stop_napcat()})
            return
        if path == "/api/napcat/update":
            if REMOTE_MODE or SERVER_MODE:
                raise ApiError("当前模式下不支持在线更新 NapCat，请在服务器执行：docker pull mlikiowa/napcat-docker:latest")
            mirror = payload.get("mirror")
            result = napcat_update.start_update(mirror if mirror is not None else None)
            if not result.get("ok"):
                raise ApiError(result.get("error") or "更新启动失败")
            self._send_json(result)
            return
        if path == "/api/napcat/update/cancel":
            if REMOTE_MODE or SERVER_MODE:
                raise ApiError("当前模式不支持该操作")
            self._send_json(napcat_update.cancel_update())
            return
        if path == "/api/shutdown":
            if SERVER_MODE:
                raise ApiError("服务器模式请用 systemctl stop im-bridge-manager 关闭管理台")
            # 远程模式下关闭管理台不应该停掉服务器上的 bot
            stopped = False if REMOTE_MODE else bot_manager.stop()["stopped"]
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

    def _stream_instance_logs(self, instance_id: str) -> None:
        """按实例推送日志。云端走 journal（systemd 单元），本地走进程管道。"""
        process = _instance_backend(instance_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        subscriber = process.subscribe()
        shutdown_event = getattr(self.server, "shutdown_event", None)
        try:
            self._sse_write({"type": "snapshot", "lines": process.snapshot(300)})
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
            process.unsubscribe(subscriber)

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


def create_server(host: str | None = None, port: int | None = None) -> ManagerServer:
    host = host or LISTEN_HOST
    port = int(port or LISTEN_PORT)
    server = ManagerServer((host, port), ManagerHandler)
    _state_port[0] = server.server_address[1]
    return server
