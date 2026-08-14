"""
爱弥斯 QQ 机器人 - 配置文件
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent   # D:\amis_bot

# ---------- NapCatQQ 连接 ----------
NAPCAT_WS_URL = "ws://127.0.0.1:3001"
NAPCAT_HTTP_URL = "http://127.0.0.1:3000"

# ---------- LLM API ----------
LLM_API_KEY = "sk-your-api-key-here"
LLM_BASE_URL = "https://api.openai.com/v1"
LLM_MODEL = "gpt-4o"

# ---------- 机器人行为 ----------
BOT_QQ = 123456789
RESPOND_TO_GROUPS = False
ALLOWED_USERS: list[str] = []
ADMIN_USERS: list[str] = []
MAX_HISTORY = 20

# ---------- 数据与日志存储 (D盘) ----------
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
for _log_name in ("status.log", "questions.log", "answers.log"):
    (LOG_DIR / _log_name).touch(exist_ok=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return {}


def _csv_to_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Config:
    def __init__(
        self,
        api_key: str = LLM_API_KEY,
        base_url: str = LLM_BASE_URL,
        model: str = LLM_MODEL,
        bot_qq: int = BOT_QQ,
        respond_to_groups: bool = RESPOND_TO_GROUPS,
        allowed_users: list[str] | None = None,
        admin_users: list[str] | None = None,
    ):
        self.api_key = api_key
        self.llm_base_url = base_url
        self.model = model
        self.bot_qq = bot_qq
        self.respond_to_groups = respond_to_groups
        self.allowed_users = allowed_users or []
        self.admin_users = admin_users or []
        self.napcat_ws_url = NAPCAT_WS_URL
        self.napcat_http_url = NAPCAT_HTTP_URL

    def save(self):
        """保存白名单和管理员列表到 config.json"""
        path = BASE_DIR / "config.json"
        data = _read_json(path)
        data["allowed_users"] = self.allowed_users
        data["admin_users"] = self.admin_users
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4), "utf-8")

    def add_allowed_user(self, user_id: str):
        user_id = str(user_id)
        if user_id not in self.allowed_users:
            self.allowed_users.append(user_id)
            self.save()

    def remove_allowed_user(self, user_id: str):
        user_id = str(user_id)
        if user_id in self.allowed_users:
            self.allowed_users.remove(user_id)
            self.save()

    @classmethod
    def from_env_or_file(cls, config_path: Path | None = None) -> "Config":
        path = config_path or (BASE_DIR / "config.json")
        data = _read_json(path)
        secret = _read_json(BASE_DIR / "config.secret.json")

        return cls(
            api_key=os.getenv("AMIS_API_KEY") or secret.get("api_key") or data.get("api_key") or LLM_API_KEY,
            base_url=os.getenv("AMIS_BASE_URL") or data.get("base_url") or LLM_BASE_URL,
            model=os.getenv("AMIS_MODEL") or data.get("model") or LLM_MODEL,
            bot_qq=int(data.get("bot_qq", BOT_QQ)),
            respond_to_groups=bool(data.get("respond_to_groups", RESPOND_TO_GROUPS)),
            allowed_users=_csv_to_list(os.getenv("AMIS_ALLOWED_USERS")) or data.get("allowed_users", ALLOWED_USERS),
            admin_users=_csv_to_list(os.getenv("AMIS_ADMIN_USERS")) or data.get("admin_users", ADMIN_USERS),
        )
