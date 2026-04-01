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
        _page: "Page | None"
        _context: "BrowserContext | None"
        _browser: "Browser | None"
        _playwright: "Playwright | None"
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        browser_config: "BrowserConfig"
        answers: Dict[str, Any]
        current_url: str
        current_hash: str
        page_state: Dict[str, Any]

    # _clean_text moved to weban.app.runtime.clean_text

    def _get_current_url(self) -> str:
        """安全获取当前页面 URL。"""
        if not self._page:
            return ""
        try:
            return (self._page.url or "").strip()
        except Exception:
            return ""

    def _refresh_page_state(self) -> Dict[str, Any]:
        """刷新并缓存当前 SPA 页面状态。"""
        url = self._get_current_url()
        hash_part = ""
        path = ""
        query: Dict[str, str] = {}

        if "#/" in url:
            hash_part = url.split("#/", 1)[1]
        elif "#" in url:
            hash_part = url.split("#", 1)[1]

        if hash_part:
            path = hash_part.split("?", 1)[0].strip("/")
            query_str = hash_part.split("?", 1)[1] if "?" in hash_part else ""
            if query_str:
                for part in query_str.split("&"):
                    if not part:
                        continue
                    key, _, value = part.partition("=")
                    if key:
                        query[key] = value

        state = "unknown"
        if not url:
            state = "blank"
        elif "learning-task-list" in url:
            state = "project_list"
        elif any(k in url for k in ["study", "course", "resource"]):
            state = "study"
        elif any(k in url for k in ["exam", "paper", "review"]):
            state = "exam"

        if self._page:
            try:
                if self._page.locator(".quest-stem, .quest-option-item").count() > 0:
                    state = "exam_question"
                elif (
                    self._page.locator(
                        ".score-num, .score, .exam-score, .result-score, .score-text"
                    ).count()
                    > 0
                ):
                    state = "exam_result"
                elif (
                    self._page.locator(
                        ".van-collapse-item, .img-texts-item, .fchl-item"
                    ).count()
                    > 0
                ):
                    state = "course_list"
                elif self._page.locator(".task-block, .img-text-block").count() > 0:
                    state = "project_list"
            except Exception:
                pass

        self.current_url = url
        self.current_hash = hash_part
        self.page_state = {
            "url": url,
            "hash": hash_part,
            "path": path,
            "query": query,
            "state": state,
        }
        return self.page_state

    def _ensure_page_state(
        self, expected_states: set[str] | None = None
    ) -> Dict[str, Any]:
        """获取当前页面状态，并在需要时校验状态是否符合预期。"""
        page_state = self._refresh_page_state()
        if expected_states and page_state["state"] not in expected_states:
            self.log.debug(
                f"[页面状态] 当前 state={page_state['state']} "
                f"url={page_state['url'] or '<blank>'}"
            )
        return page_state

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
