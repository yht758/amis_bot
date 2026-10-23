"""
配置字段表一致性测试（离线，不依赖网络）。

守护四件事：
1. ``.env.example`` 里不出现字段表没登记的 ``AMIS_*`` 键（防止文档乱加）；
2. 字段表里每个字段都在 ``.env.example`` 有说明（防止新增字段忘记写文档）；
3. 字段表指向的 ``Config`` 属性真实存在（防止改名后字段表失效）；
4. 密钥类字段的展示值不泄露原文。

运行：python tests/test_config_specs.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libs"))

from im_bridge.config import (  # noqa: E402
    BASE_DIR,
    FIELD_BY_ENV,
    FIELD_SPECS,
    Config,
)

ENV_EXAMPLE = BASE_DIR / ".env.example"


def env_example_keys() -> set[str]:
    """解析 .env.example 里声明的键（忽略注释与空行）。"""
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text("utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        keys.add(text.split("=", 1)[0].strip())
    return keys


class ConfigSpecTest(unittest.TestCase):
    def test_env_example_exists(self) -> None:
        self.assertTrue(ENV_EXAMPLE.is_file(), f"缺少 {ENV_EXAMPLE}")

    def test_no_unknown_keys_in_env_example(self) -> None:
        """文档里不能出现字段表之外的 AMIS_* 键。"""
        declared = env_example_keys()
        unknown = {
            key for key in declared if key.startswith("AMIS_") and key not in FIELD_BY_ENV
        }
        self.assertFalse(
            unknown,
            f".env.example 存在未登记的键：{sorted(unknown)}，请在 FIELD_SPECS 中补上",
        )

    def test_every_field_is_documented(self) -> None:
        """字段表里的字段必须都在 .env.example 里出现。"""
        declared = env_example_keys()
        missing = [spec.env for spec in FIELD_SPECS if spec.env not in declared]
        self.assertFalse(
            missing, f".env.example 缺少这些字段的说明：{missing}"
        )

    def test_attrs_exist_on_config(self) -> None:
        """字段表指向的属性必须真实存在。"""
        config = Config(api_key="x")
        for spec in FIELD_SPECS:
            self.assertTrue(
                hasattr(config, spec.attr),
                f"{spec.env} 指向的属性 {spec.attr} 在 Config 上不存在",
            )

    def test_required_fields_have_hint(self) -> None:
        """必填字段必须给出修复建议，否则报错时用户不知道怎么办。"""
        for spec in FIELD_SPECS:
            if spec.required:
                self.assertTrue(spec.hint, f"{spec.env} 是必填项但没有 hint")

    def test_secret_never_leaks_plaintext(self) -> None:
        """密钥原文不能出现在任何展示文本里。"""
        secret = "sk-super-secret-value-123456"
        config = Config(api_key=secret)
        self.assertNotIn("super-secret", config.masked_api_key)
        for issue in config.validate_issues():
            self.assertNotIn(secret, str(issue))

    def test_default_config_passes_validation(self) -> None:
        """填好 Key 之后，全默认值应当能通过校验。"""
        config = Config(api_key="sk-test-key")
        self.assertEqual(config.validate(), [], "默认配置不应报错")


if __name__ == "__main__":
    unittest.main(verbosity=2)
