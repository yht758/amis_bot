"""NapCat 自动更新：检查 GitHub 最新版、下载 Framework.zip、校验、备份并安装。

设计：
- 下载走 GitHub 官方地址，可在设置中配置镜像前缀（如 https://ghproxy.net/）；
- 安装前用官方 SHA256 校验下载内容；
- 更新前备份现有框架与 config，更新时保留 config；
- 更新需要 NapCat/QQ 处于退出状态（文件占用），否则直接返回错误提示。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from config_store import ROOT, load_settings, save_settings

REPO = "NapNeko/NapCatQQ"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "NapCat.Framework.zip"
VERSION_FILE = ".napcat-version"
CHECK_TTL = 120.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.RLock()
_check_cache: dict = {"ts": 0.0, "data": None}
_state: dict = {
    "running": False,
    "stage": "idle",
    "message": "",
    "downloaded": 0,
    "total": 0,
    "percent": 0,
    "error": "",
    "result": None,
    "started_at": None,
    "finished_at": None,
}
_cancel = threading.Event()


def _extract_executable(command: str) -> str:
    match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', command or "")
    return (match.group(1) or match.group(2) or "").strip() if match else ""


def resolve_paths() -> tuple[Path | None, Path | None]:
    """返回 (framework_dir, bootmain_dir)；无法定位时返回 (None, None)。"""
    command = str(load_settings().get("napcat_command") or "").strip()
    exe = _extract_executable(command)
    if not exe:
        return None, None
    path = Path(exe)
    name = path.name.lower()
    if name in ("napiloader.bat", "napiLoader.bat".lower(), "napcat.bat", "napcat.quick.bat"):
        framework = path.parent
        return framework, framework.parent
    if name in ("napcatwinbootmain.exe", "napimain.exe"):
        if name == "napimain.exe":
            return path.parent, path.parent.parent
        return path.parent / "napcat", path.parent
    return None, None


def _read_local_version(framework: Path | None) -> dict:
    if framework is None:
        return {"installed": False, "tag": "", "mtime": 0, "path": ""}
    marker = framework / VERSION_FILE
    tag = ""
    if marker.exists():
        try:
            tag = marker.read_text("utf-8-sig").strip()
        except OSError:
            tag = ""
    mjs = framework / "napcat.mjs"
    installed = mjs.exists()
    mtime = int(mjs.stat().st_mtime) if installed else 0
    return {
        "installed": installed,
        "tag": tag,
        "mtime": mtime,
        "path": str(framework),
    }


def _github_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "im-bridge-manager",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def check_update(refresh: bool = False) -> dict:
    """检查最新版本（带缓存）。"""
    now = time.time()
    with _lock:
        if not refresh and _check_cache["data"] and now - _check_cache["ts"] < CHECK_TTL:
            return _check_cache["data"]

    framework, bootmain = resolve_paths()
    local = _read_local_version(framework)
    data: dict = {
        "ok": True,
        "repo": REPO,
        "local": local,
        "framework_dir": str(framework) if framework else "",
        "latest_tag": "",
        "published_at": "",
        "asset": None,
        "update_available": False,
        "error": "",
    }
    try:
        release = _github_json(API_URL)
        tag = str(release.get("tag_name") or "")
        asset = None
        for item in release.get("assets") or []:
            if item.get("name") == ASSET_NAME:
                asset = item
                break
        if asset is None:
            raise RuntimeError(f"最新版本中未找到 {ASSET_NAME}")
        digest = str(asset.get("digest") or "")
        data.update(
            {
                "latest_tag": tag,
                "published_at": str(release.get("published_at") or ""),
                "asset": {
                    "name": ASSET_NAME,
                    "size": int(asset.get("size") or 0),
                    "digest": digest,
                    "url": str(asset.get("browser_download_url") or ""),
                },
                "update_available": local.get("tag") != tag,
            }
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
        data["error"] = f"检查更新失败：{exc}"

    with _lock:
        _check_cache.update({"ts": now, "data": data})
    return data


def get_status(refresh: bool = False) -> dict:
    with _lock:
        state = dict(_state)
    check = check_update(refresh=refresh)
    return {
        "ok": True,
        "state": state,
        "check": check,
        "mirror": str(load_settings().get("napcat_update_mirror") or ""),
    }


def _running_qq_or_napcat() -> list[str]:
    """返回正在运行的相关进程名列表（需要用户先退出才能更新）。"""
    found: list[str] = []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        names = {line.split(",")[0].strip('"').lower() for line in result.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.TimeoutExpired):
        return found
    for process in ("qq.exe", "napcatwinbootmain.exe", "napimain.exe", "napcat.exe"):
        if process in names:
            found.append(process)
    return found


def _set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _download(url: str, target: Path, expected_size: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "im-bridge-manager"})
    with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or expected_size or 0)
        downloaded = 0
        _set_state(total=total, downloaded=0, percent=0)
        while True:
            if _cancel.is_set():
                raise RuntimeError("已取消")
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            downloaded += len(chunk)
            percent = int(downloaded * 100 / total) if total else 0
            _set_state(downloaded=downloaded, total=total, percent=min(percent, 100))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_framework(source: Path, target: Path) -> None:
    """覆盖复制新框架，保留 target 中的 config 目录。"""
    for item in source.iterdir():
        destination = target / item.name
        if item.name == "config":
            continue
        if item.is_dir():
            if destination.exists() and destination.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                shutil.copytree(item, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def _do_update(mirror: str = "") -> None:
    framework, bootmain = resolve_paths()
    try:
        if framework is None or bootmain is None:
            raise RuntimeError("无法定位 NapCat 目录，请先在「管理台设置」配置正确的启动命令")

        running = _running_qq_or_napcat()
        if running:
            raise RuntimeError(
                "检测到 QQ / NapCat 正在运行（" + ", ".join(running) + "），请先退出后再更新"
            )

        _set_state(stage="checking", message="正在获取最新版本…", error="", result=None, percent=0)
        check = check_update(refresh=True)
        if check.get("error"):
            raise RuntimeError(check["error"])
        asset = check.get("asset") or {}
        url = str(asset.get("url") or "")
        expected = int(asset.get("size") or 0)
        digest = str(asset.get("digest") or "")
        if not url:
            raise RuntimeError("未获取到下载地址")
        if not check.get("update_available"):
            raise RuntimeError(f"已是最新版本（{check.get('latest_tag')}），无需更新")

        update_dir = bootmain.parent / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        archive = update_dir / "NapCat.Framework.zip.download"
        if archive.exists():
            archive.unlink()

        download_url = (mirror or "") + url
        _set_state(stage="downloading", message="正在下载框架包…")
        _download(download_url, archive, expected)

        _set_state(stage="verifying", message="正在校验 SHA256…", percent=100)
        actual = _sha256(archive)
        expected_hash = digest.replace("sha256:", "").lower()
        if expected_hash and actual != expected_hash:
            archive.unlink(missing_ok=True)
            raise RuntimeError("SHA256 校验失败，下载文件可能不完整或被篡改，已删除")

        _set_state(stage="backing-up", message="正在备份旧框架与配置…")
        backup_root = bootmain.parent / "backup"
        backup_dir = backup_root / f"napcat-{time.strftime('%Y%m%d-%H%M%S')}"
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(framework, backup_dir, dirs_exist_ok=True)

        _set_state(stage="installing", message="正在解压并安装新框架…")
        extract_dir = update_dir / "framework-new"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        with zipfile.ZipFile(archive) as archive_file:
            archive_file.extractall(extract_dir)
        _copy_framework(extract_dir, framework)
        (framework / VERSION_FILE).write_text(check.get("latest_tag", ""), "utf-8")

        try:
            archive.unlink()
        except OSError:
            pass
        try:
            shutil.rmtree(extract_dir)
        except OSError:
            pass

        _set_state(
            stage="done",
            message="更新完成，请从管理台重新启动 NapCat",
            percent=100,
            finished_at=time.time(),
            result={
                "tag": check.get("latest_tag"),
                "backup": str(backup_dir),
                "framework_dir": str(framework),
            },
        )
    except Exception as exc:  # noqa: BLE001
        _set_state(
            stage="error",
            message="更新失败",
            error=str(exc),
            finished_at=time.time(),
        )
    finally:
        with _lock:
            _state["running"] = False


def start_update(mirror: str | None = None) -> dict:
    with _lock:
        if _state["running"]:
            return {"ok": False, "error": "更新正在进行中"}
        _state.update(
            {
                "running": True,
                "stage": "starting",
                "message": "正在启动更新…",
                "downloaded": 0,
                "total": 0,
                "percent": 0,
                "error": "",
                "result": None,
                "started_at": time.time(),
                "finished_at": None,
            }
        )
        _cancel.clear()
    if mirror is not None:
        settings = load_settings()
        settings["napcat_update_mirror"] = str(mirror).strip()
        save_settings(settings)
    thread = threading.Thread(target=_do_update, args=(str(mirror or "").strip(),), daemon=True)
    thread.start()
    return {"ok": True, "started": True}


def cancel_update() -> dict:
    _cancel.set()
    return {"ok": True}
