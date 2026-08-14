"""
爱弥斯 QQ 机器人 - 主入口
"""
import asyncio
import logging
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .config import Config, DATA_DIR
from .state_manager import StateManager
from .llm_client import LLMClient
from .bot import AmisBot
from .log_writer import append_log

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def main():
    print("=" * 50)
    print("   Amis QQ Bot")
    print("=" * 50)
    print()

    config = Config.from_env_or_file()

    if config.api_key in ("", "sk-your-api-key-here"):
        print("[!] Please set your LLM API Key!")
        sys.exit(1)

    append_log("status.log", f"[运行] 机器人已启动，模型：{config.model}")

    state_manager = StateManager(DATA_DIR)
    llm = LLMClient(
        api_key=config.api_key,
        base_url=config.llm_base_url,
        model=config.model,
    )
    bot = AmisBot(config, state_manager, llm)

    print(f"Bot QQ: {config.bot_qq}")
    print(f"Model: {config.model}")
    print(f"Data: {DATA_DIR}")
    print(f"Admin: {config.admin_users}")
    print()

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        append_log("status.log", "[运行] 机器人已停止")
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
