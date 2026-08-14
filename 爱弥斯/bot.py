"""
QQ 机器人核心 —— 对接 NapCatQQ (WebSocket 收发)
"""
import asyncio
import json
import logging
from datetime import datetime

import websockets

from .config import Config, MAX_HISTORY, LOG_DIR
from .state_manager import StateManager
from .llm_client import LLMClient, LLMError
from .log_writer import append_log

logger = logging.getLogger("爱弥斯")


class AmisBot:
    """爱弥斯 QQ 机器人"""

    def __init__(self, config: Config, state_manager: StateManager, llm: LLMClient):
        self.config = config
        self.state = state_manager
        self.llm = llm
        self.history: dict[str, list[dict]] = {}
        self.processing: set[str] = set()
        self.ws = None
        self.echo_counter = 0
        self.tasks: set[asyncio.Task] = set()
        self.started_at = datetime.now()
        self.stop_event = asyncio.Event()

    def _is_admin(self, user_id: str) -> bool:
        return user_id in self.config.admin_users

    async def _cancel_pending_tasks(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_private_msg(self, user_id: int, text: str):
        """通过 OneBot v11 WebSocket 发送私聊消息"""
        if self.ws is None:
            logger.warning("WebSocket is not connected, message dropped")
            return

        self.echo_counter += 1
        payload = {
            "action": "send_private_msg",
            "params": {"user_id": user_id, "message": text},
            "echo": str(self.echo_counter),
        }
        try:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))
            logger.info(f"Sent to {user_id}")
        except Exception as e:
            logger.error(f"Send failed: {e}")

    async def handle_command(self, user_id: str, text: str) -> str:
        parts = text.strip().split()
        command = parts[0].lower()
        args = parts[1:]
        is_admin = self._is_admin(user_id)

        if command == "/help":
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

        if command == "/status":
            if not is_admin:
                return "你没有权限使用这个命令哦"
            uptime = int((datetime.now() - self.started_at).total_seconds())
            return (
                f"爱弥斯状态\n"
                f"模型：{self.config.model}\n"
                f"记录用户数：{self.state.user_count()}\n"
                f"运行时间：{uptime} 秒"
            )

        if command == "/reset":
            target = str(args[0]) if is_admin and args else user_id
            if target != user_id and not is_admin:
                return "你只能清空自己的对话记忆"
            self.history.pop(target, None)
            append_log("status.log", f"[运行] 管理员 {user_id} 清空了 {target} 的对话记忆")
            return f"已清空 {target} 的对话记忆"

        if command == "/allow":
            if not is_admin:
                return "你没有权限使用这个命令哦"
            if not args or not args[0].isdigit():
                return "用法：/allow QQ号"
            target = str(int(args[0]))
            self.config.add_allowed_user(target)
            append_log("status.log", f"[运行] 管理员 {user_id} 将 {target} 加入白名单")
            return f"已将 {target} 加入白名单"

        if command == "/deny":
            if not is_admin:
                return "你没有权限使用这个命令哦"
            if not args or not args[0].isdigit():
                return "用法：/deny QQ号"
            target = str(int(args[0]))
            if target in self.config.admin_users:
                return "该用户是管理员，不能移出白名单"
            self.config.remove_allowed_user(target)
            append_log("status.log", f"[运行] 管理员 {user_id} 将 {target} 移出白名单")
            return f"已将 {target} 移出白名单"

        if command == "/log":
            if not is_admin:
                return "你没有权限使用这个命令哦"
            log_type = "status"
            line_count = 10
            idx = 0
            if args and args[0] in ("status", "questions", "answers"):
                log_type = args[0]
                idx = 1
            if len(args) > idx:
                try:
                    line_count = max(1, min(int(args[idx]), 30))
                except ValueError:
                    line_count = 10
            return self._read_log_tail(log_type, line_count)

        if command == "/stop":
            if not is_admin:
                return "你没有权限使用这个命令哦"
            self.stop_event.set()
            append_log("status.log", f"[运行] 管理员 {user_id} 请求停止机器人")
            return "正在停止爱弥斯，再见~"

        return "未知命令，发送 /help 查看可用命令"

    def _read_log_tail(self, log_type: str, line_count: int) -> str:
        path = LOG_DIR / f"{log_type}.log"
        if not path.exists():
            return "日志文件不存在"
        try:
            lines = path.read_text("utf-8").splitlines()[-line_count:]
            return "\n".join(lines) or "日志为空"
        except Exception as e:
            return f"读取日志失败：{e}"

    async def handle_private_msg(self, data: dict):
        user_id = str(data.get("user_id", ""))
        raw_message = data.get("raw_message", data.get("message", ""))
        nickname = data.get("sender", {}).get("nickname", "")

        if not isinstance(raw_message, str):
            raw_message = str(raw_message)
        if not user_id or not raw_message:
            return
        if self.config.allowed_users and user_id not in self.config.allowed_users:
            return
        if user_id in self.processing:
            return

        self.processing.add(user_id)
        try:
            append_log("questions.log", f"[{nickname}({user_id})] {raw_message}")

            self.state.record_interaction(user_id, nickname)
            user_history = self._get_history(user_id)

            if raw_message.strip().startswith("/"):
                reply = await self.handle_command(user_id, raw_message)
                if reply:
                    append_log("answers.log", f"[{nickname}({user_id})] {reply}")
                    logger.info(f"[Amis->{nickname}]: {reply[:80]}...")
                    await self.send_private_msg(int(user_id), reply)

                if raw_message.strip().lower().split()[0] == "/stop":
                    if self.ws is not None:
                        await self.ws.close()
                return

            user_history.append({"role": "user", "content": raw_message})

            logger.info(f"[{nickname}({user_id})]: {raw_message}")
            reply = await self.llm.chat(user_history)

            user_history.append({"role": "assistant", "content": reply})
            self._trim_history(user_id)

            append_log("answers.log", f"[{nickname}({user_id})] {reply}")
            logger.info(f"[Amis->{nickname}]: {reply[:80]}...")
            await self.send_private_msg(int(user_id), reply)

        except LLMError as e:
            logger.error(f"LLM error: {e}")
            append_log("status.log", f"[运行] LLM 请求失败：{e}")
            await self.send_private_msg(int(user_id), "我刚刚没想好，稍等再试一次哦~")
        except Exception as e:
            logger.exception(f"Handle error: {e}")
            append_log("status.log", f"[运行] 处理消息出错：{e}")
            await self.send_private_msg(int(user_id), "刚刚有点卡，稍后再试~")
        finally:
            self.processing.discard(user_id)

    def _get_history(self, user_id: str) -> list[dict]:
        if user_id not in self.history:
            self.history[user_id] = []
        return self.history[user_id]

    def _trim_history(self, user_id: str):
        if user_id in self.history and len(self.history[user_id]) > MAX_HISTORY:
            self.history[user_id] = self.history[user_id][-MAX_HISTORY:]

    async def run(self):
        ws_url = self.config.napcat_ws_url
        logger.info(f"Connecting to: {ws_url}")
        append_log("status.log", f"[运行] 正在连接 NapCatQQ：{ws_url}")

        retry_delay = 1
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(ws_url, max_size=2**23) as ws:
                    self.ws = ws
                    logger.info("Connected to NapCatQQ")
                    append_log("status.log", "[运行] 已连接 NapCatQQ")
                    retry_delay = 1

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        post_type = data.get("post_type", "")
                        message_type = data.get("message_type", "")

                        if post_type == "message" and message_type == "private":
                            task = asyncio.create_task(self.handle_private_msg(data))
                            self.tasks.add(task)
                            task.add_done_callback(self.tasks.discard)

                        if self.stop_event.is_set():
                            break

            except (websockets.ConnectionClosed, OSError) as e:
                self.ws = None
                await self._cancel_pending_tasks()
                if self.stop_event.is_set():
                    break
                logger.warning(f"Disconnected: {e}, retry in {retry_delay}s")
                append_log("status.log", f"[运行] 连接断开：{e}，{retry_delay}秒后重试")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            except Exception as e:
                self.ws = None
                await self._cancel_pending_tasks()
                if self.stop_event.is_set():
                    break
                logger.error(f"Error: {e}")
                append_log("status.log", f"[运行] 发生错误：{e}")
                await asyncio.sleep(5)

        append_log("status.log", "[运行] 机器人已停止")
        logger.info("Bot stopped")
