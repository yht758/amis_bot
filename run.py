"""
项目入口。

用法：
    python run.py                 # 启动
    python run.py --check-config  # 只校验配置

说明：源码位于 src/，这里把 src/ 与可选的便携依赖目录 libs/ 加入 sys.path，
以便直接 `python run.py` 运行，无需安装为包。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# 兼容旧的 Windows 便携版：第三方依赖装在 libs/ 目录
_LIBS = ROOT / "libs"
if _LIBS.is_dir():
    sys.path.insert(0, str(_LIBS))

from im_bridge.main import main  # noqa: E402  (必须在 sys.path 设置之后再导入)

if __name__ == "__main__":
    sys.exit(main())
