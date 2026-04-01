import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from weban.core.captcha import (
    set_debug as set_captcha_debug,
    set_debug_account as set_captcha_debug_account,
)
from weban.app.runtime import get_bool_value
from weban.logger import setup_account_file_handler


@dataclass(slots=True)
class TaskSettings:
    """单个账号最终生效的运行参数快照。"""

    study_mode: str
    exam_mode: str
    study_time: int
    exam_question_time: int
    exam_question_time_offset: int
    random_answer: bool
    exam_submit_match_rate: int
    headless: bool
    timeout_ms: int
    manual_timeout: int
    close_browser: bool


class TaskEngine:
    """自动化任务调度引擎，负责多账号并发执行。"""

    def __init__(self, config: Any, logger: logging.Logger | logging.LoggerAdapter):
        self.config = config
        self.logger = logger
        self.settings = config.settings
        self._base_path = os.path.dirname(config.config_path)
        self._debug_enabled = bool(self.settings.get("debug", False))

    def _build_task_settings(self, account_cfg: dict[str, Any]) -> TaskSettings:
        """合并全局 settings 与账号级覆盖配置。"""
        settings = self.settings
        get_account = account_cfg.get

        def get_bool(key: str, default: bool = False) -> bool:
            return get_bool_value(get_account(key, settings.get(key, default)))

        def get_int(key: str, default: int) -> int:
            return int(get_account(key, settings.get(key, default)))

        def get_str(key: str, default: str) -> str:
            return str(get_account(key, settings.get(key, default)))

        account = get_account("username", get_account("account", ""))
        token = get_account("token", "")
        headless = get_bool("browser_headless", False)

        # 没有账号也没有 token 时，强制显示浏览器，便于首次手动登录。
        if not account and not token:
            headless = False

        return TaskSettings(
            study_mode=get_str("study_mode", "true"),
            exam_mode=get_str("exam_mode", "true"),
            study_time=get_int("study_time", 20),
            exam_question_time=get_int("exam_question_time", 5),
            exam_question_time_offset=get_int("exam_question_time_offset", 3),
            random_answer=get_bool("random_answer", False),
            exam_submit_match_rate=get_int("exam_submit_match_rate", 80),
            headless=headless,
            timeout_ms=get_int("browser_timeout_ms", 30000),
            manual_timeout=get_int("manual_login_timeout_sec", 300),
            # 调试模式下默认不关闭浏览器
            close_browser=get_bool("close_browser_on_finish", not self._debug_enabled),
        )

    def _build_browser_config(self, task_settings: TaskSettings) -> dict[str, Any]:
        """生成传给客户端的浏览器配置。"""
        return {
            "enabled": True,
            "headless": task_settings.headless,
            "channel": "chromium",
            "slow_mo": 0,
            "timeout_ms": task_settings.timeout_ms,
            "manual_login_timeout_sec": task_settings.manual_timeout,
        }

    def _make_account_logger(
        self, log_name: str
    ) -> tuple[logging.LoggerAdapter, logging.FileHandler]:
        """为单账号任务创建带 account 字段的日志适配器和独立文件 handler。"""
        logger = logging.LoggerAdapter(logging.getLogger(), {"account": log_name})
        handler = setup_account_file_handler(
            self._base_path, log_name, debug=self._debug_enabled
        )
        return logger, handler

    def _prepare_runtime(self) -> None:
        """初始化运行期公共状态。"""
        log_dir = os.path.join(self._base_path, "logs")
        set_captcha_debug(self._debug_enabled, log_dir=log_dir)

    def run_all(self) -> None:
        """执行所有账号的自动化流程。"""
        self._prepare_runtime()

        accounts = self.config.accounts
        for index, account_cfg in enumerate(accounts):
            name = account_cfg.get("username") or f"账号{index + 1}"
            self.logger.info(f"[{name}] 准备就绪")

        max_workers = min(len(accounts), int(self.settings.get("max_workers", 5)))
        self.logger.info(f"开始执行自动化任务，最大并发数: {max_workers}")

        success_count = 0
        failed_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._run_single_account, account_cfg, index): index
                for index, account_cfg in enumerate(accounts)
            }

            for future in as_completed(futures):
                index = futures[future]
                try:
                    if future.result():
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    self.logger.error(f"[账号 {index + 1}] 线程执行异常: {exc}")
                    failed_count += 1

        self.logger.info(
            f"所有账号执行完成！成功: {success_count}，失败: {failed_count}"
        )

    def _run_single_account(
        self, account_cfg: dict[str, Any], account_index: int
    ) -> bool:
        """执行单个账号的完整登录、学习和考试流程。"""
        from weban.core import WeBanClient

        account = account_cfg.get("username", account_cfg.get("account", ""))
        log_name = account or f"账号{account_index + 1}"
        set_captcha_debug_account(log_name)

        task_settings = self._build_task_settings(account_cfg)
        logger, account_handler = self._make_account_logger(log_name)
        browser_cfg = self._build_browser_config(task_settings)

        try:
            logger.info("开始执行任务")
            with WeBanClient(
                tenant_name=str(
                    account_cfg.get("tenant_name", account_cfg.get("tenantName", ""))
                ).strip(),
                account=str(account).strip(),
                password=str(account_cfg.get("password", "")).strip(),
                user_id=str(
                    account_cfg.get("userid", account_cfg.get("userId", ""))
                ).strip(),
                token=str(account_cfg.get("token", "")).strip(),
                user={},
                browser=browser_cfg,
                log=logger,
            ) as client:
                login_result = client.login()
                if not login_result or not login_result.get("ok"):
                    logger.error(f"登录失败：{login_result}")
                    return False

                self.config.update_account_state(account_index, login_info=login_result)

                if task_settings.study_mode != "false":
                    logger.info(
                        "开始学习流程 "
                        f"(单任务时长: {task_settings.study_time}秒, "
                        f"模式: {task_settings.study_mode})"
                    )
                    client.run_study(
                        study_time=task_settings.study_time,
                        study_mode=task_settings.study_mode,
                    )

                if task_settings.exam_mode != "false":
                    logger.info(f"开始考试流程 (模式: {task_settings.exam_mode})")
                    client.run_exam(
                        exam_question_time=task_settings.exam_question_time,
                        exam_question_time_offset=task_settings.exam_question_time_offset,
                        random_answer=task_settings.random_answer,
                        exam_mode=task_settings.exam_mode,
                        exam_submit_match_rate=task_settings.exam_submit_match_rate,
                    )

                logger.info("账号所有任务执行完成")

                if not task_settings.close_browser:
                    logger.info("配置指定任务完成后不关闭浏览器，程序将在此挂起。")
                    while True:
                        time.sleep(1)

            return True

        except Exception as exc:
            logger.error(f"任务执行失败: {exc}")
            traceback.print_exc(file=sys.stderr)
            return False

        finally:
            try:
                logging.getLogger().removeHandler(account_handler)
                account_handler.close()
            except Exception:
                pass
