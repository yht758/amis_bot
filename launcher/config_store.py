"""管理台配置存储：读写项目 .env / config.json，并复用核心校验逻辑。

设计原则：
- 密钥（api_key）只写 `.env`，不落到 config.json；
- 其余运行配置写入 `config.json`（与 bot 的动态黑白名单写回同一文件），
  同时从 `.env` 中清理同名 AMIS_* 覆盖项，避免“环境变量优先级”让管理台
  的修改不生效；
- 读取优先级与核心一致：进程环境变量 > .env > config.secret.json > config.json > 默认值。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def get_root() -> Path:
    """项目根目录：开发时是 launcher/ 的上级；打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = get_root()
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.json"
SECRET_PATH = ROOT / "config.secret.json"
SETTINGS_PATH = ROOT / "data" / "launcher_settings.json"

APP_NAME = "im-bridge-manager"
APP_VERSION = "1.0.0"

DEFAULTS: dict[str, Any] = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "bot_qq": 123456789,
    "respond_to_groups": False,
    "allowed_users": [],
    "admin_users": [],
    "napcat_ws_url": "ws://127.0.0.1:3001",
    "max_history": 40,
    "summary_enabled": True,
    "summary_threshold": 24,
    "summary_keep_recent": 8,
    "streaming_enabled": True,
    "chunking_enabled": True,
    "chunk_min_delay": 1.0,
    "chunk_max_delay": 2.0,
    "persona": "aemeath",
}

# key, 环境变量名, config.json 字段名, 类型
SCHEMA: list[dict[str, Any]] = [
    {"key": "api_key", "env": "AMIS_API_KEY", "json": None, "type": "secret"},
    {"key": "base_url", "env": "AMIS_BASE_URL", "json": "base_url", "type": "str"},
    {"key": "model", "env": "AMIS_MODEL", "json": "model", "type": "str"},
    {"key": "bot_qq", "env": "AMIS_BOT_QQ", "json": "bot_qq", "type": "int"},
    {"key": "respond_to_groups", "env": "AMIS_RESPOND_TO_GROUPS", "json": "respond_to_groups", "type": "bool"},
    {"key": "allowed_users", "env": "AMIS_ALLOWED_USERS", "json": "allowed_users", "type": "list"},
    {"key": "admin_users", "env": "AMIS_ADMIN_USERS", "json": "admin_users", "type": "list"},
    {"key": "napcat_ws_url", "env": "AMIS_NAPCAT_WS_URL", "json": "napcat_ws_url", "type": "str"},
    {"key": "max_history", "env": "AMIS_MAX_HISTORY", "json": "max_history", "type": "int"},
    {"key": "summary_enabled", "env": "AMIS_SUMMARY_ENABLED", "json": "summary_enabled", "type": "bool"},
    {"key": "summary_threshold", "env": "AMIS_SUMMARY_THRESHOLD", "json": "summary_threshold", "type": "int"},
    {"key": "summary_keep_recent", "env": "AMIS_SUMMARY_KEEP_RECENT", "json": "summary_keep_recent", "type": "int"},
    {"key": "streaming_enabled", "env": "AMIS_STREAMING_ENABLED", "json": "streaming_enabled", "type": "bool"},
    {"key": "chunking_enabled", "env": "AMIS_CHUNKING_ENABLED", "json": "chunking_enabled", "type": "bool"},
    {"key": "chunk_min_delay", "env": "AMIS_CHUNK_MIN_DELAY", "json": "chunk_min_delay", "type": "float"},
    {"key": "chunk_max_delay", "env": "AMIS_CHUNK_MAX_DELAY", "json": "chunk_max_delay", "type": "float"},
    {"key": "persona", "env": "AMIS_PERSONA", "json": "persona", "type": "str"},
]

_SCHEMA_BY_KEY = {spec["key"]: spec for spec in SCHEMA}
_NON_SECRET_ENVS = {spec["env"] for spec in SCHEMA if spec["key"] != "api_key"}
_TRUE_SET = {"1", "true", "yes", "on", "y", "t"}
_FALSE_SET = {"0", "false", "no", "off", "n", "f"}


class ConfigError(Exception):
    """配置读取/保存失败（消息可直接展示给用户）。"""


# --------------------------------------------------------------------------
# 基础 IO
# --------------------------------------------------------------------------
def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", "utf-8")


def parse_env(path: Path) -> dict[str, str]:
    """解析 .env（支持注释、export、成对引号），返回原始键值。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return values

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _quote_env_value(value: str) -> str:
    if value == "" or re.search(r"[\s#\"']", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_file(updates: dict[str, str], remove_keys: set[str] | None = None) -> None:
    """增量更新 .env：保留注释与未涉及的行；remove_keys 中的键会被删除。"""
    remove_keys = remove_keys or set()
    lines: list[str] = []
    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text("utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []

    pending = dict(updates)
    result: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if not match:
            result.append(raw)
            continue
        key = match.group(1)
        if key in remove_keys:
            continue
        if key in pending:
            result.append(f"{key}={_quote_env_value(str(pending.pop(key)))}")
        else:
            result.append(raw)

    if pending:
        if result and result[-1].strip():
            result.append("")
        result.append("# ---- 由管理台写入 ----")
        for key, value in pending.items():
            result.append(f"{key}={_quote_env_value(str(value))}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(result).rstrip() + "\n", "utf-8")


# --------------------------------------------------------------------------
# 类型转换
# --------------------------------------------------------------------------
def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_SET:
            return True
        if text in _FALSE_SET:
            return False
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def coerce(spec: dict[str, Any], value: Any) -> Any:
    kind = spec["type"]
    if kind == "int":
        return _to_int(value, DEFAULTS[spec["key"]])
    if kind == "float":
        return _to_float(value, DEFAULTS[spec["key"]])
    if kind == "bool":
        return _to_bool(value, DEFAULTS[spec["key"]])
    if kind == "list":
        return _to_list(value)
    return str(value).strip() if value is not None else ""


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def _mask_key(value: str) -> str:
    if not value:
        return ""
    prefix = "sk-***" if value.startswith("sk-") else "***"
    return f"{prefix}（已配置，长度 {len(value)}）"


def load_config() -> dict[str, Any]:
    """返回 {"fields": {key: {value, from}}, "api_key": {...}, "errors": [...]}"""
    env_file = parse_env(ENV_PATH)
    config_json = read_json_file(CONFIG_PATH)
    secret_json = read_json_file(SECRET_PATH)
    errors: list[str] = []
    fields: dict[str, dict[str, Any]] = {}

    for spec in SCHEMA:
        key = spec["key"]
        if key == "api_key":
            value = ""
            source = "default"
            for candidate, label in (
                (os.environ.get(spec["env"]), "环境变量"),
                (env_file.get(spec["env"]), ".env"),
                (secret_json.get("api_key"), "config.secret.json"),
                (config_json.get("api_key"), "config.json"),
            ):
                if candidate:
                    value = str(candidate).strip()
                    source = label
                    break
            fields[key] = {
                "value": "",
                "set": bool(value),
                "masked": _mask_key(value),
                "from": source,
            }
            continue

        value = None
        source = "default"
        if spec["env"] in os.environ and os.environ[spec["env"]].strip() != "":
            value, source = os.environ[spec["env"]], "环境变量"
        elif spec["env"] in env_file and env_file[spec["env"]].strip() != "":
            value, source = env_file[spec["env"]], ".env"
        elif spec["json"] and config_json.get(spec["json"]) not in (None, "", []):
            value, source = config_json[spec["json"]], "config.json"
        if value is None:
            value = DEFAULTS[key]
        coerced = coerce(spec, value)
        fields[key] = {"value": coerced, "from": source}

    return {"fields": fields, "errors": errors}


def get_api_key() -> str:
    data = load_config()
    # 需要明文用于测试与校验：单独读原始值
    env_file = parse_env(ENV_PATH)
    secret_json = read_json_file(SECRET_PATH)
    config_json = read_json_file(CONFIG_PATH)
    for candidate in (
        os.environ.get("AMIS_API_KEY"),
        env_file.get("AMIS_API_KEY"),
        secret_json.get("api_key"),
        config_json.get("api_key"),
    ):
        if candidate:
            return str(candidate).strip()
    return ""


def resolved_values() -> dict[str, Any]:
    """返回全部字段的当前生效值（含明文 api_key），用于校验与测试。"""
    data = load_config()
    values = {key: item["value"] for key, item in data["fields"].items()}
    values["api_key"] = get_api_key()
    return values


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def _core_config_class():
    """尝试复用核心 Config 类；失败时返回 None（例如源码目录缺失）。"""
    try:
        src = ROOT / "src"
        if src.is_dir() and str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from im_bridge.config import Config  # type: ignore

        return Config
    except Exception:  # noqa: BLE001
        return None


def validate_values(values: dict[str, Any]) -> list[str]:
    """复用核心校验；核心不可用时做基础校验。"""
    api_key = str(values.get("api_key") or "").strip()
    errors: list[str] = []
    if not api_key:
        errors.append("缺少 LLM API Key：请填写 AMIS_API_KEY")
    elif api_key == "sk-your-api-key-here":
        errors.append("API Key 仍是占位符，请填写真实密钥")

    base_url = str(values.get("base_url") or "")
    if not base_url.startswith(("http://", "https://")):
        errors.append(f"Base URL 必须以 http:// 或 https:// 开头：{base_url!r}")

    try:
        max_history = int(values.get("max_history", 0))
        threshold = int(values.get("summary_threshold", 0))
        keep = int(values.get("summary_keep_recent", 0))
        if max_history < 4:
            errors.append("上下文窗口不能小于 4")
        if keep < 2:
            errors.append("摘要保留条数不能小于 2")
        if values.get("summary_enabled", True):
            if threshold <= keep:
                errors.append("摘要阈值必须大于保留条数")
            if threshold >= max_history:
                errors.append("摘要阈值必须小于上下文窗口")
    except (TypeError, ValueError):
        errors.append("上下文参数必须是整数")

    try:
        low = float(values.get("chunk_min_delay", 0))
        high = float(values.get("chunk_max_delay", 0))
        if low < 0 or high < 0:
            errors.append("分片延迟不能为负数")
        if low > high:
            errors.append("分片最小延迟不能大于最大延迟")
    except (TypeError, ValueError):
        errors.append("分片延迟必须是数字")

    if not str(values.get("model") or "").strip():
        errors.append("模型名称不能为空")

    core = _core_config_class()
    if core is not None:
        try:
            config = core(
                api_key=api_key or "placeholder",
                base_url=base_url,
                model=str(values.get("model") or ""),
                bot_qq=int(values.get("bot_qq") or 0),
                respond_to_groups=bool(values.get("respond_to_groups")),
                allowed_users=list(values.get("allowed_users") or []),
                admin_users=list(values.get("admin_users") or []),
                napcat_ws_url=str(values.get("napcat_ws_url") or ""),
                max_history=max_history,
                summary_enabled=bool(values.get("summary_enabled", True)),
                summary_threshold=threshold,
                summary_keep_recent=keep,
                streaming_enabled=bool(values.get("streaming_enabled", True)),
                chunking_enabled=bool(values.get("chunking_enabled", True)),
                chunk_min_delay=low,
                chunk_max_delay=high,
                persona=str(values.get("persona") or ""),
            )
            for error in config.validate():
                if error not in errors:
                    errors.append(error)
        except Exception:  # noqa: BLE001 - 核心校验异常不影响管理台基础校验
            pass
    return errors


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------
def save_config(payload: dict[str, Any], current_values: dict[str, Any] | None = None) -> dict[str, Any]:
    """保存配置。api_key 为空表示沿用原值。

    返回 {"warnings": [...]}；校验失败抛 ConfigError（消息可展示）。
    """
    current = current_values or resolved_values()
    merged = dict(current)
    for spec in SCHEMA:
        key = spec["key"]
        if key in payload:
            merged[key] = payload[key]

    new_key = str(payload.get("api_key") or "").strip()
    if new_key:
        merged["api_key"] = new_key
    else:
        merged["api_key"] = current.get("api_key", "")

    errors = validate_values(merged)
    # api_key 为空只是“不修改”，不算错误；但原值也不存在时要拦截
    if not merged.get("api_key"):
        errors = [e for e in errors if "API Key" not in e]
        errors.append("API Key 未配置：请输入密钥后再保存")

    if errors:
        raise ConfigError("\n".join(errors))

    # 1) 密钥写 .env
    env_updates: dict[str, str] = {}
    env_remove: set[str] = set(_NON_SECRET_ENVS)
    if new_key:
        env_updates["AMIS_API_KEY"] = new_key
    update_env_file(env_updates, env_remove)

    # 2) 其余配置写 config.json，保留未知字段
    config_json = read_json_file(CONFIG_PATH)
    warnings: list[str] = []
    for spec in SCHEMA:
        key = spec["key"]
        if spec["json"] is None or key not in payload:
            continue
        value = coerce(spec, payload[key])
        config_json[spec["json"]] = value
    write_json_file(CONFIG_PATH, config_json)

    if os.environ.get("AMIS_API_KEY") and new_key and os.environ["AMIS_API_KEY"] != new_key:
        warnings.append("当前进程环境变量 AMIS_API_KEY 会覆盖 .env 中的新密钥，建议重启管理台或清除该环境变量")

    return {"warnings": warnings}


# --------------------------------------------------------------------------
# 管理台自身设置
# --------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    "napcat_command": "",
    "auto_restart": False,
    "auto_open": True,
    "port": 8765,
    "theme": "dark",
}


def load_settings() -> dict[str, Any]:
    data = read_json_file(SETTINGS_PATH)
    settings = dict(DEFAULT_SETTINGS)
    settings.update({key: data[key] for key in DEFAULT_SETTINGS if key in data})
    return settings


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    for key in DEFAULT_SETTINGS:
        if key in payload:
            settings[key] = payload[key]
    write_json_file(SETTINGS_PATH, settings)
    return settings


def find_python() -> str:
    """优先使用项目便携版 python，其次 venv；打包后不再返回自身（exe 不是解释器）。"""
    import shutil

    candidates = [
        ROOT / "python" / "python.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    if getattr(sys, "frozen", False):
        for name in ("python", "python3", "py"):
            found = shutil.which(name)
            if found:
                return found
        return ""
    return sys.executable
