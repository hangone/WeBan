import time
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    import logging as _logging
    from typing import Union as _Union
    from playwright.sync_api import Page, BrowserContext, Browser, Playwright
    from .browser import BrowserConfig


class BaseMixin:
    """所有 Mixin 的基类，包含共用的工具方法和类型声明。"""

    if TYPE_CHECKING:
        _page: Page
        _context: BrowserContext
        _browser: Browser
        _playwright: Playwright
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        browser_config: "BrowserConfig"
        answers: Dict[str, Any]

    # _clean_text moved to weban.app.runtime.clean_text

    def _sleep_with_progress(self, seconds: int) -> None:
        """带进度条或分阶段日志的休眠，并在休眠期间定期检测异常。"""
        if seconds <= 0:
            return

        self.log.info(f"等待中: 设定时长 {seconds}s")
        start_time = time.time()
        reported_checkpoints = set()

        while True:
            elapsed = time.time() - start_time
            remaining = int(seconds - elapsed)

            if remaining <= 0:
                break

            # 每隔 10 秒或 50%, 75%, 90% 节点打日志
            milestones = {
                int(0.5 * seconds): "50%",
                int(0.25 * seconds): "75%",
                int(0.1 * seconds): "90%",
            }
            checkpoint = None
            if remaining % 10 == 0 and remaining > 0:
                checkpoint = f"rem_{remaining}"

            for rem, label in milestones.items():
                if remaining == rem and label not in reported_checkpoints:
                    checkpoint = label
                    reported_checkpoints.add(label)
                    break

            if checkpoint and checkpoint not in reported_checkpoints:
                self.log.info(f"进行中... 剩余 {remaining}s")
                reported_checkpoints.add(checkpoint)

            time.sleep(1)

    def _pause_for_user_intervention(self, reason: str, timeout: int = 60) -> None:
        """当自动化遇到无法恢复的错误或意料外的页面时，挂起脚本供用户干预。"""
        self.log.warning(f"🚨 [人工干预请求] {reason}")
        self.log.warning(
            f"🚨 脚本将在此处暂停 {timeout} 秒，请您在此期间手动操作页面！"
        )
        for i in range(timeout):
            if i % 10 == 0 and i > 0:
                self.log.info(f"[干预等待] 还剩 {timeout - i} 秒...")
            time.sleep(1)
        self.log.info("干预等待结束，自动恢复流程！")
