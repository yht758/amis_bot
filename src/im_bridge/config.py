"""
配置加载与校验。

加载优先级（后者被前者覆盖）：
    环境变量 > .env 文件 > config.secret.json > config.json > 内置默认值

设计原则：
- 不在代码中硬编码任何密钥或个人账号；
- 所有阈值都给出可运行的默认值，缺省即可启动；
- 非法值不会让进程在导入期崩溃，而是收集到 ``Config.validate()`` 中统一提示。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 项目根目录：src/im_bridge/config.py -> 上溯三层
BASE_DIR = Path(__file__).resolve().parents[2]

DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

# ---------- 内置默认值（均为可公开的占位） ----------
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BOT_QQ = 123456789
DEFAULT_NAPCAT_WS_URL = "ws://127.0.0.1:3001"
DEFAULT_MAX_HISTORY = 40          # 上下文窗口：消息条数上限（约 20 轮对话）
DEFAULT_SUMMARY_THRESHOLD = 24    # 消息条数超过该值时触发摘要压缩
DEFAULT_SUMMARY_KEEP_RECENT = 8   # 压缩后保留的最近消息条数
DEFAULT_CHUNK_MIN_DELAY = 1.0     # 分片发送最小间隔（秒）
DEFAULT_CHUNK_MAX_DELAY = 2.0     # 分片发送最大间隔（秒）
DEFAULT_PERSONA = "aemeath"

API_KEY_PLACEHOLDER = "sk-your-api-key-here"

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}


class ConfigError(ValueError):
    """配置解析或校验失败。"""


def load_dotenv(path: Path, override: bool = False) -> None:
    """加载极简 .env 文件（不引入额外依赖）。

    支持 ``KEY=VALUE``、``# 注释``、``export KEY=VALUE`` 与成对引号。
    默认不覆盖真实环境变量。
    """
    if not path.exists():
        return
    try:
        lines = path.read_text("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return

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
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def _read_json(path: Path) -> dict:
    """读取 JSON 配置；文件不存在或损坏时返回空字典。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _as_bool(value: object, default: bool, field: str, errors: list[str]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
    errors.append(f"{field} 不是合法的布尔值：{value!r}（应为 true/false）")
    return default


def _as_int(value: object, default: int, field: str, errors: list[str]) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        errors.append(f"{field} 必须是整数：{value!r}")
        return default


def _as_float(value: object, default: float, field: str, errors: list[str]) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        errors.append(f"{field} 必须是数字：{value!r}")
        return default


def _as_id_list(value: object, field: str, errors: list[str]) -> list[str]:
    """把列表或逗号分隔字符串统一成字符串 ID 列表。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        errors.append(f"{field} 应为数组或逗号分隔字符串：{value!r}")
        return []

    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit():
            errors.append(f"{field} 中的 {text!r} 不是纯数字 QQ 号")
            continue
        result.append(text)
    return result


class Config:
    """运行期配置对象。"""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        bot_qq: int = DEFAULT_BOT_QQ,
        respond_to_groups: bool = False,
        allowed_users: list[str] | None = None,
        admin_users: list[str] | None = None,
        napcat_ws_url: str = DEFAULT_NAPCAT_WS_URL,
        max_history: int = DEFAULT_MAX_HISTORY,
        summary_enabled: bool = True,
        summary_threshold: int = DEFAULT_SUMMARY_THRESHOLD,
        summary_keep_recent: int = DEFAULT_SUMMARY_KEEP_RECENT,
        streaming_enabled: bool = True,
        chunking_enabled: bool = True,
        chunk_min_delay: float = DEFAULT_CHUNK_MIN_DELAY,
        chunk_max_delay: float = DEFAULT_CHUNK_MAX_DELAY,
        persona: str = DEFAULT_PERSONA,
        config_path: Path | None = None,
        load_errors: list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.llm_base_url = base_url
        self.model = model
        self.bot_qq = bot_qq
        self.respond_to_groups = respond_to_groups
        self.allowed_users = allowed_users or []
        self.admin_users = admin_users or []
        self.napcat_ws_url = napcat_ws_url
        self.max_history = max_history
        self.summary_enabled = summary_enabled
        self.summary_threshold = summary_threshold
        self.summary_keep_recent = summary_keep_recent
        self.streaming_enabled = streaming_enabled
        self.chunking_enabled = chunking_enabled
        self.chunk_min_delay = chunk_min_delay
        self.chunk_max_delay = chunk_max_delay
        self.persona = persona
        self.config_path = config_path or (BASE_DIR / "config.json")
        self.load_errors = load_errors or []

    # ---------- 运行期可变配置 ----------
    @property
    def masked_api_key(self) -> str:
        key = (self.api_key or "").strip()
        if not key:
            return "<未设置>"
        prefix = "sk-***" if key.startswith("sk-") else "***"
        return f"{prefix}（已配置，长度 {len(key)}）"

    def save(self) -> None:
        """把动态白名单/管理员列表写回 config.json（不落盘密钥）。"""
        path = self.config_path
        data = _read_json(path)
        data["allowed_users"] = self.allowed_users
        data["admin_users"] = self.admin_users
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4) + "\n", "utf-8"
            )
        except OSError:
            pass

    def add_allowed_user(self, user_id: str) -> bool:
        user_id = str(user_id).strip()
        if not user_id or user_id in self.allowed_users:
            return False
        self.allowed_users.append(user_id)
        self.save()
        return True

    def remove_allowed_user(self, user_id: str) -> bool:
        user_id = str(user_id).strip()
        if user_id not in self.allowed_users:
            return False
        self.allowed_users.remove(user_id)
        self.save()
        return True

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admin_users

    def is_allowed(self, user_id: str) -> bool:
        """白名单为空表示不限制；管理员始终允许。"""
        if not self.allowed_users:
            return True
        return str(user_id) in self.allowed_users or self.is_admin(user_id)

    # ---------- 校验 ----------
    def validate(self) -> list[str]:
        """返回配置错误列表，空列表表示通过。"""
        errors = list(self.load_errors)

        if not self.api_key.strip():
            errors.append(
                "缺少 LLM API Key：请设置环境变量 AMIS_API_KEY，"
                "或写入 .env / config.secret.json"
            )
        elif self.api_key.strip() == API_KEY_PLACEHOLDER:
            errors.append(
                "AMIS_API_KEY 仍是占位符 sk-your-api-key-here，请替换为真实密钥"
            )

        if not self.llm_base_url.startswith(("http://", "https://")):
            errors.append(
                f"AMIS_BASE_URL 必须以 http:// 或 https:// 开头：{self.llm_base_url!r}"
            )
        if not self.model.strip():
            errors.append("AMIS_MODEL 不能为空")
        if self.bot_qq <= 0:
            errors.append(f"AMIS_BOT_QQ 必须是正整数：{self.bot_qq!r}")
        if self.max_history < 4:
            errors.append(f"AMIS_MAX_HISTORY 不能小于 4：{self.max_history!r}")
        if self.summary_keep_recent < 2:
            errors.append(
                f"AMIS_SUMMARY_KEEP_RECENT 不能小于 2：{self.summary_keep_recent!r}"
            )
        if self.summary_enabled and self.summary_threshold <= self.summary_keep_recent:
            errors.append(
                "AMIS_SUMMARY_THRESHOLD 必须大于 AMIS_SUMMARY_KEEP_RECENT "
                f"（当前 {self.summary_threshold} <= {self.summary_keep_recent}）"
            )
        if self.summary_enabled and self.summary_threshold >= self.max_history:
            errors.append(
                "AMIS_SUMMARY_THRESHOLD 必须小于 AMIS_MAX_HISTORY，否则窗口会先裁剪、"
                f"摘要永不触发（当前 {self.summary_threshold} >= {self.max_history}）"
            )
        if self.chunk_min_delay < 0 or self.chunk_max_delay < 0:
            errors.append("分片延迟 AMIS_CHUNK_MIN_DELAY / AMIS_CHUNK_MAX_DELAY 不能为负数")
        if self.chunk_min_delay > self.chunk_max_delay:
            errors.append(
                "AMIS_CHUNK_MIN_DELAY 不能大于 AMIS_CHUNK_MAX_DELAY "
                f"（当前 {self.chunk_min_delay} > {self.chunk_max_delay}）"
            )
        if not self.persona.strip():
            errors.append("AMIS_PERSONA 不能为空（可使用 aemeath 或人设文件路径）")

        return errors

    def summary(self) -> str:
        """用于启动日志和 --check-config 的脱敏摘要。"""
        return (
            f"base_url={self.llm_base_url} model={self.model} "
            f"bot_qq={self.bot_qq} api_key={self.masked_api_key}\n"
            f"max_history={self.max_history} "
            f"summary={self.summary_enabled}"
            f"({self.summary_threshold}/{self.summary_keep_recent}) "
            f"streaming={self.streaming_enabled} chunking={self.chunking_enabled}"
            f"({self.chunk_min_delay}~{self.chunk_max_delay}s) persona={self.persona}"
        )

    # ---------- 构建 ----------
    @classmethod
    def from_env_or_file(cls, config_path: Path | str | None = None) -> "Config":
        path = Path(config_path).expanduser() if config_path else (BASE_DIR / "config.json")
        base_dir = path.resolve().parent
        load_dotenv(base_dir / ".env")

        data = _read_json(path)
        secret = _read_json(base_dir / "config.secret.json")
        errors: list[str] = []

        def pick(env_name: str, key: str, default: object) -> object:
            env_value = os.environ.get(env_name)
            if env_value is not None and env_value.strip() != "":
                return env_value
            if key in data and data[key] not in (None, ""):
                return data[key]
            return default

        api_key = str(
            os.environ.get("AMIS_API_KEY")
            or secret.get("api_key")
            or data.get("api_key")
            or ""
        ).strip()

        bot_qq = _as_int(pick("AMIS_BOT_QQ", "bot_qq", DEFAULT_BOT_QQ), DEFAULT_BOT_QQ, "AMIS_BOT_QQ/bot_qq", errors)

        return cls(
            api_key=api_key,
            base_url=str(pick("AMIS_BASE_URL", "base_url", DEFAULT_BASE_URL)).strip(),
            model=str(pick("AMIS_MODEL", "model", DEFAULT_MODEL)).strip(),
            bot_qq=bot_qq,
            respond_to_groups=_as_bool(
                pick("AMIS_RESPOND_TO_GROUPS", "respond_to_groups", False),
                False,
                "AMIS_RESPOND_TO_GROUPS/respond_to_groups",
                errors,
            ),
            allowed_users=_as_id_list(
                pick("AMIS_ALLOWED_USERS", "allowed_users", []),
                "AMIS_ALLOWED_USERS/allowed_users",
                errors,
            ),
            admin_users=_as_id_list(
                pick("AMIS_ADMIN_USERS", "admin_users", []),
                "AMIS_ADMIN_USERS/admin_users",
                errors,
            ),
            napcat_ws_url=str(
                pick("AMIS_NAPCAT_WS_URL", "napcat_ws_url", DEFAULT_NAPCAT_WS_URL)
            ).strip(),
            max_history=_as_int(
                pick("AMIS_MAX_HISTORY", "max_history", DEFAULT_MAX_HISTORY),
                DEFAULT_MAX_HISTORY,
                "AMIS_MAX_HISTORY/max_history",
                errors,
            ),
            summary_enabled=_as_bool(
                pick("AMIS_SUMMARY_ENABLED", "summary_enabled", True),
                True,
                "AMIS_SUMMARY_ENABLED/summary_enabled",
                errors,
            ),
            summary_threshold=_as_int(
                pick("AMIS_SUMMARY_THRESHOLD", "summary_threshold", DEFAULT_SUMMARY_THRESHOLD),
                DEFAULT_SUMMARY_THRESHOLD,
                "AMIS_SUMMARY_THRESHOLD/summary_threshold",
                errors,
            ),
            summary_keep_recent=_as_int(
                pick(
                    "AMIS_SUMMARY_KEEP_RECENT",
                    "summary_keep_recent",
                    DEFAULT_SUMMARY_KEEP_RECENT,
                ),
                DEFAULT_SUMMARY_KEEP_RECENT,
                "AMIS_SUMMARY_KEEP_RECENT/summary_keep_recent",
                errors,
            ),
            streaming_enabled=_as_bool(
                pick("AMIS_STREAMING_ENABLED", "streaming_enabled", True),
                True,
                "AMIS_STREAMING_ENABLED/streaming_enabled",
                errors,
            ),
            chunking_enabled=_as_bool(
                pick("AMIS_CHUNKING_ENABLED", "chunking_enabled", True),
                True,
                "AMIS_CHUNKING_ENABLED/chunking_enabled",
                errors,
            ),
            chunk_min_delay=_as_float(
                pick("AMIS_CHUNK_MIN_DELAY", "chunk_min_delay", DEFAULT_CHUNK_MIN_DELAY),
                DEFAULT_CHUNK_MIN_DELAY,
                "AMIS_CHUNK_MIN_DELAY/chunk_min_delay",
                errors,
            ),
            chunk_max_delay=_as_float(
                pick("AMIS_CHUNK_MAX_DELAY", "chunk_max_delay", DEFAULT_CHUNK_MAX_DELAY),
                DEFAULT_CHUNK_MAX_DELAY,
                "AMIS_CHUNK_MAX_DELAY/chunk_max_delay",
                errors,
            ),
            persona=str(pick("AMIS_PERSONA", "persona", DEFAULT_PERSONA)).strip(),
            config_path=path,
            load_errors=errors,
        )
