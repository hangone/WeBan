"""WeBan 基础 Mixin 类。

提供所有 Mixin 共享的工具方法和类型声明，包含统一的页面状态判断逻辑。
"""

import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    import logging as _logging
    from typing import Union as _Union
    from playwright.sync_api import Page, BrowserContext, Browser, Playwright
    from .browser import BrowserConfig


class PageContext(Enum):
    BLANK = "blank"
    PROJECT_LIST = "project-list"
    COURSE_LIST = "course-list"
    COURSE_DETAIL = "course-detail"
    EXAM_LIST = "exam-list"
    EXAM_QUESTION = "exam-question"
    EXAM_RESULT = "exam-result"
    UNKNOWN = "unknown"


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

    def _get_current_url(self) -> str:
        """安全获取当前页面 URL。"""
        if not self._page:
            return ""
        try:
            return (self._page.url or "").strip()
        except Exception:
            return ""

    def _detect_page_context(self) -> PageContext:
        """基于 DOM 元素检测当前页面上下文。

        参考：
        - LearningTaskList.vue: 项目列表页（.task-block 包含 .task-block-title）
        - CourseIndex.vue: 课程列表页（.van-collapse-item, .img-texts-item）

        Returns:
            PageContext: 当前页面上下文状态
        """
        if not self._page or self._page.is_closed():
            return PageContext.BLANK

        try:
            page = self._page

            if page.locator(".quest-stem, .quest-option-item").count() > 0:
                return PageContext.EXAM_QUESTION

            if (
                page.locator(
                    ".score-num, .score, .exam-score, .result-score, .score-text"
                ).count()
                > 0
            ):
                return PageContext.EXAM_RESULT

            if page.locator(".exam-item, .exam-list").count() > 0:
                return PageContext.EXAM_LIST

            if (
                page.locator(".van-collapse-item, .img-texts-item, .fchl-item").count()
                > 0
            ):
                return PageContext.COURSE_LIST

            task_blocks = page.locator(".task-block")
            task_block_count = task_blocks.count()
            if task_block_count > 0:
                task_titles = page.locator(".task-block-title")
                if task_titles.count() > 0:
                    return PageContext.PROJECT_LIST

            img_text_blocks = page.locator(".img-text-block")
            if img_text_blocks.count() > 0:
                return PageContext.PROJECT_LIST

            url = (page.url or "").lower()
            if "learning-task-list" in url:
                return PageContext.PROJECT_LIST
            if any(k in url for k in ["study", "course", "resource"]):
                return PageContext.COURSE_LIST
            if any(k in url for k in ["exam", "paper", "review"]):
                return PageContext.EXAM_LIST

        except Exception:
            pass

        return PageContext.UNKNOWN

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

        context = self._detect_page_context()

        self.current_url = url
        self.current_hash = hash_part
        self.page_state = {
            "url": url,
            "hash": hash_part,
            "path": path,
            "query": query,
            "state": context.value,
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

    def _is_in_context(self, *contexts: PageContext) -> bool:
        """检查当前页面是否在指定的上下文集合中。"""
        return self._detect_page_context() in contexts

    def _is_course_list_page(self) -> bool:
        """是否在课程列表页。"""
        return self._is_in_context(PageContext.COURSE_LIST, PageContext.PROJECT_LIST)

    def _is_exam_question_page(self) -> bool:
        """是否在考试答题页。"""
        return self._is_in_context(PageContext.EXAM_QUESTION)

    def _is_exam_result_page(self) -> bool:
        """是否在考试结果页。"""
        return self._is_in_context(PageContext.EXAM_RESULT)

    def _sleep_with_progress(self, seconds: int) -> None:
        """带进度日志的休眠。"""
        if seconds <= 0:
            return

        self.log.info(f"等待中: 设定时长 {seconds}s")
        start_time = time.time()
        reported = set()

        while True:
            elapsed = time.time() - start_time
            remaining = int(seconds - elapsed)

            if remaining <= 0:
                break

            milestones = {
                int(0.5 * seconds): "50%",
                int(0.25 * seconds): "75%",
                int(0.1 * seconds): "90%",
            }

            checkpoint = None
            if remaining % 10 == 0 and remaining > 0:
                checkpoint = f"rem_{remaining}"

            for rem, label in milestones.items():
                if remaining == rem and label not in reported:
                    checkpoint = label
                    reported.add(label)
                    break

            if checkpoint and checkpoint not in reported:
                self.log.info(f"进行中... 剩余 {remaining}s")
                reported.add(checkpoint)

            time.sleep(1)

    def _pause_for_user_intervention(self, reason: str, timeout: int = 60) -> None:
        """当自动化遇到无法恢复的错误时，挂起脚本供用户干预。"""
        self.log.warning(f"[人工干预请求] {reason}")
        self.log.warning(f"脚本将在此处暂停 {timeout} 秒，请您在此期间手动操作页面！")
        for i in range(timeout):
            if i % 10 == 0 and i > 0:
                self.log.info(f"[干预等待] 还剩 {timeout - i} 秒...")
            time.sleep(1)
        self.log.info("干预等待结束，自动恢复流程！")

    def _safe_click(
        self, locator, timeout: int = 5000, *, force_fallback: bool = False
    ) -> bool:
        """安全点击元素，带重试机制。"""
        if not locator or not self._page or self._page.is_closed():
            return False
        try:
            locator.scroll_into_view_if_needed(timeout=2000)
            locator.wait_for(state="visible", timeout=timeout)
            locator.click(timeout=timeout)
            return True
        except Exception:
            if not force_fallback:
                return False
            try:
                locator.click(timeout=timeout, force=True)
                return True
            except Exception:
                return False
