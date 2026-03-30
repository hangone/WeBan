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
            f"无法加载 Playwright 驱动安装器，可能需要手动运行 `uv run playwright install chromium`: {exc}"
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


def strip_side_symbols(text: str) -> str:
    """仅删除题目或答案结尾的括号和空格，保留开头的符号。"""
    if not text:
        return ""
    import re

    text = re.sub(r"^\s*\d+[\.、\s]+", "", text)
    # 仅删除题目或答案结尾的 [括号+空格] 以及 [紧跟在括号后的句号]
    # 如果结尾只有单个句号（且前面不是括号或空格），则不予处理
    text = re.sub(r"([()\[\]{}（）【】｛｝\s]+[.。]?)(\s*)$", "", text)
    return text


def clean_text(text: str) -> str:
    """归一化辅助函数：全系统统一使用最新的语义去燥逻辑。"""
    return ignore_symbols(text)


def ignore_symbols(text: str) -> str:
    """逻辑合并的核心：彻底去噪。"""
    import re

    if not text:
        return ""
    # 剥除一切非文字、非数字、非字母内容（包含空格和标点）
    text = re.sub(r"[^\w\u4e00-\u9fa5]", "", text)
    return text.lower()
