"""Bot 子进程管理：启动 / 停止 / 重启 / 日志缓冲 / SSE 广播。"""
from __future__ import annotations

import collections
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from config_store import ROOT, find_python, get_api_key, load_settings

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_LINES = 2000
MAX_TEXT = 4000

_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b")


def _detect_level(line: str) -> str:
    match = _LEVEL_RE.search(line)
    if match:
        level = match.group(1)
        if level in ("WARNING", "WARN"):
            return "WARN"
        if level == "CRITICAL":
            return "ERROR"
        return level
    lowered = line.lower()
    if "traceback (most recent call last)" in lowered or "error:" in lowered:
        return "ERROR"
    if "warning:" in lowered:
        return "WARN"
    return "INFO"


class BotManager:
    """管理**单个实例**的子进程，并维护一份可订阅的日志缓冲。

    多实例场景下，每个实例持有一个独立实例：日志缓冲、订阅者、进程互不干扰。
    ``instance_id == "default"`` 时沿用根配置（不带 ``--instance``），保证老部署行为不变。
    """

    def __init__(self, instance_id: str = "default") -> None:
        self.instance_id = instance_id or "default"
        self._lock = threading.RLock()
        self._lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_LINES)
        self._subscribers: set[queue.Queue] = set()
        self._sub_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._exit_code: int | None = None
        self._stop_requested = False
        self._auto_restart = bool(load_settings().get("auto_restart", False))

    # ---------------- 日志 ----------------
    def _append(self, text: str, source: str = "bot", level: str | None = None) -> None:
        text = (text or "").rstrip()
        if not text:
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
        self.log("日志已清空", "INFO")

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=1000)
        with self._sub_lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._sub_lock:
            self._subscribers.discard(subscriber)

    # ---------------- 状态 ----------------
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            pid = proc.pid if proc else None
            started_at = self._started_at
            exit_code = self._exit_code
            auto_restart = self._auto_restart
        uptime = max(0.0, time.time() - started_at) if running and started_at else 0.0
        return {
            "running": running,
            "pid": pid,
            "started_at": started_at,
            "uptime": round(uptime, 1),
            "exit_code": exit_code,
            "auto_restart": auto_restart,
            "python": find_python(),
        }

    def set_auto_restart(self, enabled: bool) -> None:
        with self._lock:
            self._auto_restart = bool(enabled)
        settings = load_settings()
        settings["auto_restart"] = bool(enabled)
        from config_store import save_settings

        save_settings(settings)
        self.log(f"自动重启已{'开启' if enabled else '关闭'}", "INFO")

    # ---------------- 生命周期 ----------------
    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"already": True, **self.status()}

        if not get_api_key():
            raise RuntimeError("API Key 未配置，请先在「配置」页填写 AMIS_API_KEY")
        if not (ROOT / "run.py").exists():
            raise RuntimeError(f"未找到项目入口：{ROOT / 'run.py'}")

        python = find_python()
        if not python:
            raise RuntimeError(
                "未找到可用的 Python 运行时：请确认项目目录下存在 python\\python.exe，"
                "或安装 Python 3.10+ 并加入 PATH"
            )
        env = os.environ.copy()
        libs = ROOT / "libs"
        if libs.is_dir():
            env["PYTHONPATH"] = str(libs) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        with self._lock:
            self._stop_requested = False
            self._exit_code = None
            # default 实例沿用根配置；其余实例显式传 ID，由 main.py 解析实例路径
            command = [python, "run.py"]
            if self.instance_id != "default":
                command += ["--instance", self.instance_id]
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
                )
            except OSError as exc:
                raise RuntimeError(f"启动 bot 失败：{exc}") from exc
            self._proc = proc
            self._started_at = time.time()
            self._exit_code = None

        self.log(f"已启动 bot（PID {proc.pid}），解释器：{python}", "INFO")
        threading.Thread(target=self._reader, args=(proc,), daemon=True, name="bot-stdout").start()
        threading.Thread(target=self._waiter, args=(proc,), daemon=True, name="bot-waiter").start()
        return {"already": False, **self.status()}

    def _reader(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in stream:
                self._append(raw.rstrip("\r\n"), source="bot")
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _waiter(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self._lock:
            if self._proc is not proc:
                return
            self._exit_code = code
            self._proc = None
            self._started_at = None
            auto = self._auto_restart and not self._stop_requested
        level = "INFO" if code == 0 else "WARN"
        self.log(f"bot 已退出，退出码 {code}", level)
        if auto:
            self.log("自动重启已开启，3 秒后重新启动", "INFO")
            time.sleep(3)
            if not self._stop_requested:
                try:
                    self.start()
                except RuntimeError as exc:
                    self.log(f"自动重启失败：{exc}", "ERROR")

    def stop(self, timeout: float = 8.0) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            self._stop_requested = True
        if proc is None or proc.poll() is not None:
            with self._lock:
                self._proc = None
            return {"stopped": False, "reason": "not_running"}

        self.log(f"正在停止 bot（PID {proc.pid}）…", "WARN")
        try:
            proc.terminate()
        except OSError:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._proc is None:
                    break
            time.sleep(0.15)

        with self._lock:
            still_running = self._proc is not None
        if still_running and IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif still_running:
            try:
                proc.kill()
            except OSError:
                pass

        deadline = time.time() + 3
        while time.time() < deadline:
            with self._lock:
                if self._proc is None:
                    break
            time.sleep(0.1)

        with self._lock:
            self._proc = None
        self.log("bot 已停止", "INFO")
        return {"stopped": True}

    def restart(self) -> dict[str, Any]:
        self.stop()
        time.sleep(0.4)
        return self.start()


bot_manager = BotManager()
