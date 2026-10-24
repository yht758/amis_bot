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
from .config import DATA_DIR, FIELD_SPECS, Config
from .core.context import ContextManager
from .core.engine import ChatEngine
from .core.llm import LLMClient
from .core.proactive import ProactiveScheduler
from .core.state import StateManager
from .core.voice import SpeechSynthesizer, VoiceTranscriber
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
    """打印完整字段表 + 问题清单；有 error 时返回 1（供 CI / 脚本判断）。"""
    issues = config.validate_issues()
    errors = [item for item in issues if item.level == "error"]
    warns = [item for item in issues if item.level == "warning"]

    print(BANNER)
    print(" 配置检查  ·  来源优先级：环境变量 > .env > config.secret.json > config.json")
    print(BANNER)
    print()

    print(f"{'字段':<26}{'当前值':<32}来源")
    print("-" * 76)
    for spec in FIELD_SPECS:
        if spec.kind == "secret":
            raw = (getattr(config, spec.attr, "") or "").strip()
            shown = "***（已配置）" if raw else "<未设置>"
        else:
            value = getattr(config, spec.attr, None)
            if isinstance(value, (list, tuple)):
                shown = ",".join(str(item) for item in value) or "（空）"
            elif value == "" or value is None:
                shown = "（空）"
            else:
                shown = str(value)
        if len(shown) > 30:
            shown = shown[:29] + "…"
        print(f"{spec.env:<26}{shown:<32}{config.sources.get(spec.env, '默认值')}")

    print()

    if warns:
        print(f"警告 {len(warns)} 条（不阻断启动）：")
        for item in warns:
            print(f"  ! {item}")
        print()

    if errors:
        print(f"错误 {len(errors)} 条：")
        for item in errors:
            print(f"  × {item}")
        print()
        print("修复方式：编辑 .env（推荐），或 config.json / config.secret.json，")
        print("也可用环境变量覆盖。字段说明见 .env.example。")
        return 1

    print("[OK] 配置校验通过。")
    return 0


def _build_app(config: Config) -> OneBotApp:
    persona = load_persona(config.persona)
    state_manager = StateManager(DATA_DIR)
    context_manager = ContextManager(
        max_history=config.max_history,
        summary_enabled=config.summary_enabled,
        summary_threshold=config.summary_threshold,
        summary_keep_recent=config.summary_keep_recent,
        async_summary=config.summary_async,
        token_threshold=config.summary_token_threshold,
        persist_dir=DATA_DIR / "summaries",
    )
    llm = LLMClient(
        api_key=config.api_key,
        base_url=config.llm_base_url,
        model=config.model,
    )
    transport = OneBotTransport(config.napcat_ws_url)

    # 语音：未配 endpoint 时这两个对象自身会「未启用」，不会产生任何请求
    transcriber = VoiceTranscriber(
        endpoint=config.asr_endpoint,
        api_key=config.asr_api_key or config.api_key,
    )
    synthesizer = SpeechSynthesizer(
        endpoint=config.tts_endpoint,
        api_key=config.tts_api_key or config.api_key,
    )

    engine = ChatEngine(
        config=config,
        llm=llm,
        context_manager=context_manager,
        state_manager=state_manager,
        transport=transport,
        persona=persona,
        synthesizer=synthesizer,
    )

    proactive = ProactiveScheduler(
        config=config,
        llm=llm,
        context=context_manager,
        state=state_manager,
        transport=transport,
        persona_prompt=persona.system_prompt,
    )

    return OneBotApp(
        config=config,
        engine=engine,
        transport=transport,
        transcriber=transcriber,
        proactive=proactive,
    )


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
