import os
import re
import sys
import time
from typing import Any
from loguru import logger

_LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>|<level>{level: <7}</level>|<cyan>{extra[account]}</cyan>|<level>{message}</level>"
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def setup_logger(base_path: str, debug: bool = False) -> Any:
    """初始化全局日志（控制台 + logs/weban.log）。"""
    logger.remove()
    logger.configure(extra={"account": "系统"})
    level = "DEBUG" if debug else "INFO"

    logger.add(sys.stdout, format=_LOG_FORMAT, level=level, enqueue=True)

    logs_dir = os.path.join(base_path, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    logger.add(
        os.path.join(logs_dir, "weban.log"),
        format="{time:YYYY-MM-DD HH:mm:ss}|{level: <7}|{extra[account]}|{message}",
        level=level,
        encoding="utf-8",
        enqueue=True,
    )

    if debug:
        logger.debug(
            "调试模式已开启，详尽日志将输出到控制台和 logs/weban.log，"
            "验证码截图保存到 logs/{username}/。"
        )
    return logger


def setup_account_file_handler(
    base_path: str, account: str, debug: bool = False
) -> int:
    """为指定账号创建独立日志文件 logs/{account}/weban_{ts}.log。

    返回 handler_id，用于在任务结束后 logger.remove(handler_id)。
    """
    safe_account = _SAFE_NAME_RE.sub("_", account) or "unknown"
    log_dir = os.path.join(base_path, "logs", safe_account)
    os.makedirs(log_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    level = "DEBUG" if debug else "INFO"

    handler_id = logger.add(
        os.path.join(log_dir, f"weban_{ts}.log"),
        format="{time:YYYY-MM-DD HH:mm:ss}|{level: <7}|{extra[account]}|{message}",
        filter=lambda record: record["extra"].get("account") == account,
        level=level,
        encoding="utf-8",
        enqueue=True,
    )
    return handler_id
