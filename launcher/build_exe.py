"""构建单文件 Windows 可执行程序 BotManager.exe。

用法（在项目根目录）：
    python launcher/build_exe.py

产物：
    BotManager.exe   （项目根目录，双击即用，独立运行，不依赖系统 Python）
    .build/dist/BotManager.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"
BUILD = ROOT / ".build"
VENV = BUILD / "venv"
DIST = BUILD / "dist"
WORK = BUILD / "work"
SPEC = BUILD / "spec"
APP_NAME = "BotManager"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("$ " + " ".join(str(part) for part in cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def ensure_venv(base_python: str) -> Path:
    python = VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"
    if not python.exists():
        BUILD.mkdir(parents=True, exist_ok=True)
        run([base_python, "-m", "venv", str(VENV)])
    return python


def main() -> int:
    if not (LAUNCHER / "web" / "index.html").exists():
        print("[!] 未找到 launcher/web/index.html，请先完成前端文件。")
        return 2

    base_python = sys.executable
    system_python = ROOT / "python" / "python.exe"
    if system_python.exists():
        base_python = str(system_python)

    print("=" * 60)
    print("  BotManager.exe 构建脚本")
    print("=" * 60)
    print(f"基础 Python : {base_python}")
    print(f"项目目录    : {ROOT}")

    python = ensure_venv(base_python)
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    run([str(python), "-m", "pip", "install", "pyinstaller>=6.10", "--quiet"])

    for folder in (DIST, WORK, SPEC):
        shutil.rmtree(folder, ignore_errors=True)
    for folder in (DIST, WORK, SPEC):
        folder.mkdir(parents=True, exist_ok=True)

    started = time.time()
    run(
        [
            str(python),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--noconsole",
            "--name",
            APP_NAME,
            "--icon",
            str(LAUNCHER / "assets" / "icon.ico"),
            "--add-data",
            f"{LAUNCHER / 'web'}{os.pathsep}launcher/web",
            "--paths",
            str(LAUNCHER),
            "--distpath",
            str(DIST),
            "--workpath",
            str(WORK),
            "--specpath",
            str(SPEC),
            str(LAUNCHER / "launcher.py"),
        ]
    )

    exe = DIST / f"{APP_NAME}.exe"
    if not exe.exists():
        print("[!] 构建失败：未生成 exe")
        return 1
    target = ROOT / f"{APP_NAME}.exe"
    shutil.copy2(exe, target)
    size_mb = target.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"构建完成：{target}（{size_mb:.1f} MB，耗时 {time.time() - started:.1f}s）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
