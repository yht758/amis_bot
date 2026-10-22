"""
指令系统 —— 平台无关的命令路由。

指令只负责“决定要回复什么”，不直接碰网络；真正的发送由 ChatEngine
通过 ``MessageTransport`` 完成，这样换平台时指令逻辑可原样复用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import LOG_DIR, Config
from ..log_writer import append_log
from .context import ContextManager
from .state import StateManager


@dataclass
class CommandResult:
    """一条指令的处理结果。"""

    reply: str = ""
    stop: bool = False


class CommandRouter:
    """解析并执行 /help、/status、/reset 等指令。"""

    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        context_manager: ContextManager,
        started_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.state = state_manager
        self.context = context_manager
        self.started_at = started_at or datetime.now()

    def is_command(self, text: str) -> bool:
        return (text or "").strip().startswith("/")

    async def handle(self, user_id: str, text: str) -> CommandResult:
        parts = (text or "").strip().split()
        if not parts:
            return CommandResult(reply="")
        command = parts[0].lower()
        args = parts[1:]
        is_admin = self.config.is_admin(user_id)

        if command == "/help":
            return CommandResult(reply=self._help_text(is_admin))

        if command == "/status":
            if not is_admin:
                return CommandResult(reply="你没有权限使用这个命令哦")
            uptime = int((datetime.now() - self.started_at).total_seconds())
            return CommandResult(
                reply=(
                    "桥接器状态\n"
                    f"模型：{self.config.model}\n"
                    f"记录用户数：{self.state.user_count()}\n"
                    f"活跃会话：{self.context.active_count()}\n"
                    f"运行时间：{uptime} 秒"
                )
            )

        if command == "/reset":
            target = str(args[0]) if is_admin and args else user_id
            if target != user_id and not is_admin:
                return CommandResult(reply="你只能清空自己的对话记忆")
            self.context.reset(target)
            append_log("status.log", f"[运行] 管理员 {user_id} 清空了 {target} 的对话记忆")
            return CommandResult(reply=f"已清空 {target} 的对话记忆")

        if command == "/allow":
            if not is_admin:
                return CommandResult(reply="你没有权限使用这个命令哦")
            if not args or not args[0].isdigit():
                return CommandResult(reply="用法：/allow QQ号")
            target = str(int(args[0]))
            self.config.add_allowed_user(target)
            append_log("status.log", f"[运行] 管理员 {user_id} 将 {target} 加入白名单")
            return CommandResult(reply=f"已将 {target} 加入白名单")

        if command == "/deny":
            if not is_admin:
                return CommandResult(reply="你没有权限使用这个命令哦")
            if not args or not args[0].isdigit():
                return CommandResult(reply="用法：/deny QQ号")
            target = str(int(args[0]))
            if target in self.config.admin_users:
                return CommandResult(reply="该用户是管理员，不能移出白名单")
            self.config.remove_allowed_user(target)
            append_log("status.log", f"[运行] 管理员 {user_id} 将 {target} 移出白名单")
            return CommandResult(reply=f"已将 {target} 移出白名单")

        if command == "/log":
            if not is_admin:
                return CommandResult(reply="你没有权限使用这个命令哦")
            log_type = "status"
            line_count = 10
            index = 0
            if args and args[0] in ("status", "questions", "answers"):
                log_type = args[0]
                index = 1
            if len(args) > index:
                try:
                    line_count = max(1, min(int(args[index]), 30))
                except ValueError:
                    line_count = 10
            return CommandResult(reply=self._read_log_tail(log_type, line_count))

        if command == "/stop":
            if not is_admin:
                return CommandResult(reply="你没有权限使用这个命令哦")
            append_log("status.log", f"[运行] 管理员 {user_id} 请求停止机器人")
            return CommandResult(reply="正在停止，再见~", stop=True)

        return CommandResult(reply="未知命令，发送 /help 查看可用命令")

    @staticmethod
    def _help_text(is_admin: bool) -> str:
        if is_admin:
            return (
                "管理员命令：\n"
                "/status 查看状态\n"
                "/reset [QQ] 清空对话记忆\n"
                "/allow QQ 加入白名单\n"
                "/deny QQ 移出白名单\n"
                "/log [status|questions|answers] [行数] 查看日志\n"
                "/stop 停止机器人"
            )
        return "可用命令：/help 帮助 /reset 清空我的对话记忆"

    @staticmethod
    def _read_log_tail(log_type: str, line_count: int) -> str:
        path = LOG_DIR / f"{log_type}.log"
        if not path.exists():
            return "日志文件不存在"
        try:
            lines = path.read_text("utf-8").splitlines()[-line_count:]
            return "\n".join(lines) or "日志为空"
        except OSError as exc:
            return f"读取日志失败：{exc}"
