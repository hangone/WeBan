import re
import time
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Dict
from itertools import combinations

from .const import (
    SEL_AGREE_CHECKBOX,
    SEL_BTN_NEXT_STEP,
    SEL_COURSE_TAB,
    SEL_COURSE_LIST_MARKERS,
    SEL_COURSE_LIST_CONTENT_ITEMS,
    SEL_COURSE_LIST_WAIT_TARGETS,
    SEL_FCHL_ITEM,
    SEL_COLLAPSE_ITEM,
    SEL_COLLAPSE_ITEM_TITLE,
    SEL_BROADCAST_MODAL,
    SEL_COMMENT_BACK_BTN,
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
    SEL_ITEM_COMPLETED_ICON,
    SEL_RUNTIME_ACTIVE_VIDEO,
    SEL_RUNTIME_VIDEO_PLAY_BTN,
    SEL_RUNTIME_CHOICE,
    SEL_RUNTIME_INTERACTIVE_ITEMS,
    SEL_RUNTIME_INTERACTIVE_CLOSE,
    SEL_RUNTIME_QUIZ_LABELS,
    SEL_RUNTIME_QUIZ_CHECKED,
    SEL_RUNTIME_NAV_BTNS,
    SEL_RUNTIME_PROBE_CANDIDATES,
    SEL_NAV_BAR_LEFT,
    SEL_NAV_BAR_TITLE,
    SEL_DIALOG_POP,
    SEL_DIALOG_PREV_BTN,
)
from .captcha import (
    has_captcha as _has_captcha,
    handle_tencent_captcha as _handle_tencent_captcha,
)
from .base import BaseMixin

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
            3: ["必修", "课程学习"],
            2: ["选修", "自选"],
            1: ["匹配"],
        }
        labels = _tab_keywords.get(subject_type, [])
        if not labels:
            return False

        try:
            for label in labels:
                tab = self._page.locator(f'{SEL_COURSE_TAB}:has-text("{label}")')
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
                self.log.info(f"[章节] 展开: {title_text or f'#{i + 1}'}")
                # 很多时候展开章节会触发网络请求拉取列表，等待列表元素出现
                try:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_MARKERS, state="visible", timeout=5000
                    )
                except Exception:
                    time.sleep(2)
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

        if self._get_study_page_context() == "project-list":
            self.log.debug("[扫描] 当前处于项目列表页，跳过课程扫描")
            return []

        dom_tasks: list[dict[str, Any]] = []
        loc = self._page.locator(SEL_COURSE_LIST_CONTENT_ITEMS)
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
                or it.locator(SEL_ITEM_COMPLETED_ICON).count() > 0
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

    def _summarize_collapse_progress(self) -> dict[str, int] | None:
        """汇总折叠章节标题中的已完成/总数进度。"""
        if not self._page or self._page.is_closed():
            return None

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
        if collapse_items.count() == 0:
            return None

        total = 0
        completed = 0
        found_progress = False

        for i in range(collapse_items.count()):
            item = collapse_items.nth(i)
            title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
            if title_btn.count() == 0:
                continue
            try:
                title_text = title_btn.inner_text().strip()
            except Exception:
                continue

            finished_num, total_num = self._parse_section_progress(title_text)
            if finished_num is None or total_num is None:
                continue

            found_progress = True
            total += total_num
            completed += min(finished_num, total_num)

        if not found_progress:
            return None

        return {
            "total": total,
            "completed": completed,
            "incomplete": max(0, total - completed),
        }

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
        """获取课程运行时的核心框架 (通常是 iframe)。"""
        if not self._page or self._page.is_closed():
            return None
        try:
            # 优先尝试根据常见的微课域名或路径标识符查找
            for f in self._page.frames:
                if f == self._page.main_frame:
                    continue
                url = (f.url or "").lower()
                # 匹配 mcwk 域名或 course 路径
                if "mcwk.mycourse.cn" in url or "/course/" in url or "courseid=" in url:
                    return f

            # 如果没找到，尝试查找特定的 iframe 元素 (WeBan 详情页通常使用 .page-iframe)
            try:
                iframe_el = self._page.locator("iframe.page-iframe").first
                if iframe_el.count() > 0:
                    src = (iframe_el.get_attribute("src") or "").lower()
                    if src:
                        for f in self._page.frames:
                            if (f.url or "").lower() == src:
                                return f
            except Exception:
                pass

            return None
        except Exception as e:
            self.log.debug(f"[框架探测] 异常: {e}")
            return None

    def _wait_for_mcwk_runtime(self, timeout: float = 12) -> bool:
        """等待微课播放框架加载完成。"""
        end = time.time() + timeout
        self.log.debug("[播放] 等待课程框架加载...")
        while time.time() < end:
            f = self._get_course_runtime_frame()
            if f:
                try:
                    # 检查框架内是否已经加载了基本的 DOM 骨架或关键函数
                    if f.locator(SEL_RUNTIME_NAV_BTNS + ", .page-item").count() > 0:
                        self.log.debug(f"[播放] 识别到课程框架: {f.url[:60]}...")
                        return True
                    if f.evaluate("typeof finishWxCourse === 'function'"):
                        self.log.debug(
                            f"[播放] 识别到课程框架 (JS函数确认): {f.url[:60]}..."
                        )
                        return True
                except Exception:
                    pass
            time.sleep(1)

        # 调试：输出当前所有框架信息
        if self._page:
            self.log.debug(
                f"[框架诊断] 未找到课程框架。当前页面所有框架 ({len(self._page.frames)}):"
            )
            for i, f in enumerate(self._page.frames):
                self.log.debug(f"  - Frame {i}: URL={f.url[:80]}..., Name={f.name}")

        return False

    def _is_mcwk_course_page(self) -> bool:
        return self._get_course_runtime_frame() is not None

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

    def _setup_quiz_handler(self, frame=None):
        """设置答题响应监听器，自动捕获正确答案"""
        if not hasattr(self, "_last_quiz_answer"):
            self._last_quiz_answer = None
        if not hasattr(self, "_last_quiz_is_right"):
            self._last_quiz_is_right = None
        if not hasattr(self, "_quiz_attempted_answers"):
            self._quiz_attempted_answers = set()
        if not hasattr(self, "_current_question_type"):
            self._current_question_type = 1

        # 确保只绑定一次
        if not hasattr(self, "_has_quiz_handler"):
            if self._page:
                self._page.on("response", self._quiz_response_handler)
                self._has_quiz_handler = True
                self.log.debug("[答题监听] 已注册响应处理器")

    def _quiz_response_handler(self, response):
        try:
            url = response.url

            # 只处理 mercuryprovider/router 接口
            if "mercuryprovider/router" not in url:
                return

            self.log.debug(f"[响应] {url}")

            data = response.json()

            # 检查是否包含答案信息
            if isinstance(data, dict):
                d = data.get("data", {})
                if isinstance(d, dict):
                    # 记录所有包含 isRight 或 answerLabel 的响应
                    if "answerLabel" in d or "isRight" in d:
                        # 捕获答案标签
                        if "answerLabel" in d:
                            ans = d["answerLabel"]
                            if ans:
                                self._last_quiz_answer = ans
                                self.log.info(f"[答题响应] 答案: {ans}")
                        # 捕获答题结果
                        if "isRight" in d:
                            self._last_quiz_is_right = d["isRight"]
                            result = "正确" if d["isRight"] == 1 else "错误"
                            self.log.info(f"[答题响应] 结果: {result}")
                        return

        except Exception as e:
            self.log.debug(f"[响应处理异常] {e}")

    def _parse_answer_label(self, answer_label: str) -> list[int]:
        """解析答案标签为选项索引列表。

        格式: "-A-B-C-D" 表示选择 A、B、C、D
        返回: [0, 1, 2, 3] 对应 A、B、C、D 的索引
        """
        if not answer_label:
            return []

        indices = []
        # 匹配所有字母 A-Z
        for match in re.finditer(r"([A-Z])", answer_label):
            letter = match.group(1)
            idx = ord(letter) - ord("A")
            if 0 <= idx < 26:
                indices.append(idx)

        return indices

    def _get_next_untried_answer(
        self, options_count: int, question_type: int
    ) -> list[int]:
        """获取下一个未尝试过的答案组合。

        使用智能试错策略：
        1. 单选题：顺序尝试每个选项
        2. 多选题：从少到多尝试组合，避免全选一开始就排除
        """
        attempted = getattr(self, "_quiz_attempted_answers", set())

        if question_type == 1 or options_count <= 2:
            # 单选题或选项很少：顺序尝试每个选项
            for i in range(options_count):
                combo = (i,)
                if combo not in attempted:
                    return list(combo)
        else:
            # 多选题：按选择数量从少到多尝试
            for num_select in range(1, options_count + 1):
                for combo in combinations(range(options_count), num_select):
                    if combo not in attempted:
                        return list(combo)

        # 所有组合都试过了，返回第一个选项兜底
        return [0]

    def _trigger_img_text_completion(self, frame, title: str) -> bool:
        """
        高仿真微课播放逻辑。
        参考 item.js 与 sdk.js 中的交互逻辑，通过模拟点击对应元素来推进课程。
        """
        self._setup_quiz_handler()
        # 重置答题尝试记录（新课程开始时）
        if hasattr(self, "_quiz_attempted_answers"):
            self._quiz_attempted_answers.clear()
        else:
            self._quiz_attempted_answers = set()
        self._last_quiz_answer = None
        self._last_quiz_is_right = None
        self._current_question_type = 1

        try:
            if not frame:
                if not self._page:
                    return False
                frame = self._page

            self.log.info(f"[播放] 开始交互流程: {title}")
            self.log.debug(f"[播放] 目标框架 URL: {frame.url}")

            clicked = False
            # 去除固定 60 步限制，依赖外部超时或结束状态跳出
            consecutive_no_action = 0
            while True:
                # 每 2 秒点击一次（带点随机抖动）
                jitter = random.uniform(0.1, 0.5)
                time.sleep(2.0 + jitter)

                # 1. 检查验证码 (有且只有在 csCapt=true 且 weiban=weiban 下需要，参考 sdk.js)
                url = (frame.url or "").lower()
                if "weiban=weiban" in url and "cscapt=true" in url:
                    if _has_captcha(self._page):
                        self.log.info(
                            f"[验证码] 检测到课程内验证码（当前帧: {frame.url[:40]}...），开始自动处理..."
                        )
                        _handle_tencent_captcha(
                            self._page, self.log, require_cscapt=False
                        )
                        time.sleep(2)
                        continue

                # 2. 检查是否有视频正在播放或已结束
                video_ended = False
                try:
                    videos = frame.locator(SEL_RUNTIME_ACTIVE_VIDEO)
                    if videos.count() > 0:
                        # 获取视频状态
                        is_paused = videos.first.evaluate("el => el.paused")
                        is_ended = videos.first.evaluate("el => el.ended")

                        if is_ended:
                            video_ended = True
                        elif not is_paused:
                            self.log.debug(
                                "[视频] 视频正在播放中，等待动画/视频完成..."
                            )
                            clicked = True
                            consecutive_no_action = 0
                            continue
                except Exception:
                    pass

                # 3. 处理播放前的协议勾选 (如果有)
                try:
                    agree_cb = frame.locator(SEL_AGREE_CHECKBOX).first
                    if (
                        agree_cb.count() > 0
                        and agree_cb.is_visible()
                        and not agree_cb.is_checked()
                    ):
                        self.log.info("[互动] 勾选同意协议")
                        agree_cb.click(force=True)
                        time.sleep(0.5)
                        consecutive_no_action = 0
                except Exception:
                    pass

                # 4. 处理视频播放按钮
                if not video_ended:
                    video_play_btn = frame.locator(SEL_RUNTIME_VIDEO_PLAY_BTN).first
                    if video_play_btn.count() > 0 and video_play_btn.is_visible():
                        self.log.info("[互动] 发现未播放的视频/播放按钮，点击播放")
                        video_play_btn.click(force=True)
                        time.sleep(1)
                        clicked = True
                        consecutive_no_action = 0
                        continue

                # 4. 处理页面内的特定交互逻辑 (优先尝试标准逻辑)

                # Page 12: 单选逻辑 (通用)
                p12_choice = frame.locator(SEL_RUNTIME_CHOICE).first
                if p12_choice.count() > 0 and p12_choice.is_visible():
                    self.log.debug("[互动] 处理通用选择交互")
                    p12_choice.click(force=True)
                    time.sleep(0.5)

                # Page 17: 多项交互 (通用)
                p17_items = frame.locator(SEL_RUNTIME_INTERACTIVE_ITEMS)
                for i in range(p17_items.count()):
                    it = p17_items.nth(i)
                    if it.is_visible() and "brightness(0.7)" not in (
                        it.get_attribute("style") or ""
                    ):
                        self.log.debug(f"[互动] 点击 Page 17 弹窗元素 #{i + 1}")
                        it.click(force=True)
                        time.sleep(0.3)

                # Page 17: 关闭弹出层
                p17_close = frame.locator(SEL_RUNTIME_INTERACTIVE_CLOSE).first
                if p17_close.count() > 0 and p17_close.is_visible():
                    self.log.debug("[互动] 关闭 Page 17 弹窗")
                    p17_close.click(force=True)
                    time.sleep(0.5)

                # 5. 处理投票和答题逻辑 (参考 item.js)
                aq_labels = frame.locator(SEL_RUNTIME_QUIZ_LABELS)
                options_count = aq_labels.count()

                if options_count > 0 and aq_labels.first.is_visible():
                    # 检查是否已选中答案
                    checked_count = frame.locator(SEL_RUNTIME_QUIZ_CHECKED).count()
                    ans_label = getattr(self, "_last_quiz_answer", None)

                    self.log.debug(
                        f"[答题状态] 选项数: {options_count}, 已选: {checked_count}, 服务器答案: {ans_label}"
                    )

                    if checked_count == 0:
                        # 还没有选择任何选项，需要选择
                        try:
                            if ans_label:
                                # 有服务器返回的正确答案，使用它
                                correct_indices = self._parse_answer_label(ans_label)
                                letters = [chr(65 + i) for i in correct_indices]
                                is_right = getattr(self, "_last_quiz_is_right", None)

                                if is_right == 1:
                                    # 答案已确认正确，使用并清除
                                    self.log.info(
                                        f"[自动答题] 使用已确认答案: {ans_label} -> {letters}"
                                    )
                                    # 点击正确选项
                                    for idx in correct_indices:
                                        if idx < options_count:
                                            aq_labels.nth(idx).click(force=True)
                                            time.sleep(0.2)
                                    # 等待选项被选中
                                    for _ in range(10):
                                        time.sleep(0.2)
                                        if (
                                            frame.locator(
                                                SEL_RUNTIME_QUIZ_CHECKED
                                            ).count()
                                            > 0
                                        ):
                                            break
                                    # 清除，准备下一题
                                    self._last_quiz_answer = None
                                    self._last_quiz_is_right = None
                                    self._quiz_attempted_answers.clear()
                                else:
                                    # 服务器返回答案但未确认（刚试错后），直接使用
                                    self.log.info(
                                        f"[自动答题] 使用服务器答案: {ans_label} -> {letters}"
                                    )
                                    # 点击正确选项
                                    for idx in correct_indices:
                                        if idx < options_count:
                                            aq_labels.nth(idx).click(force=True)
                                            time.sleep(0.2)
                                    # 等待选项被选中
                                    for _ in range(10):
                                        time.sleep(0.2)
                                        if (
                                            frame.locator(
                                                SEL_RUNTIME_QUIZ_CHECKED
                                            ).count()
                                            > 0
                                        ):
                                            break

                            else:
                                # 无服务器答案，需要试错
                                question_type = getattr(
                                    self, "_current_question_type", 1
                                )
                                attempted = getattr(
                                    self, "_quiz_attempted_answers", set()
                                )
                                attempted_combo = self._get_next_untried_answer(
                                    options_count, question_type
                                )
                                letters = [chr(65 + i) for i in attempted_combo]

                                self.log.info(
                                    f"[自动答题] 试错: {letters} (已试 {len(attempted)} 次)"
                                )

                                # 记录这次尝试（使用 frozenset 以便添加到 set 中）
                                attempted.add(frozenset(attempted_combo))
                                self._quiz_attempted_answers = attempted

                                # 点击选项（使用 JavaScript 确保触发事件）
                                for idx in attempted_combo:
                                    if idx < options_count:
                                        label = aq_labels.nth(idx)
                                        # 尝试点击 label 内的 input，否则点击 label 本身
                                        input_el = label.locator("input").first
                                        if input_el.count() > 0:
                                            input_el.evaluate("el => el.click()")
                                        else:
                                            label.evaluate("el => el.click()")
                                        time.sleep(0.2)

                                # 等待选项被选中（最多等待 2 秒）
                                for _ in range(10):
                                    time.sleep(0.2)
                                    new_checked = frame.locator(
                                        SEL_RUNTIME_QUIZ_CHECKED
                                    ).count()
                                    if new_checked > 0:
                                        self.log.debug(
                                            f"[自动答题] 选项已选中: {new_checked} 个"
                                        )
                                        break

                        except Exception as e:
                            self.log.debug(f"[互动] 答题处理异常: {e}")
                            # 出错时兜底：点击第一个选项
                            try:
                                if aq_labels.count() > 0:
                                    aq_labels.nth(0).click(force=True)
                            except Exception:
                                pass
                    else:
                        # 已经有选项被选中了，等待导航按钮提交
                        self.log.debug(
                            f"[自动答题] 已选 {checked_count} 个选项，等待提交"
                        )

                # 6. 寻找推进按钮 (必须限定在 .page-active 下寻找)
                # 警告：绝对不要加入 .btn-base！因为 .btn-prev 和 .btn-next 都有 .btn-base 类！
                # 加入 .btn-base 会导致在某些页面匹配到 .btn-prev 从而无休止地疯狂后退引发死循环。
                nav_selectors = [sel.strip() for sel in SEL_RUNTIME_NAV_BTNS.split(",")]

                found_nav = False
                for sel in nav_selectors:
                    try:
                        btn = frame.locator(sel).first
                        if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                            btn_text = ""
                            try:
                                btn_text = btn.inner_text().strip()[:20]
                            except Exception:
                                pass
                            btn_name = sel.split(".")[-1].split(":")[0]
                            self.log.info(
                                f"[互动] 点击按钮: {btn_name}"
                                + (f" ({btn_text})" if btn_text else "")
                            )
                            btn.click(force=True, timeout=3000)
                            found_nav = True
                            clicked = True
                            consecutive_no_action = 0

                            # 如果是答题提交按钮，等待服务器响应
                            if btn_name in ("btn-aq", "btn-at"):
                                for _ in range(10):
                                    time.sleep(0.3)
                                    if self._last_quiz_answer is not None:
                                        self.log.info(
                                            f"[答题] 收到服务器答案: {self._last_quiz_answer}"
                                        )
                                        break
                            break
                    except Exception:
                        continue

                if not found_nav:
                    # 6. 如果没找到标准导航按钮，尝试探测页面内可能的交互元素 (根据内容自动判断)
                    try:
                        # 查找所有可见的 img, div, a (仅限激活页面)
                        probe_candidates = frame.locator(SEL_RUNTIME_PROBE_CANDIDATES)
                        for i in range(probe_candidates.count()):
                            cand = probe_candidates.nth(i)
                            if cand.is_visible() and cand.is_enabled():
                                # 启发式判断：游标为 pointer，或者类名包含关键特征
                                is_likely_button = cand.evaluate("""el => {
                                    const style = window.getComputedStyle(el);
                                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                                    if (style.cursor === 'pointer') return true;
                                    
                                    const cls = String(el.className || "");
                                    const id = String(el.id || "");
                                    const text = (el.innerText || "").trim();
                                    const src = el.tagName === 'IMG' ? String(el.getAttribute('src') || "") : "";
                                    
                                    if (el.tagName === 'BODY' || el.tagName === 'HTML' || el.tagName === 'SECTION') return false;
                                    if (/prev|back|return/i.test(cls + id + src)) return false;

                                    // 匹配常见的按钮/交互类名特征
                                    const keyPatterns = /btn|click|touch|item|box|label|aq-|ce|start|next|p\\d{2,}|submit|confirm/i;
                                    if (keyPatterns.test(cls + id + src)) {
                                        if (/bg|loader|container|slide|wrap|inner/i.test(cls + id)) return false;
                                        return true;
                                    }
                                    
                                    // 文本内容判断
                                    if (text.length > 0 && text.length < 10) {
                                        if (/(下一步|确定|开始|提交|继续|点击|查看|详情|选择|答案)/.test(text)) return true;
                                    }
                                    return false;
                                }""")

                                if is_likely_button:
                                    # 避免同一周期重复点击
                                    was_probed = cand.evaluate(
                                        "el => el.dataset.probed === 'true'"
                                    )
                                    if not was_probed:
                                        self.log.debug(
                                            f"[自动互动] 探测到潜在按钮 (Class: {cand.evaluate('el => el.className')})"
                                        )
                                        cand.evaluate(
                                            "el => el.dataset.probed = 'true'"
                                        )
                                        cand.click(force=True)
                                        time.sleep(1.0)  # 点击后等待响应
                                        found_nav = True
                                        consecutive_no_action = 0  # 重置计数
                                        break
                    except Exception as e:
                        self.log.debug(f"[自动互动] 探测过程异常: {e}")

                    if found_nav:
                        continue

                    # 检查是否已经到达结束页或列表页
                    if self._get_study_page_context() != "course-detail":
                        self.log.info("[播放] 课程已切换至列表或结果页，完成。")
                        return True

                    # 检查是否有完成对话框 (参考 sdk.js renderDialog)
                    if frame.locator(SEL_DIALOG_POP).count() > 0:
                        self.log.info("[播放] 检测到完成弹窗，点击返回")
                        frame.locator(SEL_DIALOG_PREV_BTN).click(force=True)
                        return True

                    # 连续 20 次没有发现新动作则认为已经结束（约40-50秒，用于等待缓慢的文本动画）
                    consecutive_no_action += 1
                    if consecutive_no_action % 5 == 0:
                        self.log.debug(
                            f"[播放] 正在等待页面状态更新 ({consecutive_no_action}/20)..."
                        )
                    if consecutive_no_action > 20:
                        self.log.info(
                            "[播放] 连续长时间无交互动作，判定当前交互已结束。"
                        )
                        break
                else:
                    consecutive_no_action = 0

            return clicked
        except Exception as e:
            err_msg = str(e).lower()
            if "frame was detached" in err_msg or "has been closed" in err_msg:
                self.log.info("[播放] 页面已跳转或框架被卸载，判定为完成。")
                return True
            self.log.warning(f"[互动] 播放过程异常: {str(e)}")
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
            if it.locator(SEL_ITEM_COMPLETED_ICON).count() > 0:
                return True
        return False

    def _finish_img_text_course(self, title: str, study_time: int) -> bool:
        start_time = time.time()
        for _att in range(3):
            # 必须等待框架出现
            self._wait_for_mcwk_runtime()
            f = self._get_course_runtime_frame()

            if not self._trigger_img_text_completion(f, title):
                continue

            # 点击流程完毕后，检查是否达到了设定的最少学习时长
            elapsed = time.time() - start_time
            remaining = int(study_time - elapsed)
            if remaining > 0:
                self.log.info(f"等待课程达标最小学习时长... 还需 {remaining} 秒")
                self._sleep_with_progress(remaining)

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
                btn_back = self._page.locator(SEL_COMMENT_BACK_BTN).first
                if btn_back.count() > 0 and btn_back.is_visible():
                    btn_back.scroll_into_view_if_needed(timeout=2000)
                    btn_back.click(timeout=5000)
                    time.sleep(1)

            ctx_now = self._get_study_page_context()
            if ctx_now not in ("course-list", "collapse-list"):
                if self._page:
                    btn_nav_back = self._page.locator(SEL_NAV_BAR_LEFT).first
                    if btn_nav_back.count() > 0 and btn_nav_back.is_visible():
                        self.log.debug("[导航] 尝试点击顶部导航返回按钮兜底...")
                        btn_nav_back.click(timeout=5000)
                        time.sleep(1)

            ctx_now = self._get_study_page_context()
            if ctx_now not in ("course-list", "collapse-list"):
                if self._page and "undefined" in (self._page.url or ""):
                    self.log.warning(
                        "[导航] 发现异常的 undefined 路由，尝试强制浏览器后退..."
                    )
                    self._page.go_back(timeout=5000)
                    time.sleep(1)

            return self._get_study_page_context() in ("course-list", "collapse-list")
        except Exception as e:
            self.log.warning(f"[导航] 返回列表页过程异常: {e}")
            return False

    def _goto_next_project(
        self, state: _StudyRunState, completed: set, study_mode: str = "true"
    ) -> bool:
        if not self._page or self._page.is_closed():
            return False
        ctx = self._get_study_page_context()
        if ctx in ("course-list", "collapse-list"):
            # 如果当前项目已经处理过，不要直接结束，应该尝试回到列表页寻找下一个
            if state.current_project_title and state.current_project_title in completed:
                self.log.debug(
                    f"项目「{state.current_project_title}」已处理完毕，返回列表页"
                )
                # 强制导航回列表页
                try:
                    self._page.goto(
                        f"{self.base_url}/#/learning-task-list",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    time.sleep(3)
                    state.current_project_title = ""
                    state.study_tabs = []
                    state.active_section_index = -1
                except Exception:
                    return False
                ctx = self._get_study_page_context()
                if ctx not in ("course-list", "collapse-list"):
                    # 已回到项目中心，继续按项目导航逻辑处理下一个项目
                    pass
                else:
                    self.log.debug(
                        "[导航] 返回项目中心后仍处于课程页，继续按当前项目处理"
                    )
                    try:
                        nav_title = self._page.locator(SEL_NAV_BAR_TITLE).first
                        if nav_title.count() > 0:
                            self.project_title = nav_title.inner_text().strip()
                    except Exception:
                        pass
                    if not state.current_project_title:
                        state.current_project_title = self.project_title or "未知项目"
                    state.study_tabs = self._get_current_study_tabs()
                    return True
            else:
                self.log.debug("[导航] 当前已在课程列表页，直接处理该项目")
                try:
                    nav_title = self._page.locator(SEL_NAV_BAR_TITLE).first
                    if nav_title.count() > 0:
                        self.project_title = nav_title.inner_text().strip()
                except Exception:
                    pass
                if (
                    not state.current_project_title
                    and not (self.project_title or "").strip()
                ):
                    self.log.debug(
                        "[导航] 当前课程页缺少可靠项目标题，先返回项目中心重新定位项目"
                    )
                    try:
                        self._page.goto(
                            f"{self.base_url}/#/learning-task-list",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        time.sleep(3)
                    except Exception:
                        return False
                    ctx = self._get_study_page_context()
                    if ctx not in ("course-list", "collapse-list"):
                        # 已回到项目中心，走下方通用项目导航逻辑
                        pass
                    else:
                        self.project_title = self.project_title or "未知项目"
                        state.current_project_title = self.project_title
                        state.study_tabs = self._get_current_study_tabs()
                        return True
                else:
                    if not state.current_project_title:
                        state.current_project_title = self.project_title or "未知项目"
                    state.study_tabs = self._get_current_study_tabs()
                    return True

        self.log.info("正在导航至学习项目中心...")
        try:
            self._page.goto(
                f"{self.base_url}/#/learning-task-list",
                wait_until="domcontentloaded",
                timeout=15000,
            )
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

        try:
            # 先返回课程列表页
            self._return_to_chapter_list()
            time.sleep(2)

            collapse_stats = self._summarize_collapse_progress()
            if collapse_stats is not None:
                self.log.info(
                    "[课程统计] 章节汇总 - "
                    f"总课程数: {collapse_stats['total']}, "
                    f"已完成: {collapse_stats['completed']}, "
                    f"未完成: {collapse_stats['incomplete']}"
                )
                return collapse_stats

            # 扫描所有课程项
            tasks = self._collect_tasks_in_current_tab()
            total = len(tasks)
            completed_count = sum(1 for task in tasks if task.get("passed"))
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
            try:
                self._page.goto(
                    f"{self.base_url}/#/learning-task-list",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                time.sleep(3)
                self._dismiss_broadcast()
            except Exception:
                self.log.debug("[导航] 初始化跳转项目中心失败，继续使用当前页面状态")

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
