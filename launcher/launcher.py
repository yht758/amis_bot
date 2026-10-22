"""管理台入口：启动本地 HTTP 服务并打开 Edge 应用窗口。

用法：
    python launcher/launcher.py                 # 启动并打开窗口
    python launcher/launcher.py --no-browser    # 只启动服务
    python launcher/launcher.py --open          # 只打开已运行的实例
    python launcher/launcher.py --check         # 打印环境自检后退出
    python launcher/launcher.py --port 8790     # 指定端口
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

# 开发模式：保证能 import 同目录模块与项目 src/（复用核心校验）
_LAUNCHER_DIR = Path(__file__).resolve().parent
if str(_LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_LAUNCHER_DIR))

from bot_manager import bot_manager  # noqa: E402
from config_store import APP_NAME, APP_VERSION, ROOT, load_settings  # noqa: E402
import env_check  # noqa: E402
from server import create_server  # noqa: E402

DEFAULT_PORT = 8765
PORT_RANGE = 15
LOG_PATH = ROOT / "data" / "launcher.log"


def _log_file(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _probe(port: int, timeout: float = 0.5) -> bool:
    """探测指定端口是否是本管理台（先做快速 TCP 探测，避免慢超时）。

    本机回环探测必须直连：显式使用空 ProxyHandler 的 opener，避免
    HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等环境变量把 127.0.0.1 的请求
    转发给代理，导致已运行实例探测失败而重复启动第二个实例。
    """
    import socket
    import urllib.request

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.12):
            pass
    except OSError:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("app") == APP_NAME
    except Exception:  # noqa: BLE001
        return False


def find_existing_port() -> int | None:
    settings = load_settings()
    preferred = [int(settings.get("port") or DEFAULT_PORT)]
    preferred.extend(range(DEFAULT_PORT, DEFAULT_PORT + 6))
    seen: set[int] = set()
    for port in preferred:
        if port in seen:
            continue
        seen.add(port)
        if _probe(port):
            return port
    return None


def create_server_with_fallback(preferred_port: int, attempts: int = PORT_RANGE):
    last_error: OSError | None = None
    for offset in range(attempts):
        port = preferred_port + offset
        try:
            server = create_server(port=port)
            return server, port
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"端口 {preferred_port}~{preferred_port + attempts - 1} 均被占用：{last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM IM Bridge 管理台")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不打开窗口")
    parser.add_argument("--open", action="store_true", help="只打开已运行的实例")
    parser.add_argument("--check", action="store_true", help="打印环境自检后退出")
    args = parser.parse_args(argv)

    if args.check:
        checks = env_check.check_all(refresh=True)
        print(json.dumps({"ok": True, "root": str(ROOT), "checks": checks}, ensure_ascii=False, indent=2))
        return 0

    if args.open:
        port = find_existing_port()
        if port is None:
            print("未找到正在运行的管理台实例，请先启动 BotManager。")
            return 1
        url = f"http://127.0.0.1:{port}/"
        env_check.open_app_window(url)
        print(f"已打开：{url}")
        return 0

    # 单实例：已有实例则直接打开窗口
    existing = find_existing_port()
    if existing is not None:
        url = f"http://127.0.0.1:{existing}/"
        env_check.open_app_window(url)
        print(f"管理台已在运行，已打开：{url}")
        return 0

    settings = load_settings()
    preferred_port = args.port or int(settings.get("port") or DEFAULT_PORT)
    server, port = create_server_with_fallback(preferred_port)
    url = f"http://127.0.0.1:{port}/"

    bot_manager.log(f"管理台 v{APP_VERSION} 已启动：{url}")
    _log_file(f"manager started at {url} (pid={os.getpid()})")
    print(f"管理台已启动：{url}")
    print("关闭窗口不会自动退出；请在页面右上角选择「退出管理台」。")

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), env_check.open_app_window(url)), daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            bot_manager.stop(timeout=6)
        except Exception:  # noqa: BLE001
            pass
        server.server_close()
        bot_manager.log("管理台已退出")
        _log_file("manager stopped")
        print("管理台已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
