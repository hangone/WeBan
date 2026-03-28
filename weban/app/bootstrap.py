from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from typing import Protocol

from weban.app.config import AppConfig
from weban.app.runtime import ensure_playwright_browsers, get_base_path
from weban.app.task_engine import TaskEngine
from weban.logger import setup_logger
from weban.updater import check_update_async


class SupportsLogging(Protocol):
    def info(self, msg: str, *args, **kwargs) -> None: ...
    def warning(self, msg: str, *args, **kwargs) -> None: ...
    def error(self, msg: str, *args, **kwargs) -> None: ...
    def debug(self, msg: str, *args, **kwargs) -> None: ...


@dataclass(slots=True)
class AppBootstrap:
    """保存应用启动阶段创建的核心对象。"""

    version: str
    base_path: str
    config: AppConfig
    logger: SupportsLogging
    raw_logger: logging.Logger
    engine: TaskEngine


def bootstrap_app(version: str) -> AppBootstrap:
    """完成应用启动前的初始化，并返回运行上下文。"""
    base_path = get_base_path()

    # 配置加载前先使用简易 logger，避免 config 读取阶段没有日志输出
    logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(message)s")
    startup_logger = logging.getLogger("startup")

    config = AppConfig(base_path, logger=startup_logger)
    config.load()

    logger = setup_logger(base_path, config.settings.get("debug", False))
    config.logger = logger

    # setup_logger 会重置 root logger；更新检查使用原始 Logger，避免类型不匹配
    raw_logger = logging.getLogger()

    engine = TaskEngine(config, logger)

    return AppBootstrap(
        version=version,
        base_path=base_path,
        config=config,
        logger=logger,
        raw_logger=raw_logger,
        engine=engine,
    )


def initialize_runtime(app: AppBootstrap) -> None:
    """执行启动后的运行时初始化。"""
    app.logger.info(f"程序启动，当前版本：{app.version}")
    app.logger.info("程序更新地址：https://github.com/hangone/WeBan")
    check_update_async(app.version, app.raw_logger)
    ensure_playwright_browsers(app.logger)


def run_app(version: str) -> int:
    """启动并运行应用主流程。"""
    app = bootstrap_app(version)

    try:
        initialize_runtime(app)
        app.engine.run_all()
        return 0
    except KeyboardInterrupt:
        print("\n用户手动终止程序")
        return 130
    except Exception as exc:
        app.logger.error(f"运行失败: {exc}")
        traceback.print_exc()
        return 1
