"""
爱弥斯 QQ 机器人 - 项目根启动脚本
用法: python run.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "libs"))

from 爱弥斯.main import main

if __name__ == "__main__":
    main()
