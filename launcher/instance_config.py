"""实例注册表：``instances.json`` 的读写、校验与迁移。

目录布局（相对项目根）：:

    instances.json                  # 注册表：有哪些实例
    instances/<id>/config.json      # 每实例自己的业务配置
    data/<id>/                      # 每实例独立数据（状态、摘要、日志）
    .env / config.secret.json       # 所有实例共享的密钥

设计要点：

- **default 实例特殊**：直接引用根目录的 ``config.json`` 与 ``data/``，老用户升级后
  零迁移、聊天记录和配置都不动；
- **密钥共享**：新实例只复制业务配置，不复制 ``.env``，避免多份密钥互相漂移；
- **删除保留备份**：删实例时默认把配置和数据挪到 ``backups/instances/`` 而不是直接抹掉。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "instances.json"
INSTANCES_DIR = ROOT / "instances"
DATA_DIR = ROOT / "data"
BACKUP_DIR = ROOT / "backups" / "instances"

DEFAULT_INSTANCE_ID = "default"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_IDS = {"admin", "api", "new", "static", "assets"}

# 各字段的默认值（新实例从根配置继承，缺失时兜底）
FALLBACK_FIELDS: dict[str, Any] = {
    "bot_qq": "",
    "persona": "aemeath",
    "napcat_ws_url": "ws://127.0.0.1:3001",
}


class InstanceError(Exception):
    """实例操作失败，消息可直接展示给用户。"""


# --------------------------------------------------------------------------
# 注册表读写
# --------------------------------------------------------------------------
def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize(record: dict, *, is_default: bool = False) -> dict:
    """补全字段，保证对外结构稳定。"""
    instance_id = str(record.get("id") or "").strip()
    return {
        "id": instance_id,
        "name": str(record.get("name") or instance_id or "未命名").strip(),
        "enabled": bool(record.get("enabled", True)),
        "bot_qq": str(record.get("bot_qq") or "").strip(),
        "persona": str(record.get("persona") or "aemeath").strip(),
        "onebot_ws_url": str(
            record.get("onebot_ws_url") or FALLBACK_FIELDS["napcat_ws_url"]
        ).strip(),
        "config_dir": str(record.get("config_dir") or (
            "." if is_default else f"instances/{instance_id}"
        )),
        "data_dir": str(record.get("data_dir") or (
            "data" if is_default else f"data/{instance_id}"
        )),
        "created_at": int(record.get("created_at") or time.time()),
    }


def ensure_registry() -> dict:
    """读取注册表；不存在时把根配置迁移成 default 实例（不移动任何文件）。"""
    data = _read_json(REGISTRY_PATH, None)
    if isinstance(data, dict) and isinstance(data.get("instances"), list) and data["instances"]:
        instances = [
            _normalize(item, is_default=str(item.get("id")) == DEFAULT_INSTANCE_ID)
            for item in data["instances"]
            if isinstance(item, dict) and item.get("id")
        ]
        if instances:
            return {"instances": instances}

    # 首次运行：根目录的 config.json/config.secret.json 即 default 实例
    root_config = _read_json(ROOT / "config.json", {})
    default = _normalize(
        {
            "id": DEFAULT_INSTANCE_ID,
            "name": "主号",
            "bot_qq": root_config.get("bot_qq", ""),
            "persona": root_config.get("persona", "aemeath"),
            "onebot_ws_url": root_config.get("napcat_ws_url", FALLBACK_FIELDS["napcat_ws_url"]),
            "config_dir": ".",
            "data_dir": "data",
            "created_at": int(time.time()),
        },
        is_default=True,
    )
    registry = {"instances": [default]}
    _write_json(REGISTRY_PATH, registry)
    return registry


def list_instances() -> list[dict]:
    return ensure_registry()["instances"]


def get_instance(instance_id: str) -> dict | None:
    for item in list_instances():
        if item["id"] == instance_id:
            return item
    return None


def require_instance(instance_id: str) -> dict:
    instance = get_instance(instance_id)
    if instance is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    return instance


def save_instances(instances: list[dict]) -> None:
    _write_json(REGISTRY_PATH, {"instances": instances})


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def _port_of(ws_url: str) -> str:
    """从 ws://host:port 中取端口，用于检测多实例抢同一 NapCat。"""
    match = re.search(r":(\d+)(?:/|$)", ws_url or "")
    return match.group(1) if match else ""


def validate_new(
    instance_id: str,
    bot_qq: str,
    onebot_ws_url: str,
    instances: list[dict] | None = None,
) -> list[str]:
    """创建/修改前的校验，返回人类可读的错误列表。"""
    errors: list[str] = []
    current = instances if instances is not None else list_instances()

    if not _ID_PATTERN.match(instance_id or ""):
        errors.append(
            "实例 ID 只能用小写字母、数字、下划线或短横线，且以字母/数字开头（最长 32 位）"
        )
    if instance_id in _RESERVED_IDS:
        errors.append(f"实例 ID「{instance_id}」是保留字，请换一个")
    if any(item["id"] == instance_id for item in current):
        errors.append(f"实例 ID「{instance_id}」已存在")

    qq = str(bot_qq or "").strip()
    if qq:
        if not qq.isdigit() or not (5 <= len(qq) <= 12):
            errors.append("bot_qq 必须是 5~12 位数字")
        elif any(item["bot_qq"] == qq for item in current):
            errors.append(f"QQ 号 {qq} 已被其他实例使用")

    port = _port_of(onebot_ws_url)
    if port and any(_port_of(item["onebot_ws_url"]) == port for item in current):
        errors.append(
            f"端口 {port} 已被其他实例占用；每个实例需要独立的 NapCat 端口"
        )

    return errors


def validate_update(instance_id: str, payload: dict, instances: list[dict] | None = None) -> list[str]:
    """修改已有实例时的校验（忽略自身）。"""
    current = instances if instances is not None else list_instances()
    others = [item for item in current if item["id"] != instance_id]
    target = next((item for item in current if item["id"] == instance_id), None)
    if target is None:
        return [f"实例不存在：{instance_id}"]

    errors: list[str] = []
    qq = str(payload.get("bot_qq", target["bot_qq"]) or "").strip()
    if qq and any(item["bot_qq"] == qq for item in others):
        errors.append(f"QQ 号 {qq} 已被其他实例使用")

    ws_url = str(payload.get("onebot_ws_url", target["onebot_ws_url"]) or "").strip()
    port = _port_of(ws_url)
    if port and any(_port_of(item["onebot_ws_url"]) == port for item in others):
        errors.append(f"端口 {port} 已被其他实例占用")

    name = str(payload.get("name", target["name"]) or "").strip()
    if not name:
        errors.append("实例名称不能为空")
    return errors


# --------------------------------------------------------------------------
# 实例配置（instances/<id>/config.json）
# --------------------------------------------------------------------------
def config_path_of(instance: dict) -> Path:
    cfg_dir = instance.get("config_dir") or f"instances/{instance['id']}"
    return ROOT / cfg_dir / "config.json" if cfg_dir != "." else ROOT / "config.json"


def data_path_of(instance: dict) -> Path:
    return ROOT / (instance.get("data_dir") or f"data/{instance['id']}")


def read_config(instance_id: str) -> dict:
    instance = require_instance(instance_id)
    return _read_json(config_path_of(instance), {})


def write_config(instance_id: str, payload: dict) -> None:
    """写实例配置。

    密钥字段（api_key 等）不落在这里 —— 它们由共享的 .env / config.secret.json 提供，
    避免每个实例各存一份导致轮换时漏改。
    """
    instance = require_instance(instance_id)
    path = config_path_of(instance)
    existing = _read_json(path, {})
    for key in ("api_key", "asr_api_key", "tts_api_key"):
        payload.pop(key, None)
    existing.update(payload)
    _write_json(path, existing)


# --------------------------------------------------------------------------
# 创建 / 更新 / 删除
# --------------------------------------------------------------------------
def create_instance(
    instance_id: str,
    name: str = "",
    bot_qq: str = "",
    persona: str = "",
    onebot_ws_url: str = "",
) -> dict:
    instance_id = (instance_id or "").strip()
    instances = list_instances()
    ws_url = (onebot_ws_url or "").strip() or FALLBACK_FIELDS["napcat_ws_url"]
    errors = validate_new(instance_id, bot_qq, ws_url, instances)
    if errors:
        raise InstanceError("；".join(errors))

    instance = _normalize(
        {
            "id": instance_id,
            "name": (name or instance_id).strip(),
            "bot_qq": bot_qq,
            "persona": persona or _inherit("persona"),
            "onebot_ws_url": ws_url,
            "created_at": int(time.time()),
        }
    )

    # 复制根配置作为模板，去掉密钥与运行期状态
    template = _read_json(ROOT / "config.json", {})
    for key in ("api_key", "allowed_users", "admin_users", "bot_qq"):
        template.pop(key, None)
    template["bot_qq"] = int(bot_qq) if str(bot_qq).isdigit() else 0
    template["persona"] = instance["persona"]
    template["napcat_ws_url"] = ws_url

    cfg_path = config_path_of(instance)
    if cfg_path.exists():
        raise InstanceError(f"实例配置已存在：{cfg_path}")
    _write_json(cfg_path, template)

    data_path_of(instance).mkdir(parents=True, exist_ok=True)

    instances.append(instance)
    save_instances(instances)
    return instance


def _inherit(field: str) -> str:
    """从 default 实例继承默认值。"""
    default = get_instance(DEFAULT_INSTANCE_ID)
    if default:
        return str(default.get(field) or FALLBACK_FIELDS.get(field, ""))
    return str(FALLBACK_FIELDS.get(field, ""))


def update_instance(instance_id: str, payload: dict) -> dict:
    instances = list_instances()
    errors = validate_update(instance_id, payload, instances)
    if errors:
        raise InstanceError("；".join(errors))

    for item in instances:
        if item["id"] != instance_id:
            continue
        for field in ("name", "persona", "onebot_ws_url", "enabled"):
            if field in payload:
                item[field] = payload[field] if field == "enabled" else str(payload[field]).strip()
        if "bot_qq" in payload:
            item["bot_qq"] = str(payload["bot_qq"] or "").strip()
        save_instances(instances)
        # 业务配置里也同步一份，保证 bot 启动时读到的是最新值
        sync = {}
        if "persona" in payload:
            sync["persona"] = item["persona"]
        if "onebot_ws_url" in payload:
            sync["napcat_ws_url"] = item["onebot_ws_url"]
        if "bot_qq" in payload and item["bot_qq"].isdigit():
            sync["bot_qq"] = int(item["bot_qq"])
        if sync:
            write_config(instance_id, sync)
        return item
    raise InstanceError(f"实例不存在：{instance_id}")


def delete_instance(instance_id: str, keep_backup: bool = True) -> dict:
    """删除实例。默认把配置与数据挪到 backups/instances/ 而不是直接删除。"""
    if instance_id == DEFAULT_INSTANCE_ID:
        raise InstanceError("default 实例不允许删除（它是单实例模式的兼容入口）")

    instances = list_instances()
    target = next((item for item in instances if item["id"] == instance_id), None)
    if target is None:
        raise InstanceError(f"实例不存在：{instance_id}")

    backup_path = ""
    if keep_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = BACKUP_DIR / f"{instance_id}-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)
        cfg_dir = ROOT / (target.get("config_dir") or f"instances/{instance_id}")
        if cfg_dir.exists() and cfg_dir != ROOT:
            shutil.move(str(cfg_dir), str(dest / "config"))
        data_dir = data_path_of(target)
        if data_dir.exists():
            shutil.move(str(data_dir), str(dest / "data"))
        backup_path = str(dest)

    save_instances([item for item in instances if item["id"] != instance_id])
    return {"id": instance_id, "backup": backup_path}
