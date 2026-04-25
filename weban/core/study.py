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
    SEL_COURSE_LIST_ITEMS,
    SEL_COURSE_LIST_WAIT_TARGETS,
    SEL_FCHL_ITEM,
    SEL_COLLAPSE_ITEM,
    SEL_COLLAPSE_ITEM_TITLE,
    SEL_BROADCAST_MODAL,
    SEL_COMMENT_BACK_BTN,
    SEL_ITEM_TITLE_TEXT,
    SEL_TASK_BLOCK,
    SEL_IMG_TEXT_BLOCK,
    SEL_BTN_SUBMIT_SIGN,
    SEL_FCHL_ITEM_VISIBLE,
    SEL_FCHL_ITEM_NOT_PASSED,
    SEL_FCHL_ITEM_NOT_PASSED_VISIBLE,
    SEL_IMG_TEXT_ITEM_NOT_PASSED,
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
)
from .captcha import (
    handle_click_captcha_in_frame as _handle_captcha_in_frame,
)
from .base import BaseMixin, PageContext

if TYPE_CHECKING:
    from typing import Union as _Union
    from playwright.sync_api import Page, BrowserContext, Browser, Playwright
    from .browser import BrowserConfig
    import logging as _logging

_PROJECT_STUDY_TABS = {
    "pre": [3, 2],
    "normal": [3, 1, 2],
    "special": [3, 2],
    "military": [3],
    "lab": [3],
    "foods": [3],
}


@dataclass
class _StudyRunState:
    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""
    active_section_index: int = -1
    expanded_tabs: set = field(default_factory=set)
    expanded_sections: set = field(default_factory=set)  # 记录已展开的章节


class StudyMixin(BaseMixin):
    """课程学习流程 Mixin。"""

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

    # ========================================================================
    # 页面导航与状态
    # ========================================================================

    def _detect_project_type(self) -> str:
        """从 URL 或页面状态检测项目类型。"""
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

    def _get_current_study_tabs(self) -> List[int]:
        """获取当前项目应学习的 Tab 列表。"""
        pt = self._detect_project_type()
        return list(_PROJECT_STUDY_TABS.get(pt, [3, 2]))

    def _switch_to_study_tab(self, subject_type: int) -> bool:
        """切换到指定的学习 Tab。

        参考 CourseIndex.vue 中的 subjectList 定义：
        - value=3 (requirement): "课程学习" 或 "必修课程" 或 "必修"
        - value=2 (option): "选修课" 或 "自选课程" 或 "选修" 或 "自选"
        - value=1 (matching): "匹配课程" 或 "匹配"
        - value=4 (exam): "在线考试"
        """
        if not self._page or self._page.is_closed():
            return False

        _tab_keywords = {
            3: ["必修", "课程学习", "必修课程"],
            2: ["选修", "自选", "选修课", "自选课程"],
            1: ["匹配", "匹配课程"],
            4: ["在线考试", "考试"],
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
                    self.log.debug(f"[Tab] 成功切换到: {label}")
                    return True

            self.log.debug(f"[Tab] 未找到标签页: {labels}")
        except Exception as e:
            self.log.warning(f"[Tab] 切换失败: {e}")
        return False

    def _dismiss_broadcast(self) -> None:
        """关闭广播弹窗。"""
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

    # ========================================================================
    # 课程列表与章节处理
    # ========================================================================

    def _extract_item_title(self, item) -> str:
        """提取课程项标题。"""
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

    def _parse_section_progress(self, text: str) -> tuple[int | None, int | None]:
        """解析章节进度 '3/8'。"""
        if not text:
            return None, None
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if not m:
            return None, None
        try:
            return int(m.group(1)), int(m.group(2))
        except Exception:
            return None, None

    def _extract_project_overview(self) -> dict[str, Any]:
        """从页面提取项目汇总信息。

        前端 CourseIndex.vue 数据结构:
        - subjectList: [{name, value, done, total}, ...]
        - examList: [{examPlanName, examScore, passScore, examFinishNum, examOddNum, ...}, ...]
        - overview: {name, endTime, studyState, ...}
        """
        if not self._page or self._page.is_closed():
            return {}

        try:
            js_code = """
            () => {
                const app = document.querySelector('.page')?.__vue__;
                if (!app) return null;

                const result = {
                    subjects: [],
                    exams: [],
                    overview: {}
                };

                // 提取 subjectList
                if (app.subjectList && Array.isArray(app.subjectList)) {
                    for (const subj of app.subjectList) {
                        result.subjects.push({
                            name: subj.name || subj.nickName || '',
                            done: subj.done || 0,
                            total: subj.total || 0
                        });
                    }
                }

                // 提取 examList
                if (app.examList && Array.isArray(app.examList)) {
                    for (const exam of app.examList) {
                        result.exams.push({
                            name: exam.examPlanName || '',
                            score: exam.examScore || 0,
                            passScore: exam.passScore || 60,
                            finishedNum: exam.examFinishNum || 0,
                            remainingNum: exam.examOddNum || 0,
                            passed: exam.examScore >= exam.passScore
                        });
                    }
                }

                // 提取 overview
                if (app.overview) {
                    result.overview = {
                        name: app.overview.name || '',
                        endTime: app.overview.endTime || ''
                    };
                }

                return result;
            }
            """
            data = self._page.evaluate(js_code)
            return data if data else {}
        except Exception:
            return {}

    def _print_project_overview(self) -> None:
        """输出项目汇总信息。"""
        overview = self._extract_project_overview()
        if not overview:
            return

        subjects = overview.get("subjects", [])
        exams = overview.get("exams", [])
        info = overview.get("overview", {})

        if info.get("name"):
            self.log.info(f"项目名称: {info['name']}")
        if info.get("endTime"):
            self.log.info(f"截止时间: {info['endTime']}")

        if subjects:
            self.log.info("━━━━━ 课程进度 ━━━━━")
            for subj in subjects:
                name = subj.get("name", "未知")
                done = subj.get("done", 0)
                total = subj.get("total", 0)
                if total > 0:
                    pct = done / total * 100
                    self.log.info(f"  {name}: {done}/{total} ({pct:.1f}%)")

        if exams:
            self.log.info("━━━━━ 考试情况 ━━━━━")
            for exam in exams:
                name = exam.get("name", "未知")
                score = exam.get("score", 0)
                pass_score = exam.get("passScore", 60)
                finished = exam.get("finishedNum", 0)
                remaining = exam.get("remainingNum", 0)
                passed = exam.get("passed", False)

                status = "✓ 合格" if passed else "✗ 不合格"
                if finished == 0:
                    status = "未考试"

                self.log.info(
                    f"  {name}: {status} | "
                    f"最高分 {score}分 (合格线 {pass_score}分) | "
                    f"已考 {finished}次, 剩余 {remaining}次"
                )

    def _get_active_collapse_index(self) -> int:
        """获取当前展开的章节索引。"""
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

    def _expand_next_incomplete_section(
        self, state: _StudyRunState | None = None
    ) -> bool:
        """展开下一个未完成的章节。

        优化：使用 _summarize_collapse_progress 获取所有章节进度，
        优先展开未完成数量最多的章节，跳过已完成的章节。
        """
        if not self._page or self._page.is_closed():
            return False

        # 首先获取所有章节的完成情况（不需要展开）
        progress = self._summarize_collapse_progress()
        if not progress or not progress.get("sections"):
            return False

        sections = progress["sections"]
        incomplete_sections = [s for s in sections if s["incomplete"] > 0]

        if not incomplete_sections:
            self.log.debug("[章节] 所有章节已完成，无需展开")
            return False

        self.log.info(
            f"[章节] 共 {len(sections)} 个章节，{len(incomplete_sections)} 个有未完成课程"
        )

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)

        # 按未完成数量降序处理（已经在 _summarize_collapse_progress 中排序）
        for section in incomplete_sections:
            i = section["index"]
            title_text = section["title"]
            incomplete_count = section["incomplete"]

            # 检查是否已经展开过此章节
            section_key = f"{title_text}_{i}"
            if state and section_key in state.expanded_sections:
                self.log.debug(
                    f"[章节] 跳过已展开过的章节: {title_text} ({incomplete_count} 未完成)"
                )
                continue

            try:
                item = collapse_items.nth(i)
                title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
                if title_btn.count() == 0:
                    continue

                # 检查是否已展开
                cls = item.get_attribute("class") or ""
                if "van-collapse-item--active" in cls:
                    self.log.debug(f"[章节] 章节已展开: {title_text}")
                    if state:
                        state.expanded_sections.add(section_key)
                    return True

                title_btn.scroll_into_view_if_needed(timeout=2000)
                title_btn.click(timeout=5000)
                self.log.info(
                    f"[章节] 展开: {title_text} ({section['finished']}/{section['total']}, {incomplete_count} 未完成)"
                )

                # 记录已展开的章节
                if state:
                    state.expanded_sections.add(section_key)

                # 增加等待时间让课程加载
                time.sleep(2.5)

                # 等待课程列表出现
                try:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_MARKERS, state="visible", timeout=8000
                    )
                except Exception:
                    pass

                return True
            except Exception as e:
                self.log.debug(f"[章节] 展开失败({title_text}): {e}")
                continue

        # 所有未完成章节都已尝试过
        self.log.debug("[章节] 所有未完成章节都已展开过")
        return False

    def _collect_tasks_in_current_tab(self) -> List[Dict[str, Any]]:
        """收集当前 Tab 中的课程任务。"""
        if not self._page or self._page.is_closed():
            return []

        ctx = self._detect_page_context()
        if ctx == PageContext.PROJECT_LIST:
            self.log.debug("[扫描] 当前处于项目列表页，需要先进入项目")
            return []

        dom_tasks: list[dict[str, Any]] = []
        loc = self._page.locator(SEL_COURSE_LIST_ITEMS)
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

        self.log.info(f"[扫描] 发现 {len(dom_tasks)} 门课程")
        return dom_tasks

    def _summarize_collapse_progress(self) -> dict[str, Any] | None:
        """汇总折叠章节的完成进度，返回详细章节信息。

        直接从页面解析，不需要展开折叠面板。
        """
        if not self._page or self._page.is_closed():
            return None

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
        if collapse_items.count() == 0:
            return None

        total = 0
        completed = 0
        found_progress = False
        sections = []  # 存储每个章节的详细信息

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

            # 记录章节信息，用于智能展开
            sections.append(
                {
                    "index": i,
                    "title": title_text,
                    "finished": finished_num,
                    "total": total_num,
                    "incomplete": max(0, total_num - finished_num),
                    "is_completed": finished_num >= total_num,
                }
            )

        if not found_progress:
            return None

        # 按未完成数量降序排序，优先处理未完成的章节
        sections.sort(key=lambda x: x["incomplete"], reverse=True)

        return {
            "total": total,
            "completed": completed,
            "incomplete": max(0, total - completed),
            "sections": sections,  # 包含所有章节的详细信息
        }

    # ========================================================================
    # 课程运行时交互 (mcwk.mycourse.cn/item.js)
    # ========================================================================

    def _get_course_runtime_frame(self):
        """获取课程运行时的核心框架 (通常是 iframe)。"""
        if not self._page or self._page.is_closed():
            return None
        try:
            for f in self._page.frames:
                if f == self._page.main_frame:
                    continue
                url = (f.url or "").lower()
                if "mcwk.mycourse.cn" in url or "/course/" in url or "courseid=" in url:
                    return f

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
        except Exception:
            return None

    def _wait_for_mcwk_runtime(self, timeout: float = 12) -> bool:
        """等待微课播放框架加载完成。"""
        end = time.time() + timeout
        while time.time() < end:
            f = self._get_course_runtime_frame()
            if f:
                try:
                    if (
                        f.locator(
                            SEL_RUNTIME_NAV_BTNS.split(",")[0] + ", .page-item"
                        ).count()
                        > 0
                    ):
                        return True
                    if f.evaluate("typeof finishWxCourse === 'function'"):
                        return True
                except Exception:
                    pass
            time.sleep(1)
        return False

    def _handle_protocol_page(self) -> bool:
        """处理承诺书/协议页。"""
        if not self._page:
            return False
        try:
            agree_cb = self._page.locator(SEL_AGREE_CHECKBOX)
            next_btn = self._page.locator(SEL_BTN_NEXT_STEP)
            if agree_cb.count() == 0 and next_btn.count() == 0:
                return False

            self.log.info("[协议] 检测到承诺书/协议页，自动同意")
            if agree_cb.count() > 0 and not agree_cb.first.is_checked():
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

    def _handle_intermediate_pages(self) -> None:
        """处理进入课程前的中间页面。"""
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

    # ========================================================================
    # 课程播放与答题
    # ========================================================================

    def _setup_quiz_handler(self, frame=None):
        """设置答题响应监听器。"""
        if not hasattr(self, "_last_quiz_answer"):
            self._last_quiz_answer = None
        if not hasattr(self, "_last_quiz_is_right"):
            self._last_quiz_is_right = None
        if not hasattr(self, "_quiz_attempted_answers"):
            self._quiz_attempted_answers = set()
        if not hasattr(self, "_current_question_type"):
            self._current_question_type = 1

        if not hasattr(self, "_has_quiz_handler"):
            if self._page:
                self._page.on("response", self._quiz_response_handler)
                self._has_quiz_handler = True

    def _quiz_response_handler(self, response):
        """答题响应处理器。"""
        try:
            url = response.url
            if "mercuryprovider/router" not in url:
                return

            data = response.json()
            if isinstance(data, dict):
                d = data.get("data", {})
                if isinstance(d, dict):
                    if "answerLabel" in d:
                        ans = d["answerLabel"]
                        if ans:
                            self._last_quiz_answer = ans
                            self.log.info(f"[答题响应] 答案: {ans}")
                    if "isRight" in d:
                        self._last_quiz_is_right = d["isRight"]
                        result = "正确" if d["isRight"] == 1 else "错误"
                        self.log.info(f"[答题响应] 结果: {result}")
        except Exception:
            pass

    def _parse_answer_label(self, answer_label: str) -> list[int]:
        """解析答案标签为选项索引列表。"""
        if not answer_label:
            return []
        indices = []
        for match in re.finditer(r"([A-Z])", answer_label):
            letter = match.group(1)
            idx = ord(letter) - ord("A")
            if 0 <= idx < 26:
                indices.append(idx)
        return indices

    def _get_next_untried_answer(
        self, options_count: int, question_type: int
    ) -> list[int]:
        """获取下一个未尝试过的答案组合。"""
        attempted = getattr(self, "_quiz_attempted_answers", set())

        if question_type == 1 or options_count <= 2:
            for i in range(options_count):
                combo = frozenset([i])
                if combo not in attempted:
                    return [i]
        else:
            for num_select in range(1, options_count + 1):
                for combo in combinations(range(options_count), num_select):
                    if frozenset(combo) not in attempted:
                        return list(combo)

        self.log.warning(f"[答题] 所有 {options_count} 个选项都已尝试，重置尝试记录")
        self._quiz_attempted_answers.clear()
        return [0]

    def _handle_video_playback(self, frame) -> bool:
        """处理视频播放逻辑。"""
        try:
            videos = frame.locator(SEL_RUNTIME_ACTIVE_VIDEO)
            if videos.count() > 0:
                is_paused = videos.first.evaluate("el => el.paused")
                is_ended = videos.first.evaluate("el => el.ended")

                if is_ended:
                    return True
                elif not is_paused:
                    self.log.debug("[视频] 视频正在播放中...")
                    return True
        except Exception:
            pass
        return False

    def _handle_video_play_button(self, frame) -> bool:
        """处理视频播放按钮点击。"""
        try:
            video_play_btn = frame.locator(SEL_RUNTIME_VIDEO_PLAY_BTN).first
            if video_play_btn.count() > 0 and video_play_btn.is_visible():
                self.log.info("[互动] 发现播放按钮，点击播放")
                video_play_btn.click(force=True)
                time.sleep(1)
                return True
        except Exception:
            pass
        return False

    def _extract_local_answer(self, frame) -> str | None:
        """从页面中提取本地答案（本地判断题型）。

        前端 QuestionPage.vue 中：
        - resultList.answerIds 或 resultList.answerList 存储正确答案
        - 单选题: "optionId" 字符串
        - 多选题: ["id1", "id2", ...] 数组

        返回: 答案标签如 "A" 或 "AB"，无法提取返回 None
        """
        try:
            js_code = """
            () => {
                // 尝试获取 Vue 实例数据
                const app = document.querySelector('.answerPg-content')?.__vue__
                    || document.querySelector('.page')?.__vue__;
                if (!app) return { found: false, reason: '未找到Vue实例' };
                if (!app.resultList) return { found: false, reason: '未找到resultList数据' };

                const rl = app.resultList;
                const answerIds = rl.answerIds || rl.answerList;
                if (!answerIds) return { found: false, reason: '未找到answerIds/answerList', hasResultList: true };

                const optionList = rl.optionList || [];
                if (!optionList.length) return { found: false, reason: 'optionList为空', hasAnswerIds: true };

                // 找出正确答案对应的索引
                const indices = [];
                const ids = Array.isArray(answerIds) ? answerIds : [answerIds.toString()];
                const matchedOptions = [];

                for (const id of ids) {
                    const idx = optionList.findIndex(opt => opt.id === id);
                    if (idx >= 0) {
                        indices.push(String.fromCharCode(65 + idx));
                        matchedOptions.push({
                            index: idx,
                            label: String.fromCharCode(65 + idx),
                            content: (optionList[idx].content || '').substring(0, 30),
                            isCorrect: optionList[idx].isCorrect
                        });
                    }
                }

                if (indices.length > 0) {
                    return {
                        found: true,
                        answer: indices.join(''),
                        matchedOptions: matchedOptions,
                        totalOptions: optionList.length,
                        answerIdsType: Array.isArray(answerIds) ? 'array' : 'string'
                    };
                }
                return { found: false, reason: '未能匹配到选项索引', ids: ids };
            }
            """
            result = frame.evaluate(js_code)
            if result and isinstance(result, dict):
                if result.get("found"):
                    self.log.info(
                        f"[答案判断] ✓ 成功从页面提取答案: {result.get('answer')}"
                    )
                    self.log.info(
                        f"[答案判断]   - 匹配的选项: {result.get('matchedOptions')}"
                    )
                    self.log.info(
                        f"[答案判断]   - 总选项数: {result.get('totalOptions')}, 答案ID类型: {result.get('answerIdsType')}"
                    )
                    return result.get("answer")
                else:
                    self.log.debug(
                        f"[答案判断] ✗ 未能提取本地答案: {result.get('reason')}"
                    )
                    if result.get("hasResultList"):
                        self.log.debug("[答案判断]   - 已找到resultList但缺少answerIds")
        except Exception as e:
            self.log.debug(f"[答案判断] ✗ 提取本地答案异常: {e}")
        return None

    def _handle_quiz(self, frame) -> bool:
        """处理答题逻辑 - 输出详细日志。"""
        aq_labels = frame.locator(SEL_RUNTIME_QUIZ_LABELS)
        options_count = aq_labels.count()

        if options_count == 0 or not aq_labels.first.is_visible():
            return False

        checked_count = frame.locator(SEL_RUNTIME_QUIZ_CHECKED).count()
        ans_label = getattr(self, "_last_quiz_answer", None)
        is_right = getattr(self, "_last_quiz_is_right", None)

        if checked_count == 0:
            try:
                source = None

                if ans_label:
                    source = "服务器响应"
                    self.log.info(f"[答题] 答案来源: {source}, 答案: {ans_label}")
                    self.log.debug(
                        "[答题] 判断依据: mercuryprovider/router 接口返回的 answerLabel"
                    )
                    self.log.debug(
                        f"[答题] isRight 标记: {is_right} (1=正确, 0=错误, None=未知)"
                    )
                else:
                    local_answer = self._extract_local_answer(frame)
                    if local_answer:
                        ans_label = local_answer
                        source = "页面本地数据"
                        self.log.info(
                            f"[答题] 答案来源: {source}, 答案: {local_answer}"
                        )
                        self.log.debug(
                            "[答题] 判断依据: Vue 组件 resultList.answerIds/answerList"
                        )
                    else:
                        self.log.debug("[答题] 未找到服务器答案或本地答案")

                if ans_label:
                    correct_indices = self._parse_answer_label(ans_label)

                    option_texts = []
                    for idx in correct_indices:
                        if idx < options_count:
                            try:
                                option_text = (
                                    aq_labels.nth(idx).inner_text().strip()[:50]
                                )
                                option_letter = chr(65 + idx)
                                option_texts.append(f"{option_letter}: {option_text}")
                            except Exception:
                                option_texts.append(chr(65 + idx))

                    self.log.info(f"[答题] 选择答案: {ans_label} -> {option_texts}")

                    for idx in correct_indices:
                        if idx < options_count:
                            label = aq_labels.nth(idx)
                            label.click(force=True)
                            time.sleep(0.2)

                    self._last_quiz_answer = None
                    if is_right == 1:
                        self._quiz_attempted_answers.clear()
                        self._last_quiz_is_right = None
                        self.log.debug("[答题] 答案正确，已清除尝试记录")
                else:
                    question_type = getattr(self, "_current_question_type", 1)
                    attempted = getattr(self, "_quiz_attempted_answers", set())
                    attempted_combo = self._get_next_untried_answer(
                        options_count, question_type
                    )
                    letters = [chr(65 + i) for i in attempted_combo]

                    self.log.info(
                        f"[答题] 试错模式: 选择 {letters} (已试 {len(attempted)} 次)"
                    )
                    self.log.debug(
                        f"[答题] 题目类型: {'单选' if question_type == 1 else '多选'}, 选项总数: {options_count}"
                    )

                    for idx in attempted_combo:
                        if idx < options_count:
                            label = aq_labels.nth(idx)
                            input_el = label.locator("input").first
                            if input_el.count() > 0:
                                input_el.evaluate("el => el.click()")
                            else:
                                label.evaluate("el => el.click()")
                            time.sleep(0.2)

                    attempted.add(frozenset(attempted_combo))
                    self._quiz_attempted_answers = attempted

            except Exception as e:
                self.log.warning(f"[答题] 答题处理异常: {e}")
                try:
                    if aq_labels.count() > 0:
                        aq_labels.nth(0).click(force=True)
                except Exception:
                    pass
        else:
            self.log.debug(f"[答题] 已有 {checked_count} 个选项被选中，等待提交")

        return True

    def _handle_navigation(self, frame) -> bool:
        """处理页面导航按钮 - 输出详细日志。"""
        nav_selectors = [sel.strip() for sel in SEL_RUNTIME_NAV_BTNS.split(",")]

        for sel in nav_selectors:
            try:
                btn = frame.locator(sel).first
                if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                    btn_name = sel.split(".")[-1].split(":")[0]
                    self.log.info(f"[导航] 点击按钮: {btn_name}")
                    btn.click(force=True, timeout=3000)

                    if btn_name in ("btn-aq", "btn-at", "btn-af"):
                        self.log.debug("[答题] 等待服务器返回答案...")
                        for i in range(10):
                            time.sleep(0.3)
                            if self._last_quiz_answer is not None:
                                self.log.info(
                                    f"[答题] 收到服务器答案: {self._last_quiz_answer}"
                                )
                                break

                        if self._last_quiz_answer is None:
                            self.log.debug("[答题] 未收到服务器答案，记录当前尝试")
                            attempted = getattr(self, "_quiz_attempted_answers", set())
                            aq_labels = frame.locator(SEL_RUNTIME_QUIZ_LABELS)
                            if aq_labels.count() > 0:
                                for i in range(aq_labels.count()):
                                    try:
                                        inp = aq_labels.nth(i).locator("input").first
                                        if inp.count() > 0 and inp.is_checked():
                                            attempted.add(frozenset([i]))
                                            self.log.debug(
                                                f"[答题] 记录已选选项: {chr(65 + i)}"
                                            )
                                    except Exception:
                                        pass
                                self._quiz_attempted_answers = attempted
                                self.log.debug(
                                    f"[答题] 已记录尝试: {len(attempted)} 次"
                                )

                    return True
            except Exception:
                continue

        return self._probe_interactive_elements(frame)

    def _probe_interactive_elements(self, frame) -> bool:
        """探测并点击潜在的交互元素。"""
        try:
            probe_candidates = frame.locator(SEL_RUNTIME_PROBE_CANDIDATES)
            for i in range(probe_candidates.count()):
                cand = probe_candidates.nth(i)
                if cand.is_visible() and cand.is_enabled():
                    is_likely_button = cand.evaluate("""el => {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        if (style.cursor === 'pointer') return true;
                        const cls = String(el.className || "");
                        const id = String(el.id || "");
                        if (/prev|back|return/i.test(cls + id)) return false;
                        if (/btn|click|touch|item|box|label|aq-|ce|start|next|submit/i.test(cls + id)) {
                            if (/bg|loader|container/i.test(cls + id)) return false;
                            return true;
                        }
                        return false;
                    }""")

                    if is_likely_button:
                        was_probed = cand.evaluate("el => el.dataset.probed === 'true'")
                        if not was_probed:
                            cand.evaluate("el => el.dataset.probed = 'true'")
                            cand.click(force=True)
                            time.sleep(1.0)
                            return True
        except Exception:
            pass
        return False

    def _return_from_comment_page(self) -> bool:
        """从评论页返回课程列表。

        评论页在 weiban.mycourse.cn 主域名下，URL 包含 /wk/comment
        底部有 comment-footer 区域，包含"返回列表"按钮
        """
        if not self._page or self._page.is_closed():
            return False

        try:
            main_url = self._page.url or ""
            self.log.info(f"[评论] 当前页面: {main_url[:80]}")

            # 优先：点击底部"返回列表"按钮
            # 按钮类名: comment-footer-button，点击后调用 backProject() 返回课程列表
            back_list_btn = self._page.locator(".comment-footer-button").first
            if back_list_btn.count() > 0 and back_list_btn.is_visible():
                self.log.info("[评论] 点击「返回列表」按钮")
                back_list_btn.click(force=True)
                time.sleep(2)
                return True

            # 备用：点击底部导航栏的"学习中心"按钮
            # MkTabBar 组件: van-tabbar-item，点击后跳转到 /learning-task-list
            curriculum_btn = self._page.locator(
                ".van-tabbar-item:has-text('学习中心')"
            ).first
            if curriculum_btn.count() > 0 and curriculum_btn.is_visible():
                self.log.info("[评论] 点击底部导航「学习中心」")
                curriculum_btn.click(force=True)
                time.sleep(2)
                return True

            # 备用：点击导航栏返回
            nav_back = self._page.locator(SEL_NAV_BAR_LEFT).first
            if nav_back.count() > 0 and nav_back.is_visible():
                self.log.info("[评论] 点击导航栏返回")
                nav_back.click(force=True)
                time.sleep(1)
                return True

            # 最后手段：浏览器后退
            self.log.info("[评论] 使用浏览器后退")
            self._page.go_back()
            time.sleep(1)
            return True

        except Exception as e:
            self.log.warning(f"[评论] 返回失败: {e}")
            return False

    def _try_finish_course(self, frame) -> dict:
        """尝试调用 finishWxCourse() 完成课程。

        根据 sdk.js 逻辑：
        - 成功 (code=0/1) + csCom=true → 跳转评论页 weiban.mycourse.cn/#/comment
        - 成功 (code=0/1) + csCom!=true → 显示完成弹窗 .pop-jsv
        - 失败 → alert('发送完成失败')
        - csCapt=true → 先验证码再调用后端

        Returns:
            dict: {
                'completed': bool,  # 课程是否真正完成
                'need_captcha': bool,  # 是否需要验证码
                'detached': bool,  # iframe 是否已分离（跳转到评论页）
            }
        """
        try:
            iframe_url = frame.url or ""
            self.log.info("[完成] 调用 finishWxCourse()...")
            self.log.debug(f"[完成] iframe URL: {iframe_url[:80]}")

            result = frame.evaluate("""() => {
                if (typeof finishWxCourse !== 'function') {
                    return { completed: false, need_captcha: false, detached: false, reason: 'finishWxCourse not defined' };
                }
                
                return new Promise((resolve) => {
                    const originalAlert = window.alert;
                    let alertMsg = '';
                    
                    window.alert = (msg) => { alertMsg = msg; };
                    
                    const checkCompletion = () => {
                        window.alert = originalAlert;
                        
                        // 检查是否显示完成弹窗
                        const popJs = document.querySelector('.pop-jsv');
                        if (popJs && popJs.offsetParent !== null) {
                            resolve({ completed: true, need_captcha: false, popup: true });
                            return;
                        }
                        
                        // 检查是否有验证码弹窗
                        const captcha = document.querySelector('.tencent-captcha-dy__verify-bg-img');
                        if (captcha && captcha.offsetParent !== null) {
                            resolve({ completed: false, need_captcha: true });
                            return;
                        }
                        
                        // 检查是否失败
                        if (alertMsg.includes('失败')) {
                            resolve({ completed: false, need_captcha: false, reason: alertMsg });
                            return;
                        }
                        
                        // 默认：未完成但无错误
                        resolve({ completed: false, need_captcha: false });
                    };
                    
                    try {
                        finishWxCourse();
                        setTimeout(checkCompletion, 3000);
                    } catch (e) {
                        window.alert = originalAlert;
                        resolve({ completed: false, need_captcha: false, reason: e.message });
                    }
                });
            }""")

            reason = result.get("reason", "")
            if result.get("completed"):
                if result.get("popup"):
                    self.log.info("[完成] 后端成功 - 显示完成弹窗")
                else:
                    self.log.info("[完成] 后端成功")
            elif result.get("need_captcha"):
                self.log.info("[完成] 需要验证码")
            elif reason:
                self.log.warning(f"[完成] 后端返回失败: {reason}")
            else:
                self.log.debug("[完成] 继续学习中...")

            return result

        except Exception as e:
            err_msg = str(e).lower()
            # Frame detached 说明页面跳转了，可能是跳转到评论页
            if "frame was detached" in err_msg or "has been closed" in err_msg:
                self.log.info("[完成] iframe 已分离，检查主页面是否跳转到评论页...")

                # 检查主页面是否跳转到评论页
                try:
                    main_url = self._page.url if self._page else ""
                    self.log.info(f"[完成] 主页面 URL: {main_url}")

                    if "/comment" in main_url or "/wk/comment" in main_url:
                        self.log.info("[完成] 主页面已跳转到评论页，课程完成!")
                        return {
                            "completed": True,
                            "need_captcha": False,
                            "detached": True,
                        }
                except Exception:
                    pass

                self.log.warning("[完成] iframe 分离但未检测到评论页")
                return {
                    "completed": False,
                    "need_captcha": False,
                    "detached": True,
                    "reason": "frame detached",
                }

            self.log.warning(f"[完成] finishWxCourse() 调用异常: {e}")
            return {"completed": False, "need_captcha": False}

    def _handle_captcha_if_needed(self, frame) -> bool:
        """检测并处理验证码。

        Returns:
            True: 验证码处理成功或不需要验证码
            False: 验证码处理失败
        """
        try:
            # 检查是否有可见的验证码元素
            # 主验证码图片元素
            captcha_selectors = [
                ".tencent-captcha-dy__verify-bg-img",
                ".tencent-captcha-dy__verify-img-area img",
                ".WPA3-SELECT-BG",
            ]

            has_visible_captcha = False
            for sel in captcha_selectors:
                try:
                    loc = frame.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        # 额外检查尺寸
                        bb = loc.first.bounding_box()
                        if bb and bb.get("width", 0) > 50 and bb.get("height", 0) > 50:
                            has_visible_captcha = True
                            self.log.debug(f"[验证码] 检测到可见验证码元素: {sel}")
                            break
                except Exception:
                    continue

            if has_visible_captcha:
                self.log.info("[验证码] 检测到验证码，开始处理...")
                if _handle_captcha_in_frame(frame, self.log):
                    self.log.info("[验证码] 验证码处理成功")
                    time.sleep(2)
                    return True
                else:
                    self.log.warning("[验证码] 验证码处理失败")
                    return False
        except Exception as e:
            self.log.debug(f"[验证码] 检测异常: {e}")
        return True

    def _check_course_completed(self, frame) -> bool:
        """检查课程是否已完成（基于页面状态）。

        注意：不能仅凭主页面上下文判断完成，必须基于 finishWxCourse() 的结果。

        检查项：
        1. iframe URL 是否跳转到评论页
        2. 是否显示完成弹窗 .pop-jsv

        Returns:
            True: 课程已完成
            False: 课程未完成
        """
        try:
            url = (frame.url or "").lower()

            # 跳转到评论页（在 iframe 内）
            if "/wk/comment" in url:
                self.log.info("[状态] iframe URL 跳转到评论页，课程完成")
                return True

            # 完成弹窗（在 iframe 内）
            pop_jsv = frame.locator(".pop-jsv").first
            if pop_jsv.count() > 0 and pop_jsv.is_visible():
                self.log.info("[状态] 检测到完成弹窗 .pop-jsv")

                # 点击返回列表
                prev_btn = frame.locator(".pop-jsv-prev").first
                if prev_btn.count() > 0 and prev_btn.is_visible():
                    self.log.info("[状态] 点击完成弹窗的返回列表按钮")
                    prev_btn.click(force=True)
                    time.sleep(1)
                return True

        except Exception as e:
            self.log.debug(f"[状态] 检查异常: {e}")
        return False

    def _return_to_list(self, frame) -> bool:
        """返回课程列表。

        Returns:
            True: 成功返回
            False: 返回失败
        """
        try:
            # 优先点击完成弹窗的返回按钮
            prev_btn = frame.locator(".pop-jsv-prev").first
            if prev_btn.count() > 0 and prev_btn.is_visible():
                self.log.info("[返回] 点击完成弹窗返回按钮")
                prev_btn.click(force=True)
                time.sleep(1)
                return True

            # 尝试调用 backToList
            if frame.evaluate("typeof backToList === 'function'"):
                self.log.info("[返回] 调用 backToList()")
                frame.evaluate("backToList()")
                time.sleep(1)
                return True

            # 尝试点击返回按钮
            back_btn = frame.locator(".back-list, .btn-back").first
            if back_btn.count() > 0 and back_btn.is_visible():
                self.log.info("[返回] 点击返回按钮")
                back_btn.click(force=True)
                time.sleep(1)
                return True

        except Exception:
            pass
        return False

    def _trigger_img_text_completion(self, frame, title: str) -> bool:
        """高仿真微课播放逻辑。

        课程完成的唯一判断依据：
        1. 调用 finishWxCourse()
        2. 后端返回成功 → URL 跳转评论页 或 显示完成弹窗
        3. 后端返回失败 → 继续学习

        流程：
        1. 计时学习，达到最小时长后尝试调用 finishWxCourse()
        2. 如果成功，处理验证码（如有），返回列表
        3. 如果失败，继续模拟学习，直到 finishWxCourse() 成功
        """
        self._setup_quiz_handler()
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
                    self.log.error("[播放] frame 和 page 均为空")
                    return False
                frame = self._page

            self.log.info("[播放] ════════════════════════════════════════")
            self.log.info(f"[播放] 开始课程: {title}")
            self.log.info(f"[播放] iframe URL: {frame.url if frame.url else 'N/A'}")
            self.log.debug(
                f"[播放] 主页面 URL: {self._page.url if self._page else 'N/A'}"
            )

            min_study_time = getattr(self, "study_time", 20)
            start_time = time.time()
            last_finish_attempt = 0
            loop_count = 0
            finish_attempt_count = 0

            self.log.info(f"[计时] 最小学习时长配置: {min_study_time} 秒")

            while True:
                loop_count += 1
                time.sleep(2.0 + random.uniform(0.1, 0.5))

                elapsed = time.time() - start_time

                # 每 5 次循环输出一次详细状态
                if loop_count % 5 == 1:
                    self.log.debug(f"[状态] ── 循环 #{loop_count} ──")
                    self.log.debug(f"[状态] 已学习: {int(elapsed)} 秒")
                    self.log.debug(
                        f"[状态] iframe URL: {frame.url[:80] if frame.url else 'N/A'}..."
                    )
                    self.log.debug(
                        f"[状态] 主页面上下文: {self._detect_page_context().value}"
                    )

                # 检查是否已完成
                if self._check_course_completed(frame):
                    self.log.info("[播放] ════════════════════════════════════════")
                    self.log.info("[播放] 课程完成确认!")
                    self.log.info(f"[播放] 总学习时长: {int(elapsed)} 秒")
                    self.log.info(
                        f"[播放] 尝试 finishWxCourse() 次数: {finish_attempt_count}"
                    )
                    self.log.info("[播放] ════════════════════════════════════════")
                    return True

                # 处理验证码
                if not self._handle_captcha_if_needed(frame):
                    self.log.debug("[验证码] 处理中，继续循环")
                    continue

                # 达到最小时长后，每 5 秒尝试调用 finishWxCourse()
                if elapsed >= min_study_time and time.time() - last_finish_attempt > 5:
                    last_finish_attempt = time.time()
                    finish_attempt_count += 1

                    self.log.info("[计时] ──────────────────────────────────────")
                    self.log.info(
                        f"[计时] 已学习 {int(elapsed)} 秒 (≥ {min_study_time} 秒)"
                    )
                    self.log.info(
                        f"[计时] 第 {finish_attempt_count} 次尝试调用 finishWxCourse()"
                    )

                    result = self._try_finish_course(frame)

                    self.log.info(
                        f"[计时] finishWxCourse() 结果: completed={result.get('completed')}, need_captcha={result.get('need_captcha')}, detached={result.get('detached')}"
                    )

                    if result.get("completed"):
                        self.log.info("[完成] 后端返回成功，课程完成!")
                        # 如果跳转到评论页，点击返回按钮
                        if result.get("detached"):
                            self._return_from_comment_page()
                        return True

                    if result.get("detached"):
                        # iframe 已分离，检查主页面状态
                        self.log.info("[完成] iframe 已分离，检查主页面...")
                        try:
                            main_url = self._page.url if self._page else ""
                            if "/comment" in main_url:
                                self.log.info(
                                    "[完成] 主页面已跳转到评论页，点击返回..."
                                )
                                self._return_from_comment_page()
                                return True
                        except Exception:
                            pass
                        continue

                    # 需要验证码
                    if result.get("need_captcha"):
                        self.log.info("[验证码] 需要处理验证码")
                        if not self._handle_captcha_if_needed(frame):
                            continue
                        # 验证码处理后再检查
                        if self._check_course_completed(frame):
                            return True

                # === 正常学习流程 ===

                if self._handle_video_playback(frame):
                    self.log.debug("[视频] 播放中...")
                    continue

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
                except Exception as e:
                    self.log.debug(f"[互动] 协议勾选检查异常: {e}")

                if self._handle_video_play_button(frame):
                    self.log.info("[视频] 点击播放按钮")
                    continue

                try:
                    p12_choice = frame.locator(SEL_RUNTIME_CHOICE).first
                    if p12_choice.count() > 0 and p12_choice.is_visible():
                        self.log.info("[互动] 点击选择题选项")
                        p12_choice.click(force=True)
                        time.sleep(0.5)
                except Exception as e:
                    self.log.debug(f"[互动] 选择题检查异常: {e}")

                try:
                    p17_items = frame.locator(SEL_RUNTIME_INTERACTIVE_ITEMS)
                    visible_count = 0
                    for i in range(p17_items.count()):
                        it = p17_items.nth(i)
                        if it.is_visible() and "brightness(0.7)" not in (
                            it.get_attribute("style") or ""
                        ):
                            visible_count += 1
                            self.log.info(f"[互动] 点击交互元素 #{i + 1}")
                            it.click(force=True)
                            time.sleep(0.3)
                    if visible_count > 0:
                        self.log.debug(f"[互动] 共点击 {visible_count} 个交互元素")

                    p17_close = frame.locator(SEL_RUNTIME_INTERACTIVE_CLOSE).first
                    if p17_close.count() > 0 and p17_close.is_visible():
                        self.log.info("[互动] 关闭弹窗")
                        p17_close.click(force=True)
                        time.sleep(0.5)
                except Exception as e:
                    self.log.debug(f"[互动] 交互元素检查异常: {e}")

                self._handle_quiz(frame)

                if self._handle_navigation(frame):
                    self.log.info("[导航] 点击下一页/继续按钮")
                    continue

                # 检测结束页面
                try:
                    page_end = frame.locator(".page-end.page-active")
                    if page_end.count() > 0 and page_end.is_visible():
                        self.log.info("[状态] 检测到课程结束页面 .page-end.page-active")

                        # 尝试完成
                        finish_attempt_count += 1
                        self.log.info(
                            f"[完成] 结束页面，第 {finish_attempt_count} 次调用 finishWxCourse()"
                        )
                        result = self._try_finish_course(frame)

                        self.log.info(
                            f"[完成] 结果: completed={result.get('completed')}, need_captcha={result.get('need_captcha')}"
                        )

                        if result.get("completed"):
                            if self._check_course_completed(frame):
                                return True

                        if result.get("need_captcha"):
                            self._handle_captcha_if_needed(frame)

                        # 尝试返回
                        if self._return_to_list(frame):
                            time.sleep(1)
                            if self._check_course_completed(frame):
                                return True
                except Exception as e:
                    self.log.debug(f"[状态] 结束页面检查异常: {e}")

        except Exception as e:
            err_msg = str(e).lower()
            if "frame was detached" in err_msg or "has been closed" in err_msg:
                self.log.info("[播放] 页面已跳转/关闭，视为完成")
                return True
            self.log.error(f"[播放] 异常退出: {str(e)}")
            return False

    # ========================================================================
    # 课程完成与返回
    # ========================================================================

    def _return_to_chapter_list(self) -> bool:
        """返回章节列表。"""
        if not self._page or self._page.is_closed():
            return False

        for attempt in range(3):
            ctx = self._detect_page_context()
            if ctx in (PageContext.COURSE_LIST, PageContext.PROJECT_LIST):
                time.sleep(1)
                return True

            try:
                f = self._get_course_runtime_frame()
                if f:
                    try:
                        f.evaluate("if(typeof backToList === 'function') backToList();")
                        self.log.debug("[导航] 调用 backToList()")
                    except Exception:
                        pass
                    time.sleep(1)

                if self._page:
                    btn_back = self._page.locator(SEL_COMMENT_BACK_BTN).first
                    if btn_back.count() > 0 and btn_back.is_visible():
                        btn_back.scroll_into_view_if_needed(timeout=2000)
                        btn_back.click(timeout=5000)
                        time.sleep(1)

                ctx = self._detect_page_context()
                if ctx in (PageContext.COURSE_LIST, PageContext.PROJECT_LIST):
                    return True

                if self._page:
                    btn_nav_back = self._page.locator(SEL_NAV_BAR_LEFT).first
                    if btn_nav_back.count() > 0 and btn_nav_back.is_visible():
                        self.log.debug("[导航] 点击左上角返回")
                        btn_nav_back.click(timeout=5000)
                        time.sleep(1)

                    back_list_btn = self._page.locator(
                        ".back-list, .btn-back, [class*='back']"
                    ).first
                    if back_list_btn.count() > 0 and back_list_btn.is_visible():
                        self.log.debug("[导航] 点击 back-list")
                        back_list_btn.click(timeout=5000)
                        time.sleep(1)

                ctx = self._detect_page_context()
                if ctx in (PageContext.COURSE_LIST, PageContext.PROJECT_LIST):
                    return True

            except Exception as e:
                self.log.debug(f"[导航] 返回尝试 {attempt + 1} 异常: {e}")

            time.sleep(0.5)

        result = self._is_in_context(PageContext.COURSE_LIST, PageContext.PROJECT_LIST)
        if not result:
            self.log.warning("[导航] 未能返回课程列表页")
        return result

    def finish_study(self) -> None:
        """完成学习。"""
        try:
            if self._page and not self._page.is_closed():
                self._page.evaluate(
                    "if(typeof finishWxCourse === 'function') finishWxCourse();"
                )
        except Exception:
            pass

    # ========================================================================
    # 主学习流程
    # ========================================================================

    def _find_fchl_target(self, study_mode: str, failed: set, completed: set):
        """查找 FCHL 课程目标。"""
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

    def _find_img_text_item_by_title(self, title: str):
        """根据标题查找课程项。"""
        if not self._page or self._page.is_closed():
            return None

        self.log.debug(f"[定位] 开始查找课程: {title}")

        # 首先尝试直接查找（已展开的课程）
        for sel in [SEL_IMG_TEXT_ITEM_NOT_PASSED, SEL_IMG_TEXT_ITEM]:
            try:
                loc = self._page.locator(sel)
                count = loc.count()
                self.log.debug(f"[定位] 选择器 {sel} 找到 {count} 个元素")

                visible_count = 0
                for i in range(count):
                    try:
                        it = loc.nth(i)
                        if not it.is_visible():
                            continue
                        visible_count += 1
                        item_title = self._extract_item_title(it)
                        self.log.debug(f"[定位] 检查元素 {i}: '{item_title}'")
                        if item_title == title:
                            self.log.debug(f"[定位] ✓ 找到课程元素: {title}")
                            return it
                    except Exception as e:
                        self.log.debug(f"[定位] 检查元素 {i} 异常: {e}")
                        continue

                self.log.debug(
                    f"[定位] 选择器 {sel}: {visible_count}/{count} 个可见元素"
                )
            except Exception as e:
                self.log.debug(f"[定位] 搜索异常 ({sel}): {e}")
                continue

        self.log.debug(f"[定位] 未在可见区域找到: {title}，尝试展开章节...")

        # 尝试展开章节查找
        try:
            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            collapse_count = collapse_items.count()
            self.log.debug(f"[定位] 找到 {collapse_count} 个章节")

            for i in range(collapse_count):
                collapse = collapse_items.nth(i)
                try:
                    collapse_title_elem = collapse.locator(
                        SEL_COLLAPSE_ITEM_TITLE
                    ).first
                    if collapse_title_elem.count() == 0:
                        continue

                    # 获取章节标题
                    chapter_title = ""
                    try:
                        chapter_title = collapse_title_elem.inner_text().strip()
                    except Exception:
                        pass

                    is_expanded = collapse.get_attribute("class") or ""
                    expanded = "van-collapse-item--active" in is_expanded
                    self.log.debug(
                        f"[定位] 章节 {i} ('{chapter_title}'): 已展开={expanded}"
                    )

                    if not expanded:
                        collapse_title_elem.scroll_into_view_if_needed(timeout=2000)
                        collapse_title_elem.click(timeout=5000)
                        time.sleep(1.5)  # 增加等待时间
                        self.log.debug(f"[定位] 已展开章节 {i}")

                    # 在章节内查找课程
                    content_items = collapse.locator(SEL_IMG_TEXT_ITEM)
                    content_count = content_items.count()
                    self.log.debug(f"[定位] 章节 {i} 内有 {content_count} 个课程项")

                    for j in range(content_count):
                        try:
                            item = content_items.nth(j)
                            if not item.is_visible():
                                continue
                            item_title = self._extract_item_title(item)
                            self.log.debug(f"[定位] 章节 {i} 课程 {j}: '{item_title}'")
                            if item_title == title:
                                self.log.debug(
                                    f"[定位] ✓ 在章节 {i} 中找到课程: {title}"
                                )
                                return item
                        except Exception as e:
                            self.log.debug(f"[定位] 检查章节 {i} 课程 {j} 异常: {e}")
                            continue

                except Exception as e:
                    self.log.debug(f"[定位] 处理章节 {i} 异常: {e}")
                    continue

        except Exception as e:
            self.log.debug(f"[定位] 章节搜索异常: {e}")

        self.log.warning(f"[定位] ✗ 最终未找到课程: {title}")
        return None

    def _finish_img_text_course(self, title: str, study_time: int) -> bool:
        """完成图文课程。

        流程：
        1. 进入课程 iframe
        2. 计时 + 模拟交互
        3. 达到最小时长后调用 finishWxCourse()
        4. 检查后端响应判断是否完成
        """
        for attempt in range(3):
            self.log.info(f"[课程] 等待框架加载 (尝试 {attempt + 1}/3)...")

            if not self._wait_for_mcwk_runtime():
                self.log.warning("[课程] 等待框架超时")
                continue

            f = self._get_course_runtime_frame()
            if not f:
                self.log.warning("[课程] 未获取到课程框架")
                continue

            iframe_url = f.url or ""
            self.log.info(f"[课程] 进入 iframe: {iframe_url[:100]}...")

            self.log.info(f"[课程] 开始播放: {title}")
            result = self._trigger_img_text_completion(f, title)

            if result:
                self.log.info(f"[课程] 播放流程完成: {title}")
                return True
            else:
                self.log.warning("[课程] 播放流程未完成，尝试重试")

        self.log.warning(f"[课程] 所有尝试均失败: {title}")
        return False

    def _process_task_list(
        self, v_tasks, study_time, study_mode, completed, failed
    ) -> int:
        """处理任务列表。"""
        processed_cnt = 0
        total_tasks = len(v_tasks)

        for idx, task in enumerate(v_tasks):
            title = task["title"]

            if study_mode != "force" and task.get("passed"):
                self.log.debug(f"[{idx + 1}/{total_tasks}] 跳过已完成: {title}")
                continue
            if title in completed or title in failed:
                continue

            self._return_to_chapter_list()

            # 刷新页面确保看到最新状态（课程可能已完成或章节状态已更新）
            try:
                if self._page and not self._page.is_closed():
                    self._page.reload(wait_until="networkidle", timeout=15000)
                    time.sleep(2)
                    self.log.debug("[刷新] 页面已刷新")
            except Exception as e:
                self.log.debug(f"[刷新] 页面刷新失败: {e}")

            self.log.info(f"[{idx + 1}/{total_tasks}] 正在学习: {title}")

            ok = False
            fail_reason = ""

            if task.get("type") == "fchl":
                item = self._find_fchl_target(study_mode, failed, completed)
                if not item:
                    fail_reason = "未找到FCHL课程元素"
                elif self._extract_item_title(item) != title:
                    fail_reason = "课程元素标题不匹配"
                else:
                    if not self._safe_click(item):
                        fail_reason = "点击课程元素失败"
                    else:
                        self._sleep_with_progress(study_time)
                        self.finish_study()
                        self._return_to_chapter_list()
                        ok = True
            else:
                item = self._find_img_text_item_by_title(title)
                if not item:
                    fail_reason = "未找到课程元素"
                elif not self._safe_click(item):
                    fail_reason = "点击课程元素失败"
                else:
                    time.sleep(2)
                    if not self._wait_for_mcwk_runtime():
                        fail_reason = "等待课程框架超时"
                    else:
                        ok = self._finish_img_text_course(title, study_time)
                        if not ok:
                            fail_reason = "课程播放/交互流程未完成"
                    self._return_to_chapter_list()
                    time.sleep(1)

            if ok:
                completed.add(title)
                processed_cnt += 1
                self.log.info(f"[{idx + 1}/{total_tasks}] 课程完成: {title}")
            else:
                failed.add(title)
                self.log.warning(
                    f"[{idx + 1}/{total_tasks}] 课程失败: {title} - {fail_reason}"
                )

            time.sleep(1.5)
            if self._page and self._page.is_closed():
                break

        return processed_cnt

        return processed_cnt

    def _check_course_completion(self) -> dict:
        """检查课程完成情况。"""
        if not self._page or self._page.is_closed():
            return {"total": 0, "completed": 0, "incomplete": 0}

        try:
            self._return_to_chapter_list()
            time.sleep(2)

            collapse_stats = self._summarize_collapse_progress()
            if collapse_stats is not None:
                # 提取统计信息（不包含 sections 详情）
                stats = {
                    "total": collapse_stats["total"],
                    "completed": collapse_stats["completed"],
                    "incomplete": collapse_stats["incomplete"],
                }
                # 如果有章节详情，打印出来
                if collapse_stats.get("sections"):
                    incomplete_sections = [
                        s for s in collapse_stats["sections"] if s["incomplete"] > 0
                    ]
                    if incomplete_sections:
                        self.log.info(
                            f"[课程统计] 未完成章节: {len(incomplete_sections)} 个"
                        )
                        for sec in incomplete_sections[:5]:  # 只显示前5个
                            self.log.info(
                                f"  - {sec['title']}: {sec['finished']}/{sec['total']} ({sec['incomplete']} 未完成)"
                            )

                self.log.info(
                    f"[课程统计] 总计: {stats['total']}, "
                    f"已完成: {stats['completed']}, "
                    f"未完成: {stats['incomplete']}"
                )
                return stats

            tasks = self._collect_tasks_in_current_tab()
            total = len(tasks)
            completed_count = sum(1 for task in tasks if task.get("passed"))

            return {
                "total": total,
                "completed": completed_count,
                "incomplete": total - completed_count,
            }
        except Exception as e:
            self.log.warning(f"统计课程完成情况失败: {e}")
            return {"total": 0, "completed": 0, "incomplete": 0}

    def _goto_next_project(
        self, state: _StudyRunState, completed: set, study_mode: str = "true"
    ) -> bool:
        """导航到下一个项目。"""
        if not self._page or self._page.is_closed():
            return False

        ctx = self._detect_page_context()

        if ctx == PageContext.COURSE_LIST:
            if state.current_project_title and state.current_project_title in completed:
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

            try:
                nav_title = self._page.locator(SEL_NAV_BAR_TITLE).first
                if nav_title.count() > 0 and nav_title.is_visible():
                    self.project_title = nav_title.inner_text().strip()
                else:
                    try:
                        doc_title = self._page.evaluate("document.title")
                        if doc_title and doc_title.strip():
                            self.project_title = doc_title.strip()
                    except Exception:
                        pass
            except Exception:
                pass

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
        projs = self._page.locator(SEL_TASK_BLOCK)
        proj_count = projs.count()
        self.log.debug(f"[导航] 发现 {proj_count} 个学习项目")

        if proj_count == 0:
            try:
                self._page.reload()
                time.sleep(5)
                projs = self._page.locator(SEL_TASK_BLOCK)
                proj_count = projs.count()
            except Exception:
                pass

        for i in range(proj_count):
            it = projs.nth(i)
            title = self._extract_item_title(it)
            if not title or title in completed:
                self.log.debug(f"[导航] 跳过项目: {title or '(无标题)'}")
                continue

            self.project_title = title
            self.log.info(f"======== 目标项目: {title} ========")
            state.current_project_title = title
            it.scroll_into_view_if_needed(timeout=2000)
            it.click(timeout=5000)

            for attempt in range(5):
                time.sleep(1.5)
                self._handle_intermediate_pages()
                new_ctx = self._detect_page_context()
                if new_ctx == PageContext.COURSE_LIST:
                    self.log.info(f"成功进入项目：{title}")
                    state.study_tabs = self._get_current_study_tabs()
                    time.sleep(1)
                    self._print_project_overview()
                    return True
                self.log.debug(f"[导航] 等待进入项目 ({attempt + 1}/5)...")

            self.log.warning(f"进入项目「{title}」超时，尝试继续")
            return True

        self.log.info("所有项目已处理完毕")
        return False

    def run_study(self, study_time: int, study_mode: str) -> dict:
        """主学习流程。"""
        self.log.info("开始学习流程")
        self.study_time = study_time
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
                pass

            while self._goto_next_project(state, completed_projs, study_mode):
                proj_title = state.current_project_title
                failed, completed = set(), set()

                study_tabs = state.study_tabs or self._get_current_study_tabs()
                if not study_tabs:
                    self.log.warning(
                        f"项目「{proj_title}」未找到学习 Tab，尝试默认处理"
                    )
                    study_tabs = [3, 2]

                for tab_id in study_tabs:
                    self.log.debug(f"[Tab] 尝试切换到 Tab {tab_id}")
                    if not self._switch_to_study_tab(tab_id):
                        self.log.debug(f"[Tab] 切换失败，跳过 Tab {tab_id}")
                        continue

                    time.sleep(1.5)
                    self._dismiss_broadcast()

                    while not self._page.is_closed():
                        tasks = self._collect_tasks_in_current_tab()
                        self.log.info(f"[Tab {tab_id}] 扫描到 {len(tasks)} 门课程")

                        if not tasks:
                            self.log.debug(
                                f"[Tab {tab_id}] 无课程任务，检查是否需要展开章节"
                            )
                            if self._expand_next_incomplete_section(state):
                                time.sleep(1)
                                continue
                            break

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

                        if self._expand_next_incomplete_section(state):
                            time.sleep(1)
                            continue
                        break

                completed_projs.add(proj_title)
                self.log.info(f"项目「{proj_title}」处理完毕。")

                stats = self._check_course_completion()
                completion_stats["total"] += stats["total"]
                completion_stats["completed"] += stats["completed"]
                completion_stats["incomplete"] += stats["incomplete"]

        except Exception as e:
            self.log.error(f"严重异常: {e}")

        self.log.info("全部学习任务已处理。")
        self.log.info(
            f"[学习完成] 总课程: {completion_stats['total']}, "
            f"已完成: {completion_stats['completed']}, "
            f"未完成: {completion_stats['incomplete']}"
        )

        return completion_stats
