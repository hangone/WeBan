import logging
import os
import re
import sys
import time


_LOG_FORMAT = "%(asctime)s|%(levelname)-7s|%(account)s|%(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


class AccountFilter(logging.Filter):
    """确保日志 record 始终带有 account 字段。"""

    def filter(self, record):
        if not hasattr(record, "account"):
            record.account = "系统"
        return True


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)


def setup_logger(base_path: str, debug: bool = False) -> logging.LoggerAdapter:
    """初始化全局日志（控制台 + logs/weban.log）。"""
    formatter = _make_formatter()
    account_filter = AccountFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(account_filter)

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(console_handler)

    logs_dir = os.path.join(base_path, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(logs_dir, "weban.log"), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(account_filter)
    root.addHandler(file_handler)

    logger = logging.LoggerAdapter(root, {"account": "系统"})
    if debug:
        logger.debug(
            "调试模式已开启，详尽日志将输出到控制台和 logs/weban.log，"
            "验证码截图保存到 logs/{username}/。"
        )
    return logger


def setup_account_file_handler(
    base_path: str, account: str, debug: bool = False
) -> logging.FileHandler:
    """为指定账号创建独立日志文件 logs/{account}/weban_{ts}.log。

    调用方负责在任务结束后 handler.close() 并从 root logger 移除。
    """
    safe_account = _SAFE_NAME_RE.sub("_", account) or "unknown"
    log_dir = os.path.join(base_path, "logs", safe_account)
    os.makedirs(log_dir, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    handler = logging.FileHandler(
        os.path.join(log_dir, f"weban_{ts}.log"), encoding="utf-8"
    )
    handler.setFormatter(_make_formatter())
    handler.addFilter(AccountFilter())
    handler.setLevel(logging.DEBUG if debug else logging.INFO)

    logging.getLogger().addHandler(handler)
    return handler
