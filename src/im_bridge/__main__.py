"""包入口：支持 ``python -m im_bridge``。"""
from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
