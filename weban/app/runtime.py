"""应用运行时工具。

职责：
- 解析程序运行根目录，兼容源码运行与 PyInstaller 打包运行
- 确保 Playwright Chromium 浏览器可用
- 提供配置值到布尔值的安全转换
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import Any, Protocol


class LoggerLike(Protocol):
    """兼容 logging.Logger / logging.LoggerAdapter 的最小日志接口。"""

    def info(self, msg: str, *args: Any, **kwargs: Any) -> Any: ...
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> Any: ...
    def debug(self, msg: str, *args: Any, **kwargs: Any) -> Any: ...


def get_base_path() -> str:
    """获取程序运行根目录，兼容 PyInstaller 打包环境。"""
    if getattr(sys, "frozen", False):
        base_path = os.path.dirname(sys.executable)
        meipass = getattr(sys, "_MEIPASS", base_path)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(meipass, "pw-browsers")
        return base_path

    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ensure_playwright_browsers(logger: LoggerLike) -> None:
    """确保 Playwright 的 Chromium 浏览器已安装。"""
    try:
        driver_module = importlib.import_module("playwright._impl._driver")
        compute_driver_executable = getattr(driver_module, "compute_driver_executable")
        get_driver_env = getattr(driver_module, "get_driver_env")
    except Exception as exc:
        logger.warning(
            f"无法加载 Playwright 驱动安装器，可能需要手动运行 `playwright install chromium`: {exc}"
        )
        return

    try:
        if getattr(sys, "frozen", False):
            browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            if browsers_path and os.path.exists(browsers_path):
                logger.debug("检测到打包环境内置浏览器，跳过自动安装")
                return

        logger.info("正在检查并安装所需浏览器内核，请稍候...")
        driver_executable, driver_cli = compute_driver_executable()
        env = get_driver_env()

        subprocess.run(
            [driver_executable, driver_cli, "install", "chromium"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except Exception as exc:
        logger.warning(
            f"自动安装浏览器内核失败，可能需要手动运行 `playwright install chromium`: {exc}"
        )


def get_bool_value(value: Any, default: bool = False) -> bool:
    """将配置值稳健地转换为布尔类型。"""
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False

    return default


def clean_text(text: str) -> str:
    """归一化辅助函数。"""
    import re

    text = text or ""
    # 去除题号（如 "1. " 或 "12、"）
    text = re.sub(r"^\s*[A-Z0-9]+[\.、\s]+", "", text)
    # 仅保留中文和字母数字，去除空格和符号
    return re.sub(r"[^\w\u4e00-\u9fa5]", "", text)
