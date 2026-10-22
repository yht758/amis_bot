"""
用户状态管理 —— 互动记录、连续互动天数、消息计数。

注意：这里只做“轻量统计”，不保存对话内容；对话内容由
``context.py`` 的 ContextManager 负责，两者职责分离。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 北京时间
CST = timezone(timedelta(hours=8))


class UserState:
    """单个用户的状态数据。"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.consecutive_days = 0     # 连续互动天数
        self.last_interact_ts = 0.0   # 上次互动时间戳
        self.last_interact_date = ""  # 上次互动日期 (YYYY-MM-DD)
        self.total_messages = 0       # 总消息数
        self.nickname = ""            # 用户昵称
        self.memory_flags: list[str] = []  # 预留的长期记忆标记

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "consecutive_days": self.consecutive_days,
            "last_interact_ts": self.last_interact_ts,
            "last_interact_date": self.last_interact_date,
            "total_messages": self.total_messages,
            "nickname": self.nickname,
            "memory_flags": self.memory_flags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserState":
        user = cls(str(data.get("user_id", "")))
        user.consecutive_days = int(data.get("consecutive_days", 0) or 0)
        user.last_interact_ts = float(data.get("last_interact_ts", 0.0) or 0.0)
        user.last_interact_date = str(data.get("last_interact_date", "") or "")
        user.total_messages = int(data.get("total_messages", 0) or 0)
        user.nickname = str(data.get("nickname", "") or "")
        flags = data.get("memory_flags", [])
        user.memory_flags = [str(item) for item in flags] if isinstance(flags, list) else []
        return user


class StateManager:
    """全局状态管理器：按用户持久化轻量状态。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, UserState] = {}
        self._load_all()

    def _state_path(self, user_id: str) -> Path:
        return self.data_dir / f"user_{user_id}.json"

    def _load_all(self) -> None:
        for file in self.data_dir.glob("user_*.json"):
            try:
                data = json.loads(file.read_text("utf-8"))
                user_id = str(data.get("user_id") or file.stem.replace("user_", ""))
                if user_id:
                    self._states[user_id] = UserState.from_dict(data)
            except (OSError, ValueError, KeyError):
                continue

    def user_count(self) -> int:
        return len(self._states)

    def _save(self, user_id: str) -> None:
        if user_id not in self._states:
            return
        try:
            self._state_path(user_id).write_text(
                json.dumps(self._states[user_id].to_dict(), ensure_ascii=False, indent=2),
                "utf-8",
            )
        except OSError:
            pass

    def get(self, user_id: str) -> UserState:
        """获取用户状态，不存在则创建。"""
        if user_id not in self._states:
            self._states[user_id] = UserState(user_id)
            self._save(user_id)
        return self._states[user_id]

    def record_interaction(self, user_id: str, nickname: str = "") -> None:
        """记录一次互动，更新连续天数与计数。"""
        state = self.get(user_id)
        now = time.time()
        today = datetime.now(CST).strftime("%Y-%m-%d")

        if nickname:
            state.nickname = nickname
        state.total_messages += 1

        if state.last_interact_date == today:
            pass  # 同一天，不更新
        elif state.last_interact_date:
            try:
                last_date = datetime.strptime(state.last_interact_date, "%Y-%m-%d")
                today_date = datetime.strptime(today, "%Y-%m-%d")
                diff = (today_date - last_date).days
                if diff == 1:
                    state.consecutive_days += 1
                elif diff > 1:
                    state.consecutive_days = 1
            except ValueError:
                state.consecutive_days = 1
        else:
            state.consecutive_days = 1

        state.last_interact_date = today
        state.last_interact_ts = now
        self._save(user_id)
