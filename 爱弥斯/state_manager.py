"""
状态管理模块 —— 互动记录、人物状态、时间流逝
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 北京时间
CST = timezone(timedelta(hours=8))


class UserState:
    """单个用户的状态数据"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.consecutive_days = 0    # 连续互动天数
        self.last_interact_ts = 0.0  # 上次互动时间戳
        self.last_interact_date = "" # 上次互动日期 (YYYY-MM-DD)
        self.total_messages = 0      # 总消息数
        self.nickname = ""           # 用户昵称
        self.memory_flags: list[str] = []  # 已触发过的记忆标记

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
        u = cls(data["user_id"])
        u.consecutive_days = data.get("consecutive_days", 0)
        u.last_interact_ts = data.get("last_interact_ts", 0.0)
        u.last_interact_date = data.get("last_interact_date", "")
        u.total_messages = data.get("total_messages", 0)
        u.nickname = data.get("nickname", "")
        u.memory_flags = data.get("memory_flags", [])
        return u


class StateManager:
    """全局状态管理器"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(exist_ok=True)
        self._states: dict[str, UserState] = {}
        self._load_all()

    def _state_path(self, user_id: str) -> Path:
        return self.data_dir / f"user_{user_id}.json"

    def _load_all(self):
        """加载所有用户状态"""
        for f in self.data_dir.glob("user_*.json"):
            try:
                data = json.loads(f.read_text("utf-8"))
                uid = data.get("user_id", f.stem.replace("user_", ""))
                self._states[uid] = UserState.from_dict(data)
            except Exception:
                pass

    def user_count(self) -> int:
        return len(self._states)

    def _save(self, user_id: str):
        """保存单个用户状态"""
        if user_id in self._states:
            path = self._state_path(user_id)
            path.write_text(
                json.dumps(self._states[user_id].to_dict(), ensure_ascii=False, indent=2),
                "utf-8",
            )

    def get(self, user_id: str) -> UserState:
        """获取用户状态，不存在则创建"""
        if user_id not in self._states:
            self._states[user_id] = UserState(user_id)
            self._save(user_id)
        return self._states[user_id]

    def record_interaction(self, user_id: str, nickname: str = ""):
        """记录一次互动，更新连续天数等"""
        state = self.get(user_id)
        now = time.time()
        today = datetime.now(CST).strftime("%Y-%m-%d")

        if nickname:
            state.nickname = nickname
        state.total_messages += 1

        # 连续天数计算
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
            except Exception:
                state.consecutive_days = 1
        else:
            state.consecutive_days = 1

        state.last_interact_date = today
        state.last_interact_ts = now
        self._save(user_id)