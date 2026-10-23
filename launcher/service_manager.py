"""服务器模式：管理台直接跑在云服务器上，用 systemd / docker 管理 bot 与 NapCat。

与 bot_manager.BotManager 保持同一套接口（start/stop/restart/status/snapshot/
subscribe/...），这样 server.py 与前端页面完全不用改逻辑，只换实现。

设计要点：
- bot 进程由 systemd（im-bridge.service）托管，因此这里只做「转发 + 观测」，
  不自己 Popen，避免出现两份进程抢同一个 QQ 号；
- 日志直接跟随 journalctl，所以重启/崩溃后的日志也拿得到；
- NapCat 是 docker 容器，启停用 docker start/stop。
"""
from __future__ import annotations

import json
import queue
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from config_store import ROOT, get_api_key, resolved_values

BOT_UNIT = "im-bridge"
NAPCAT_CONTAINER = "napcat"

MAX_TEXT = 4000
MAX_LINES = 800
CACHE_TTL = 20


# ---------------- 工具 ----------------
def _run(cmd: list[str], timeout: float = 12.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时：{' '.join(cmd)}"


def _level(text: str) -> str:
    low = text.lower()
    if "traceback" in low or "error" in low or "[err" in low or "失败" in text or "异常" in text:
        return "ERROR"
    if "warn" in low or "警告" in text or "重连" in text or "retry" in low:
        return "WARN"
    if "debug" in low or "[dbg" in low:
        return "DEBUG"
    return "INFO"


_TS_RE = None


def _parse_journal_line(raw: str) -> dict[str, Any] | None:
    """解析 `journalctl -o short-iso` 的一行：2026-10-23T00:12:33+0800 host unit[pid]: msg"""
    global _TS_RE
    if _TS_RE is None:
        import re

        _TS_RE = re.compile(
            r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+"
            r"\S+\s+(?P<unit>[^:\[]+)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$"
        )
    raw = raw.rstrip("\n")
    if not raw.strip():
        return None
    match = _TS_RE.match(raw)
    if not match:
        text = raw.strip()
        if text.startswith("-- "):  # journal 的分隔提示行
            return None
        return {"ts": time.time(), "level": _level(text), "text": text[:MAX_TEXT], "source": "bot"}
    stamp = match.group("ts").replace(",", ".")
    ts = time.time()
    try:
        from datetime import datetime

        ts = datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        pass
    text = match.group("msg").strip()
    if not text:
        return None
    return {"ts": ts, "level": _level(text), "text": text[:MAX_TEXT], "source": "bot"}


def _port_open(host: str, port: int, timeout: float = 1.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------- 管理器 ----------------
class SystemdBotManager:
    """把 systemd / docker 的能力包装成管理台认识的 bot_manager。"""

    def __init__(self, unit: str = BOT_UNIT) -> None:
        self.unit = unit
        self._lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._follower: subprocess.Popen | None = None
        self._follower_thread: threading.Thread | None = None
        self._ensure_follower()
        self.log(f"管理台以服务器模式启动（systemd 单元 {self.unit}）", "INFO")

    # ---- 日志 ----
    def _append(self, text: str, source: str = "bot", level: str | None = None, ts: float | None = None) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + " …(截断)"
        line = {
            "ts": ts or time.time(),
            "level": level or _level(text),
            "text": text,
            "source": source,
        }
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > MAX_LINES:
                del self._lines[: len(self._lines) - MAX_LINES]
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
        tail = max(1, min(tail, MAX_LINES))
        with self._lock:
            lines = list(self._lines)
            if len(lines) >= tail:
                return lines[-tail:]
            need = tail - len(lines)
        history = self._journal_history(need)
        merged = history + lines
        return merged[-tail:]

    def _journal_history(self, count: int) -> list[dict[str, Any]]:
        code, out, _ = _run(
            ["journalctl", "-u", self.unit, "-n", str(max(1, count)), "--no-pager", "-o", "short-iso"],
            timeout=15,
        )
        if code != 0 or not out:
            return []
        lines = []
        for raw in out.splitlines():
            parsed = _parse_journal_line(raw)
            if parsed:
                lines.append(parsed)
        return lines

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
        self.log("视图已清空（journald 原始日志仍在，可用 journalctl --vacuum-time 清理磁盘）", "INFO")

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=1000)
        with self._sub_lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._sub_lock:
            self._subscribers.discard(subscriber)

    def _ensure_follower(self) -> None:
        if self._follower_thread and self._follower_thread.is_alive():
            return
        self._follower_thread = threading.Thread(target=self._follow_loop, name="journal-follow", daemon=True)
        self._follower_thread.start()

    def _follow_loop(self) -> None:
        while True:
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-u", self.unit, "-f", "-n", "0", "--no-pager", "-o", "short-iso"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except (OSError, ValueError) as exc:
                self.log(f"日志跟随启动失败：{exc}", "ERROR")
                time.sleep(15)
                continue
            self._follower = proc
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    parsed = _parse_journal_line(raw)
                    if parsed:
                        self._append(parsed["text"], source="bot", level=parsed["level"], ts=parsed["ts"])
            except Exception as exc:  # noqa: BLE001
                self.log(f"日志流中断：{exc}", "WARN")
            finally:
                try:
                    proc.terminate()
                except OSError:
                    pass
            time.sleep(3)

    # ---- 状态 ----
    def _active_state(self) -> str:
        code, out, _ = _run(["systemctl", "is-active", self.unit], timeout=8)
        return out or ("unknown" if code != 0 else "unknown")

    def is_running(self) -> bool:
        return self._active_state() == "active"

    def _pid(self) -> int | None:
        _, out, _ = _run(["systemctl", "show", self.unit, "-p", "MainPID", "--value"], timeout=8)
        try:
            pid = int(out)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def _uptime(self, pid: int | None) -> float:
        if not pid:
            return 0.0
        code, out, _ = _run(["ps", "-o", "etimes=", "-p", str(pid)], timeout=6)
        if code != 0:
            return 0.0
        try:
            return max(0.0, float(out.strip()))
        except ValueError:
            return 0.0

    def status(self) -> dict[str, Any]:
        state = self._active_state()
        running = state == "active"
        pid = self._pid() if running else None
        uptime = self._uptime(pid)
        _, enabled, _ = _run(["systemctl", "is-enabled", self.unit], timeout=8)
        code, sub, _ = _run(["systemctl", "show", self.unit, "-p", "SubState", "--value"], timeout=8)
        return {
            "running": running,
            "pid": pid,
            "started_at": (time.time() - uptime) if uptime else None,
            "uptime": round(uptime, 1),
            "exit_code": None,
            "auto_restart": enabled.strip() in ("enabled", "enabled-runtime"),
            "python": "systemd: " + self.unit,
            "state": state,
            "sub_state": sub if code == 0 else "",
        }

    def set_auto_restart(self, enabled: bool) -> None:
        """对应 systemctl enable / disable（开机自启）。"""
        action = "enable" if enabled else "disable"
        code, _, err = _run(["systemctl", action, self.unit], timeout=20)
        if code != 0:
            raise RuntimeError(f"systemctl {action} 失败：{err}")
        self.log(f"开机自启已{'开启' if enabled else '关闭'}", "INFO")

    # ---- 生命周期 ----
    def start(self) -> dict[str, Any]:
        if self.is_running():
            return {"already": True, **self.status()}
        if not get_api_key():
            raise RuntimeError("API Key 未配置，请先在「配置」页填写 AMIS_API_KEY")
        code, _, err = _run(["systemctl", "start", self.unit], timeout=45)
        time.sleep(2.0)
        if not self.is_running():
            tail = "\n".join(item["text"] for item in self.snapshot(12))
            raise RuntimeError(f"启动失败：{err or '服务未能进入 active 状态'}\n{tail}")
        self.log("bot 已启动", "INFO")
        return self.status()

    def stop(self) -> dict[str, Any]:
        if not self.is_running():
            return {"stopped": False, "already": True, **self.status()}
        code, _, err = _run(["systemctl", "stop", self.unit], timeout=45)
        time.sleep(1.0)
        if self.is_running():
            raise RuntimeError(f"停止失败：{err or '服务仍在运行'}")
        self.log("bot 已停止", "INFO")
        return {"stopped": True, **self.status()}

    def restart(self) -> dict[str, Any]:
        code, _, err = _run(["systemctl", "restart", self.unit], timeout=60)
        time.sleep(2.0)
        if not self.is_running():
            tail = "\n".join(item["text"] for item in self.snapshot(12))
            raise RuntimeError(f"重启失败：{err or '服务未能进入 active 状态'}\n{tail}")
        self.log("bot 已重启", "INFO")
        return self.status()

    # ---- NapCat（docker 容器） ----
    def napcat_status(self) -> dict[str, Any]:
        if not shutil.which("docker"):
            return {"running": False, "available": False, "detail": "未安装 docker"}
        code, out, _ = _run(
            ["docker", "inspect", "-f", "{{.State.Running}}|{{.State.StartedAt}}", NAPCAT_CONTAINER], timeout=15
        )
        if code != 0:
            return {"running": False, "available": False, "detail": "容器不存在"}
        running = out.split("|", 1)[0].strip().lower() == "true"
        return {"running": running, "available": True, "detail": NAPCAT_CONTAINER}

    def napcat_start(self) -> dict[str, Any]:
        code, _, err = _run(["docker", "start", NAPCAT_CONTAINER], timeout=90)
        if code != 0:
            raise RuntimeError(f"启动 NapCat 容器失败：{err}")
        self.log("NapCat 容器已启动（扫码登录请走 SSH 隧道访问 6099）", "INFO")
        return self.napcat_status()

    def napcat_stop(self) -> dict[str, Any]:
        code, _, err = _run(["docker", "stop", NAPCAT_CONTAINER], timeout=90)
        if code != 0:
            raise RuntimeError(f"停止 NapCat 容器失败：{err}")
        self.log("NapCat 容器已停止", "WARN")
        return self.napcat_status()

    # ---- 环境自检（服务器版） ----
    def env_checks(self, refresh: bool = False) -> list[dict[str, Any]]:
        values = resolved_values()
        checks: list[dict[str, Any]] = []

        project_ok = (ROOT / "run.py").is_file() and (ROOT / "src" / "im_bridge").is_dir()
        checks.append({
            "id": "project",
            "title": "项目文件",
            "status": "ok" if project_ok else "fail",
            "detail": str(ROOT),
            "hint": "" if project_ok else "缺少 run.py 或 src/im_bridge",
        })

        import sys

        checks.append({
            "id": "python",
            "title": "Python 运行时",
            "status": "ok" if sys.version_info >= (3, 10) else "warn",
            "detail": f"{sys.version.split()[0]}（{sys.executable}）",
            "hint": "" if sys.version_info >= (3, 10) else "建议 3.10+",
        })

        missing = []
        for mod in ("httpx", "websockets"):
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)
        checks.append({
            "id": "deps",
            "title": "Python 依赖",
            "status": "fail" if missing else "ok",
            "detail": "httpx / websockets 已就绪" if not missing else f"缺少 {', '.join(missing)}",
            "hint": "" if not missing else "在服务器执行 .venv/bin/pip install -r requirements.txt",
        })

        key_ok = bool(get_api_key())
        checks.append({
            "id": "config",
            "title": "Bot 配置",
            "status": "ok" if key_ok else "fail",
            "detail": f"bot_qq={values.get('bot_qq')}  model={values.get('model')}  api_key={'已设置' if key_ok else '缺失'}",
            "hint": "" if key_ok else "在 .env 里填写 AMIS_API_KEY",
        })

        ws_url = str(values.get("napcat_ws_url") or "")
        port = 3001
        try:
            tail = ws_url.rsplit(":", 1)[-1].split("/")[0]
            port = int(tail)
        except ValueError:
            pass
        reachable = _port_open("127.0.0.1", port)
        checks.append({
            "id": "onebot_port",
            "title": "OneBot 连接",
            "status": "ok" if reachable else "warn",
            "detail": f"127.0.0.1:{port} {'可连通' if reachable else '不可连通'}",
            "hint": "" if reachable else "NapCat 未启动或 OneBot WS 未开启（bot 会一直重连）",
        })

        napcat = self.napcat_status()
        checks.append({
            "id": "napcat",
            "title": "NapCat 容器",
            "status": "ok" if napcat.get("running") else "warn",
            "detail": "docker 容器运行中" if napcat.get("running") else str(napcat.get("detail") or "未运行"),
            "hint": "" if napcat.get("running") else "在「NapCat」区点启动，或用 docker start napcat",
        })

        st = self.status()
        checks.append({
            "id": "service",
            "title": f"服务状态（{self.unit}）",
            "status": "ok" if st["running"] else "warn",
            "detail": f"{st.get('state')}  PID {st.get('pid') or '-'}  运行 {int(st.get('uptime') or 0)} 秒",
            "hint": "" if st["running"] else "点「启动」或用 systemctl start im-bridge",
        })

        mem = _mem_info()
        checks.append({
            "id": "memory",
            "title": "服务器资源",
            "status": "warn" if mem["percent"] >= 90 else "ok",
            "detail": f"内存 {mem['used']}/{mem['total']} MB（{mem['percent']}%） 磁盘剩余 {_disk_free()}",
            "hint": "" if mem["percent"] < 90 else "内存偏高，建议检查 swap 或升级配置",
        })

        return checks


def _mem_info() -> dict[str, Any]:
    try:
        data = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            data[key.strip()] = int(rest.strip().split()[0])  # kB
        total = data.get("MemTotal", 0) // 1024
        avail = data.get("MemAvailable", 0) // 1024
        used = max(0, total - avail)
        percent = round(used / total * 100) if total else 0
        return {"total": total, "used": used, "percent": percent}
    except (OSError, ValueError, IndexError):
        return {"total": 0, "used": 0, "percent": 0}


def _disk_free() -> str:
    try:
        usage = shutil.disk_usage("/")
        return f"{usage.free / 1024 ** 3:.1f}G / {usage.total / 1024 ** 3:.1f}G"
    except OSError:
        return "未知"


def _self_check() -> None:
    """命令行自检：python -m service_manager"""
    manager = SystemdBotManager()
    print("== 状态 ==")
    print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
    print("== NapCat ==")
    print(json.dumps(manager.napcat_status(), ensure_ascii=False))
    print("== 日志尾部 ==")
    for line in manager.snapshot(5):
        print(f"  [{line['level']}] {line['text']}")


if __name__ == "__main__":
    _self_check()
