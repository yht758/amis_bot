"""
日志写入模块 —— 分别记录提问、回答和运行状态
"""
import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3
_loggers: dict[str, logging.Logger] = {}


def _get_logger(filename: str) -> logging.Logger:
    if filename not in _loggers:
        logger = logging.getLogger(f"amis.{filename}")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        handler = RotatingFileHandler(
            LOG_DIR / filename,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        _loggers[filename] = logger

    return _loggers[filename]


def append_log(filename: str, text: str):
    """向指定日志追加一行，自动按大小轮转"""
    try:
        _get_logger(filename).info(text)
    except Exception:
        pass
