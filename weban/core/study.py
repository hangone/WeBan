import re
import time
import datetime
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
    SEL_COLLAPSE_CELL_TITLE,
    SEL_BROADCAST_MODAL,
    SEL_COMMENT_BACK_BTN,
    SEL_NAV_BAR_LEFT,
    SEL_DIALOG_POP,
    SEL_ITEM_TITLE_TEXT,
    SEL_RUNTIME_MARKERS,
    SEL_RUNTIME_FRAME_SKELETON,
    SEL_COURSE_JS_ITEMS_VISIBLE,
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
    handle_click_captcha as _handle_click_captcha,
    _log_captcha_contexts as _log_captcha_contexts,
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
# 运行状态数据类（取代原闭包中的 nonlocal 变量）
# ---------------------------------------------------------------------------


@dataclass
class _StudyRunState:
    """run_study 运行期间的可变状态，在各拆分方法之间通过传参共享。

    字段说明：
      study_tabs          当前项目需遍历的 subjectType 列表
      active_tab_index    当前正在学习的 Tab 在 study_tabs 中的下标
      current_project_title  当前学习项目标题，完成时计入 completed_projects
      active_section_index   当前所在折叠章节的下标，防止重复展开陷入死循环
    """

    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""
    active_section_index: int = -1


# ---------------------------------------------------------------------------
# StudyMixin
# ---------------------------------------------------------------------------


class StudyMixin(BaseMixin):
    """自动学习流程 Mixin，通过多重继承供 WeBanClient 使用。"""

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

    # ------------------------------------------------------------------
    # 页面结构辅助方法
    # ------------------------------------------------------------------

    def _detect_project_type(self) -> str:
        """从统一页面状态中推断 projectType（pre / normal / special 等）。"""
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
        """处理承诺书/协议签署页（ProtocolPageWk.vue / ProtocolPage.vue）。

        自动勾选同意框并点击下一步；若有签名提交按钮也一并点击。
        返回是否处理了该页面（True = 检测到并已处理）。
        """
        if not self._page:
            return False
        try:
            agree_cb = self._page.locator(SEL_AGREE_CHECKBOX)
            next_btn = self._page.locator(SEL_BTN_NEXT_STEP)
            if agree_cb.count() == 0 and next_btn.count() == 0:
                return False

            self.log.info("[协议] 检测到承诺书/协议页，自动同意")
            if agree_cb.count() > 0 and not agree_cb.first.is_checked():
                agree_cb.first.click(force=True)
                time.sleep(0.5)

            if next_btn.count() > 0 and next_btn.first.is_visible():
                next_btn.first.click(force=True)
                time.sleep(2)

            # 签名画布页：直接点击"提交"
            submit_btn = self._page.locator(SEL_BTN_SUBMIT_SIGN)
            if submit_btn.count() > 0 and submit_btn.first.is_visible():
                submit_btn.first.click(force=True)
                time.sleep(2)

            return True
        except Exception as e:
            self.log.warning(f"[协议] 处理承诺书页异常: {e}")
            return False

    def _handle_special_index(self) -> bool:
        """处理专题/实验室中间列表页（SpecialIndex.vue）。

        点击第一个未完成的子项目，返回是否成功点击进入下一层。
        """
        if not self._page:
            return False
        try:
            blocks = self._page.locator(SEL_TASK_OR_IMG_BLOCK)
            if blocks.count() == 0:
                return False
            self.log.info(
                f"[专题/实验室] 检测到中间列表页，共 {blocks.count()} 个子项目"
            )
            # 优先点击未完成的子项目
            for i in range(blocks.count()):
                blk = blocks.nth(i)
                if blk.locator(SEL_TASK_DONE_LABEL).count() > 0:
                    continue  # 跳过已完成
                blk.click(force=True)
                time.sleep(3)
                return True
            # 全部完成则点最后一个
            blocks.last.click(force=True)
            time.sleep(3)
            return True
        except Exception as e:
            self.log.warning(f"[专题/实验室] 处理中间页异常: {e}")
            return False

    def _handle_intermediate_pages(self) -> None:
        """进入项目后，自动处理可能出现的中间页（最多循环 5 次）。

        处理顺序：
          1. 协议/承诺书页（ProtocolPageWk）
          2. 专题/实验室中间列表页（SpecialIndex）
          3. LabIndex 的 .img-text-block 列表
        直到进入真正的课程列表页（含 .van-collapse-item / .img-texts-item / .fchl-item）为止。
        """
        if not self._page:
            return

        for _round in range(5):
            time.sleep(1)

            # 检查 _page 是否仍然有效
            if not self._page:
                return

            # 已在课程列表页，直接返回
            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return

            if self._handle_protocol_page():
                continue

            if self._handle_special_index():
                continue

            # LabIndex（.img-text-block 独立存在）
            if not self._page:
                return
            lab_blocks = self._page.locator(SEL_IMG_TEXT_BLOCK)
            if lab_blocks.count() > 0:
                self.log.info("[实验室] 检测到 LabIndex 页，点击第一个实验项目")
                lab_blocks.first.click(force=True)
                time.sleep(3)
                continue

            # 等待任意一种课程结构出现，超时则跳出本轮探测
            try:
                if self._page:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_WAIT_TARGETS,
                        timeout=5000,
                    )
            except Exception:
                pass

        # 始终未能进入课程页，呼叫用户干预
        if self._page and self._page.locator(SEL_COURSE_LIST_MARKERS).count() == 0:
            self._pause_for_user_intervention(
                "无法突破中间页到达章节列表，请确认是否存在未知的反爬验证或签到，手动点击进入课程目录页。"
            )

    def _get_current_study_tabs(self) -> List[int]:
        """根据当前页面 URL 的 projectType 返回需要遍历的 subjectType 列表。"""
        pt = self._detect_project_type()
        return list(_PROJECT_STUDY_TABS.get(pt, [3, 2]))

    def _switch_to_study_tab(self, subject_type: int) -> bool:
        """在 CourseIndex 页切换到指定 subjectType 对应的 Tab。

        subjectType 对应关系：3=必修课程  2=选修课程  1=匹配课程
        返回是否切换成功（Tab 不存在时返回 False）。
        """
        if not self._page:
            return False

        _tab_labels = {3: "必修课程", 2: "选修课程", 1: "匹配课程"}
        label = _tab_labels.get(subject_type, "")
        if not label:
            return False
        try:
            tab = self._page.locator(f'.van-tab:has-text("{label}")')
            if tab.count() == 0:
                return False
            # 已经是激活状态，不需要再点击
            if "van-tab--active" not in (tab.first.get_attribute("class") or ""):
                tab.first.click(force=True)
                time.sleep(1.5)
            page_state = self._ensure_page_state()
            self.log.debug(
                f"[Tab] 切换到「{label}」"
                f" state={page_state['state']} path={page_state['path'] or '<root>'}"
            )
            return True
        except Exception as e:
            self.log.warning(f"[Tab] 切换 Tab ({label}) 失败: {e}")
            return False

    def _extract_item_title(self, item) -> str:
        """从 .img-texts-item 或 .fchl-item 中提取课程标题。

        优先取 .title / .fchl-item-content-title 子元素文本，
        失败时回退到整体文本的第一行。
        """
        try:
            title_el = item.locator(SEL_ITEM_TITLE_TEXT)
            if title_el.count() > 0:
                return title_el.first.inner_text().strip()
        except Exception:
            pass
        try:
            return item.inner_text().strip().split("\n")[0].strip()
        except Exception:
            return ""

    def _find_fchl_target(
        self, study_mode: str, failed_courses: set, completed_courses: set
    ):
        """在 fchl 页面（.fchl-item 结构）中查找下一个未完成的课程项。

        force 模式：从全部项目中查找（含已通过）。
        其他模式：仅查找未通过（:not(.fchl-item-active)）的项目。
        """
        selectors = (
            [SEL_FCHL_ITEM_VISIBLE, SEL_FCHL_ITEM]
            if study_mode == "force"
            else [
                SEL_FCHL_ITEM_NOT_PASSED_VISIBLE,
                SEL_FCHL_ITEM_NOT_PASSED,
            ]
        )
        if not self._page:
            return None
        for sel in selectors:
            items = self._page.locator(sel)
            for i in range(items.count()):
                item = items.nth(i)
                title = self._extract_item_title(item)
                if (
                    title
                    and title not in failed_courses
                    and title not in completed_courses
                ):
                    return item
        return None

    def _get_study_page_context(self) -> str:
        """基于统一页面状态判断当前学习页面上下文。"""
        if not self._page:
            return "unknown"

        page_state = self._ensure_page_state()
        route_path = str(page_state.get("path", "")).strip("/")
        query = page_state.get("query", {})
        state = str(page_state.get("state", "unknown"))

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
            if self._page.locator(SEL_IMG_TEXT_BLOCK).count() > 0:
                return "sub-project-list"
            if self._page.locator(SEL_TASK_BLOCK).count() > 0:
                return "project-list"
        except Exception:
            pass

        if route_path == "learning-task-list":
            if query.get("projectType") or query.get("subjectType"):
                return "course-list"
            return "project-list"

        if state == "study":
            return "course-detail"
        if state == "project_list":
            return "project-list"
        if state == "course_list":
            return "course-list"

        return "unknown"

    def _count_page_tasks(self) -> tuple:
        """统计当前页面的课程总数和已完成数，返回 (total, finished)。"""
        if not self._page:
            return (0, 0)

        context = self._get_study_page_context()
        if context not in {"collapse-list", "course-list"}:
            self.log.debug(f"[课程统计] 跳过非课程列表上下文: {context}")
            return (0, 0)

        fchl_items = self._page.locator(SEL_FCHL_ITEM)
        if fchl_items.count() > 0:
            finished = self._page.locator(f"{SEL_FCHL_ITEM}.fchl-item-active").count()
            return fchl_items.count(), finished

        img_items = self._page.locator(SEL_IMG_TEXT_ITEM)
        if img_items.count() > 0:
            finished = self._page.locator(f"{SEL_IMG_TEXT_ITEM}.passed").count()
            return img_items.count(), finished

        total, finished = 0, 0
        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
        for i in range(collapse_items.count()):
            title_el = collapse_items.nth(i).locator(SEL_COLLAPSE_CELL_TITLE)
            if title_el.count() == 0:
                continue
            text = title_el.first.inner_text().strip()
            m = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if m:
                f, t = int(m.group(1)), int(m.group(2))
                total += t
                finished += f
        return total, finished

    def _current_tab_has_unfinished_courses(
        self,
        study_mode: str,
        completed_courses: set,
        failed_courses: set,
    ) -> bool:
        """判断当前 Tab 是否仍存在未完成课程。"""
        if not self._page:
            return False

        context = self._get_study_page_context()
        if context not in {"collapse-list", "course-list"}:
            self.log.debug(f"[课程判定] 跳过非课程列表上下文: {context}")
            return False

        try:
            if study_mode == "force":
                if (
                    self._page.locator(f"{SEL_IMG_TEXT_ITEM}, {SEL_FCHL_ITEM}").count()
                    > 0
                ):
                    return True
            else:
                items = self._page.locator(SEL_IMG_TEXT_ITEM_NOT_PASSED)
                for i in range(items.count()):
                    item = items.nth(i)
                    title = self._extract_item_title(item)
                    if (
                        title
                        and title not in completed_courses
                        and title not in failed_courses
                    ):
                        return True

                items = self._page.locator(SEL_FCHL_ITEM_NOT_PASSED)
                for i in range(items.count()):
                    item = items.nth(i)
                    title = self._extract_item_title(item)
                    if (
                        title
                        and title not in completed_courses
                        and title not in failed_courses
                    ):
                        return True
        except Exception:
            pass

        try:
            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            for i in range(collapse_items.count()):
                title_el = collapse_items.nth(i).locator(SEL_COLLAPSE_CELL_TITLE)
                if title_el.count() == 0:
                    continue
                text = title_el.first.inner_text().strip()
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if not m:
                    if study_mode == "force":
                        return True
                    continue
                finished, total = int(m.group(1)), int(m.group(2))
                if total > 0 and (study_mode == "force" or finished < total):
                    return True
        except Exception:
            pass

        return False

    def _log_round_start(
        self, current_round: int, all_tasks: int, study_time: int
    ) -> None:
        """输出新一轮强制学习开始的日志（含课程数和预计用时）。"""
        round_seconds = all_tasks * study_time
        m, s = divmod(round_seconds, 60)
        self.log.info(
            f"--- 第 {current_round} 轮开始（共 {all_tasks} 课，预计用时 {m}分{s}秒）---"
        )

    # _sleep_with_progress removed (moved to BaseMixin)

    def _new_round(
        self,
        current_round: int,
        all_tasks: int,
        study_time: int,
        completed_courses: set,
    ) -> int:
        """清空本轮已完成集合，递增轮次并输出日志，返回新轮次号。"""
        next_round = current_round + 1
        completed_courses.clear()
        self._log_round_start(next_round, all_tasks, study_time)
        return next_round

    def _expand_next_section(
        self,
        state: _StudyRunState,
        study_mode: str,
        completed_courses: set,
        failed_courses: set,
    ) -> bool:
        """尝试展开下一个有未完成课程的折叠章节（.van-collapse-item）。

        非强制模式下跳过已全部完成（完成数 >= 总数）的章节。
        返回 True 表示成功展开了一个新章节，False 表示所有章节均已展开或完成。
        """
        if not self._page:
            return False

        if self._get_study_page_context() != "collapse-list":
            return False

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)

        for i in range(state.active_section_index + 1, collapse_items.count()):
            item = collapse_items.nth(i)

            if study_mode != "force":
                try:
                    title_el = item.locator(SEL_COLLAPSE_CELL_TITLE)
                    if title_el.count() > 0:
                        text = title_el.first.inner_text().strip()
                        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                        if m and int(m.group(1)) >= int(m.group(2)):
                            continue
                except Exception:
                    pass

            btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
            try:
                if btn.count() > 0:
                    expanded = (
                        btn.get_attribute("aria-expanded") or ""
                    ).lower() == "true"
                    if not expanded:
                        btn.click(force=True)
                else:
                    item.click(force=True)
            except Exception:
                continue

            state.active_section_index = i

            for _wait in range(8):
                time.sleep(1)
                try:
                    if self._page.locator(SEL_COURSE_JS_ITEMS_VISIBLE).count() > 0:
                        break
                except Exception:
                    pass

            time.sleep(1)
            return True
        return False

    # ------------------------------------------------------------------
    # 从 run_study 提升的辅助方法（原为闭包，现为类方法）
    # ------------------------------------------------------------------

    # _pause_for_user_intervention removed (moved to BaseMixin)

    def _dismiss_broadcast(self) -> None:
        """检测并关闭广播公告弹窗（.broadcast-modal），有则点击关闭按钮。"""
        try:
            if self._page:
                broadcast = self._page.locator(SEL_BROADCAST_MODAL)
                if broadcast.count() > 0 and broadcast.first.is_visible():
                    broadcast.first.locator("button").first.click(force=True)
                    self.log.info("[公告] 已关闭广播公告弹窗")
                    time.sleep(0.5)
        except Exception:
            pass

    def _get_course_runtime_frame(self):
        """获取课程详情页内承载 mcwk 课件运行时的 iframe。"""
        if not self._page:
            return None
        try:
            for frame in self._page.frames:
                if frame == self._page.main_frame:
                    continue

                try:
                    raw_frame_url = (frame.url or "").strip()
                except Exception:
                    raw_frame_url = ""

                frame_url = raw_frame_url.lower()
                has_url = bool(raw_frame_url)
                is_mycourse_host = (
                    ".mycourse.cn" in frame_url or "mycourse.cn" in frame_url
                )

                try:
                    has_runtime = frame.evaluate(
                        "typeof finishWxCourse === 'function' || typeof backToList === 'function' || typeof callApinext === 'function'"
                    )
                except Exception:
                    has_runtime = False

                try:
                    has_course_markers = frame.locator(SEL_RUNTIME_MARKERS).count() > 0
                except Exception:
                    has_course_markers = False

                is_mcwk_host = "mcwk.mycourse.cn" in frame_url
                has_cscapt = "cscapt=" in frame_url
                if "cscapt=true" in frame_url:
                    cscapt_state = "true"
                elif has_cscapt:
                    cscapt_state = "false"
                else:
                    cscapt_state = "missing"

                if has_url and is_mycourse_host:
                    self.log.debug(
                        f"[img-texts] 检查 iframe: url={raw_frame_url}, has_runtime={has_runtime}, has_course_markers={has_course_markers}, is_mcwk_host={is_mcwk_host}, cscapt={cscapt_state}"
                    )

                if has_runtime or is_mcwk_host:
                    if has_url and is_mycourse_host:
                        self.log.debug(
                            f"[img-texts] 命中课程 iframe: url={raw_frame_url}, cscapt={cscapt_state}"
                        )
                    else:
                        self.log.debug(
                            "[img-texts] 命中匿名课程 iframe: reason=runtime"
                        )
                    return frame

                if has_course_markers and not has_url:
                    self.log.debug("[img-texts] 命中匿名课程 iframe: reason=markers")
                    return frame
        except Exception as e:
            self.log.debug(f"[img-texts] 枚举 iframe 异常: error={e}")

        return None

    def _is_mcwk_course_page(self) -> bool:
        """判断当前是否处于 mcwk 课件页或其 iframe 已加载。"""
        try:
            if self._page and "mcwk.mycourse.cn" in self._page.url.lower():
                return True
        except Exception:
            pass
        return self._get_course_runtime_frame() is not None

    def _wait_for_mcwk_runtime(self, timeout_sec: float = 8) -> bool:
        """等待 mcwk 课件运行时加载完成。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            course_frame = self._get_course_runtime_frame()
            if course_frame is None:
                time.sleep(0.5)
                continue

            try:
                ready = course_frame.evaluate(
                    "typeof finishWxCourse === 'function' || typeof backToList === 'function' || typeof callApinext === 'function'"
                )
                if ready:
                    self.log.debug("[img-texts] mcwk iframe 运行时已加载完成")
                    return True
            except Exception:
                pass

            try:
                if course_frame.locator(SEL_RUNTIME_FRAME_SKELETON).count() > 0:
                    self.log.debug(
                        "[img-texts] 课程 iframe 页面骨架已出现，继续等待运行时"
                    )
            except Exception:
                pass

            time.sleep(0.5)
        return False

    def _wait_for_post_course_state(self, timeout_sec: float = 8) -> bool:
        """等待课程页稳定就绪或回到列表/评论状态。"""
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            try:
                if (
                    self._page
                    and self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0
                ):
                    return True
            except Exception:
                pass

            try:
                if self._page and _has_captcha(self._page):
                    self.log.info(
                        "[img-texts] 课程完成阶段检测到点选验证码，开始自动处理"
                    )
                    captcha_ok = _handle_click_captcha(self._page, self.log)
                    if captcha_ok:
                        self.log.info(
                            "[img-texts] 点选验证码处理完成，继续等待课程完成状态变化"
                        )
                        time.sleep(2)
                        continue
                    self.log.warning(
                        "[img-texts] 点选验证码未确认通过，继续等待页面变化"
                    )
            except Exception as e:
                self.log.debug(f"[img-texts] 处理课程完成后的点选验证码异常: {e}")

            course_frame = self._get_course_runtime_frame()
            if course_frame is not None:
                try:
                    if (
                        course_frame.locator(
                            f"{SEL_RUNTIME_MARKERS}, {SEL_DIALOG_POP}"
                        ).count()
                        > 0
                    ):
                        return True
                except Exception:
                    pass

                try:
                    has_course_api = course_frame.evaluate(
                        "typeof callApinext === 'function' || typeof backToList === 'function' || typeof finishWxCourse === 'function'"
                    )
                    if has_course_api:
                        return True
                except Exception:
                    pass

            try:
                if self._page:
                    return_btn = self._page.locator(SEL_COMMENT_BACK_BTN)
                    if return_btn.count() > 0 and return_btn.first.is_visible():
                        return True
            except Exception:
                pass

            try:
                if self._page:
                    url = self._page.url.lower()
                    if "comment" in url or "rating" in url:
                        return True
            except Exception:
                pass

            # 循环检查 mcwk iframe 中是否存在评论页元素
            deadline = time.time() + timeout_sec
            last_error = ""
            while time.time() < deadline:
                course_frame = self._get_course_runtime_frame()
                if course_frame is None:
                    time.sleep(0.5)
                    continue

                try:
                    # 检查评论区是否存在
                    comment_area = course_frame.locator(".comment-area, .rating-area")
                    if comment_area.count() > 0:
                        return True

                    # 检查评分按钮是否存在
                    submit_btn = course_frame.locator(
                        "button:has-text('提交'), button:has-text('确定')"
                    )
                    if submit_btn.count() > 0 and submit_btn.first.is_visible():
                        return True
                except Exception as e:
                    last_error = str(e)

                time.sleep(0.5)

            if last_error:
                self.log.debug(f"[img-texts] 未检测到课程完成标志: error={last_error}")
            return False

            time.sleep(0.5)

        return False

    def _trigger_img_text_completion(self, course_frame, title: str) -> bool:
        """只触发课程完成动作，不立即返回列表。"""
        if course_frame is not None:
            try:
                action = course_frame.evaluate(
                    """() => {
                        let acted = [];
                        if (typeof finishWxCourse === 'function') {
                            finishWxCourse();
                            acted.push('finishWxCourse');
                        }
                        if (typeof callApinext === 'function') {
                            callApinext('next', 1);
                            acted.push('callApinext');
                        }
                        return acted.join(',');
                    }"""
                )
                if action:
                    self.log.debug(
                        f"[img-texts] iframe 已执行完成动作: title={title}, action={action}"
                    )
                    return True
                self.log.debug(f"[img-texts] iframe 未命中完成动作: title={title}")
                return False
            except Exception as e:
                self.log.warning(f"[img-texts] 课程完成流程异常，第 1 次：{title}")
                self.log.debug(
                    f"[img-texts] iframe 完成动作异常: title={title}, error={e}"
                )
                return False

        try:
            if not self._page:
                return False
            has_finish_func = self._page.evaluate(
                "typeof finishWxCourse === 'function'"
            )
        except Exception:
            has_finish_func = False

        self.log.debug(
            f"[img-texts] 顶层页完成函数检测: title={title}, has_finish_func={has_finish_func}"
        )

        if not has_finish_func:
            return False

        try:
            if self._page:
                self._page.evaluate("finishWxCourse()")
                self.log.debug(
                    f"[img-texts] 已调用顶层 finishWxCourse(): title={title}"
                )
            return True
        except Exception as e:
            self.log.warning(f"[img-texts] 课程完成流程异常，第 1 次：{title}")
            self.log.debug(
                f"[img-texts] 顶层页发送完成标记异常: title={title}, error={e}"
            )
            return False

    def _wait_for_img_text_completion_result(self, timeout_sec: float = 12) -> str:
        """等待真实完成结果，不在发送完成后立刻返回。"""
        if not self._page:
            return "error"

        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            try:
                if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                    return "list"
            except Exception:
                pass

            try:
                if self._page and _has_captcha(self._page):
                    self.log.info(
                        "[img-texts] 课程完成结果等待阶段检测到点选验证码，开始自动处理"
                    )
                    captcha_ok = _handle_click_captcha(self._page, self.log)
                    if captcha_ok:
                        self.log.info(
                            "[img-texts] 点选验证码验证通过，继续等待课程完成结果"
                        )
                        time.sleep(2)
                        continue
                    self.log.warning(
                        "[img-texts] 点选验证码未确认通过，继续等待完成结果"
                    )
            except Exception as e:
                self.log.debug(f"[img-texts] 完成结果等待期间处理点选验证码异常: {e}")

            if not self._page:
                continue

            try:
                url = self._page.url.lower()
                if "comment" in url or "rating" in url:
                    return "comment"
            except Exception:
                pass

            try:
                if self._page:
                    return_btn = self._page.locator(SEL_COMMENT_BACK_BTN)
                    if return_btn.count() > 0 and return_btn.first.is_visible():
                        return "return"
            except Exception:
                pass

            course_frame = self._get_course_runtime_frame()
            if course_frame is not None:
                try:
                    if course_frame.locator(SEL_DIALOG_POP).count() > 0:
                        return "dialog"
                except Exception:
                    pass

            time.sleep(0.5)

        return ""

    def _find_img_text_item_by_title(self, title: str):
        """按标题在章节列表中查找图文课程项。"""
        selectors = [
            SEL_IMG_TEXT_ITEM_NOT_PASSED,
            SEL_IMG_TEXT_ITEM,
        ]
        if not self._page:
            return None
        for sel in selectors:
            items = self._page.locator(sel)
            self.log.debug(
                f"[img-texts] 按标题查找课程项: selector={sel}, count={items.count()}, title={title}"
            )
            for i in range(items.count()):
                item = items.nth(i)
                current_title = self._extract_item_title(item)
                self.log.debug(
                    f"[img-texts] 检查课程项: selector={sel}, index={i}, extracted_title={current_title}"
                )
                if current_title == title:
                    self.log.debug(
                        f"[img-texts] 命中课程项: selector={sel}, index={i}, title={title}"
                    )
                    return item
        self.log.debug(f"[img-texts] 未找到课程项: title={title}")
        return None

    def _is_img_text_course_passed(self, title: str) -> bool:
        """通过 Vue 数据或列表项 css class 确认图文课程是否真正完成。"""
        if not self._page:
            return False
        # 优先通过 Vue 组件读取数据
        try:
            is_passed = self._page.evaluate(
                """(title) => {
                    const page = document.querySelector('.page');
                    if (page && page.__vue__ && page.__vue__.courseList) {
                        const course = page.__vue__.courseList.find(c => c.resourceName === title);
                        if (course) return Number(course.finished) === 1;
                    }
                    return null;
                }""",
                title,
            )
            if is_passed is not None:
                self.log.debug(
                    f"[Vue解析] 课程完成状态: title={title}, passed={is_passed}"
                )
                return bool(is_passed)
        except Exception:
            pass

        item = self._find_img_text_item_by_title(title)
        if item is None:
            return False
        try:
            klass = item.get_attribute("class") or ""
            return "passed" in klass
        except Exception:
            return False

    def _finish_img_text_course(
        self, title: str, study_time: int, max_attempts: int = 3
    ) -> bool:
        """完成图文课并以列表 passed 状态为准校验，失败时重试整门课程。"""
        for attempt in range(1, max_attempts + 1):
            current_url = ""
            try:
                if self._page:
                    current_url = self._page.url
            except Exception:
                pass
            self.log.debug(
                f"[img-texts] 开始完成校验: title={title}, attempt={attempt}/{max_attempts}, url={current_url}"
            )

            course_frame = self._get_course_runtime_frame()
            if course_frame is not None:
                runtime_ready = self._wait_for_mcwk_runtime(timeout_sec=8)
                self.log.debug(
                    f"[img-texts] iframe 运行时检测结果: title={title}, ready={runtime_ready}"
                )
                if not runtime_ready:
                    if attempt < max_attempts:
                        self.log.warning(
                            f"[img-texts] 课程完成动作未就绪，第 {attempt}/{max_attempts} 次：{title}"
                        )
                    continue

            triggered = self._trigger_img_text_completion(course_frame, title)
            self.log.debug(
                f"[img-texts] 完成动作触发结果: title={title}, attempt={attempt}/{max_attempts}, triggered={triggered}"
            )
            if not triggered:
                if attempt < max_attempts:
                    self.log.warning(
                        f"[img-texts] 课程完成动作未就绪，第 {attempt}/{max_attempts} 次：{title}"
                    )
                continue

            try:
                needs_captcha = False
                if course_frame is not None:
                    try:
                        frame_url = (course_frame.url or "").lower()
                        needs_captcha = (
                            "mcwk.mycourse.cn" in frame_url
                            and "cscapt=true" in frame_url
                        )
                    except Exception:
                        needs_captcha = False

                if needs_captcha and self._page:
                    captcha_handled = False
                    for _ in range(20):
                        if _has_captcha(self._page):
                            self.log.info(
                                "[img-texts] 课程完成阶段检测到点选验证码，开始自动处理"
                            )
                            captcha_ok = _handle_click_captcha(self._page, self.log)
                            captcha_handled = True
                            if captcha_ok:
                                self.log.info("[img-texts] 点选验证码已处理完成")
                                time.sleep(2)
                            else:
                                self.log.warning("[img-texts] 点选验证码未确认通过")
                            break

                        time.sleep(0.5)

                    if not captcha_handled:
                        self.log.debug(
                            "[img-texts] cscapt=true，但在课程完成阶段等待窗口内未检测到点选验证码"
                        )
                else:
                    time.sleep(1)
            except Exception as e:
                self.log.debug(f"[img-texts] finishWxCourse 后处理点选验证码异常: {e}")

            completion_state = self._wait_for_img_text_completion_result(timeout_sec=12)
            self.log.debug(
                f"[img-texts] 完成结果状态: title={title}, attempt={attempt}/{max_attempts}, state={completion_state}"
            )

            if not completion_state:
                if attempt < max_attempts:
                    self.log.warning(
                        f"[img-texts] 未等待到真实完成结果，准备重试整门课程，第 {attempt + 1}/{max_attempts} 次：{title}"
                    )
                continue

            returned = completion_state == "list"
            if not returned:
                if not self._return_to_chapter_list():
                    if attempt < max_attempts:
                        self.log.warning(
                            f"[img-texts] 完成后返回章节列表失败，准备重试整门课程，第 {attempt + 1}/{max_attempts} 次：{title}"
                        )
                    self.log.debug(
                        f"[img-texts] 返回章节列表失败: title={title}, attempt={attempt}/{max_attempts}, state={completion_state}"
                    )
                    continue

            time.sleep(1)

            if self._is_img_text_course_passed(title):
                self.log.info(f"[img-texts] 已确认课程完成：{title}")
                return True

            if attempt < max_attempts:
                self.log.warning(
                    f"[img-texts] 返回列表后课程仍未完成，准备重试整门课程，第 {attempt + 1}/{max_attempts} 次：{title}"
                )
                retry_item = self._find_img_text_item_by_title(title)
                if retry_item is None:
                    self.log.warning(f"[img-texts] 未找到可重试的课程项：{title}")
                    continue
                retry_item.click(force=True)
                self._wait_for_post_course_state(timeout_sec=4)
                time.sleep(study_time)

        try:
            if self._page:
                self.log.warning(
                    f"[img-texts] 课程完成校验最终失败，输出验证码上下文诊断: {title}"
                )
                _log_captcha_contexts(self._page, self.log)
        except Exception as e:
            self.log.debug(f"[img-texts] 输出验证码上下文诊断异常: {e}")

        return False

    def _return_to_chapter_list(self) -> bool:
        """从课程详情页、评论页或 mcwk 课件页返回章节列表。"""
        if not self._page:
            return False

        list_markers = (
            ".van-collapse-item, .img-texts-item, .fchl-item, "
            ".task-block, .img-text-block"
        )

        try:
            if self._page.locator(list_markers).count() > 0:
                return True

            acted = ""
            course_frame = self._get_course_runtime_frame()
            if course_frame is not None:
                try:
                    acted = course_frame.evaluate(
                        """() => {
                            const okBtn = document.querySelector('.pop-jsv-prev');
                            if (okBtn) {
                                okBtn.click();
                                return 'dialog';
                            }
                            if (typeof backToList === 'function') {
                                backToList();
                                return 'backToList';
                            }
                            const back = document.querySelector('.back-list');
                            if (back) {
                                back.click();
                                return 'back-list';
                            }
                            return '';
                        }"""
                    )
                    self.log.debug(f"[img-texts] iframe 返回动作结果: {acted}")
                except Exception:
                    acted = ""

            if not acted:
                try:
                    return_btn = self._page.locator(
                        '.comment-footer-button:has-text("返回")'
                    )
                    if return_btn.count() > 0:
                        return_btn.first.click(force=True)
                        acted = "comment-return"
                except Exception:
                    acted = ""

            if not acted:
                try:
                    back = self._page.locator(SEL_NAV_BAR_LEFT)
                    if back.count() > 0:
                        back.first.click(force=True)
                        acted = "top-back"
                except Exception:
                    acted = ""

            return bool(acted)
        except Exception as e:
            self.log.debug(f"[img-texts] 返回章节列表异常: {e}")
            return False

    def _find_course_target(self, locator, failed_courses: set, completed_courses: set):
        """从 locator 中找到第一个不在已失败/已完成集合中的课程项。"""
        for i in range(locator.count()):
            item = locator.nth(i)
            t = self._extract_item_title(item)
            if t and t not in failed_courses and t not in completed_courses:
                return item
        return None

    def _goto_next_project(
        self,
        state: _StudyRunState,
        completed_projects: set,
    ) -> bool:
        """导航到学习任务列表，进入下一个未完成的学习项目。

        成功进入后更新 state 的 current_project_title / study_tabs / active_tab_index。
        返回是否成功进入了一个新项目。
        """
        if not self._page:
            return False

        try:
            self._page.goto(
                f"{self.base_url}/#/learning-task-list",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            time.sleep(3)
        except Exception as e:
            self.log.warning(f"导航到学习任务列表失败: {e}")
            return False

        self._dismiss_broadcast()

        projects = self._page.locator(SEL_TASK_BLOCK)
        if projects.count() == 0:
            self._pause_for_user_intervention(
                "学习任务列表中未找到任务项（.task-block），如果页面加载缓慢，请您手动刷新。"
            )
            projects = self._page.locator(SEL_TASK_BLOCK)
            if projects.count() == 0:
                self.log.warning(
                    "干预后学习任务列表中仍未找到任务项，退出进入下一项目流程。"
                )
                return False

        for i in range(projects.count()):
            proj = projects.nth(i)
            try:
                title_el = proj.locator(".task-block-title")
                title = (
                    title_el.first.inner_text().strip()
                    if title_el.count() > 0
                    else proj.inner_text().strip().split("\n")[0].strip()
                )
            except Exception:
                title = ""
            if title and title in completed_projects:
                continue

            # 读取项目状态文本用于日志展示
            try:
                cat_el = proj.locator(".task-block-state, [class*='state']")
                state_txt = (
                    cat_el.first.inner_text().strip() if cat_el.count() > 0 else ""
                )
            except Exception:
                state_txt = ""

            self.log.info(
                f"进入学习项目：{title or i + 1}"
                f"{(' [' + state_txt + ']') if state_txt else ''}"
            )
            state.current_project_title = title
            proj.click(force=True)
            time.sleep(3)
            self._dismiss_broadcast()

            # 处理协议页、专题/实验室中间列表等中间页
            self._handle_intermediate_pages()
            self._dismiss_broadcast()

            # 初始化当前项目的 Tab 遍历状态
            state.study_tabs = self._get_current_study_tabs()
            state.active_tab_index = 0
            state.active_section_index = -1
            if state.study_tabs:
                self._switch_to_study_tab(state.study_tabs[0])

            return True

        self.log.info("所有学习项目已遍历完成。")
        return False

    def _try_next_study_tab(
        self,
        state: _StudyRunState,
        study_mode: str,
        completed_courses: set,
        failed_courses: set,
    ) -> bool:
        """尝试切换到当前项目的下一个课程 Tab（选修/匹配等）。

        切换成功后检查该 Tab 下是否仍有未完成课程。
        返回 True 表示成功切换到有未完成课程的新 Tab，False 表示已无更多 Tab。
        """
        while state.active_tab_index + 1 < len(state.study_tabs):
            state.active_tab_index += 1
            state.active_section_index = -1
            st = state.study_tabs[state.active_tab_index]
            if self._switch_to_study_tab(st):
                time.sleep(1.5)
                if self._current_tab_has_unfinished_courses(
                    study_mode, completed_courses, failed_courses
                ):
                    self.log.info(f"[切换Tab] 已切换到课程类型: {st}，继续学习")
                    return True

                self.log.info(f"[切换Tab] 课程类型: {st} 无未完成课程，尝试下一个")
                self.log.info("[Tab] 切换后该 Tab 无未完成课程，继续下一 Tab")
        return False

    # ------------------------------------------------------------------
    # 主学习流程
    # ------------------------------------------------------------------

    def run_study(
        self,
        study_time: int,
        study_mode: str,
    ) -> None:
        """自动学习主流程。

        遍历所有学习项目 → 各 Tab（必修/选修/匹配）→ 各章节 → 各课程，
        等待 study_time 秒后调用 finishWxCourse() 标记完成，返回列表继续下一课。

        Args:
            study_time: 每个课程的等待学习时长（秒）。
            study_mode: "false"=不学习  "true"=跳过已完成  "force"=全部学习并循环。
        """
        self.log.info("开始学习流程")

        # 已完成的项目标题集合，用于跳过已遍历的项目
        completed_projects: set = set()
        # 本次运行的可变状态（Tab 索引、当前项目标题等）
        state = _StudyRunState()

        # 进入第一个学习项目
        if not self._goto_next_project(state, completed_projects):
            self.log.warning("未找到任何学习任务，退出学习流程。")
            return

        # 等待课程列表页加载完成
        if not self._page:
            self.log.error("Page is not initialized, cannot start study process")
            return

        try:
            self._page.wait_for_selector(
                ".van-collapse-item, .img-texts-item, .fchl-item",
                state="attached",
                timeout=10000,
            )
        except Exception:
            pass

        # 统计课程总数并输出预计完成时间
        # 针对可能存在的动画加载，增加一次简短探测
        all_tasks, all_finished = self._count_page_tasks()
        if all_tasks == 0:
            time.sleep(2)
            all_tasks, all_finished = self._count_page_tasks()

        remaining = all_tasks - all_finished
        if all_tasks > 0:
            self.log.info(f"课程进度：{all_finished}/{all_tasks}")
            if study_mode == "force":
                rm, rs = divmod(all_tasks * study_time, 60)
                self.log.info(f"强制模式：每轮预计用时 {rm}分{rs}秒")
            if remaining > 0:
                em, es = divmod(remaining * study_time, 60)
                finish_time = datetime.datetime.now() + datetime.timedelta(
                    seconds=remaining * study_time
                )
                self.log.info(
                    f"预计剩余用时：{em}分{es}秒，"
                    f"预计完成时间：{finish_time.strftime('%H:%M:%S')}"
                )
            else:
                self.log.info("所有课程已完成。")
        else:
            self.log.warning(
                "当前项目未检测到有效课程（可能已全部完成或页面为空），继续探测。"
            )

        failed_courses: set = set()
        completed_courses: set = set()
        current_round = 1
        round_completed = 0

        if study_mode == "force" and all_tasks > 0:
            self._log_round_start(current_round, all_tasks, study_time)

        # 主循环：最多迭代 500 次防止死循环
        for _ in range(500):
            try:
                page_state = self._ensure_page_state()
                current_context = self._get_study_page_context()

                if (
                    page_state["state"] in {"blank", "unknown"}
                    and current_context == "unknown"
                ):
                    self.log.debug(
                        f"[页面状态] 学习流程等待稳定: "
                        f"state={page_state['state']} url={page_state['url'] or '<blank>'}"
                    )
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 关闭广播公告弹窗
                # ----------------------------------------------------------
                self._dismiss_broadcast()

                # ----------------------------------------------------------
                # 查找下一个未完成课程（fchl 结构）
                # ----------------------------------------------------------
                fchl_target = self._find_fchl_target(
                    study_mode, failed_courses, completed_courses
                )
                if fchl_target is not None:
                    # 获取当前实时进度
                    all_tasks, all_finished = self._count_page_tasks()
                    title = self._extract_item_title(fchl_target)
                    prog_prefix = (
                        f"[{all_finished + 1}/{all_tasks}] " if all_tasks > 0 else ""
                    )

                    self.log.info(f"{prog_prefix}开始学习：{title}")
                    fchl_target.click(force=True)
                    self._sleep_with_progress(study_time)

                    # 调用 JS 接口标记完成
                    try:
                        self._page.evaluate("finishWxCourse()")
                    except Exception:
                        pass

                    completed_courses.add(title)
                    round_completed += 1

                    # 返回章节列表继续
                    self._return_to_chapter_list()
                    time.sleep(1)

                    # 学习完成后再次刷新进度提示
                    all_tasks, all_finished = self._count_page_tasks()
                    prog_prefix = (
                        f"[{all_finished}/{all_tasks}] "
                        if all_tasks > 0
                        else f"[+{round_completed}] "
                    )
                    self.log.info(f"{prog_prefix}课程学习完成：{title}")
                    continue

                # ----------------------------------------------------------
                # 查找下一个未完成课程（img-texts-item 结构）
                # ----------------------------------------------------------
                img_sel = (
                    ".img-texts-item:visible"
                    if study_mode == "force"
                    else ".img-texts-item:not(.passed):visible"
                )
                img_items = self._page.locator(img_sel)
                target = self._find_course_target(
                    img_items, failed_courses, completed_courses
                )

                if target is not None:
                    all_tasks, all_finished = self._count_page_tasks()
                    title = self._extract_item_title(target)
                    prog_prefix = (
                        f"[{all_finished + 1}/{all_tasks}] " if all_tasks > 0 else ""
                    )

                    self.log.info(f"{prog_prefix}开始学习：{title}")
                    self.log.debug(f"[img-texts] 准备点击课程项：{title}")
                    target.click(force=True)
                    try:
                        self.log.debug(
                            f"[img-texts] 课程项点击后 URL: {self._page.url}"
                        )
                    except Exception:
                        pass
                    self._sleep_with_progress(study_time)

                    # 以章节列表中的 passed 状态为准校验是否真正完成，失败则重试整门课程
                    if not self._finish_img_text_course(title, study_time):
                        self.log.warning(
                            f"[img-texts] 完成校验失败，已放弃本次课程：{title}"
                        )
                        failed_courses.add(title)
                        time.sleep(1)
                        continue

                    completed_courses.add(title)
                    round_completed += 1

                    # 学习完成后再次刷新进度提示
                    all_tasks, all_finished = self._count_page_tasks()
                    prog_prefix = (
                        f"[{all_finished}/{all_tasks}] "
                        if all_tasks > 0
                        else f"[+{round_completed}] "
                    )
                    self.log.info(f"{prog_prefix}课程学习完成：{title}")
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 当前章节无课程：尝试展开下一个折叠章节
                # ----------------------------------------------------------
                if self._expand_next_section(
                    state, study_mode, completed_courses, failed_courses
                ):
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 当前 Tab 已无更多内容：切换到下一个 Tab
                # 在判定无课前，先确认当前页面已恢复到课程列表上下文，
                # 避免验证码/播放器中间态导致误判整页为空。
                # ----------------------------------------------------------
                if not self._return_to_chapter_list():
                    self.log.warning(
                        "[Tab] 当前不在课程列表页，且自动返回失败，等待页面恢复后重试"
                    )
                    time.sleep(2)
                    continue

                time.sleep(1)
                if self._current_tab_has_unfinished_courses(
                    study_mode, completed_courses, failed_courses
                ):
                    retry_fchl_target = self._find_fchl_target(
                        study_mode, failed_courses, completed_courses
                    )
                    retry_img_target = self._find_course_target(
                        self._page.locator(img_sel),
                        failed_courses,
                        completed_courses,
                    )
                    if retry_fchl_target is not None or retry_img_target is not None:
                        self.log.info(
                            "[Tab] 检测到当前 Tab 仍有未完成课程，等待页面刷新后重试"
                        )
                        time.sleep(2)
                        continue

                    self.log.warning(
                        "[Tab] 当前 Tab 仍有未完成课程，但均已标记为失败课程，结束当前 Tab 以避免死循环"
                    )

                self.log.info("[Tab] 当前 Tab 已无未完成课程，尝试切换下一个 Tab")
                if self._try_next_study_tab(
                    state, study_mode, completed_courses, failed_courses
                ):
                    failed_courses.clear()
                    completed_courses.clear()
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 当前项目全部 Tab 已完成：记录并切换到下一个项目
                # ----------------------------------------------------------
                self.log.info(
                    f"项目「{state.current_project_title or '未知'}」"
                    f"已完成，共学习 {round_completed} 课"
                )
                if state.current_project_title:
                    completed_projects.add(state.current_project_title)

                # force 模式：若本轮有完成课程则进入下一轮，否则退出
                if study_mode == "force":
                    if round_completed > 0:
                        current_round = self._new_round(
                            current_round, all_tasks, study_time, completed_courses
                        )
                        round_completed = 0
                        # 重新进入同一项目（completed_projects 中移除以重新访问）
                        completed_projects.discard(state.current_project_title)
                        if not self._goto_next_project(state, completed_projects):
                            self.log.info("强制模式：无更多项目，退出学习流程。")
                            return
                        failed_courses.clear()
                        time.sleep(1)
                        continue
                    else:
                        self.log.info("强制模式：本轮无新完成课程，退出学习流程。")
                        return

                # 普通模式：进入下一个项目
                failed_courses.clear()
                completed_courses.clear()
                round_completed = 0
                if not self._goto_next_project(state, completed_projects):
                    self.log.info("所有学习项目已完成，退出学习流程。")
                    return
                continue

            except Exception as e:
                self.log.error(f"学习主循环异常：{e}", exc_info=True)
                time.sleep(3)

    def _sleep_with_progress(self, seconds: int) -> None:
        """带进度条的休眠。"""
        if seconds <= 0:
            return
        self.log.info(f"正在学习中，预计剩余 {seconds} 秒...")
        for i in range(seconds, 0, -1):
            if i % 10 == 0 and i != seconds:
                self.log.info(f"正在学习中，剩余 {i} 秒...")
            time.sleep(1)
        self.log.info("课程阅读时长已达标")

    def finish_study(self) -> None:
        """标记当前学习任务完成。"""
        try:
            if self._page:
                self._page.evaluate("finishWxCourse()")
        except Exception:
            pass
