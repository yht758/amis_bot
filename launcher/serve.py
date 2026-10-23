"""服务器模式的管理台入口：无窗口、无浏览器，直接起 HTTP 服务。

与 launcher.py 的区别：
- 不打开 Edge 应用窗口、不找已占用端口、不做单实例互斥；
- 交给 systemd 托管（im-bridge-manager.service），失败由 systemd 重启；
- 默认监听 0.0.0.0，靠访问口令（data/manager_token.txt）保护。

用法：
    python launcher/serve.py                    # 默认 0.0.0.0:8765
    python launcher/serve.py --port 9000
    IM_BRIDGE_MANAGER_MODE=server python launcher/serve.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (str(HERE), str(ROOT / "src"), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# 服务器模式由环境变量决定，避免依赖 data/launcher_settings.json 是否已存在
os.environ.setdefault("IM_BRIDGE_MANAGER_MODE", "server")


def main() -> int:
    parser = argparse.ArgumentParser(description="im-bridge 管理台（服务器模式）")
    parser.add_argument("--host", default=os.environ.get("IM_BRIDGE_MANAGER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IM_BRIDGE_MANAGER_PORT", "8765")))
    args = parser.parse_args()

    os.environ["IM_BRIDGE_MANAGER_HOST"] = args.host
    os.environ["IM_BRIDGE_MANAGER_PORT"] = str(args.port)

    import server  # noqa: E402  延迟导入，确保上面的环境变量先生效

    httpd = server.create_server(args.host, args.port)
    print(f"[manager] 监听 http://{args.host}:{args.port}  模式={server.MODE}", flush=True)
    if server.AUTH_TOKEN:
        print(f"[manager] 访问地址：http://<服务器IP>:{args.port}/?token={server.AUTH_TOKEN}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("[manager] 收到中断，退出", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
