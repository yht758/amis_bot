"""多实例进程编排：把 N 个 ``BotManager`` 组织成「一个管理台管一群 bot」。

每个实例对应一个独立子进程（``python run.py --instance <id>``），
进程、日志缓冲、SSE 订阅者全部隔离；一个实例崩溃不影响其他实例。
"""
from __future__ import annotations

import threading
from typing import Any

import instance_config as ic
from bot_manager import BotManager

DEFAULT_INSTANCE_ID = ic.DEFAULT_INSTANCE_ID


class InstanceManager:
    """实例到进程的映射，进程对象按需创建并常驻。"""

    def __init__(self) -> None:
        self._processes: dict[str, BotManager] = {}
        self._lock = threading.RLock()

    # ---------------- 内部 ----------------
    def _ensure(self, instance_id: str) -> BotManager:
        with self._lock:
            process = self._processes.get(instance_id)
            if process is None:
                process = BotManager(instance_id)
                self._processes[instance_id] = process
            return process

    def process(self, instance_id: str) -> BotManager:
        """取某实例的进程对象（不存在则创建，用于读取日志等只读操作）。"""
        return self._ensure(instance_id)

    def require_process(self, instance_id: str) -> BotManager:
        """取进程对象，同时校验实例在注册表里存在。"""
        ic.require_instance(instance_id)
        return self._ensure(instance_id)

    # ---------------- 查询 ----------------
    @staticmethod
    def _enrich(status: dict[str, Any]) -> dict[str, Any]:
        """把 BotManager 的 running/exit_code 归一成前端要的 state 字段。"""
        if status.get("running"):
            state = "running"
        elif status.get("exit_code") not in (None, 0):
            state = "error"
        else:
            state = "stopped"
        return {**status, "state": state}

    def status(self, instance_id: str) -> dict[str, Any]:
        instance = ic.require_instance(instance_id)
        process = self._ensure(instance_id)
        return {"instance": instance, "status": self._enrich(process.status())}

    def list_instances(self) -> list[dict[str, Any]]:
        """注册表 + 实时进程状态，供列表页使用。"""
        items: list[dict[str, Any]] = []
        for instance in ic.list_instances():
            process = self._ensure(instance["id"])
            try:
                status = self._enrich(process.status())
            except Exception as exc:  # noqa: BLE001 - 单个实例状态异常不影响列表
                status = {"state": "unknown", "error": str(exc), "pid": None}
            items.append({**instance, "status": status})
        return items

    # ---------------- 生命周期 ----------------
    def start(self, instance_id: str) -> dict[str, Any]:
        process = self.require_process(instance_id)
        return process.start()

    def stop(self, instance_id: str, timeout: float = 8.0) -> dict[str, Any]:
        process = self.require_process(instance_id)
        return process.stop(timeout=timeout)

    def restart(self, instance_id: str) -> dict[str, Any]:
        process = self.require_process(instance_id)
        return process.restart()

    def start_all(self) -> dict[str, Any]:
        """批量启动：跳过已运行与已禁用的实例，单个失败不影响其他。"""
        started: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []

        for instance in ic.list_instances():
            instance_id = instance["id"]
            if not instance.get("enabled", True):
                skipped.append(instance_id)
                continue
            process = self._ensure(instance_id)
            if process.is_running():
                skipped.append(instance_id)
                continue
            try:
                process.start()
                started.append(instance_id)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": instance_id, "error": str(exc)})

        return {"started": started, "skipped": skipped, "failed": failed, "affected": len(started)}

    def stop_all(self, timeout: float = 8.0) -> dict[str, Any]:
        stopped: list[str] = []
        failed: list[dict[str, str]] = []

        for instance in ic.list_instances():
            instance_id = instance["id"]
            process = self._ensure(instance_id)
            if not process.is_running():
                continue
            try:
                process.stop(timeout=timeout)
                stopped.append(instance_id)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": instance_id, "error": str(exc)})

        return {"stopped": stopped, "failed": failed, "affected": len(stopped)}

    def running_count(self) -> int:
        with self._lock:
            processes = list(self._processes.values())
        return sum(1 for process in processes if process.is_running())


# 全局单例：server.py 全程复用，保证进程句柄不丢
instance_manager = InstanceManager()
