"""
日志写入模块 —— 分别记录提问、回答和运行状态。

每类日志独立轮转，避免单文件无限增长；写入失败不影响主流程。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3
LOG_FILES = ("status.log", "questions.log", "answers.log")

_loggers: dict[str, logging.Logger] = {}


def _get_logger(filename: str) -> logging.Logger:
    if filename not in _loggers:
        logger = logging.getLogger(f"im_bridge.{filename}")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        LOG_DIR.mkdir(parents=True, exist_ok=True)
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


def append_log(filename: str, text: str) -> None:
    """向指定日志追加一行，自动按大小轮转；失败静默。"""
    try:
        _get_logger(filename).info(text)
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响业务
        pass
