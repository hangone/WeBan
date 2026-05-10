import re
import json
import time
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Dict

from .const import (
    SEL_COURSE_TAB,
    SEL_COURSE_LIST_MARKERS,
    SEL_COURSE_LIST_ITEMS,
    SEL_COURSE_LIST_WAIT_TARGETS,
    SEL_FCHL_ITEM,
    SEL_COLLAPSE_ITEM,
    SEL_COLLAPSE_ITEM_TITLE,
    SEL_COLLAPSE_ITEM_CONTENT,
    SEL_BROADCAST_MODAL,
    SEL_COMMENT_BACK_BTN,
    SEL_ITEM_TITLE_TEXT,
    SEL_TASK_BLOCK,
    SEL_FCHL_ITEM_VISIBLE,
    SEL_FCHL_ITEM_NOT_PASSED,
    SEL_FCHL_ITEM_NOT_PASSED_VISIBLE,
    SEL_IMG_TEXT_ITEM_NOT_PASSED,
    SEL_IMG_TEXT_ITEM,
    SEL_ITEM_COMPLETED_ICON,
    SEL_RUNTIME_NAV_BTNS,
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
    "contest": [3],
}


@dataclass
class _StudyRunState:
    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""
    active_section_index: int = -1
    expanded_tabs: set = field(default_factory=set)
    expanded_sections: set = field(default_factory=set)  # 记录已展开的章节
    _expand_count_map: dict = field(default_factory=dict)  # 章节展开次数追踪
    _verified_complete_sections: set = field(default_factory=set)  # 实际扫描确认全部完成的章节
    _last_expanded_section_key: str = ""  # 最近一次展开的章节 key


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

        StudyPage.vue 使用自定义 <li class="s1"> / <li class="s2"> 元素，
        仅 subject_type=3（课程学习）需要处理。
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
            # 等待页面 Tab 元素加载（SPA 可能延迟渲染）
            try:
                self._page.wait_for_selector(
                    f"{SEL_COURSE_TAB}, .scontain li, [role='tab']",
                    state="attached",
                    timeout=8000,
                )
            except Exception:
                pass

            # 方案 1: JS 直接查找并点击（与 run_exam 方式一致）
            for label in labels:
                try:
                    clicked = self._page.evaluate("""(args) => {
                        const [label, subjectType] = args;
                        %s
                        // 当前源码中 Tab 由 activeSubjectValue 驱动，直接改 Vue 状态比点击 DOM 更稳。
                        const app = findVueProxy(['activeSubjectValue']);
                        if (app) {
                            if (app.activeSubjectValue === subjectType) {
                                return { found: true, clicked: false, sel: 'vue.activeSubjectValue' };
                            }
                            app.activeSubjectValue = subjectType;
                            return { found: true, clicked: true, sel: 'vue.activeSubjectValue', text: label };
                        }
                        // 搜索所有可能的 Tab 元素
                        const tabSels = ['.van-tab', '[role="tab"]', '.scontain li',
                            'li[class^="s"]', '.tab-item', '[class*="tab"]'];
                        for (const sel of tabSels) {
                            const tabs = document.querySelectorAll(sel);
                            for (const tab of tabs) {
                                const text = (tab.textContent || '').trim();
                                if (text.includes(label)) {
                                    // 检查是否已激活
                                    const cls = tab.className || '';
                                    if (cls.includes('active')) return { found: true, clicked: false, sel };
                                    tab.click();
                                    return { found: true, clicked: true, sel, text: text.substring(0, 30) };
                                }
                            }
                        }
                        return { found: false, label };
                    }""" % self._vue_app_finder_js(), [label, subject_type])
                    if clicked and clicked.get("found"):
                        if clicked.get("clicked"):
                            self.log.info(f"[Tab] JS click: {label} ({clicked.get('sel')})")
                        else:
                            self.log.debug(f"[Tab] 已激活: {label}")
                        time.sleep(0.8)
                        return True
                except Exception:
                    pass

            # 方案 2: Playwright locator 回退
            for label in labels:
                tab = self._page.locator(f'{SEL_COURSE_TAB}:has-text("{label}")')
                if tab.count() > 0:
                    active_cls = tab.first.get_attribute("class") or ""
                    if "van-tab--active" not in active_cls:
                        tab.first.scroll_into_view_if_needed(timeout=2000)
                        tab.first.click(timeout=5000)
                    self.log.debug(f"[Tab] locator 成功切换到: {label}")
                    return True

            # 调试：输出页面上实际存在的 Tab 元素
            try:
                tab_texts = self._page.evaluate("""() => {
                    const tabs = document.querySelectorAll(
                        '.van-tab, [role="tab"], .scontain li, li[class^="s"], .tab-item'
                    );
                    return Array.from(tabs).map(t => t.textContent.trim().substring(0, 40));
                }""")
                self.log.debug(f"[Tab] 未找到 {labels}，页面 Tab: {tab_texts}")
            except Exception:
                self.log.debug(f"[Tab] 未找到标签页: {labels}")

        except Exception as e:
            self.log.warning(f"[Tab] 切换失败: {e}")
        return False

    def _dismiss_broadcast(self) -> None:
        """关闭广播弹窗。优先使用 JS 直接移除，回退到 click。"""
        try:
            if self._page and not self._page.is_closed():
                dismissed = self._page.evaluate("""() => {
                    const bc = document.querySelector('.broadcast-modal');
                    if (!bc) return false;
                    const r = bc.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    // 方案 1: 点击关闭按钮
                    const btn = bc.querySelector('button, .close-btn, [class*="close"]');
                    if (btn) { btn.click(); return true; }
                    // 方案 2: 直接隐藏
                    bc.style.display = 'none';
                    return true;
                }""")
                if not dismissed:
                    # 回退: 点击空白区域
                    bc = self._page.locator(SEL_BROADCAST_MODAL)
                    if bc.count() > 0 and bc.first.is_visible():
                        self._page.mouse.click(10, 10)
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
        """展开顺序上第一个未完成的章节。

        使用 _summarize_collapse_progress 获取所有章节进度，
        按页面从上到下的顺序依次展开未完成的章节，跳过已完成的章节。
        通过 state.expanded_sections 追踪已展开的章节，避免重复展开。
        """
        if not self._page or self._page.is_closed():
            return False

        progress = self._summarize_collapse_progress()
        if not progress or not progress.get("sections"):
            # Fallback: 无法解析进度时，尝试盲目展开第一个未激活的章节
            self.log.debug("[章节] 无法解析章节进度，尝试盲目展开...")
            return self._expand_first_inactive_section(state)

        sections = progress["sections"]
        incomplete_sections = [s for s in sections if s["incomplete"] > 0]

        if not incomplete_sections:
            self.log.debug("[章节] 所有章节已完成，无需展开")
            return False

        self.log.info(
            f"[章节] 共 {len(sections)} 个章节，{len(incomplete_sections)} 个有未完成课程"
        )

        collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
        expanded_sections = state.expanded_sections if state else set()
        expand_count_map = state._expand_count_map if state else {}

        for section in incomplete_sections:
            i = section["index"]
            title_text = section["title"]
            incomplete_count = section["incomplete"]

            expand_key = f"{i}:{title_text}"
            cur_count = expand_count_map.get(expand_key, 0)
            if cur_count >= 3:
                self.log.debug(
                    f"[章节] 跳过重复展开: {title_text} (已展开 {cur_count} 次)"
                )
                continue

            verified = state._verified_complete_sections if state else set()
            if expand_key in verified:
                self.log.debug(
                    f"[章节] 跳过已确认完成的章节: {title_text}"
                )
                continue

            try:
                collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
                if i >= collapse_items.count():
                    self.log.debug(
                        f"[章节] 索引 {i} 超出范围 ({collapse_items.count()})，尝试标题匹配"
                    )
                    item = self._find_collapse_by_title(title_text)
                    if item is None:
                        continue
                else:
                    item = collapse_items.nth(i)

                title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
                if title_btn.count() == 0:
                    continue

                cls = item.get_attribute("class") or ""
                if "van-collapse-item--active" in cls:
                    self.log.debug(f"[章节] 已展开(非由本次): {title_text}")
                    if expand_key not in expand_count_map:
                        expand_count_map[expand_key] = cur_count + 1
                        if state:
                            state._expand_count_map = expand_count_map
                            expanded_sections.add(expand_key)
                            state.expanded_sections = expanded_sections
                    continue

                # 优先通过 Vue collapse API 展开，回退到 click
                expanded_via_api = False
                try:
                    expanded_via_api = self._page.evaluate("""async (index) => {
                        %s
                        const app = findVueProxy(['activeNames']) || findVueProxy(['loadCourseData']);
                        if (!app) return false;
                        const val = app.activeNames || app.value || app.expanded;
                        const items = document.querySelectorAll('.van-collapse-item');
                        const item = items[index];
                        if (!item) return false;
                        const name = item.getAttribute('name') || String(index);
                        const normalizedName = /^\\d+$/.test(name) ? Number(name) : name;
                        if (Array.isArray(val) && !val.includes(name) && !val.includes(normalizedName)) {
                            val.push(name);
                            if (app.activeNames !== undefined) app.activeNames = [...val];
                            else if (app.value !== undefined) app.value = [...val];
                        } else if (app.activeNames !== undefined) {
                            app.activeNames = normalizedName;
                        } else if (app.value !== undefined) {
                            app.value = normalizedName;
                        }
                        // 当前源码展开章节后会调用 loadCourseData；仅改 activeNames 不会加载课程。
                        try {
                            if (typeof app.onCollapseChange === 'function') {
                                const ret = app.onCollapseChange(normalizedName);
                                if (ret && typeof ret.then === 'function') await ret;
                            } else if (typeof app.loadCourseData === 'function' && app.categoryList?.[normalizedName]) {
                                const ret = app.loadCourseData(app.categoryList[normalizedName].categoryCode);
                                if (ret && typeof ret.then === 'function') await ret;
                            }
                            if (typeof app.$nextTick === 'function') await app.$nextTick();
                        } catch(e) {
                            return false;
                        }
                        return true;
                    }""" % self._vue_app_finder_js(), i)
                except Exception:
                    pass

                if not expanded_via_api:
                    title_btn.scroll_into_view_if_needed(timeout=3000)
                    title_btn.click(timeout=5000)
                self.log.info(
                    f"[章节] 展开: {title_text} ({section['finished']}/{section['total']}, {incomplete_count} 未完成)"
                )

                expand_count_map[expand_key] = cur_count + 1
                if state:
                    state._expand_count_map = expand_count_map
                    expanded_sections.add(expand_key)
                    state.expanded_sections = expanded_sections
                    state._last_expanded_section_key = expand_key

                # 等待展开动画完成，确保课程列表已渲染
                try:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_ITEMS, state="attached", timeout=8000
                    )
                except Exception:
                    pass
                time.sleep(0.5)

                return True
            except Exception as e:
                self.log.debug(f"[章节] 展开失败({title_text}): {e}")
                continue

        self.log.debug("[章节] 所有未完成章节都已展开过")
        return False

    def _expand_first_inactive_section(self, state) -> bool:
        """展开第一个未激活且未完成的折叠章节（备用方案）。

        通过章节标题中的 "N/M" 进度判断是否已完成，避免展开已有 8/8 等完成标记的章节。
        """
        try:
            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            for i in range(collapse_items.count()):
                item = collapse_items.nth(i)
                cls = item.get_attribute("class") or ""
                if "van-collapse-item--active" in cls:
                    continue

                expand_key = f"blind_{i}"
                expand_count_map = state._expand_count_map if state else {}
                if expand_count_map.get(expand_key, 0) >= 2:
                    continue

                # 检查章节标题中的进度，跳过已完成的章节
                title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
                if title_btn.count() == 0:
                    continue
                try:
                    title_text = title_btn.inner_text().strip()
                except Exception:
                    title_text = "未知"

                # 跳过已确认完成的章节（计数器可能未更新）
                verified = state._verified_complete_sections if state else set()
                sec_key = f"{i}:{title_text}"
                if sec_key in verified:
                    self.log.debug(
                        f"[章节] 跳过已确认完成的章节: {title_text}"
                    )
                    continue

                finished_num, total_num = self._parse_section_progress(title_text)
                if (
                    finished_num is not None
                    and total_num is not None
                    and total_num > 0
                    and finished_num >= total_num
                ):
                    self.log.debug(
                        f"[章节] 跳过已完成章节: {title_text} ({finished_num}/{total_num})"
                    )
                    continue

                # 优先通过 Vue collapse API 展开，回退到 click
                expanded_via_api = False
                try:
                    expanded_via_api = self._page.evaluate("""async (index) => {
                        %s
                        const app = findVueProxy(['activeNames']) || findVueProxy(['loadCourseData']);
                        if (!app) return false;
                        const val = app.activeNames || app.value || app.expanded;
                        const items = document.querySelectorAll('.van-collapse-item');
                        const item = items[index];
                        if (!item) return false;
                        const name = item.getAttribute('name') || String(index);
                        const normalizedName = /^\\d+$/.test(name) ? Number(name) : name;
                        if (Array.isArray(val) && !val.includes(name) && !val.includes(normalizedName)) {
                            val.push(name);
                            if (app.activeNames !== undefined) app.activeNames = [...val];
                            else if (app.value !== undefined) app.value = [...val];
                        } else if (app.activeNames !== undefined) {
                            app.activeNames = normalizedName;
                        } else if (app.value !== undefined) {
                            app.value = normalizedName;
                        }
                        try {
                            if (typeof app.onCollapseChange === 'function') {
                                const ret = app.onCollapseChange(normalizedName);
                                if (ret && typeof ret.then === 'function') await ret;
                            } else if (typeof app.loadCourseData === 'function' && app.categoryList?.[normalizedName]) {
                                const ret = app.loadCourseData(app.categoryList[normalizedName].categoryCode);
                                if (ret && typeof ret.then === 'function') await ret;
                            }
                            if (typeof app.$nextTick === 'function') await app.$nextTick();
                        } catch(e) {
                            return false;
                        }
                        return true;
                    }""" % self._vue_app_finder_js(), i)
                except Exception:
                    pass

                if not expanded_via_api:
                    title_btn.scroll_into_view_if_needed(timeout=3000)
                    title_btn.click(timeout=5000)
                self.log.info(f"[章节] 盲目展开 #{i}: {title_text}")

                if state:
                    state._expand_count_map = expand_count_map
                    expand_count_map[expand_key] = (
                        expand_count_map.get(expand_key, 0) + 1
                    )

                try:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_ITEMS, state="attached", timeout=6000
                    )
                except Exception:
                    pass
                time.sleep(0.5)
                return True
        except Exception as e:
            self.log.debug(f"[章节] 盲目展开失败: {e}")
        return False

    def _find_collapse_by_title(self, title_text: str):
        """通过标题文本匹配查找折叠章节元素。"""
        try:
            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            for i in range(collapse_items.count()):
                item = collapse_items.nth(i)
                title_btn = item.locator(SEL_COLLAPSE_ITEM_TITLE).first
                if title_btn.count() > 0:
                    try:
                        item_title = title_btn.inner_text().strip()
                        if item_title == title_text or title_text in item_title:
                            return item
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _collect_tasks_via_vue(self) -> List[Dict[str, Any]] | None:
        """尝试从 Vue 组件数据直接读取当前 Tab 的课程列表。

        优先级高于 DOM 扫描，速度快且不需要滚动/等待懒加载。
        返回 None 表示 Vue 数据不可用，应回退到 DOM 扫描。
        """
        if not self._page or self._page.is_closed():
            return None
        try:
            result = self._page.evaluate(r"""() => {
                %s
                const app = findVueProxy(['courseList']) || findVueProxy(['categoryList']);
                if (!app) return null;

                // 尝试从 Vue 数据中读取课程列表
                // CourseIndex.vue / StudyPage.vue 当前实际使用 courseList。
                const candidates = [
                    'courseList', 'listData', 'filterList', 'currentList',
                    'showList', 'courseData', 'items'
                ];
                let list = null;
                for (const key of candidates) {
                    if (Array.isArray(app[key]) && app[key].length > 0) {
                        list = app[key];
                        break;
                    }
                }

                // 也检查 collapse 内部的课程列表
                if (!list && Array.isArray(app.sectionList)) {
                    list = [];
                    for (const sec of app.sectionList) {
                        if (Array.isArray(sec.courseList || sec.list || sec.children)) {
                            list.push(...(sec.courseList || sec.list || sec.children));
                        }
                    }
                }

                if (!list || list.length === 0) return null;

                return list.map(item => ({
                    // 目标源码中的课程名字段是 resourceName，旧脚本只读 courseName 会漏扫。
                    title: item.resourceName || item.courseName || item.name || item.title || item.nickName || '',
                    // 与 CourseIndex.vue / StudyPage.vue 模板保持一致: Number(course.finished) === 1
                    // 避免 !!item.finished 对字符串 "0" 产生误判、studyState===1 表示进行中而非完成
                    passed: Number(item.finished) === 1 || !!item.passed || !!item.isFinish,
                    type: item.courseType === 'fchl' || item.type === 'fchl' ? 'fchl' : 'img-text',
                    courseId: item.resourceId || item.courseId || item.id || item.userCourseId || '',
                    userCourseId: item.userCourseId || '',
                    source: item.source || '',
                    url: item.url || item.courseUrl || '',
                })).filter(t => t.title && t.title.length >= 2);
            }""" % self._vue_app_finder_js())
            if result and len(result) > 0:
                self.log.info(f"[扫描] Vue 数据: {len(result)} 门课程")
                return result
        except Exception as e:
            self.log.debug(f"[扫描] Vue 数据读取失败: {e}")
        return None

    def _collect_tasks_in_current_tab(self) -> List[Dict[str, Any]]:
        """收集当前 Tab 中可见的课程任务。

        优先从 Vue 组件数据直接读取（快速、不需要滚动），
        回退到 DOM 扫描（兼容性好）。
        """
        if not self._page or self._page.is_closed():
            return []

        ctx = self._detect_page_context()
        if ctx == PageContext.PROJECT_LIST:
            self.log.debug("[扫描] 当前处于项目列表页，需要先进入项目")
            return []

        # 等待课程列表项出现（SPA 可能延迟渲染）
        try:
            self._page.wait_for_selector(
                SEL_COURSE_LIST_WAIT_TARGETS, state="attached", timeout=8000
            )
        except Exception:
            pass

        # 优先从 Vue 数据读取
        vue_tasks = self._collect_tasks_via_vue()
        if vue_tasks is not None:
            return vue_tasks

        # 回退到 DOM 扫描
        dom_tasks: list[dict[str, Any]] = []
        loc = self._page.locator(SEL_COURSE_LIST_ITEMS)

        for i in range(loc.count()):
            it = loc.nth(i)
            try:
                # 尝试滚动到视口内以确保懒加载内容可见
                try:
                    it.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                time.sleep(0.05)

                if not it.is_visible():
                    continue
            except Exception:
                continue

            title = self._extract_item_title(it)
            if not title or len(title) < 2:
                continue

            cls = (it.get_attribute("class") or "").lower()
            if "van-cell__title" in cls:
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

        self.log.info(f"[扫描] DOM: {len(dom_tasks)} 门课程")
        return dom_tasks

    def _summarize_collapse_progress(self) -> dict[str, Any] | None:
        """汇总折叠章节的完成进度，返回按页面顺序排列的详细章节信息。

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
                # Fallback: 章节标题不含进度数字时，尝试通过子项状态推断
                finished_num, total_num = self._count_section_items(item)

            if total_num is None or total_num == 0:
                continue

            found_progress = True
            total += total_num
            completed += min(finished_num, total_num)

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

        sections.sort(key=lambda x: x["index"])

        return {
            "total": total,
            "completed": completed,
            "incomplete": max(0, total - completed),
            "sections": sections,
        }

    def _count_section_items(self, collapse_item) -> tuple[int, int]:
        """备用方案：通过检查折叠项内课程子元素的 passed/finished 状态推断进度。

        DOM 结构中，课程项位于 .van-collapse-item__content 内。
        即使折叠面板收起，子元素仍然存在于 DOM 中（仅 CSS 隐藏）。
        """
        try:
            content = collapse_item.locator(SEL_COLLAPSE_ITEM_CONTENT).first
            if content.count() == 0:
                return 0, 0

            img_items = content.locator(".img-texts-item")
            fchl_items = content.locator(".fchl-item")
            total_count = img_items.count() + fchl_items.count()
            if total_count == 0:
                return 0, 0

            finished_count = 0
            for it_sel in [
                ".img-texts-item.passed",
                ".img-texts-item.finished",
                ".fchl-item.fchl-item-active",
            ]:
                finished_count += content.locator(it_sel).count()

            # 也检查是否有绿色完成图标
            finished_count += content.locator(SEL_ITEM_COMPLETED_ICON).count()

            # 避免重复计数：最多不超过总数
            finished_count = min(finished_count, total_count)
            return finished_count, total_count
        except Exception:
            return 0, 0

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
                            SEL_RUNTIME_NAV_BTNS.split(",")[0]
                            + ", .page-item, .page-tranformImg, .page-360, .page-start"
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

    def _extract_nonstr_map(self, frame) -> dict[int, str]:
        """从课程页面脚本中提取 nonstrMap（apicenext.js 步骤追踪令牌）。

        每个课程的 item.js 定义 nonstrMap = new Map([N, 'token'], ...)，
        用于 callApinext() 的反爬虫校验。

        支持两种来源：
        1. 内联 <script> 标签中的 nonstrMap 定义
        2. 外部 JS 文件（webpack 打包的课程脚本）中的 Map([[N, 'token'], ...]) 模式
        """
        try:
            result = frame.evaluate("""() => {
                // 通用：从文本内容中提取 Map([[N, 'token'], ...]) 模式
                function extractMapEntries(content) {
                    if (!content) return [];
                    const entries = [];
                    // 匹配 new Map([[N, "token"], [N, "token"], ...])
                    const mapRe = /new\\s+Map\\s*\\(\\s*\\[([^\\]]+)\\]\\s*\\)/g;
                    let mm;
                    while ((mm = mapRe.exec(content)) !== null) {
                        const body = mm[1];
                        const pairRe = /\\[\\s*(\\d+)\\s*,\\s*['"]([^'"]+)['"]\\s*\\]/g;
                        let pm;
                        while ((pm = pairRe.exec(body)) !== null) {
                            entries.push([parseInt(pm[1]), pm[2]]);
                        }
                    }
                    // 回退：直接匹配 [N, "token"] 模式（nonstrMap 赋值语句中）
                    if (entries.length === 0 && content.includes('nonstrMap')) {
                        const re = /\\[\\s*(\\d+)\\s*,\\s*['"]([^'"]+)['"]\\s*\\]/g;
                        let m;
                        while ((m = re.exec(content)) !== null) {
                            entries.push([parseInt(m[1]), m[2]]);
                        }
                    }
                    return entries;
                }

                // 方案 1：搜索内联脚本
                const scripts = document.querySelectorAll('script');
                for (const script of scripts) {
                    const content = script.textContent;
                    if (!content) continue;
                    if (!content.includes('nonstrMap') && !content.includes('callApinext')) continue;
                    const entries = extractMapEntries(content);
                    if (entries.length > 0) return { entries: entries, source: 'inline' };
                }

                // 方案 2：搜索外部脚本（webpack 打包的课程脚本如 A08030.js）
                const extScripts = document.querySelectorAll('script[src]');
                for (const script of extScripts) {
                    const src = script.getAttribute('src') || '';
                    // 跳过公共库，只搜索课程特定脚本
                    if (/jquery|jweixin|crypto|sdk|captcha|tgJCap|apicenext|wx\\.js|video-js|fontRem/i.test(src)) continue;
                    if (!/\\.js(\\?|$)/i.test(src)) continue;
                    try {
                        const xhr = new XMLHttpRequest();
                        xhr.open('GET', src, false);
                        xhr.send();
                        if (xhr.status === 200) {
                            const entries = extractMapEntries(xhr.responseText);
                            if (entries.length > 0) return { entries: entries, source: 'external:' + src.split('/').pop() };
                        }
                    } catch(e) {}
                }

                return null;
            }""")
            if result:
                entries = result.get("entries", [])
                source = result.get("source", "unknown")
                if entries:
                    nonstr_map = dict(entries)
                    self.log.debug(f"[nonstrMap] 提取到 {len(nonstr_map)} 个令牌 ({source}): {nonstr_map}")
                    return nonstr_map
        except Exception as e:
            self.log.debug(f"[nonstrMap] 提取异常: {e}")
        return {}

    def _analyze_course_structure(self, frame) -> dict:
        """提取课程页数和 nonstrMap 令牌。

        Returns:
            dict: {
                'total_pages':   int   — .page-item 元素数量
                'nonstr_map':    dict  — apicenext 步骤追踪令牌
            }
        """
        info = {
            'total_pages': 0,
            'nonstr_map': {},
        }
        try:
            total = frame.evaluate(
                "() => document.querySelectorAll('.page-item').length"
            )
            info['total_pages'] = total or 0
            info['nonstr_map'] = self._extract_nonstr_map(frame)

            self.log.info(
                f"[分析] 页数={info['total_pages']} "
                f"nonstrMap={len(info['nonstr_map'])}个"
            )
        except Exception as e:
            self.log.debug(f"[分析] 课程结构分析异常: {e}")
        return info

    def _call_apicenext(self, frame, nextprev: str, finish: int, nonstr_map: dict) -> bool:
        """调用 callApinext 追踪课程步骤进度。

        callApinext 内部有 nextnummax 守卫，重复调用同一步骤会被忽略，
        因此在按钮点击（课程 JS 自带 callApinext）之后再调一次是安全的。
        """
        if not nonstr_map:
            return False
        try:
            map_entries = ", ".join(f'[{k}, "{v}"]' for k, v in nonstr_map.items())
            result = frame.evaluate(f"""() => {{
                if (typeof callApinext !== 'function') return false;
                const nonstrMap = new Map([{map_entries}]);
                callApinext("{nextprev}", {finish}, nonstrMap);
                return true;
            }}""")
            if result:
                self.log.debug(f"[apicenext] {nextprev} finish={finish}")
            return result
        except Exception as e:
            self.log.debug(f"[apicenext] 调用异常: {e}")
            return False

    def _handle_protocol_page(self) -> bool:
        """直接 JS 处理承诺书/协议页。

        只在页面确实存在协议相关元素时返回 True，避免误判普通页面。
        """
        if not self._page:
            return False
        try:
            result = self._page.evaluate("""() => {
                const cb = document.querySelector('#agree, .agree-checkbox, input[type="checkbox"]');
                const protocolText = document.querySelector('.protocol, .promise, .agree-text, .protocol-content');
                const hasProtocol = cb || (protocolText && protocolText.textContent.length > 50);
                if (!hasProtocol) return false;
                if (cb && !cb.checked) cb.click();
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const txt = (btn.textContent || '').trim();
                    const r = btn.getBoundingClientRect();
                    if (/下一步|同意|保存|确认/.test(txt) && r.width > 0 && r.height > 0) {
                        btn.click(); return true;
                    }
                }
                return !!cb;
            }""")
            if result:
                self.log.info("[协议] JS 处理承诺书页")
            return result
        except Exception as e:
            self.log.warning(f"[协议] 处理承诺书页异常: {e}")
            return False

    def _handle_intermediate_pages(self) -> None:
        """处理进入课程前的中间页面。"""
        if not self._page:
            return

        for _round in range(5):
            if not self._page or self._page.is_closed():
                return

            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return

            if self._handle_protocol_page():
                time.sleep(0.5)
                continue

            try:
                clicked = self._page.evaluate("""() => {
                    const el = document.querySelector('.img-text-block');
                    if (el) { el.click(); return true; }
                    return false;
                }""")
                if clicked:
                    self.log.info("[实验室] JS click 第一个子实验")
                    time.sleep(0.5)
                    continue
            except Exception:
                pass

            try:
                self._page.wait_for_selector(SEL_COURSE_LIST_WAIT_TARGETS, timeout=5000)
            except Exception:
                pass

    def _handle_quiz_via_api(self, frame) -> bool:
        """直接调用课程 JS API 完成答题，不模拟点击。

        优先级：getQuestions + saveExamQuestion/saveQuestions。
        适用于 Type 6 课程（DA0416068 等），也兼容 Type 2/3（slFn/slFn2）。
        """
        try:
            has_api = frame.evaluate("""() => typeof getQuestions === 'function'
                && (typeof saveExamQuestion === 'function' || typeof saveQuestions === 'function')""")
            if not has_api:
                return False

            self.log.info("[答题API] 检测到服务端答题 API，直接调用")

            raw = frame.evaluate("""() => {
                return new Promise((resolve, reject) => {
                    getQuestions().then(function(res) {
                        const data = (typeof res === 'string' ? JSON.parse(res) : res).data || {};
                        resolve({
                            viewpoint: data.viewpointQuestionList || [],
                            exam: data.examQuestionList || []
                        });
                    }).catch(function(e) { reject(e.message); });
                });
            }""")

            if not raw:
                return False

            viewpoint = raw.get("viewpoint", [])
            exam = raw.get("exam", [])

            if not viewpoint and not exam:
                self.log.info("[答题API] 无题目")
                return True

            # ── 课中观点题（仅提交，无对错）──
            for q in viewpoint:
                qid = q.get("id")
                opts = q.get("optionList", [])
                if not qid or not opts:
                    continue
                first_answer = json.dumps([opts[0]["id"]])
                self.log.info(f"[答题API] 观点题 {qid[:8]}... → 提交")
                try:
                    frame.evaluate(f"""() => {{
                        return new Promise((resolve) => {{
                            saveQuestions('{first_answer}', '{qid}')
                                .then(function(r) {{ resolve(JSON.stringify(r)); }})
                                .catch(function() {{ resolve(null); }});
                        }});
                    }}""")
                except Exception:
                    pass

            # ── 课后考试题（逐题试到正确）──
            all_answered = True
            for idx, q in enumerate(exam):
                qid = q.get("id")
                opts = q.get("optionList", [])
                qtype = q.get("type", 1)
                if not qid or not opts:
                    continue

                opt_ids = [o["id"] for o in opts]
                type_name = "单选" if qtype == 1 else "多选"
                self.log.info(
                    f"[答题API] 考试题 {idx+1}/{len(exam)} ({type_name}, {len(opt_ids)} 选项)"
                )

                answered = False
                if qtype == 1:
                    for oid in opt_ids:
                        ans_json = json.dumps([oid])
                        result = frame.evaluate(f"""() => {{
                            return new Promise((resolve) => {{
                                saveExamQuestion('{ans_json}', '{qid}')
                                    .then(function(r) {{ resolve(r); }})
                                    .catch(function() {{ resolve(null); }});
                            }});
                        }}""")
                        if result and result.get("isRight") == 1:
                            self.log.info("[答题API] ✓ 正确")
                            answered = True
                            break
                        time.sleep(0.3)
                else:
                    # 多选：先试全部，再逐个排除
                    combos = [opt_ids]
                    for oid in opt_ids:
                        combo = [o for o in opt_ids if o != oid]
                        if combo:
                            combos.append(combo)
                    for combo in combos:
                        ans_json = json.dumps(combo)
                        result = frame.evaluate(f"""() => {{
                            return new Promise((resolve) => {{
                                saveExamQuestion('{ans_json}', '{qid}')
                                    .then(function(r) {{ resolve(r); }})
                                    .catch(function() {{ resolve(null); }});
                            }});
                        }}""")
                        if result and result.get("isRight") == 1:
                            self.log.info(f"[答题API] ✓ 正确 ({len(combo)} 项)")
                            answered = True
                            break
                        time.sleep(0.3)

                if not answered:
                    all_answered = False
                    self.log.warning(f"[答题API] 题目 {idx+1} 未能找到正确答案")

            self.log.info(f"[答题API] 全部 {len(exam)} 题处理完毕")
            # 不设 _quiz_api_done，允许后续回退到 DOM 答题路径
            return all_answered

        except Exception as e:
            self.log.debug(f"[答题API] 异常: {e}")
            return False


    def _suppress_course_alert(self, frame) -> None:
        """抑制课程反作弊 alert。"""
        try:
            frame.evaluate("""() => {
                const orig = window.alert;
                window.alert = (msg) => {
                    if (!msg) return;
                    const text = String(msg);
                    if (text.includes('刷课') || text.includes('请学完') || text.includes('请重新学习')) return;
                    orig(msg);
                };
                setTimeout(() => { window.alert = orig; }, 60000);
            }""")
        except Exception:
            pass

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

            # 确认当前在评论页，否则不执行返回操作
            if "/comment" not in main_url:
                self.log.debug("[评论] 当前不在评论页，跳过返回操作")
                return False

            # 优先：Vue Router back()
            try:
                navigated = self._page.evaluate("""() => {
                    %s
                    const app = findVueProxy(['$router']) || findVueProxy(null);
                    if (app && app.$router) {
                        app.$router.back();
                        return true;
                    }
                    return false;
                }""" % self._vue_app_finder_js())
                if navigated:
                    self.log.info("[评论] Vue Router back()")
                    return True
            except Exception:
                pass

            # 回退：JS click 底部"返回列表"按钮
            try:
                clicked = self._page.evaluate("""() => {
                    const btn = document.querySelector('.comment-footer-button');
                    if (btn) { const r = btn.getBoundingClientRect(); if (r.width > 0 && r.height > 0) { btn.click(); return true; } }
                    const nav = document.querySelector('.van-nav-bar__left');
                    if (nav) { const r = nav.getBoundingClientRect(); if (r.width > 0 && r.height > 0) { nav.click(); return true; } }
                    return false;
                }""")
                if clicked:
                    self.log.info("[评论] JS click 返回按钮")
                    return True
            except Exception:
                pass

            self.log.warning("[评论] 在评论页但未找到返回按钮")
            return False

        except Exception as e:
            self.log.warning(f"[评论] 返回失败: {e}")
            return False

    def _try_finish_course(self, frame) -> dict:
        """尝试调用 finishWxCourse() / callApinext() 完成课程。

        根据 sdk.js 逻辑 (finishWxCourse — 旧版 API):
        - 成功 (code=0/1) + csCom=true → 跳转评论页 weiban.mycourse.cn/#/comment
        - 成功 (code=0/1) + csCom!=true → 显示完成弹窗 .pop-jsv
        - 失败 → alert('发送完成失败')
        - csCapt=true → 先验证码再调用后端

        根据 apicenext.js 逻辑 (callApinext — 新版 API):
        - 通过 jupiterapi/api/statusercourse/v1/next 追踪步骤
        - finished=1 标记课程完成，finished=2 仅记录进度
        - 对于使用 callApinext 的课程，仅调用 finishWxCourse() 可能不够

        Returns:
            dict: {
                'completed': bool,  # 课程是否真正完成
                'need_captcha': bool,  # 是否需要验证码
                'detached': bool,  # iframe 是否已分离（跳转到评论页）
            }
        """
        try:
            iframe_url = frame.url or ""
            self.log.info("[完成] 调用 finishWxCourse() / callApinext()...")
            self.log.debug(f"[完成] iframe URL: {iframe_url[:80]}")

            result = frame.evaluate("""() => {
                // 等待脚本加载完成（sdk.js / apicenext.js 可能延迟加载）
                return new Promise((resolve) => {
                    const start = Date.now();
                    const waitForScripts = () => {
                        const hasFinishWxCourse = typeof finishWxCourse === 'function';
                        const hasCallApinext = typeof callApinext === 'function';
                        // finishWxCourse 是真正完成课程的函数，优先使用
                        if (hasFinishWxCourse) {
                            doComplete(true, hasCallApinext, resolve);
                            return;
                        }
                        // sdk.js 未加载（wx.js 外部脚本阻塞），使用直接 API 调用
                        // callApinext 仅用于页面追踪，不能完成课程
                        if (Date.now() - start >= 3000) {
                            doComplete(false, hasCallApinext, resolve);
                            return;
                        }
                        setTimeout(waitForScripts, 300);
                    };
                    waitForScripts();
                });

                function doComplete(hasFinishWxCourse, hasCallApinext, resolve) {
                    const originalAlert = window.alert;
                    let alertMsg = '';
                    let apiResponse = null;

                    window.alert = (msg) => { alertMsg = msg; };

                    // 拦截 XHR 响应以获取 finishWxCourse() 的实际 API 结果
                    // 因为 alert() 被拦截后 JS 会继续执行 redirect，
                    // 仅靠 alert 消息或 frame 分离无法判断服务端是否真正完成
                    const origOpen = XMLHttpRequest.prototype.open;
                    const origSend = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function(method, url, ...args) {
                        this._url = url;
                        return origOpen.call(this, method, url, ...args);
                    };
                    XMLHttpRequest.prototype.send = function(...args) {
                        this.addEventListener('load', function() {
                            try {
                                if (this._url && (
                                    this._url.includes('finishWxCourse') ||
                                    this._url.includes('finish') ||
                                    this._url.includes('complete') ||
                                    this._url.includes('usercourse')
                                )) {
                                    apiResponse = { url: this._url, status: this.status, text: this.responseText.substring(0, 500) };
                                }
                            } catch(e) {}
                        });
                        return origSend.call(this, ...args);
                    };

                    const checkCompletion = () => {
                        window.alert = originalAlert;
                        XMLHttpRequest.prototype.open = origOpen;
                        XMLHttpRequest.prototype.send = origSend;

                        // 检查是否显示完成弹窗
                        const popJs = document.querySelector('.pop-jsv');
                        if (popJs) { const r = popJs.getBoundingClientRect(); if (r.width > 0 && r.height > 0) {
                            resolve({ completed: true, need_captcha: false, popup: true });
                            return;
                        } }

                        // 检查是否有验证码弹窗（排除预加载的隐藏容器）
                        const captchaContainer = document.querySelector('#tcaptcha_transform_dy');
                        if (captchaContainer) {
                            const cs = (captchaContainer.getAttribute('style') || '').toLowerCase();
                            const isActive = !cs.includes('opacity: 0') && !cs.includes('opacity:0')
                                && !cs.includes('top: -1e+06') && !cs.includes('top:-1e+06');
                            if (isActive) {
                                resolve({ completed: false, need_captcha: true });
                                return;
                            }
                        }

                        // 检查 XHR 响应判断服务端结果
                        if (apiResponse) {
                            try {
                                const data = JSON.parse(apiResponse.text);
                                if (data.code === 0 || data.code === 1 || data.code === '0' || data.code === '1') {
                                    resolve({ completed: true, need_captcha: false, apiSuccess: true });
                                    return;
                                }
                            } catch(e) {
                                // 非 JSON 响应，检查文本
                                if (apiResponse.text.includes('"code":0') || apiResponse.text.includes('"code":1')) {
                                    resolve({ completed: true, need_captcha: false, apiSuccess: true });
                                    return;
                                }
                            }
                            resolve({ completed: false, need_captcha: false, reason: 'API 返回失败: ' + apiResponse.text.substring(0, 200) });
                            return;
                        }

                        // 检查是否失败（alert 消息）
                        if (alertMsg.includes('失败')) {
                            resolve({ completed: false, need_captcha: false, reason: alertMsg });
                            return;
                        }

                        // 默认：未完成但无错误
                        resolve({ completed: false, need_captcha: false });
                    };

                    // 从 iframe URL 提取参数，用于直接调用 finish API（fallback）
                    function getQueryParam(name) {
                        const m = window.location.search.match(new RegExp('(^|&)' + name + '=([^&]*)(&|$)', 'i'));
                        return m ? decodeURIComponent(m[2]) : null;
                    }

                    function directFinishAPI() {
                        const userCourseId = getQueryParam('userCourseId');
                        const tenantCode = getQueryParam('tenantCode');
                        const weiban = getQueryParam('weiban');
                        const lyra = getQueryParam('lyra');
                        const source = getQueryParam('source');

                        if (!userCourseId) return;

                        let url = '';
                        if (weiban !== 'weiban') {
                            if (lyra === 'lyra') {
                                url = 'https://lyra.mycourse.cn/lyraapi/study/course/finish.api';
                            } else {
                                url = 'https://open.mycourse.cn/proteus/usercourse/finish.do';
                            }
                        } else if (source === 'moon') {
                            url = 'https://moon.mycourse.cn/moonapi/api/study/activity/microCourse/v1/finishedCourse';
                        } else {
                            url = 'https://weiban.mycourse.cn/pharos/usercourse/v2/' + userCourseId + '.do';
                        }

                        // 尝试 JSONP（与 sdk.js 的 $.ajax jsonp 模式一致）
                        const callbackName = '_finishCb_' + Date.now();
                        const script = document.createElement('script');
                        const params = 'userCourseId=' + encodeURIComponent(userCourseId)
                            + '&tenantCode=' + encodeURIComponent(tenantCode)
                            + '&callback=' + callbackName;
                        script.src = url + (url.includes('?') ? '&' : '?') + params;
                        window[callbackName] = function(data) {
                            apiResponse = { url: url, status: 200, text: JSON.stringify(data) };
                            delete window[callbackName];
                            try { document.head.removeChild(script); } catch(e) {}
                        };
                        script.onerror = function() {
                            delete window[callbackName];
                            try { document.head.removeChild(script); } catch(e) {}
                            // JSONP 失败，尝试 XHR POST（lyra 等非 JSONP 接口）
                            try {
                                const xhr = new XMLHttpRequest();
                                xhr.open('POST', url, true);
                                xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
                                xhr.onload = function() {
                                    apiResponse = { url: url, status: xhr.status, text: xhr.responseText.substring(0, 500) };
                                };
                                xhr.onerror = function() {};
                                xhr.send('userCourseId=' + encodeURIComponent(userCourseId) + '&tenantCode=' + encodeURIComponent(tenantCode));
                            } catch(e) {}
                        };
                        document.head.appendChild(script);
                    }

                    try {
                        // 确保 pageNums >= 4（反作弊检查）
                        // pageNums 是 let 变量，不能通过 window.pageNums 修改
                        try {
                            if (typeof pageNums !== 'undefined' && pageNums < 4) {
                                pageNums = 4;
                            }
                        } catch(e) {}

                        if (hasFinishWxCourse) {
                            // 正常路径：调用 sdk.js 定义的 finishWxCourse()
                            finishWxCourse();
                        } else {
                            // fallback：finishWxCourse 不可用（sdk.js 未加载），
                            // 直接调用 finish API
                            directFinishAPI();
                        }

                        // 轮询等待完成状态（弹窗/跳转），最多 8 秒
                        let elapsed = 0;
                        const poll = () => {
                            const popJs = document.querySelector('.pop-jsv');
                            if (popJs) { const r = popJs.getBoundingClientRect(); if (r.width > 0 && r.height > 0) {
                                window.alert = originalAlert;
                                XMLHttpRequest.prototype.open = origOpen;
                                XMLHttpRequest.prototype.send = origSend;
                                resolve({ completed: true, need_captcha: false, popup: true });
                                return;
                            } }
                            elapsed += 500;
                            if (elapsed >= 8000) {
                                checkCompletion();
                                return;
                            }
                            setTimeout(poll, 500);
                        };
                        setTimeout(poll, 1000);
                    } catch (e) {
                        window.alert = originalAlert;
                        XMLHttpRequest.prototype.open = origOpen;
                        XMLHttpRequest.prototype.send = origSend;
                        resolve({ completed: false, need_captcha: false, reason: e.message });
                    }
                }
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
            if "frame was detached" in err_msg or "has been closed" in err_msg:
                # Frame 分离是客户端行为（course JS redirect to /comment），
                # 不能作为服务端完成的判断依据。finishWxCourse() 失败时
                # 同样会触发 frame 分离（因为 alert 被拦截后 JS 继续执行 redirect）。
                self.log.info("[完成] iframe 已分离（客户端行为，不代表服务端完成）")
                return {
                    "completed": False,
                    "need_captcha": False,
                    "detached": True,
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
            # 验证码容器在 mcwk 页面预加载，仅当 URL 含 cscapt=true 时才需要处理
            frame_url = (frame.url or "").lower()
            if "cscapt=true" not in frame_url:
                return True

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
                try:
                    frame.evaluate("""() => {
                        const btn = document.querySelector('.pop-jsv-prev');
                        if (btn) btn.click();
                    }""")
                except Exception:
                    pass
                return True

        except Exception as e:
            self.log.debug(f"[状态] 检查异常: {e}")
        return False

    @staticmethod
    def _is_frame_detached_error(exc: Exception) -> bool:
        """判断异常是否为 iframe 分离（课程 JS 跳转到评论页）。"""
        return "frame was detached" in str(exc).lower() or "has been closed" in str(exc).lower()

    def _check_monitor_guard(self, frame) -> bool:
        """Check if finishWxCourse is null (monitor.js guard active).

        monitor.js sets finishWxCourse = null and only restores it after
        enough .btn-next clicks have been registered. When guarded,
        calling finishWxCourse() would fail — the course needs more clicks.

        Returns:
            True: guard is active (finishWxCourse is null/undefined)
            False: guard is not active (finishWxCourse is available)
        """
        try:
            result = frame.evaluate("""() => {
                return typeof finishWxCourse === 'undefined' || finishWxCourse === null;
            }""")
            return result
        except Exception:
            return False

    def _finish_course_via_api(self, frame) -> bool:
        """直接调用 finish API 完成课程（sdk.js 未加载时的 fallback）。"""
        self.log.info("[完成] 尝试直接调用 finish API...")
        try:
            result = self._try_finish_course(frame)
            if result.get("completed"):
                self.log.info("[完成] 直接 API 调用成功")
                if result.get("popup"):
                    self._return_from_comment_page()
                return True
            if result.get("need_captcha"):
                self.log.info("[完成] 需要验证码")
                if self._handle_captcha_if_needed(frame):
                    result = self._try_finish_course(frame)
                    if result.get("completed"):
                        return True
        except Exception as e:
            if self._is_frame_detached_error(e):
                self.log.info("[完成] frame 分离 → 课程完成")
                self._return_from_comment_page()
                return True
            self.log.debug(f"[完成] 直接 API 调用异常: {e}")
        self.log.warning("[完成] 课程未完成")
        return False

    def _trigger_img_text_completion(self, frame, title: str) -> bool:
        """通过直接调用 callApinext + finishWxCourse 完成课程。

        简化流程：
        1. 提取 nonstrMap（步骤追踪令牌，嵌入在 item.js 中）
        2. 分析课程结构获取页数
        3. 对每一页调用 callApinext("next", 2, nonstrMap)
        4. 等待至少 study_time 秒
        5. 调用 finishWxCourse() 完成课程
        """
        try:
            if not frame:
                if not self._page:
                    self.log.error("[播放] frame 和 page 均为空")
                    return False
                frame = self._page

            self.log.info("[播放] ════════════════════════════════════════")
            self.log.info(f"[播放] 开始课程: {title}")
            self.log.info(f"[播放] iframe URL: {frame.url if frame.url else 'N/A'}")

            min_study_time = getattr(self, "study_time", 20)
            start_time = time.time()

            # 1. 抑制课程弹窗
            self._suppress_course_alert(frame)

            # 2. 提取 nonstrMap（apicenext.js 步骤追踪令牌）
            nonstr_map = self._extract_nonstr_map(frame)
            self._course_nonstr_map = nonstr_map

            # 3. 分析课程结构获取页数
            course_info = self._analyze_course_structure(frame)
            total_pages = course_info.get("total_pages", 0)
            if total_pages <= 0:
                # 回退：至少调用 4 次（满足 pageNums >= 4 的反作弊要求）
                total_pages = 4
                self.log.info(f"[播放] 未检测到页面数，使用默认 {total_pages} 页")
            else:
                self.log.info(f"[播放] 检测到 {total_pages} 页")

            # 4. 设置 API 响应拦截
            api_results = {"steps": 0, "finished": False}

            def _on_course_response(response):
                try:
                    url = response.url
                    if "statusercourse/v1/next" in url:
                        api_results["steps"] += 1
                        self.log.debug(f"[API] 步骤追踪 #{api_results['steps']}")
                    elif "usercourse/v2/" in url and ".do" in url:
                        try:
                            data = response.json()
                            if data.get("code") in (0, 1, "0", "1"):
                                api_results["finished"] = True
                                self.log.info("[API] 课程完成确认")
                        except Exception:
                            pass
                except Exception:
                    pass

            self._page.on("response", _on_course_response)

            try:
                # 5. 逐页调用 callApinext("next", 2, nonstrMap)
                if nonstr_map:
                    self.log.info(f"[播放] 开始调用 callApinext，共 {total_pages} 页")
                    for i in range(total_pages):
                        if self._check_course_completed(frame):
                            self.log.info("[播放] 课程已自动完成")
                            return True
                        expected_steps = api_results["steps"] + 1
                        self._call_apicenext(frame, "next", 2, nonstr_map)
                        # 同步递增课程内部计数器
                        try:
                            frame.evaluate("""() => {
                                try { if (typeof pageNums !== 'undefined') pageNums++; } catch(e) {}
                                try { if (typeof sumClick !== 'undefined') sumClick++; } catch(e) {}
                                try { if (typeof atsum !== 'undefined') atsum++; } catch(e) {}
                            }""")
                        except Exception:
                            pass
                        # 等待服务端响应，避免请求堆积导致 finishWxCourse 时步骤未处理完
                        for _ in range(30):  # 最多等 3 秒
                            if api_results["steps"] >= expected_steps:
                                break
                            time.sleep(0.1)
                        elapsed = int(time.time() - start_time)
                        self.log.info(
                            f"[推进] callApinext {i + 1}/{total_pages} "
                            f"({elapsed}s/{min_study_time}s)"
                        )
                else:
                    self.log.info("[播放] 无 nonstrMap，跳过 callApinext 调用")

                # 6. 等待至少 study_time 秒
                elapsed = time.time() - start_time
                remaining = min_study_time - elapsed
                if remaining > 0:
                    self.log.info(f"[等待] 等待剩余学时 {int(remaining)} 秒")
                    time.sleep(remaining)

                # 7. 调用 finishWxCourse() 完成课程
                return self._finish_course_at_end(frame, api_results)

            finally:
                try:
                    self._page.remove_listener("response", _on_course_response)
                except Exception:
                    pass

        except Exception as e:
            if self._is_frame_detached_error(e):
                self.log.info("[播放] frame 分离 → 课程完成")
                self._return_from_comment_page()
                return True
            self.log.error(f"[播放] 异常退出: {str(e)}")
            return False

    def _finish_course_at_end(self, frame, api_results: dict) -> bool:
        """Consolidated course completion flow at the end of the course loop.

        1. Check if already completed (popup / comment page / API response)
        2. Ensure pageNums >= 4 (anti-cheat guard)
        3. Check monitor guard — if finishWxCourse is null, return False
           so the loop can continue clicking pages
        4. Call finishWxCourse() via frame.evaluate
        5. Wait for completion signals: .pop-jsv popup, frame detach, or
           API response
        """
        # ── Step 1: Already completed? ──
        if self._check_course_completed(frame):
            return True

        if api_results.get("finished"):
            self.log.info("[完成] API 响应已确认课程完成")
            self._return_from_comment_page()
            return True

        # ── Step 2: Anti-cheat guard — ensure pageNums >= 4 ──
        try:
            frame.evaluate("""() => {
                try {
                    if (typeof pageNums !== 'undefined' && pageNums < 4) {
                        pageNums = 4;
                    }
                } catch(e) {}
            }""")
        except Exception:
            pass

        # ── Step 3: Check monitor guard ──
        if self._check_monitor_guard(frame):
            self.log.info(
                "[完成] finishWxCourse 被 monitor.js 守卫（null），"
                "需要继续点击更多页面"
            )
            return False

        # ── Step 4: Call finishWxCourse() ──
        self.log.info("[完成] 调用 finishWxCourse()...")
        try:
            result = frame.evaluate("""() => {
                return new Promise((resolve) => {
                    if (typeof finishWxCourse !== 'function') {
                        resolve({ ok: false, reason: 'finishWxCourse is not a function' });
                        return;
                    }

                    const originalAlert = window.alert;
                    let alertMsg = '';
                    window.alert = (msg) => { alertMsg = msg || ''; };

                    try {
                        finishWxCourse();
                    } catch(e) {
                        window.alert = originalAlert;
                        resolve({ ok: false, reason: e.message });
                        return;
                    }

                    // Poll for completion signals, up to 8 seconds
                    let elapsed = 0;
                    const poll = () => {
                        // Check .pop-jsv popup
                        const pop = document.querySelector('.pop-jsv');
                        if (pop) { const r = pop.getBoundingClientRect(); if (r.width > 0 && r.height > 0) {
                            window.alert = originalAlert;
                            resolve({ ok: true, popup: true });
                            return;
                        } }
                        elapsed += 500;
                        if (elapsed >= 8000) {
                            window.alert = originalAlert;
                            if (alertMsg.includes('失败')) {
                                resolve({ ok: false, reason: alertMsg });
                            } else {
                                resolve({ ok: false, reason: 'timeout' });
                            }
                            return;
                        }
                        setTimeout(poll, 500);
                    };
                    setTimeout(poll, 1000);
                });
            }""")

            if result and result.get("ok"):
                if result.get("popup"):
                    self.log.info("[完成] finishWxCourse 成功 - 显示完成弹窗")
                else:
                    self.log.info("[完成] finishWxCourse 成功")
                return True

            reason = (result or {}).get("reason", "unknown")
            if reason:
                self.log.debug(f"[完成] finishWxCourse 未成功: {reason}")

        except Exception as e:
            if self._is_frame_detached_error(e):
                self.log.info("[完成] frame 已分离 → 课程 JS 已完成流程")
                self._return_from_comment_page()
                return True
            self.log.debug(f"[完成] finishWxCourse 调用异常: {e}")

        # ── Step 5: Final check — frame detach / completion state ──
        time.sleep(2)

        if self._check_course_completed(frame):
            return True

        try:
            frame.url
        except Exception:
            self.log.info("[完成] frame 已分离 → 课程 JS 已完成流程")
            self._return_from_comment_page()
            return True

        self.log.warning("[完成] 课程未完成")
        return False

    # ========================================================================
    # 课程完成与返回
    # ========================================================================

    def _return_to_chapter_list(self) -> bool:
        """返回章节列表。优先使用 Vue Router / history.back()，回退到 click。"""
        if not self._page or self._page.is_closed():
            return False

        for attempt in range(3):
            ctx = self._detect_page_context()
            if ctx == PageContext.COURSE_LIST:
                return True

            if ctx == PageContext.PROJECT_LIST:
                self.log.debug("[导航] 当前在项目列表页，需要直接导航回课程列表")
                return False

            try:
                # 课程完成后 iframe 常跳转到评论页，优先处理
                if self._return_from_comment_page():
                    self.log.debug("[导航] 从评论页返回")
                    time.sleep(1)
                    continue

                f = self._get_course_runtime_frame()
                if f:
                    try:
                        f.evaluate("if(typeof backToList === 'function') backToList();")
                        self.log.debug("[导航] 调用 backToList()")
                    except Exception:
                        pass

                # 优先使用 Vue Router 导航
                if self._page:
                    try:
                        navigated = self._page.evaluate("""() => {
                            %s
                            const app = findVueProxy(['$router']) || findVueProxy(null);
                            if (app && app.$router) {
                                app.$router.back();
                                return true;
                            }
                            return false;
                        }""" % self._vue_app_finder_js())
                        if navigated:
                            self.log.debug("[导航] Vue Router back()")
                            time.sleep(1)
                            continue
                    except Exception:
                        pass

                # 回退: history.back()
                if self._page:
                    try:
                        self._page.evaluate("() => { history.back(); }")
                        self.log.debug("[导航] history.back()")
                        time.sleep(1)
                        continue
                    except Exception:
                        pass

                # 回退: 点击返回按钮
                if self._page:
                    btn_back = self._page.locator(SEL_COMMENT_BACK_BTN).first
                    if btn_back.count() > 0 and btn_back.is_visible():
                        btn_back.scroll_into_view_if_needed(timeout=2000)
                        btn_back.click(timeout=5000)

                ctx = self._detect_page_context()
                if ctx == PageContext.COURSE_LIST:
                    return True
                if ctx == PageContext.PROJECT_LIST:
                    self.log.debug("[导航] 返回后落在项目列表页，可能需要重新进入项目")
                    return False

                if self._page:
                    btn_nav_back = self._page.locator(SEL_NAV_BAR_LEFT).first
                    if btn_nav_back.count() > 0 and btn_nav_back.is_visible():
                        self.log.debug("[导航] 点击左上角返回")
                        btn_nav_back.click(timeout=5000)

                    back_list_btn = self._page.locator(
                        ".back-list, .btn-back, [class*='back']"
                    ).first
                    if back_list_btn.count() > 0 and back_list_btn.is_visible():
                        self.log.debug("[导航] 点击 back-list")
                        back_list_btn.click(timeout=5000)

                ctx = self._detect_page_context()
                if ctx == PageContext.COURSE_LIST:
                    return True

            except Exception as e:
                self.log.debug(f"[导航] 返回尝试 {attempt + 1} 异常: {e}")

        # 所有返回尝试失败，尝试刷新页面作为最后手段
        self.log.debug("[导航] 常规返回失败，尝试刷新页面...")
        try:
            self._page.reload(wait_until="domcontentloaded", timeout=15000)
            time.sleep(1)
            ctx = self._detect_page_context()
            if ctx == PageContext.COURSE_LIST:
                self.log.info("[导航] 刷新后回到课程列表")
                return True
            if ctx == PageContext.PROJECT_LIST:
                self.log.debug("[导航] 刷新后落在项目列表页")
                return False
        except Exception as e:
            self.log.debug(f"[导航] 刷新页面异常: {e}")

        self.log.warning("[导航] 未能返回课程列表页")
        return False

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

        # 等待课程列表元素渲染完成（SPA 页面可能延迟加载）
        try:
            self._page.wait_for_selector(
                SEL_COURSE_LIST_WAIT_TARGETS, state="attached", timeout=8000
            )
        except Exception:
            pass

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

        # 使用章节进度信息，只展开有未完成课程的章节（跳过已完成的章节）
        try:
            progress = self._summarize_collapse_progress()
            incomplete_indices = set()
            if progress and progress.get("sections"):
                for sec in progress["sections"]:
                    if sec["incomplete"] > 0:
                        incomplete_indices.add(sec["index"])
                self.log.debug(
                    f"[定位] {len(incomplete_indices)} 个章节有未完成课程，"
                    f"跳过 {len(progress['sections']) - len(incomplete_indices)} 个已完成章节"
                )

            collapse_items = self._page.locator(SEL_COLLAPSE_ITEM)
            collapse_count = collapse_items.count()
            self.log.debug(f"[定位] 找到 {collapse_count} 个章节")

            for i in range(collapse_count):
                # 跳过已完成的章节
                if incomplete_indices and i not in incomplete_indices:
                    continue

                collapse = collapse_items.nth(i)
                try:
                    collapse_title_elem = collapse.locator(
                        SEL_COLLAPSE_ITEM_TITLE
                    ).first
                    if collapse_title_elem.count() == 0:
                        continue

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
                        # 优先通过 Vue collapse API 展开，回退到 click
                        expanded_via_api = False
                        try:
                            expanded_via_api = self._page.evaluate("""async (index) => {
                                %s
                                const app = findVueProxy(['activeNames']) || findVueProxy(['loadCourseData']);
                                if (!app) return false;
                                const val = app.activeNames || app.value || app.expanded;
                                const items = document.querySelectorAll('.van-collapse-item');
                                const item = items[index];
                                if (!item) return false;
                                const name = item.getAttribute('name') || String(index);
                                const normalizedName = /^\\d+$/.test(name) ? Number(name) : name;
                                if (Array.isArray(val) && !val.includes(name) && !val.includes(normalizedName)) {
                                    val.push(name);
                                    if (app.activeNames !== undefined) app.activeNames = [...val];
                                    else if (app.value !== undefined) app.value = [...val];
                                } else if (app.activeNames !== undefined) {
                                    app.activeNames = normalizedName;
                                } else if (app.value !== undefined) {
                                    app.value = normalizedName;
                                }
                                try {
                                    if (typeof app.onCollapseChange === 'function') {
                                        const ret = app.onCollapseChange(normalizedName);
                                        if (ret && typeof ret.then === 'function') await ret;
                                    } else if (typeof app.loadCourseData === 'function' && app.categoryList?.[normalizedName]) {
                                        const ret = app.loadCourseData(app.categoryList[normalizedName].categoryCode);
                                        if (ret && typeof ret.then === 'function') await ret;
                                    }
                                    if (typeof app.$nextTick === 'function') await app.$nextTick();
                                } catch(e) {
                                    return false;
                                }
                                return true;
                            }""" % self._vue_app_finder_js(), i)
                        except Exception:
                            pass

                        if not expanded_via_api:
                            collapse_title_elem.scroll_into_view_if_needed(timeout=2000)
                            collapse_title_elem.click(timeout=5000)
                        time.sleep(1.5)
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
        2. JS 逐页推进 + 等待最小学时
        3. 调用 finishWxCourse() 完成课程
        """
        self._quiz_api_done = False
        f = self._get_course_runtime_frame()
        if not f:
            self.log.warning("[课程] 未获取到课程框架")
            return False

        iframe_url = f.url or ""
        self.log.info(f"[课程] 进入 iframe: {iframe_url[:100]}...")
        self.log.info(f"[课程] 开始播放: {title}")

        result = self._trigger_img_text_completion(f, title)

        if result:
            self.log.info(f"[课程] 播放流程完成: {title}")
            return True

        self.log.warning("[课程] 播放流程未完成")
        return False

    def _verify_course_passed_on_list(self, title: str, course_type: str) -> bool:
        """回到课程列表页后，验证指定课程是否真的显示为已完成。

        因为 finishWxCourse() 的返回值不一定可靠（有时服务端未真正记录），
        必须以列表页实际显示的完成状态为准。

        先检查当前 DOM 状态；如果未通过，刷新一次页面后重试。
        """
        if not self._page or self._page.is_closed():
            return False

        ctx = self._detect_page_context()
        if ctx != PageContext.COURSE_LIST:
            self.log.debug(f"[验证] 当前不在课程列表页 (state={ctx.value})，跳过验证")
            return False

        # 第一次检查（不刷新）
        if self._check_passed_in_dom(title, course_type):
            return True

        # 刷新一次页面重试
        self.log.debug("[验证] 刷新页面重试...")
        try:
            self._page.reload(wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
            self._page.wait_for_selector(
                SEL_COURSE_LIST_WAIT_TARGETS, state="attached", timeout=10000
            )
        except Exception:
            pass

        if self._check_passed_in_dom(title, course_type):
            return True

        self.log.warning(f"[验证] ✗ 课程未在列表页显示为已完成: {title}")
        return False

    def _check_passed_in_dom(self, title: str, course_type: str) -> bool:
        """检查当前 DOM 中课程是否显示为已完成。"""
        try:
            if course_type == "fchl":
                items = self._page.locator(".fchl-item.fchl-item-active")
                for i in range(items.count()):
                    it = items.nth(i)
                    try:
                        if it.is_visible() and self._extract_item_title(it) == title:
                            self.log.info(f"[验证] ✓ FCHL 课程确认已完成: {title}")
                            return True
                    except Exception:
                        continue
            else:
                for sel in [".img-texts-item.passed", ".img-texts-item"]:
                    items = self._page.locator(sel)
                    for i in range(items.count()):
                        it = items.nth(i)
                        try:
                            if not it.is_visible():
                                continue
                            if self._extract_item_title(it) != title:
                                continue
                            cls = (it.get_attribute("class") or "").lower()
                            is_passed = (
                                "passed" in cls
                                or "finished" in cls
                                or it.locator(SEL_ITEM_COMPLETED_ICON).count() > 0
                            )
                            if is_passed:
                                self.log.info(f"[验证] ✓ 图文课程确认已完成: {title}")
                                return True
                        except Exception:
                            continue
        except Exception as e:
            self.log.debug(f"[验证] DOM 检查异常: {e}")
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

            if not self._return_to_chapter_list():
                self.log.warning(
                    f"[{idx + 1}/{total_tasks}] 无法返回课程列表页，"
                    f"中止当前批次 (剩余 {total_tasks - idx} 门)"
                )
                break

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
                    try:
                        item.evaluate("el => el.click()")
                    except Exception:
                        fail_reason = "点击课程元素失败"
                    else:
                        self._sleep_with_progress(study_time)
                        self.finish_study()
                        if not self._return_to_chapter_list():
                            fail_reason = "返回课程列表失败"
                        else:
                            ok = self._verify_course_passed_on_list(title, "fchl")
                            if not ok:
                                fail_reason = "列表页验证未通过（课程未显示为已完成）"
            else:
                # 重试机制：frame 分离后重新点击课程项打开 iframe
                for course_attempt in range(2):
                    item = self._find_img_text_item_by_title(title)
                    if not item:
                        fail_reason = "未找到课程元素"
                        break
                    try:
                        item.evaluate("el => el.click()")
                    except Exception:
                        fail_reason = "点击课程元素失败"
                        break

                    flow_ok = False
                    if not self._wait_for_mcwk_runtime():
                        fail_reason = "等待课程框架超时"
                    else:
                        flow_ok = self._finish_img_text_course(title, study_time)

                    if flow_ok:
                        break

                    # 播放未完成（可能 frame 分离），返回章节列表后重试
                    self.log.warning(
                        f"[{idx + 1}/{total_tasks}] 播放未完成"
                        f"{', 重试' if course_attempt == 0 else ''}"
                    )
                    self._return_to_chapter_list()

                if not flow_ok:
                    fail_reason = "课程播放/交互流程未完成"

                if not self._return_to_chapter_list():
                    self.log.warning(
                        f"[{idx + 1}/{total_tasks}] 课后返回课程列表失败，"
                        f"中止当前批次"
                    )
                    if flow_ok:
                        completed.add(title)
                        processed_cnt += 1
                    else:
                        failed.add(title)
                    break
                if flow_ok:
                    ok = self._verify_course_passed_on_list(title, "img-text")
                    if not ok:
                        fail_reason = "列表页验证未通过（课程未显示为已完成）"

            if ok:
                completed.add(title)
                processed_cnt += 1
                self.log.info(f"[{idx + 1}/{total_tasks}] 课程完成: {title}")
            else:
                failed.add(title)
                self.log.warning(
                    f"[{idx + 1}/{total_tasks}] 课程失败: {title} - {fail_reason}"
                )

            if self._page and self._page.is_closed():
                break

        return processed_cnt

    def _parse_tab_completion_from_dom(self) -> dict:
        """从 DOM 的 .van-tab 标签中直接读取课程完成数据。

        页面 DOM 结构:
        <div class="van-tab"> 或 <li class="s1">
          <span class="completion"><em>100</em>/100</span>
          <span class="name">课程学习</span>
        </div>

        只统计「课程学习/必修/选修」tab，排除「在线考试」tab。
        这是页面实际展示给用户的数据，比 Vue 内部状态更可靠。
        """
        if not self._page or self._page.is_closed():
            return {}

        try:
            js_code = r"""() => {
                // 优先从 .van-tab 读取（标准 CourseIndex.vue）
                const tabs = document.querySelectorAll('.van-tab');
                // 兜底：StudyPage.vue 使用自定义 <li class="s1"> / <li class="s2">
                const sTabs = document.querySelectorAll('.scontain .s1, .scontain .s2');

                const allTabs = tabs.length > 0 ? tabs : sTabs;
                const result = { course: { total: 0, done: 0 }, exam: { total: 0, done: 0 }, raw: [] };

                for (const tab of allTabs) {
                    const text = (tab.textContent || '').trim();
                    // 匹配 "100/100 课程学习" 或 "0/1 在线考试" 格式
                    const m = text.match(/(\d+)\s*[/]\s*(\d+)/);
                    if (!m) continue;

                    const done = parseInt(m[1], 10);
                    const total = parseInt(m[2], 10);
                    const entry = { done, total, text: text.substring(0, 60) };
                    result.raw.push(entry);

                    // 区分课程和考试
                    if (/考试|exam/i.test(text)) {
                        result.exam.total += total;
                        result.exam.done += done;
                    } else {
                        result.course.total += total;
                        result.course.done += done;
                    }
                }
                return result;
            }"""
            data = self._page.evaluate(js_code)
            if data and (data.get("course", {}).get("total", 0) > 0 or data.get("raw")):
                return data
        except Exception as e:
            self.log.debug(f"[统计] DOM tab 解析失败: {e}")
        return {}

    def _check_course_completion(self) -> dict:
        """检查课程完成情况。以页面 DOM 显示的 tab 数据为准。

        优先读取 .van-tab 标签中的完成数（页面实际展示给用户的数据），
        排除考试 tab，只统计课程学习相关 tab。
        Vue 数据和章节解析作为兜底方案。
        """
        if not self._page or self._page.is_closed():
            return {"total": 0, "completed": 0, "incomplete": 0}

        try:
            # 回到课程列表页
            self._return_to_chapter_list()

            # 强制刷新页面，确保数据从服务端重新获取
            try:
                self._page.reload(wait_until="domcontentloaded", timeout=15000)
                self._dismiss_broadcast()
            except Exception as e:
                self.log.debug(f"[统计] 页面刷新失败: {e}")

            # 等待课程列表元素出现
            try:
                self._page.wait_for_selector(
                    SEL_COURSE_LIST_WAIT_TARGETS, state="attached", timeout=10000
                )
            except Exception:
                pass

            # 方案 1：从 DOM tab 标签直接读取（最可靠，页面实际显示的数据）
            tab_data = self._parse_tab_completion_from_dom()
            course_data = tab_data.get("course", {})
            if course_data.get("total", 0) > 0:
                total = course_data["total"]
                completed = course_data["done"]
                incomplete = max(0, total - completed)
                self.log.info(
                    f"[课程统计] DOM tab: 总计={total}, 已完成={completed}, 未完成={incomplete}"
                )
                exam_data = tab_data.get("exam", {})
                if exam_data.get("total", 0) > 0:
                    self.log.info(
                        f"[考试统计] DOM tab: 总计={exam_data['total']}, 已完成={exam_data['done']}"
                    )
                return {
                    "total": total,
                    "completed": completed,
                    "incomplete": incomplete,
                }

            # 方案 2：从 Vue 数据读取（排除考试类 subject）
            overview = self._extract_project_overview()
            if overview and overview.get("subjects"):
                total = 0
                completed = 0
                for subj in overview["subjects"]:
                    name = subj.get("name", "")
                    # 排除考试相关 tab
                    if any(kw in name for kw in ["考试", "exam", "Exam"]):
                        continue
                    done = int(subj.get("done", 0))
                    subj_total = int(subj.get("total", 0))
                    total += subj_total
                    completed += min(done, subj_total)
                if total > 0:
                    incomplete = max(0, total - completed)
                    self.log.info(
                        f"[课程统计] Vue: 总计={total}, 已完成={completed}, 未完成={incomplete}"
                    )
                    return {
                        "total": total,
                        "completed": completed,
                        "incomplete": incomplete,
                    }

            # 方案 2：从折叠章节标题解析进度
            collapse_stats = self._summarize_collapse_progress()
            if collapse_stats is not None:
                stats = {
                    "total": collapse_stats["total"],
                    "completed": collapse_stats["completed"],
                    "incomplete": collapse_stats["incomplete"],
                }
                if collapse_stats.get("sections"):
                    incomplete_sections = [
                        s for s in collapse_stats["sections"] if s["incomplete"] > 0
                    ]
                    if incomplete_sections:
                        self.log.info(
                            f"[课程统计] 未完成章节: {len(incomplete_sections)} 个"
                        )
                        for sec in incomplete_sections[:5]:
                            self.log.info(
                                f"  - {sec['title']}: {sec['finished']}/{sec['total']} ({sec['incomplete']} 未完成)"
                            )

                self.log.info(
                    f"[课程统计] 总计: {stats['total']}, "
                    f"已完成: {stats['completed']}, "
                    f"未完成: {stats['incomplete']}"
                )
                return stats

            # 方案 3：仅检查当前可见 tab 的课程项
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
        except Exception:
            pass

        self._dismiss_broadcast()

        # 等待页面渲染完成（SPA 可能需要时间加载项目列表）
        try:
            self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=8000)
        except Exception:
            pass

        projs = self._page.locator(SEL_TASK_BLOCK)
        proj_count = projs.count()
        self.log.info(f"[导航] 发现 {proj_count} 个学习项目")

        if proj_count == 0:
            try:
                self._page.reload()
                projs = self._page.locator(SEL_TASK_BLOCK)
                proj_count = projs.count()
            except Exception:
                pass

        # 从 Vue 数据读取项目进度，用于跳过已完成项目
        project_progress = self._get_project_list_progress()
        progress_map = {p["name"]: p for p in project_progress}

        for i in range(proj_count):
            it = projs.nth(i)
            title = self._extract_item_title(it)
            if not title or title in completed:
                self.log.debug(f"[导航] 跳过项目: {title or '(无标题)'}")
                continue

            # 通过 Vue 数据检查项目进度，跳过已完成的项目
            proj_info = progress_map.get(title, {})
            progress = proj_info.get("progress", -1)
            if progress >= 100 and study_mode != "force":
                self.log.info(f"[导航] 项目「{title}」进度 {progress}%，跳过")
                completed.add(title)
                continue

            self.project_title = title
            self.log.info(f"======== 目标项目: {title} ========")
            if progress >= 0:
                self.log.info(f"  项目进度: {progress}%")
            state.current_project_title = title

            # 优先调用 LearningTaskList.vue 的 navToProject/checkSubscription。
            # 直接拼 /course?userProjectId=... 会缺 projectType/id，实际会停在项目列表页。
            navigated = False
            if proj_info:
                try:
                    nav_url = self._page.evaluate("""async (args) => {
                        %s
                        const [index, info] = args;
                        const app = findVueProxy(['taskList']) || findVueProxy(['navToProject']);
                        const task = app?.taskList?.[index] || null;
                        if (app && task && typeof app.navToProject === 'function') {
                            const ret = app.navToProject(task);
                            if (ret && typeof ret.then === 'function') await ret;
                            return true;
                        }
                        if (app && task && typeof app.checkSubscription === 'function') {
                            const ret = app.checkSubscription(task);
                            if (ret && typeof ret.then === 'function') await ret;
                            return true;
                        }
                        const router = app?.$router;
                        const category = Number(info.projectCategory);
                        const query = {};
                        if (category === 1) {
                            query.projectType = 'pre';
                            query.projectId = info.userProjectId;
                        } else if (category === 2) {
                            query.projectType = 'normal';
                            query.projectId = info.userProjectId;
                            query.id = info.projectId;
                        } else if (category === 3) {
                            query.projectType = 'special';
                            query.projectId = info.userProjectId;
                        } else if (category === 4) {
                            query.projectType = 'military';
                            query.projectId = info.userProjectId;
                        } else if (category === 9) {
                            query.projectType = 'lab';
                            query.projectId = info.userProjectId;
                            if (info.projectAttribute) query.labType = info.projectAttribute;
                        } else {
                            return false;
                        }
                        if (router) {
                            router.push({ name: 'courseIndex', query });
                            return true;
                        }
                        return false;
                    }""" % self._vue_app_finder_js(), [i, proj_info])
                    if nav_url:
                        navigated = True
                        self.log.debug("[导航] 调用项目入口逻辑 navToProject")
                except Exception:
                    pass

            if not navigated:
                it.scroll_into_view_if_needed(timeout=2000)
                it.click(timeout=5000)

            for attempt in range(20):
                if attempt % 4 == 0:
                    self._handle_intermediate_pages()
                new_ctx = self._detect_page_context()
                if new_ctx == PageContext.COURSE_LIST:
                    self.log.info(f"成功进入项目：{title}")
                    state.study_tabs = self._get_current_study_tabs()
                    self._print_project_overview()
                    return True
                try:
                    self.log.debug(
                        f"[导航] 等待进入项目 ({attempt + 1}/20) "
                        f"state={new_ctx.value} url={self._page.url[:120]}"
                    )
                except Exception:
                    self.log.debug(f"[导航] 等待进入项目 ({attempt + 1}/20)...")
                time.sleep(1)

            self.log.warning(f"进入项目「{title}」超时，跳过该项目")
            completed.add(title)
            continue

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
                self._dismiss_broadcast()
            except Exception:
                pass

            while self._goto_next_project(state, completed_projs, study_mode):
                proj_title = state.current_project_title
                failed, completed = set(), set()

                # 通过 API 精确检查课程完成情况，避免不必要的扫描
                progress_data = self._call_show_progress_api()
                if progress_data:
                    required_done = progress_data.get("requiredFinishedNum", 0)
                    required_total = progress_data.get("requiredNum", 0)
                    push_done = progress_data.get("pushFinishedNum", 0)
                    push_total = progress_data.get("pushNum", 0)
                    optional_done = progress_data.get("optionalFinishedNum", 0)
                    optional_total = progress_data.get("optionalNum", 0)

                    course_total = required_total + push_total + optional_total
                    course_done = required_done + push_done + optional_done

                    self.log.info(
                        f"[API] 课程进度: {course_done}/{course_total} "
                        f"(必修 {required_done}/{required_total}, "
                        f"匹配 {push_done}/{push_total}, "
                        f"选修 {optional_done}/{optional_total})"
                    )

                    exam_done = progress_data.get("examFinishedNum", 0)
                    exam_total = progress_data.get("examAssessmentNum", 0)
                    if exam_total > 0:
                        self.log.info(
                            f"[API] 考试进度: {exam_done}/{exam_total}"
                        )

                    if course_total > 0 and course_done >= course_total:
                        self.log.info(
                            f"项目「{proj_title}」课程已全部完成 "
                            f"({course_done}/{course_total})，跳过学习扫描"
                        )
                        completion_stats["total"] += course_total
                        completion_stats["completed"] += course_done
                        completed_projs.add(proj_title)
                        continue

                    completion_stats["total"] += course_total
                    completion_stats["completed"] += course_done
                    completion_stats["incomplete"] += max(0, course_total - course_done)

                study_tabs = state.study_tabs or self._get_current_study_tabs()
                if not study_tabs:
                    self.log.warning(
                        f"项目「{proj_title}」未找到学习 Tab，尝试默认处理"
                    )
                    study_tabs = [3, 2]

                # 等待课程列表页加载完成（SPA 渲染可能需要时间）
                try:
                    self._page.wait_for_selector(
                        SEL_COURSE_LIST_WAIT_TARGETS, state="attached", timeout=10000
                    )
                except Exception:
                    pass

                for tab_id in study_tabs:
                    self.log.debug(f"[Tab] 尝试切换到 Tab {tab_id}")
                    if not self._switch_to_study_tab(tab_id):
                        self.log.debug(f"[Tab] 切换失败，跳过 Tab {tab_id}")
                        continue

                    self._dismiss_broadcast()

                    while not self._page.is_closed():
                        tasks = self._collect_tasks_in_current_tab()
                        self.log.info(f"[Tab {tab_id}] 扫描到 {len(tasks)} 门课程")

                        if not tasks:
                            self.log.debug(
                                f"[Tab {tab_id}] 无课程任务，检查是否需要展开章节"
                            )
                            if self._expand_next_incomplete_section(state):
                                time.sleep(0.5)
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
                            time.sleep(0.5)
                            continue

                        # 所有任务都已完成（processed==0），标记当前章节为已确认完成
                        if tasks and all(t.get("passed") for t in tasks):
                            if state and state._last_expanded_section_key:
                                state._verified_complete_sections.add(
                                    state._last_expanded_section_key
                                )
                                self.log.debug(
                                    f"[章节] 确认章节全部完成: {state._last_expanded_section_key}"
                                )

                        if self._expand_next_incomplete_section(state):
                            time.sleep(0.5)
                            continue
                        break

                completed_projs.add(proj_title)
                self.log.info(f"项目「{proj_title}」处理完毕。")

                # 用 API 获取学习后的最新进度，更新统计
                final_progress = self._call_show_progress_api()
                if final_progress:
                    req_done = final_progress.get("requiredFinishedNum", 0)
                    req_total = final_progress.get("requiredNum", 0)
                    push_done = final_progress.get("pushFinishedNum", 0)
                    push_total = final_progress.get("pushNum", 0)
                    opt_done = final_progress.get("optionalFinishedNum", 0)
                    opt_total = final_progress.get("optionalNum", 0)
                    final_total = req_total + push_total + opt_total
                    final_done = req_done + push_done + opt_done
                    self.log.info(
                        f"[API] 学习后课程进度: {final_done}/{final_total}"
                    )
                    # 用最终数据覆盖（不累加，避免重复计数）
                    completion_stats["total"] = final_total
                    completion_stats["completed"] = final_done
                    completion_stats["incomplete"] = max(
                        0, final_total - final_done
                    )

        except Exception as e:
            self.log.error(f"严重异常: {e}")

        self.log.info("全部学习任务已处理。")
        self.log.info(
            f"[学习完成] 总课程: {completion_stats['total']}, "
            f"已完成: {completion_stats['completed']}, "
            f"未完成: {completion_stats['incomplete']}"
        )

        return completion_stats
