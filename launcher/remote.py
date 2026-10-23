"""远程模式：通过系统 OpenSSH（ssh.exe）+ 密钥认证管理云服务器上的 im-bridge。

设计取舍
--------
不使用 paramiko，而是调用 Windows 自带的 ssh.exe。原因：
  1. PyInstaller 打包 exe 不需要新增任何第三方依赖；
  2. Windows 10/11 默认已装 OpenSSH 客户端；
  3. 密钥认证后与本地 subprocess 用法几乎一致，日志流可直接读 stdout。

所有密码都不落盘：认证靠 ~/.ssh 之外的项目内私钥 `.tools/deploy/id_im_bridge`
（该目录已被 .gitignore 忽略）。
"""
from __future__ import annotations

import base64
import collections
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import config_store
from bot_manager import _detect_level
from config_store import ROOT

SERVICE = "im-bridge"
CONTAINER = "napcat"
MAX_LINES = 2000
MAX_TEXT = 4000
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ENV_FILE = ROOT / ".tools" / "deploy" / "deploy.env"
KEY_FILE = ROOT / ".tools" / "deploy" / "id_im_bridge"
CACHE_DIR = ROOT / ".tools" / "deploy" / "remote-cache"

CONFIG_FILES = (".env", "config.json", "config.secret.json")


class RemoteError(RuntimeError):
    """远程操作失败。"""


# ---------------------------------------------------------------- 连接参数
def read_deploy_env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    settings = config_store.load_settings()
    return {
        "host": cfg.get("SRV_HOST") or str(settings.get("remote_host") or ""),
        "port": cfg.get("SRV_PORT") or "22",
        "user": cfg.get("SRV_USER") or "root",
        "dir": cfg.get("REMOTE_DIR") or "/opt/im-bridge",
    }


def ssh_exe() -> str:
    found = shutil.which("ssh")
    if found:
        return found
    for candidate in (
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        r"C:\Program Files\OpenSSH\ssh.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    raise RemoteError("未找到 ssh.exe，请确认 Windows 已安装 OpenSSH 客户端")


def _base_cmd() -> list[str]:
    cfg = read_deploy_env()
    if not cfg["host"]:
        raise RemoteError("未配置服务器地址：请在 .tools/deploy/deploy.env 填写 SRV_HOST")
    if not KEY_FILE.is_file():
        raise RemoteError(f"未找到 SSH 私钥：{KEY_FILE}")
    return [
        ssh_exe(),
        "-i", str(KEY_FILE),
        "-p", str(cfg["port"]),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        f"{cfg['user']}@{cfg['host']}",
    ]


def run(cmd: str, timeout: float = 30, stdin_text: str | None = None) -> tuple[int, str]:
    """在服务器上执行命令，返回 (退出码, 输出)。"""
    try:
        proc = subprocess.run(
            _base_cmd() + [cmd],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"远程命令超时（{timeout}s）：{cmd}") from exc
    except OSError as exc:
        raise RemoteError(f"无法执行 ssh：{exc}") from exc
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def run_checked(cmd: str, timeout: float = 30) -> str:
    code, out = run(cmd, timeout=timeout)
    if code != 0 and "Warning: Permanently added" not in out:
        raise RemoteError(out or f"远程命令失败（退出码 {code}）")
    return out


def test_connection() -> dict[str, Any]:
    """连通性自检，供 UI 展示。"""
    info = read_deploy_env()
    try:
        code, out = run("echo ok; hostname", timeout=15)
        ok = code == 0 and "ok" in out
        return {
            "ok": ok,
            "host": info["host"],
            "dir": info["dir"],
            "detail": out.splitlines()[-1] if ok and out else out,
            "error": "" if ok else out,
        }
    except RemoteError as exc:
        return {"ok": False, "host": info["host"], "dir": info["dir"], "detail": "", "error": str(exc)}


# ---------------------------------------------------------------- 配置同步
def pull_config() -> list[str]:
    """把服务器上的 .env / config.json 拉到本地缓存，返回成功拉取的文件名。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    remote_dir = read_deploy_env()["dir"]
    pulled: list[str] = []
    for name in CONFIG_FILES:
        code, out = run(f"base64 -w0 {remote_dir}/{name} 2>/dev/null", timeout=20)
        target = CACHE_DIR / name
        if code == 0 and out:
            try:
                target.write_bytes(base64.b64decode(out))
                pulled.append(name)
                continue
            except (ValueError, OSError):
                pass
        if target.exists():
            target.unlink()
    return pulled


def push_config() -> list[str]:
    """把本地缓存里的配置文件推回服务器，返回推送的文件名。"""
    remote_dir = read_deploy_env()["dir"]
    pushed: list[str] = []
    for name in CONFIG_FILES:
        local = CACHE_DIR / name
        if not local.is_file():
            continue
        data = base64.b64encode(local.read_bytes()).decode("ascii")
        code, out = run(f"base64 -d > {remote_dir}/{name} && chmod 600 {remote_dir}/{name}", timeout=25, stdin_text=data)
        if code != 0:
            raise RemoteError(f"推送 {name} 失败：{out}")
        pushed.append(name)
    return pushed


def activate_config_cache() -> list[str]:
    """把 config_store 的文件路径切到远程缓存，并拉取一次。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    config_store.ENV_PATH = CACHE_DIR / ".env"
    config_store.CONFIG_PATH = CACHE_DIR / "config.json"
    config_store.SECRET_PATH = CACHE_DIR / "config.secret.json"
    return pull_config()


# ---------------------------------------------------------------- Bot 管理
class RemoteBotManager:
    """与本地 BotManager 暴露完全相同的接口，但操作的是服务器的 systemd 服务。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_LINES)
        self._subscribers: set[queue.Queue] = set()
        self._sub_lock = threading.Lock()
        self._tail_proc: subprocess.Popen | None = None
        self._tail_stop = threading.Event()
        self._auto_restart = bool(config_store.load_settings().get("auto_restart", False))
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        threading.Thread(target=self._tail_loop, daemon=True, name="remote-log-tail").start()

    # ---- 日志 ----
    def _append(self, text: str, source: str = "bot", level: str | None = None) -> None:
        text = (text or "").rstrip()
        if not text or text.startswith("Warning: Permanently added"):
            return
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + " …(截断)"
        line = {
            "ts": time.time(),
            "level": level or _detect_level(text),
            "text": text,
            "source": source,
        }
        with self._lock:
            self._lines.append(line)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(line)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(line)
                except (queue.Empty, queue.Full):
                    pass

    def log(self, text: str, level: str = "INFO") -> None:
        self._append(text, source="manager", level=level)

    def snapshot(self, tail: int = 400) -> list[dict[str, Any]]:
        with self._lock:
            lines = list(self._lines)
        return lines[-max(1, tail):]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
        self.log("日志已清空（仅清空管理台缓冲区，服务器 journal 不受影响）", "INFO")

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=1000)
        with self._sub_lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._sub_lock:
            self._subscribers.discard(subscriber)

    def _tail_loop(self) -> None:
        """长驻 ssh + journalctl -f，把服务器日志实时灌进缓冲区。"""
        first = True
        while not self._tail_stop.is_set():
            try:
                proc = subprocess.Popen(
                    _base_cmd() + [f"journalctl -u {SERVICE} -f -n 0 --no-pager -o short-iso"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, RemoteError) as exc:
                self.log(f"日志流启动失败：{exc}", "ERROR")
                time.sleep(15)
                continue

            self._tail_proc = proc
            if first:
                self.log("已连接服务器日志流（journalctl -f）", "INFO")
                self._load_recent(200)
                first = False
            try:
                for raw in proc.stdout or []:
                    if self._tail_stop.is_set():
                        break
                    self._append(raw.rstrip("\r\n"), source="bot")
            except (OSError, ValueError):
                pass
            finally:
                self._tail_proc = None
                try:
                    proc.terminate()
                except OSError:
                    pass
            if not self._tail_stop.is_set():
                self.log("日志流断开，5 秒后重连", "WARN")
                time.sleep(5)

    def _load_recent(self, lines: int) -> None:
        try:
            code, out = run(f"journalctl -u {SERVICE} -n {lines} --no-pager -o short-iso", timeout=25)
        except RemoteError:
            return
        if code != 0:
            return
        for row in out.splitlines():
            self._append(row, source="bot")

    def stop_tail(self) -> None:
        self._tail_stop.set()
        proc = self._tail_proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    # ---- 状态 ----
    def is_running(self) -> bool:
        return bool(self.status().get("running"))

    def status(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and self._status_cache and now - self._status_cache[0] < 2.0:
            return dict(self._status_cache[1])

        cmd = (
            f"state=$(systemctl is-active {SERVICE} 2>/dev/null); "
            f"enabled=$(systemctl is-enabled {SERVICE} 2>/dev/null); "
            f"pid=$(systemctl show -p MainPID --value {SERVICE} 2>/dev/null); "
            f"up=$(ps -o etimes= -p $pid 2>/dev/null | tr -d ' '); "
            f'echo "$state|$enabled|$pid|$up"'
        )
        try:
            code, out = run(cmd, timeout=15)
        except RemoteError as exc:
            return {
                "running": False, "pid": None, "started_at": None, "uptime": 0.0,
                "exit_code": None, "auto_restart": self._auto_restart,
                "python": "", "mode": "remote", "error": str(exc), "service": "unknown",
            }

        parts = (out.splitlines()[-1] if out else "|").split("|")
        while len(parts) < 4:
            parts.append("")
        state, enabled, pid, uptime = parts[0], parts[1], parts[2], parts[3]
        running = state.strip() == "active"
        result = {
            "running": running,
            "pid": int(pid) if pid.strip().isdigit() else None,
            "started_at": (time.time() - int(uptime)) if (running and uptime.strip().isdigit()) else None,
            "uptime": float(uptime) if uptime.strip().isdigit() else 0.0,
            "exit_code": None,
            "auto_restart": self._auto_restart,
            "python": "/opt/im-bridge/.venv/bin/python",
            "mode": "remote",
            "service": state.strip() or "unknown",
            "enabled": enabled.strip() == "enabled",
        }
        self._status_cache = (now, dict(result))
        return result

    def set_auto_restart(self, enabled: bool) -> None:
        with self._lock:
            self._auto_restart = bool(enabled)
        settings = config_store.load_settings()
        settings["auto_restart"] = bool(enabled)
        config_store.save_settings(settings)
        self.log(
            f"自动重启已{'开启' if enabled else '关闭'}"
            "（远程模式由 systemd Restart=always 兜底，此项仅作记录）",
            "INFO",
        )

    # ---- 生命周期 ----
    def _systemctl(self, action: str, timeout: float = 60) -> None:
        code, out = run(f"systemctl {action} {SERVICE} 2>&1", timeout=timeout)
        if code != 0:
            _, tail = run(f"journalctl -u {SERVICE} -n 20 --no-pager -o short-iso", timeout=20)
            raise RemoteError(f"{action} 失败：{out}\n{tail[-800:]}")

    def start(self) -> dict[str, Any]:
        if self.is_running():
            return {"already": True, **self.status(force=True)}
        self.log("正在启动服务器上的 bot（systemctl start）…", "INFO")
        self._systemctl("start")
        time.sleep(1.2)
        st = self.status(force=True)
        if not st["running"]:
            raise RemoteError("启动后服务未进入 active 状态，请查看日志")
        self.log("bot 已启动", "INFO")
        return {"already": False, **st}

    def stop(self) -> dict[str, Any]:
        if not self.is_running():
            return {"stopped": False, "reason": "not_running"}
        self.log("正在停止服务器上的 bot（systemctl stop）…", "WARN")
        self._systemctl("stop")
        time.sleep(0.6)
        self.log("bot 已停止", "INFO")
        return {"stopped": True}

    def restart(self) -> dict[str, Any]:
        self.log("正在重启服务器上的 bot（systemctl restart）…", "INFO")
        self._systemctl("restart")
        time.sleep(1.2)
        st = self.status(force=True)
        if not st["running"]:
            raise RemoteError("重启后服务未进入 active 状态，请查看日志")
        self.log("bot 已重启", "INFO")
        return {"already": False, **st}


# ---------------------------------------------------------------- NapCat
def napcat_status() -> dict[str, Any]:
    try:
        code, out = run(
            f"docker inspect -f '{{{{.State.Status}}}}|{{{{.State.StartedAt}}}}' {CONTAINER} 2>/dev/null",
            timeout=15,
        )
    except RemoteError as exc:
        return {"ok": False, "running": False, "error": str(exc)}
    if code != 0 or "|" not in out:
        return {"ok": True, "running": False, "detail": "容器不存在或未运行"}
    state, started = out.splitlines()[-1].split("|", 1)
    return {"ok": True, "running": state.strip() == "running", "state": state.strip(), "started_at": started.strip()}


def napcat_action(action: str) -> dict[str, Any]:
    if action not in ("start", "stop", "restart"):
        raise RemoteError(f"不支持的操作：{action}")
    code, out = run(f"docker {action} {CONTAINER} 2>&1", timeout=90)
    if code != 0:
        raise RemoteError(f"NapCat {action} 失败：{out}")
    time.sleep(1.0)
    return {"ok": True, **napcat_status()}


# ---------------------------------------------------------------- 探活与自检
def onebot_status() -> dict[str, Any]:
    values = config_store.resolved_values()
    url = str(values.get("napcat_ws_url") or "ws://127.0.0.1:3001")
    port = 3001
    try:
        from urllib.parse import urlparse
        port = urlparse(url).port or 3001
    except ValueError:
        pass
    try:
        code, out = run(f"ss -lnt 2>/dev/null | grep -c ':{port} '", timeout=12)
        reachable = code == 0 and out.strip().isdigit() and int(out.strip()) > 0
    except RemoteError:
        reachable = False
    return {"reachable": reachable, "host": read_deploy_env()["host"], "port": port, "url": url}


def env_checks(refresh: bool = False) -> list[dict[str, Any]]:
    remote_dir = read_deploy_env()["dir"]

    def probe(cmd: str, timeout: float = 15) -> tuple[bool, str]:
        try:
            code, out = run(cmd, timeout=timeout)
            return code == 0, out
        except RemoteError as exc:
            return False, str(exc)

    conn = test_connection()
    checks: list[dict[str, Any]] = [
        {
            "id": "ssh",
            "title": "SSH 连接",
            "status": "ok" if conn["ok"] else "fail",
            "detail": f"{conn['host']}" + (f"（{conn['detail']}）" if conn.get("detail") else ""),
            "hint": "" if conn["ok"] else conn.get("error", ""),
        }
    ]
    if not conn["ok"]:
        checks.append({
            "id": "project", "title": "项目文件", "status": "skip",
            "detail": "SSH 不通，跳过后续检查", "hint": "",
        })
        return checks

    ok, out = probe(f"test -f {remote_dir}/run.py && echo yes")
    checks.append({
        "id": "project", "title": "项目文件",
        "status": "ok" if "yes" in out else "fail",
        "detail": remote_dir,
        "hint": "" if "yes" in out else "服务器上缺少 run.py，请先部署",
    })

    ok, out = probe(f"{remote_dir}/.venv/bin/python --version 2>&1")
    checks.append({
        "id": "python", "title": "Python 运行时",
        "status": "ok" if ok and "Python" in out else "fail",
        "detail": out.splitlines()[0] if out else "未找到 .venv",
        "hint": "" if ok else "请在服务器重建 venv",
    })

    ok, out = probe(f"{remote_dir}/.venv/bin/pip list 2>/dev/null | grep -ciE 'httpx|websockets'")
    cnt = int(out) if out.strip().isdigit() else 0
    checks.append({
        "id": "deps", "title": "Python 依赖",
        "status": "ok" if cnt >= 2 else "fail",
        "detail": f"找到 {cnt} / 2 个依赖（httpx、websockets）",
        "hint": "" if cnt >= 2 else "运行 deploy.py 重新安装依赖",
    })

    try:
        code, out = run(f"systemctl is-active {SERVICE}; systemctl is-enabled {SERVICE}", timeout=12)
        lines = out.splitlines()
        active = lines[0].strip() if lines else ""
        enabled = lines[1].strip() if len(lines) > 1 else ""
        checks.append({
            "id": "service", "title": f"服务 {SERVICE}",
            "status": "ok" if active == "active" else "fail",
            "detail": f"{active} / 开机自启 {enabled}",
            "hint": "" if active == "active" else "在总览页点「启动」",
        })
    except RemoteError as exc:
        checks.append({"id": "service", "title": f"服务 {SERVICE}", "status": "fail", "detail": str(exc), "hint": ""})

    ob = onebot_status()
    checks.append({
        "id": "onebot_port", "title": "OneBot 连接",
        "status": "ok" if ob["reachable"] else "fail",
        "detail": f"{ob['url']} {'可连通' if ob['reachable'] else '不可达'}",
        "hint": "" if ob["reachable"] else "确认 NapCat 容器在运行且已配置 3001 端口",
    })

    nc = napcat_status()
    checks.append({
        "id": "napcat", "title": "NapCat",
        "status": "ok" if nc.get("running") else "fail",
        "detail": f"容器状态：{nc.get('state', '未知')}",
        "hint": "" if nc.get("running") else "在 NapCat 页点「启动容器」",
    })

    checks.append({
        "id": "edge", "title": "部署工具",
        "status": "ok" if (ROOT / ".tools" / "deploy" / "deploy.py").is_file() else "warn",
        "detail": ".tools/deploy/deploy.py 可用" if (ROOT / ".tools" / "deploy" / "deploy.py").is_file() else "未找到部署脚本",
        "hint": "",
    })
    return checks
