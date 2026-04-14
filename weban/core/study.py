import re
import time
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Dict

from .const import (
    SEL_AGREE_CHECKBOX,
    SEL_BTN_NEXT_STEP,
    SEL_COURSE_LIST_MARKERS,
    SEL_COURSE_LIST_WAIT_TARGETS,
    SEL_FCHL_ITEM,
    SEL_COLLAPSE_ITEM,
    SEL_COLLAPSE_ITEM_TITLE,
    SEL_BROADCAST_MODAL,
    SEL_COMMENT_BACK_BTN,
    SEL_NAV_BAR_LEFT,
    SEL_ITEM_TITLE_TEXT,
    SEL_TASK_OR_IMG_BLOCK,
    SEL_IMG_TEXT_BLOCK,
    SEL_BTN_SUBMIT_SIGN,
    SEL_TASK_DONE_LABEL,
    SEL_FCHL_ITEM_VISIBLE,
    SEL_FCHL_ITEM_NOT_PASSED,
    SEL_FCHL_ITEM_NOT_PASSED_VISIBLE,
    SEL_IMG_TEXT_ITEM_NOT_PASSED,
    SEL_TASK_BLOCK,
    SEL_IMG_TEXT_ITEM,
)
from .captcha import (
    has_captcha as _has_captcha,
    handle_tencent_captcha as _handle_tencent_captcha,
)
from .base import BaseMixin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# projectCategory → 类型名称映射
# ---------------------------------------------------------------------------
_PROJECT_CATEGORY_NAMES = {
    1: "新生安全教育",
    2: "安全课程",
    3: "专题学习",
    4: "军事理论",
    9: "实验室安全",
}

# 课程 Tab 顺序
_PROJECT_STUDY_TABS = {
    "pre": [3, 2],
    "normal": [3, 1, 2],
    "special": [3, 2],
    "military": [3],
    "lab": [3],
    "foods": [3],
}

# ---------------------------------------------------------------------------
# 运行状态数据类
# ---------------------------------------------------------------------------


@dataclass
class _StudyRunState:
    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""
    active_section_index: int = -1
    expanded_tabs: set = field(default_factory=set)  # 已展开过章节的tab


# ---------------------------------------------------------------------------
# StudyMixin
# ---------------------------------------------------------------------------


class StudyMixin(BaseMixin):
    if TYPE_CHECKING:
        from typing import Union as _Union
        from playwright.sync_api import Page, BrowserContext, Browser, Playwright
        from .browser import BrowserConfig
        import logging as _logging

        _page: Page | None
        _context: BrowserContext | None
        _browser: Browser | None
        _playwright: Playwright | None
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        browser_config: "BrowserConfig"
        answers: Dict[str, Any]

    def _detect_project_type(self) -> str:
        page_state = self._ensure_page_state()
        query = page_state.get("query", {})
        project_type = str(query.get("projectType", "")).strip()
        if project_type:
            return project_type

        url = page_state.get("url", "") or self.current_url
        if not url:
            return ""

        m = re.search(r"projectType=([^&/#]+)", url)
        return m.group(1) if m else ""

    def _handle_protocol_page(self) -> bool:
        if not self._page:
            return False
        try:
            agree_cb = self._page.locator(SEL_AGREE_CHECKBOX)
            next_btn = self._page.locator(SEL_BTN_NEXT_STEP)
            if agree_cb.count() == 0 and next_btn.count() == 0:
                return False

            self.log.info("[协议] 检测到承诺书/协议页，自动同意")
            if agree_cb.count() > 0 and not agree_cb.first.is_checked():
                # Some pages render a visually-hidden checkbox; allow force fallback.
                self._safe_click(agree_cb.first, force_fallback=True)
                time.sleep(0.5)

            if next_btn.count() > 0 and next_btn.first.is_visible():
                self._safe_click(next_btn.first)
                time.sleep(2)

            submit_btn = self._page.locator(SEL_BTN_SUBMIT_SIGN)
            if submit_btn.count() > 0 and submit_btn.first.is_visible():
                self._safe_click(submit_btn.first)
                time.sleep(2)

            return True
        except Exception as e:
            self.log.warning(f"[协议] 处理承诺书页异常: {e}")
            return False

    def _handle_special_index(self) -> bool:
        if not self._page:
            return False
        try:
            blocks = self._page.locator(SEL_TASK_OR_IMG_BLOCK)
            if blocks.count() == 0:
                return False
            self.log.info(
                f"[专题/实验室] 检测到中间列表页，共 {blocks.count()} 个子项目"
            )
            for i in range(blocks.count()):
                blk = blocks.nth(i)
                if blk.locator(SEL_TASK_DONE_LABEL).count() > 0:
                    continue
                self._safe_click(blk)
                time.sleep(3)
                return True
            self._safe_click(blocks.last)
            time.sleep(3)
            return True
        except Exception as e:
            self.log.warning(f"[专题/实验室] 处理中间页异常: {e}")
            return False

    def _handle_intermediate_pages(self) -> None:
        if not self._page:
            return

        for _round in range(5):
            time.sleep(1)
            if not self._page or self._page.is_closed():
                return

            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return

            if self._handle_protocol_page():
                continue

            if self._handle_special_index():
                continue

            lab_blocks = self._page.locator(SEL_IMG_TEXT_BLOCK)
            if lab_blocks.count() > 0:
                self.log.info("[实验室] 检测到 LabIndex 页，点击第一个子实验")
                self._safe_click(lab_blocks.first)
                time.sleep(3)
                continue

            try:
                self._page.wait_for_selector(SEL_COURSE_LIST_WAIT_TARGETS, timeout=5000)
            except Exception:
                pass

        if self._page and self._page.locator(SEL_COURSE_LIST_MARKERS).count() == 0:
            if self._get_study_page_context() == "course-list":
                return
            self.log.debug("未能自动进入课程页，尝试继续执行。")

    def _get_current_study_tabs(self) -> List[int]:
        pt = self._detect_project_type()
        return list(_PROJECT_STUDY_TABS.get(pt, [3, 2]))

    def _switch_to_study_tab(self, subject_type: int) -> bool:
        if not self._page or self._page.is_closed():
            return False

        # Tab 文案在不同项目里不稳定，尽量用关键字匹配。
        _tab_keywords = {
            3: ["必修"],
            2: ["选修", "自选"],
            1: ["匹配"],
        }
        labels = _tab_keywords.get(subject_type, [])
        if not labels:
            return False

        try:
            for label in labels:
                tab = self._page.locator(f'.van-tab:has-text("{label}")')
                if tab.count() > 0:
                    active_cls = tab.first.get_attribute("class") or ""
                    if "van-tab--active" not in active_cls:
                        tab.first.scroll_into_view_if_needed(timeout=2000)
                        tab.first.click(timeout=5000)
                        time.sleep(1)
                    return True

            self.log.debug(f"[Tab] 当前页面未发现标签: {labels}")
            return False
        except Exception as e:
            self.log.warning(f"[Tab] 切换失败: {e}")
            return False

    def _parse_section_progress(self, text: str) -> tuple[int | None, int | None]:
        """Parse section progress from title like '3/8'."""
        if not text:
            return None, None
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if not m:
            return None, None
        try:
            return int(m.group(1)), int(m.group(2))
        except Exception:
            return None, None

    def _get_active_collapse_index(self) -> int:
        if not self._page or self._page.is_closed():
            return -1
        items = self._page.locator(SEL_COLLAPSE_ITEM)
        for i in range(items.count()):
            it = items.nth(i)
            try:
                cls = it.get_attribute("class") or ""
                if "van-collapse-item--active" in cls:
                    return i
                btn = it.locator(SEL_COLLAPSE_ITEM_TITLE).first
                if btn.count() > 0:
                    if (btn.get_attribute("aria-expanded") or "").lower() == "true":
                        return i
            except Exception:
                continue
        return -1

    def _expand_next_incomplete_section_dom(self) -> bool:
        """Expand the next incomplete section using DOM only."""
        if not self._page or self._page.is_closed():
            return False

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
        if collapse_items.count() == 0:
            return False

        start = self._get_active_collapse_index() + 1
        if start < 0:
            start = 0

        for i in range(start, collapse_items.count()):
            item = collapse_items.nth(i)
            title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
            if title_btn.count() == 0:
                continue

            try:
                cls = item.get_attribute("class") or ""
                if "van-collapse-item--active" in cls:
                    continue
            except Exception:
                pass

            title_text = ""
            try:
                title_text = title_btn.inner_text().strip()
            except Exception:
                pass

            finished, total = self._parse_section_progress(title_text)
            if (
                finished is not None
                and total is not None
                and total > 0
                and finished >= total
            ):
                continue

            try:
                title_btn.scroll_into_view_if_needed(timeout=2000)
                title_btn.click(timeout=5000)
                time.sleep(1.5)
                self.log.info(f"[章节] 展开: {title_text or f'#{i + 1}'}")
                return True
            except Exception as e:
                self.log.debug(f"[章节] 展开失败({i + 1}): {e}")
                continue
        return False

    def _expand_next_section_if_needed(self) -> bool:
        # DOM-only implementation
        return self._expand_next_incomplete_section_dom()

    def _expand_next_incomplete_section(self) -> bool:
        return self._expand_next_incomplete_section_dom()

    def _expand_all_sections(self) -> None:
        # 按用户要求：不允许一次性展开所有章节。
        return

    def _collect_tasks_in_current_tab(self) -> List[Dict[str, Any]]:
        if not self._page or self._page.is_closed():
            return []

        dom_tasks: list[dict[str, Any]] = []
        selectors = [
            SEL_IMG_TEXT_ITEM,
            SEL_FCHL_ITEM,
            ".van-collapse-item__content .van-cell",
            ".van-collapse-item__content .course-item",
            ".van-collapse-item__content .lesson-item",
            ".list-item-content",
        ]

        loc = self._page.locator(", ".join(selectors))
        for i in range(loc.count()):
            it = loc.nth(i)
            try:
                if not it.is_visible():
                    continue
            except Exception:
                continue
            title = self._extract_item_title(it)
            if not title or len(title) < 2:
                continue

            cls = (it.get_attribute("class") or "").lower()
            if "van-collapse-item__title" in cls or "chapter" in cls:
                continue

            passed = (
                "passed" in cls
                or "finished" in cls
                or it.locator(
                    ".van-icon-success, .van-icon-passed, .icon-finish"
                ).count()
                > 0
                or "已完成" in (it.inner_text() or "")
            )

            dom_tasks.append(
                {
                    "title": title,
                    "passed": passed,
                    "type": "fchl" if "fchl" in cls else "img-text",
                }
            )

        if len(dom_tasks) == 0:
            self._log_page_diagnostics()

        self.log.debug(f"[扫描] DOM 扫描完成，总计 {len(dom_tasks)} 门课程")
        return dom_tasks

    def _log_page_diagnostics(self):
        if not self._page or self._page.is_closed():
            return
        try:
            diag = self._page.evaluate("""() => {
                const getInfo = (sel) => {
                    const el = document.querySelector(sel);
                    return el ? { sel, text: el.innerText.substring(0, 50), html: el.outerHTML.substring(0, 100) } : null;
                };

                return {
                    url: window.location.href,
                    body_classes: document.body.className,
                    first_collapse: getInfo('.van-collapse-item'),
                    first_cell: getInfo('.van-cell'),
                    first_img_texts: getInfo('.img-texts-item'),
                    all_text_len: document.body.innerText.length
                };
            }""")
            self.log.debug(f"[扫描诊断] 页面诊断信息: {diag}")
        except Exception as e:
            self.log.debug(f"[扫描诊断] 失败: {e}")

    def _extract_item_title(self, item) -> str:
        if not item:
            return ""
        try:
            for sel in SEL_ITEM_TITLE_TEXT.split(", "):
                s = sel.strip()
                if not s:
                    continue
                t_loc = item.locator(s)
                if t_loc.count() > 0:
                    text = t_loc.first.inner_text().strip()
                    if text:
                        return text

            for attr in ["aria-label", "title", "name", "data-title"]:
                val = item.get_attribute(attr)
                if val and val.strip():
                    return val.strip()

            raw_text = item.inner_text().strip()
            if raw_text:
                return raw_text.split("\n")[0].strip()
        except Exception:
            pass
        return ""

    def _find_fchl_target(self, study_mode: str, failed: set, completed: set):
        sels = (
            [SEL_FCHL_ITEM_VISIBLE, SEL_FCHL_ITEM]
            if study_mode == "force"
            else [SEL_FCHL_ITEM_NOT_PASSED_VISIBLE, SEL_FCHL_ITEM_NOT_PASSED]
        )
        if not self._page or self._page.is_closed():
            return None
        for sel in sels:
            items = self._page.locator(sel)
            for i in range(items.count()):
                it = items.nth(i)
                t = self._extract_item_title(it)
                if t and t not in failed and t not in completed:
                    return it
        return None

    def _get_study_page_context(self) -> str:
        if not self._page or self._page.is_closed():
            return "unknown"
        p_state = self._ensure_page_state()
        route = str(p_state.get("path", "")).strip("/")
        state = str(p_state.get("state", "unknown"))

        try:
            if (
                self._page.locator(SEL_COMMENT_BACK_BTN).count() > 0
                or self._is_mcwk_course_page()
            ):
                return "course-detail"
            if self._page.locator(SEL_COLLAPSE_ITEM).count() > 0:
                return "collapse-list"
            if self._page.locator(f"{SEL_IMG_TEXT_ITEM}, {SEL_FCHL_ITEM}").count() > 0:
                return "course-list"
            if self._page.locator(SEL_TASK_BLOCK).count() > 0:
                return "project-list"
        except Exception:
            pass

        if route == "learning-task-list":
            return (
                "course-list"
                if p_state.get("query", {}).get("projectType")
                else "project-list"
            )
        if state == "study":
            return "course-detail"
        return "unknown"

    def _current_tab_has_unfinished_courses(
        self, mode: str, completed: set, failed: set
    ) -> bool:
        if not self._page or self._page.is_closed():
            return False
        if mode == "force":
            return True
        ctx = self._get_study_page_context()
        if ctx == "course-list" or ctx == "collapse-list":
            if self._page.locator(SEL_IMG_TEXT_ITEM_NOT_PASSED).count() > 0:
                return True
            if self._page.locator(SEL_FCHL_ITEM_NOT_PASSED).count() > 0:
                return True
        return False

    def _expand_next_section(
        self, state: _StudyRunState, mode: str, completed: set, failed: set
    ) -> bool:
        if not self._page or self._page.is_closed():
            return False
        collapses = self._page.locator(SEL_COLLAPSE_ITEM)
        for i in range(state.active_section_index + 1, collapses.count()):
            item = collapses.nth(i)
            btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
            if btn.count() > 0:
                expanded = (btn.get_attribute("aria-expanded") or "").lower() == "true"
                if not expanded:
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(timeout=5000)
                    time.sleep(1)
            state.active_section_index = i
            return True
        return False

    def _dismiss_broadcast(self) -> None:
        try:
            if self._page and not self._page.is_closed():
                bc = self._page.locator(SEL_BROADCAST_MODAL)
                if bc.count() > 0 and bc.first.is_visible():
                    close_btn = bc.first.locator(
                        "button, .close-btn, [class*='close']"
                    ).first
                    if close_btn.count() > 0:
                        close_btn.scroll_into_view_if_needed(timeout=2000)
                        close_btn.click(timeout=5000)
                    else:
                        self._page.mouse.click(10, 10)
                    time.sleep(0.5)
        except Exception:
            pass

    def _get_course_runtime_frame(self):
        if not self._page or self._page.is_closed():
            return None
        try:
            for f in self._page.frames:
                if f == self._page.main_frame:
                    continue
                url = (f.url or "").lower()
                if "mcwk" in url or "course" in url:
                    return f
                try:
                    if f.evaluate("typeof finishWxCourse === 'function'"):
                        return f
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _is_mcwk_course_page(self) -> bool:
        return self._get_course_runtime_frame() is not None

    def _wait_for_mcwk_runtime(self, timeout: float = 8) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            f = self._get_course_runtime_frame()
            if f:
                try:
                    if f.evaluate("typeof finishWxCourse === 'function'"):
                        return True
                except Exception:
                    pass
            time.sleep(0.5)
        return False

    def _wait_for_post_course_state(self, timeout: float = 8) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self._page and self._get_study_page_context() in (
                    "course-list",
                    "collapse-list",
                ):
                    return True
            except Exception:
                pass
            if _has_captcha(self._page):
                _handle_tencent_captcha(self._page, self.log)
                continue
            time.sleep(0.5)
        return False

    def _trigger_img_text_completion(self, frame, title: str) -> bool:
        try:
            if frame:
                frame.evaluate(
                    "if(typeof finishWxCourse === 'function') finishWxCourse();"
                )
                return True
            if self._page:
                self._page.evaluate(
                    "if(typeof finishWxCourse === 'function') finishWxCourse();"
                )
                return True
        except Exception:
            pass
        return False

    def _wait_for_img_text_completion_result(self, timeout: float = 12) -> str:
        end = time.time() + timeout
        while time.time() < end:
            if self._get_study_page_context() in ("course-list", "collapse-list"):
                return "list"
            if _has_captcha(self._page):
                _handle_tencent_captcha(self._page, self.log)
                continue
            if self._page:
                url = self._page.url.lower()
                if "comment" in url or "rating" in url:
                    return "comment"
                if self._page.locator(SEL_COMMENT_BACK_BTN).count() > 0:
                    return "return"
            time.sleep(0.5)
        return ""

    def _find_img_text_item_by_title(self, title: str):
        if not self._page or self._page.is_closed():
            return None

        # 先在当前可见的课程项中查找
        sels = [SEL_IMG_TEXT_ITEM_NOT_PASSED, SEL_IMG_TEXT_ITEM]
        for sel in sels:
            try:
                loc = self._page.locator(sel)
                count = loc.count()
                for i in range(count):
                    it = loc.nth(i)
                    if not it.is_visible():
                        continue
                    item_title = self._extract_item_title(it)
                    if item_title == title:
                        self.log.debug(f"在可见区域找到课程：{title}")
                        return it
            except Exception as e:
                self.log.debug(f"查找课程元素出错: {e}")
                continue

        # 如果没找到，只在折叠项中尝试定位并按需展开对应章节。
        try:
            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            for i in range(collapse_items.count()):
                collapse = collapse_items.nth(i)
                try:
                    collapse_title_elem = collapse.locator(
                        SEL_COLLAPSE_ITEM_TITLE
                    ).first
                    if collapse_title_elem.count() == 0:
                        continue

                    # 先展开再在内容区查找
                    is_expanded = collapse.get_attribute("class") or ""
                    if "van-collapse-item--active" not in is_expanded:
                        collapse_title_elem.scroll_into_view_if_needed(timeout=2000)
                        collapse_title_elem.click(timeout=5000)
                        time.sleep(0.8)

                    content_items = collapse.locator(SEL_IMG_TEXT_ITEM)
                    for j in range(content_items.count()):
                        item = content_items.nth(j)
                        if not item.is_visible():
                            continue
                        if self._extract_item_title(item) == title:
                            return item
                except Exception:
                    continue
        except Exception:
            pass

        return None

    def _is_img_text_course_passed(self, title: str) -> bool:
        if not self._page:
            return False
        it = self._find_img_text_item_by_title(title)
        if it:
            cls = (it.get_attribute("class") or "").lower()
            if "passed" in cls or "finished" in cls:
                return True
            if (
                it.locator(".van-icon-success, .van-icon-passed, .icon-finish").count()
                > 0
            ):
                return True
        return False

    def _finish_img_text_course(self, title: str, study_time: int) -> bool:
        for _att in range(3):
            f = self._get_course_runtime_frame()
            if f:
                self._wait_for_mcwk_runtime()
            if not self._trigger_img_text_completion(f, title):
                continue
            res = self._wait_for_img_text_completion_result()
            if res:
                if res != "list":
                    self._return_to_chapter_list()
                if self._is_img_text_course_passed(title):
                    return True
            time.sleep(1)
        return False

    def _return_to_chapter_list(self) -> bool:
        if not self._page or self._page.is_closed():
            return False
        ctx = self._get_study_page_context()
        if ctx in ("course-list", "collapse-list"):
            return True
        try:
            f = self._get_course_runtime_frame()
            if f:
                f.evaluate("if(typeof backToList === 'function') backToList();")
                time.sleep(1)
            if self._page:
                back = self._page.locator(SEL_NAV_BAR_LEFT).first
                if back.count() > 0 and back.is_visible():
                    back.scroll_into_view_if_needed(timeout=2000)
                    back.click(timeout=5000)
                    time.sleep(1)
                btn_back = self._page.locator(SEL_COMMENT_BACK_BTN).first
                if btn_back.count() > 0 and btn_back.is_visible():
                    btn_back.scroll_into_view_if_needed(timeout=2000)
                    btn_back.click(timeout=5000)
                    time.sleep(1)
            return self._get_study_page_context() in ("course-list", "collapse-list")
        except Exception:
            return False

    def _goto_next_project(
        self, state: _StudyRunState, completed: set, study_mode: str = "true"
    ) -> bool:
        if not self._page or self._page.is_closed():
            return False
        ctx = self._get_study_page_context()
        if ctx in ("course-list", "collapse-list"):
            # 如果当前项目已经处理过，返回False结束学习
            if state.current_project_title and state.current_project_title in completed:
                self.log.debug(f"项目「{state.current_project_title}」已处理完毕")
                return False

            # 不再检查项目是否100%完成，而是直接进入项目检查课程
            self.log.debug("[导航] 当前已在课程列表页，直接处理该项目")
            try:
                nav_title = self._page.locator(
                    ".van-nav-bar__title, .project-title, .header-title"
                ).first
                if nav_title.count() > 0:
                    self.project_title = nav_title.inner_text().strip()
            except Exception:
                pass
            if not state.current_project_title:
                state.current_project_title = self.project_title or "未知项目"
            state.study_tabs = self._get_current_study_tabs()
            return True

        self.log.info("正在导航至学习项目中心...")
        try:
            self._page.goto(f"{self.base_url}/#/learning-task-list", timeout=15000)
            time.sleep(3)
        except Exception:
            pass
        self._dismiss_broadcast()
        projs = self._page.locator(SEL_TASK_OR_IMG_BLOCK)
        if projs.count() == 0:
            try:
                self._page.reload()
                time.sleep(5)
            except Exception:
                pass
            projs = self._page.locator(SEL_TASK_OR_IMG_BLOCK)
        self.log.debug(f"[导航] 发现 {projs.count()} 个学习项目")
        for i in range(projs.count()):
            it = projs.nth(i)
            title = self._extract_item_title(it)
            if not title or title in completed:
                continue
            self.project_title = title
            self.log.info(f"======== 目标项目: {title} ========")
            # 不再基于项目百分比跳过，而是进入项目检查课程完成情况
            state.current_project_title = title
            it.scroll_into_view_if_needed(timeout=2000)
            it.click(timeout=5000)
            for _ in range(5):
                time.sleep(1.5)
                self._handle_intermediate_pages()
                if self._get_study_page_context() in ("course-list", "collapse-list"):
                    self.log.info(f"成功进入项目：{title}")
                    state.study_tabs = self._get_current_study_tabs()
                    return True
            self.log.warning(f"进入项目「{title}」超时，尝试继续探测...")
            return True
        return False

    def _process_task_list(
        self, v_tasks, study_time, study_mode, completed, failed
    ) -> int:
        processed_cnt = 0
        for idx, task in enumerate(v_tasks):
            title = task["title"]
            if study_mode != "force" and task.get("passed"):
                self.log.debug(f"[{idx + 1}/{len(v_tasks)}] 跳过已完成课程：{title}")
                continue
            if title in completed or title in failed:
                self.log.debug(f"[{idx + 1}/{len(v_tasks)}] 跳过已处理课程：{title}")
                continue
            self._return_to_chapter_list()
            self.log.info(f"[{idx + 1}/{len(v_tasks)}] 正在学习：{title}")
            ok = False
            if task.get("type") == "fchl":
                item = self._find_fchl_target(study_mode, failed, completed)
                if item and self._extract_item_title(item) == title:
                    if self._safe_click(item):
                        self._sleep_with_progress(study_time)
                        self.finish_study()
                        self._return_to_chapter_list()
                        ok = True
                else:
                    self.log.warning(f"未找到FCHL课程元素：{title}")
            else:
                item = self._find_img_text_item_by_title(title)
                if item:
                    if self._safe_click(item):
                        time.sleep(2)
                        self._sleep_with_progress(study_time)
                        ok = self._finish_img_text_course(title, study_time)
                        if not ok:
                            self.log.warning(f"课程完成失败：{title}")
                        # 无论成功失败，都返回课程列表
                        self._return_to_chapter_list()
                        time.sleep(1)
                    else:
                        self.log.warning(f"点击课程元素失败：{title}")
                else:
                    self.log.warning(f"未找到课程元素：{title}")
            if ok:
                completed.add(title)
                processed_cnt += 1
                self.log.info(f"[{idx + 1}/{len(v_tasks)}] ✅ 课程完成：{title}")
            else:
                failed.add(title)
                self.log.warning(f"[{idx + 1}/{len(v_tasks)}] ❌ 课程失败：{title}")
            time.sleep(1.5)
            if self._page and self._page.is_closed():
                break
        return processed_cnt

    def _check_course_completion(self) -> dict:
        """检查当前项目的课程完成情况"""
        if not self._page or self._page.is_closed():
            return {"total": 0, "completed": 0, "incomplete": 0}

        total = 0
        completed_count = 0

        try:
            # 先返回课程列表页
            self._return_to_chapter_list()
            time.sleep(2)

            # 扫描所有课程项
            tasks = self._collect_tasks_in_current_tab()
            total = len(tasks)

            for task in tasks:
                if task.get("passed"):
                    completed_count += 1

            incomplete = total - completed_count

            self.log.info(
                f"[课程统计] 总课程数: {total}, 已完成: {completed_count}, 未完成: {incomplete}"
            )

            return {
                "total": total,
                "completed": completed_count,
                "incomplete": incomplete,
            }
        except Exception as e:
            self.log.warning(f"统计课程完成情况失败: {e}")
            return {"total": 0, "completed": 0, "incomplete": 0}

        total = 0
        completed_count = 0

        try:
            # 扫描所有课程项
            tasks = self._collect_tasks_in_current_tab()
            total = len(tasks)

            for task in tasks:
                if task.get("passed"):
                    completed_count += 1

            incomplete = total - completed_count

            self.log.info(
                f"[课程统计] 总课程数: {total}, 已完成: {completed_count}, 未完成: {incomplete}"
            )

            return {
                "total": total,
                "completed": completed_count,
                "incomplete": incomplete,
            }
        except Exception as e:
            self.log.warning(f"统计课程完成情况失败: {e}")
            return {"total": 0, "completed": 0, "incomplete": 0}

    def run_study(self, study_time: int, study_mode: str) -> dict:
        self.log.info("开始学习流程 (State-Aware 模式)")
        completed_projs, state = set(), _StudyRunState()
        completion_stats = {"total": 0, "completed": 0, "incomplete": 0}

        if not self._page or self._page.is_closed():
            return completion_stats
        try:
            while self._goto_next_project(state, completed_projs, study_mode):
                proj_title = state.current_project_title
                failed, completed = set(), set()
                # 在当前项目内：按 Tab 逐个处理，且在每个 Tab 内“当前章节无任务”才展开下一章节。
                tab_found_any = False
                for tab_id in state.study_tabs:
                    if not self._switch_to_study_tab(tab_id):
                        continue
                    tab_found_any = True
                    time.sleep(1.5)
                    self._dismiss_broadcast()

                    while not self._page.is_closed():
                        tasks = self._collect_tasks_in_current_tab()
                        self.log.info(f"[Tab {tab_id}] 扫描到 {len(tasks)} 门课程")
                        processed = self._process_task_list(
                            [t for t in tasks if t.get("title")],
                            study_time,
                            study_mode,
                            completed,
                            failed,
                        )
                        if processed > 0:
                            time.sleep(1)
                            continue

                        if self._expand_next_section_if_needed():
                            time.sleep(1)
                            continue
                        break

                if not tab_found_any:
                    self.log.debug("[Fallback] 未找到课程 Tab，直接按当前页处理...")
                    attempt_count = 0
                    while (
                        not self._page.is_closed() and attempt_count < 3
                    ):  # 最多尝试3次
                        tasks = self._collect_tasks_in_current_tab()
                        if not tasks:
                            self.log.debug("未找到任何课程，退出循环")
                            break

                        self.log.info(f"[Fallback] 扫描到 {len(tasks)} 门课程")
                        processed = self._process_task_list(
                            [t for t in tasks if t.get("title")],
                            study_time,
                            study_mode,
                            completed,
                            failed,
                        )
                        if processed > 0:
                            time.sleep(1)
                            continue
                        if self._expand_next_section_if_needed():
                            time.sleep(1)
                            continue
                        attempt_count += 1
                        if attempt_count >= 3:
                            self.log.debug("多次扫描未发现新课程，退出循环")
                            break
                        time.sleep(2)  # 等待后重新扫描

                completed_projs.add(proj_title)
                self.log.info(f"项目「{proj_title}」处理完毕。")

                # 统计当前项目的课程完成情况
                stats = self._check_course_completion()
                completion_stats["total"] += stats["total"]
                completion_stats["completed"] += stats["completed"]
                completion_stats["incomplete"] += stats["incomplete"]

        except Exception as e:
            self.log.error(f"严重异常: {e}")

        self.log.info("全部可探测的学习任务已处理。")
        self.log.info(
            f"[学习完成情况] 总课程: {completion_stats['total']}, 已完成: {completion_stats['completed']}, 未完成: {completion_stats['incomplete']}"
        )

        return completion_stats

    def _safe_click(
        self, locator, timeout: int = 5000, *, force_fallback: bool = False
    ) -> bool:
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

    def _sleep_with_progress(self, seconds: int) -> None:
        if seconds <= 0:
            return
        self.log.info(f"正在展示中，预计剩余 {seconds} 秒...")
        for i in range(seconds, 0, -1):
            if i % 10 == 0 and i != seconds:
                self.log.info(f"剩余 {i} 秒...")
            time.sleep(1)

    def finish_study(self) -> None:
        try:
            if self._page and not self._page.is_closed():
                self._page.evaluate(
                    "if(typeof finishWxCourse === 'function') finishWxCourse();"
                )
        except Exception:
            pass
