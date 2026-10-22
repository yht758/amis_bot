"""环境自检、路径打开、NapCat 与 Edge 集成。"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from config_store import (
    ROOT,
    find_python,
    get_api_key,
    load_config,
    load_settings,
    resolved_values,
    validate_values,
)

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_cache: dict = {"ts": 0.0, "checks": []}
CACHE_TTL = 8.0

_NAPCAT_CANDIDATES = [
    Path(r"D:\NapCatQQ\bootmain\NapCatWinBootMain.exe"),
    Path(r"D:\NapCatQQ\bootmain\napcat\napimain.exe"),
]


# --------------------------------------------------------------------------
# 探测工具
# --------------------------------------------------------------------------
def _extract_executable(command: str) -> str:
    """从启动命令中取出第一个可执行文件路径（支持带引号路径）。"""
    match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', command or "")
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def _command_ok(command: str) -> bool:
    exe = _extract_executable(command)
    if not exe:
        return False
    if Path(exe).exists():
        return True
    return shutil.which(exe) is not None


def _python_info() -> dict:
    python = find_python()
    if not python or not Path(python).exists():
        return {
            "status": "fail",
            "detail": "未找到可用的 Python 运行时",
            "hint": "请确认项目目录下存在 python\\python.exe，或安装 Python 3.10+ 并加入 PATH",
        }
    try:
        result = subprocess.run(
            [python, "-V"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        version = (result.stdout or result.stderr).strip()
        if result.returncode == 0:
            return {"status": "ok", "detail": f"{version}（{python}）", "hint": ""}
        return {"status": "warn", "detail": f"{python} 无法执行：{version}", "hint": "尝试重新安装 Python 运行时"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "warn", "detail": f"检测 Python 失败：{exc}", "hint": "检查 python\\python.exe 是否可执行"}


def _deps_info() -> dict:
    python = find_python()
    if not python or not Path(python).exists():
        return {"status": "fail", "detail": "无 Python 解释器，无法检查依赖", "hint": "先修复 Python 运行时"}
    env = os.environ.copy()
    libs = ROOT / "libs"
    if libs.is_dir():
        env["PYTHONPATH"] = str(libs)
    try:
        result = subprocess.run(
            [python, "-c", "import httpx, websockets; print('ok')"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode == 0 and "ok" in result.stdout:
            return {"status": "ok", "detail": "httpx / websockets 已就绪（libs 或 site-packages）", "hint": ""}
        message = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["未知错误"]
        return {
            "status": "fail",
            "detail": f"依赖缺失：{message[0][:160]}",
            "hint": "运行 pip install -r requirements.txt，或确认 libs\\ 目录完整",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "fail", "detail": f"依赖检查失败：{exc}", "hint": "手动运行 pip install -r requirements.txt"}


def _parse_port(url: str, default: int = 3001) -> int:
    try:
        parsed = urlparse(url or "")
        return parsed.port or default
    except ValueError:
        return default


def _onebot_info(values: dict) -> dict:
    port = _parse_port(str(values.get("napcat_ws_url") or ""), 3001)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.2):
            return {"status": "ok", "detail": f"127.0.0.1:{port} 可连接", "hint": ""}
    except OSError:
        return {
            "status": "warn",
            "detail": f"127.0.0.1:{port} 暂未监听",
            "hint": "先启动 NapCat 并登录机器人 QQ；bot 启动后会自动重连，不阻塞使用",
        }


def _napcat_info() -> dict:
    values = resolved_values()
    port = _parse_port(str(values.get("napcat_ws_url") or ""), 3001)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return {
                "status": "ok",
                "detail": f"OneBot 端口 {port} 已监听，NapCat 可能已在运行",
                "hint": "",
            }
    except OSError:
        pass

    command = str(load_settings().get("napcat_command") or "").strip()
    if command:
        if _command_ok(command):
            return {
                "status": "warn",
                "detail": f"已配置启动命令，但 OneBot 端口 {port} 尚未监听",
                "hint": "点击概览页「启动 NapCat」拉起，或手动启动并确认 WebSocket 端口与 AMIS_NAPCAT_WS_URL 一致",
            }
        executable = _extract_executable(command) or command
        return {
            "status": "fail",
            "detail": f"NapCat 启动命令指向的程序不存在：{executable}",
            "hint": "请在「配置 → 管理台设置」更新路径；新版 NapCat 启动器通常为 D:\\NapCatQQ\\bootmain\\NapCatWinBootMain.exe",
        }
    for candidate in _NAPCAT_CANDIDATES:
        if candidate.exists():
            return {
                "status": "warn",
                "detail": f"检测到 {candidate}",
                "hint": "可在「配置 → 管理台设置」填写 NapCat 启动命令，一键从管理台启动",
            }
    return {
        "status": "warn",
        "detail": "尚未配置 NapCat 启动命令",
        "hint": "在「配置 → 管理台设置」填写启动命令，或手动启动 NapCat",
    }


def find_edge() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _edge_info() -> dict:
    edge = find_edge()
    if edge:
        return {"status": "ok", "detail": str(edge), "hint": ""}
    return {
        "status": "warn",
        "detail": "未找到 Microsoft Edge",
        "hint": "将使用默认浏览器打开管理台，体验略差；安装 Edge 可获得应用窗口体验",
    }


def _config_info() -> dict:
    try:
        values = resolved_values()
        errors = validate_values(values)
        if not get_api_key():
            return {
                "status": "fail",
                "detail": "尚未配置 API Key",
                "hint": "在「配置」页填写 AMIS_API_KEY 并保存",
            }
        if errors:
            return {
                "status": "fail",
                "detail": f"{len(errors)} 项配置需要修正",
                "hint": "；".join(errors[:3]),
            }
        return {"status": "ok", "detail": "配置有效", "hint": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail", "detail": f"配置读取失败：{exc}", "hint": "检查 .env / config.json 格式"}


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def check_all(refresh: bool = False) -> list[dict]:
    now = time.time()
    if not refresh and _cache["checks"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["checks"]

    values = resolved_values()
    checks = [
        {"id": "project", "title": "项目文件",
         "status": "ok" if (ROOT / "run.py").exists() and (ROOT / "src" / "im_bridge").is_dir() else "fail",
         "detail": str(ROOT),
         "hint": "" if (ROOT / "src" / "im_bridge").is_dir() else "请把管理台放在项目根目录（与 run.py 同级）"},
        {"id": "python", "title": "Python 运行时", **_python_info()},
        {"id": "deps", "title": "Python 依赖", **_deps_info()},
        {"id": "config", "title": "Bot 配置", **_config_info()},
        {"id": "onebot_port", "title": "OneBot 连接", **_onebot_info(values)},
        {"id": "napcat", "title": "NapCat", **_napcat_info()},
        {"id": "edge", "title": "Edge 应用窗口", **_edge_info()},
    ]
    _cache["ts"] = now
    _cache["checks"] = checks
    return checks


def open_target(target: str) -> None:
    """在资源管理器中打开项目相关目录/文件。"""
    target = (target or "").strip()
    paths = {
        "project": ROOT,
        "data": ROOT / "data",
        "logs": ROOT / "logs",
        "env": ROOT / ".env",
        "config": ROOT / "config.json",
        "dist": ROOT,
    }
    path = paths.get(target)
    if path is None:
        raise ValueError(f"未知的打开目标：{target}")
    if not path.exists():
        if target in ("env", "config"):
            path = ROOT
        else:
            path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        try:
            if path.is_dir():
                subprocess.Popen(["explorer", str(path)])
            else:
                subprocess.Popen(["notepad", str(path)])
        except OSError:
            os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


_napcat_process: subprocess.Popen | None = None


def start_napcat() -> dict:
    global _napcat_process
    command = str(load_settings().get("napcat_command") or "").strip()
    if not command:
        raise RuntimeError("尚未配置 NapCat 启动命令，请先在「配置 → 管理台设置」中填写")
    if not _command_ok(command):
        executable = _extract_executable(command) or command
        raise RuntimeError(
            f"NapCat 启动命令无效：找不到可执行文件 {executable}。"
            "请在「配置 → 管理台设置」更新为当前 NapCat 启动器路径"
            "（新版通常为 D:\\NapCatQQ\\bootmain\\NapCatWinBootMain.exe）"
        )
    if _napcat_process is not None and _napcat_process.poll() is None:
        return {"already": True, "pid": _napcat_process.pid}
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except OSError as exc:
        raise RuntimeError(f"启动 NapCat 失败：{exc}") from exc
    _napcat_process = process
    return {"already": False, "pid": process.pid}


def stop_napcat() -> dict:
    global _napcat_process
    if _napcat_process is None or _napcat_process.poll() is not None:
        _napcat_process = None
        return {"stopped": False}
    pid = _napcat_process.pid
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
        else:
            _napcat_process.terminate()
    except (OSError, subprocess.TimeoutExpired):
        pass
    _napcat_process = None
    return {"stopped": True, "pid": pid}


def open_app_window(url: str) -> bool:
    """优先用 Edge --app 打开无地址栏应用窗口。"""
    edge = find_edge()
    if edge:
        try:
            subprocess.Popen(
                [str(edge), f"--app={url}", "--window-size=1280,860"],
                creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            return True
        except OSError:
            pass
    return webbrowser.open(url)
