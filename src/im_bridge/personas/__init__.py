"""
人设（Persona）注册表。

内置人设只作为示例；生产环境可通过 ``AMIS_PERSONA`` 指向自定义提示词文件
（.txt / .md），从而把“角色”与“桥接器”彻底解耦。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .aemeath import SYSTEM_PROMPT as AEMEATH_SYSTEM_PROMPT

# 内置人设注册表。新增人设时在此登记即可。
BUILTIN_PERSONAS: dict[str, str] = {
    "aemeath": AEMEATH_SYSTEM_PROMPT,
}


@dataclass(frozen=True)
class Persona:
    """一个可注入 LLM 的人设。"""

    name: str
    system_prompt: str
    display_name: str = ""


def list_personas() -> list[str]:
    return sorted(BUILTIN_PERSONAS)


def load_persona(name: str) -> Persona:
    """按名称或文件路径加载人设。

    - ``aemeath``：内置示例人设；
    - 其它字符串：视为提示词文件路径（.txt / .md），内容作为 system prompt。
    """
    key = (name or "").strip()
    if not key:
        key = "aemeath"

    if key in BUILTIN_PERSONAS:
        return Persona(name=key, system_prompt=BUILTIN_PERSONAS[key], display_name="示例人设")

    path = Path(key).expanduser()
    if not path.is_absolute():
        # 相对路径按当前工作目录解析；仍找不到则报错
        path = Path.cwd() / path
    if path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError(
            f"人设文件仅支持 .txt / .md 文本：{path}；"
            f"内置人设：{', '.join(list_personas())}"
        )
    if not path.exists():
        raise FileNotFoundError(
            f"找不到人设 {key!r}：既不是内置人设（{', '.join(list_personas())}），"
            f"也不是已存在的提示词文件路径"
        )

    text = path.read_text("utf-8-sig").strip()
    if not text:
        raise ValueError(f"人设文件为空：{path}")
    return Persona(name=path.stem, system_prompt=text, display_name=path.stem)
