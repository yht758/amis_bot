"""
装配入口 —— 把配置、核心层与平台适配器组装成可运行的程序。

用法：
    python run.py                 # 启动机器人
    python run.py --check-config  # 只校验配置并退出
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .adapters.onebot.app import OneBotApp
from .adapters.onebot.transport import OneBotTransport
from .config import DATA_DIR, Config
from .core.context import ContextManager
from .core.engine import ChatEngine
from .core.llm import LLMClient
from .core.state import StateManager
from .log_writer import append_log
from .personas import load_persona

logger = logging.getLogger("im_bridge.main")

BANNER = "=" * 50


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 非 TTY 环境下可能不可用
        pass


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _print_errors(errors: list[str]) -> None:
    print("配置校验未通过：")
    for index, error in enumerate(errors, 1):
        print(f"  [{index}] {error}")
    print()
    print("修复方式：编辑 .env（推荐）或 config.json / config.secret.json，")
    print("也可用环境变量覆盖，详见 .env.example。")
    print("提示：可运行 `python run.py --check-config` 只做校验。")


def _check_config(config: Config) -> int:
    errors = config.validate()
    print("配置来源优先级：环境变量 > .env > config.secret.json > config.json")
    if errors:
        _print_errors(errors)
        return 1
    print("  [OK] 配置校验通过")
    print(f"  {config.summary()}")
    return 0


def _build_app(config: Config) -> OneBotApp:
    persona = load_persona(config.persona)
    state_manager = StateManager(DATA_DIR)
    context_manager = ContextManager(
        max_history=config.max_history,
        summary_enabled=config.summary_enabled,
        summary_threshold=config.summary_threshold,
        summary_keep_recent=config.summary_keep_recent,
    )
    llm = LLMClient(
        api_key=config.api_key,
        base_url=config.llm_base_url,
        model=config.model,
    )
    transport = OneBotTransport(config.napcat_ws_url)
    engine = ChatEngine(
        config=config,
        llm=llm,
        context_manager=context_manager,
        state_manager=state_manager,
        transport=transport,
        persona=persona,
    )
    return OneBotApp(config=config, engine=engine, transport=transport)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = list(sys.argv[1:] if argv is None else argv)

    config = Config.from_env_or_file()
    errors = config.validate()

    if "--check-config" in args:
        return _check_config(config)

    print(BANNER)
    print("  LLM IM Bridge  ·  轻量拟人聊天桥接器")
    print(BANNER)
    print()

    if errors:
        _print_errors(errors)
        return 1

    _setup_logging()

    try:
        app = _build_app(config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[!] 初始化失败：{exc}")
        return 2

    print(f"Persona: {config.persona}")
    print(f"Bot QQ : {config.bot_qq}")
    print(f"Model  : {config.model}")
    print(f"API    : {config.llm_base_url}")
    print(f"Data   : {DATA_DIR}")
    print(f"Admin  : {config.admin_users or ['<未设置>']}")
    print()

    append_log("status.log", f"[运行] 已启动，模型：{config.model}，人设：{config.persona}")
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        append_log("status.log", "[运行] 收到 Ctrl+C，机器人已停止")
        print()
        print("已停止，再见！")
    return 0
