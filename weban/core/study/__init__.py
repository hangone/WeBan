import re
import time
from typing import TYPE_CHECKING, Any, List, Dict

from ..const import (
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
from ..captcha import (
    handle_click_captcha_in_frame as _handle_captcha_in_frame,
)
from ..base import BaseMixin, PageContext
from .state import PROJECT_STUDY_TABS as _PROJECT_STUDY_TABS
from .state import StudyRunState as _StudyRunState
from .runtime_analysis import (
    extract_nonstr_map_from_text,
    normalize_page_item_count,
    resolve_course_archetype,
)

if TYPE_CHECKING:
    from typing import Union as _Union
    from playwright.sync_api import Page, BrowserContext, Browser, Playwright
    from ..browser import BrowserConfig
    import logging as _logging


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
    # Vue collapse 展开辅助
    # ========================================================================

    def _vue_expand_collapse(self, index: int) -> bool:
        """通过 Vue collapse API 展开指定索引的章节，回退到 click。

        Returns True if expanded via API or click.
        """
        if not self._page or self._page.is_closed():
            return False
        try:
            expanded = self._page.evaluate("""async (index) => {
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
            }""" % self._vue_app_finder_js(), index)
            if expanded:
                return True
        except Exception:
            pass

        # Fallback: click
        try:
            items = self._page.locator(SEL_COLLAPSE_ITEM)
            if index < items.count():
                title_btn = items.nth(index).locator(SEL_COLLAPSE_ITEM_TITLE).first
                if title_btn.count() > 0:
                    title_btn.scroll_into_view_if_needed(timeout=3000)
                    title_btn.click(timeout=5000)
                    return True
        except Exception:
            pass
        return False

    def _wait_for_collapse_content(self, timeout: int = 8) -> None:
        """等待章节展开后课程列表渲染完成。"""
        try:
            self._page.wait_for_selector(
                SEL_COURSE_LIST_ITEMS, state="attached", timeout=timeout * 1000
            )
        except Exception:
            pass
        time.sleep(0.5)

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

                self._vue_expand_collapse(i)
                self.log.info(
                    f"[章节] 展开: {title_text} ({section['finished']}/{section['total']}, {incomplete_count} 未完成)"
                )

                expand_count_map[expand_key] = cur_count + 1
                if state:
                    state._expand_count_map = expand_count_map
                    expanded_sections.add(expand_key)
                    state.expanded_sections = expanded_sections
                    state._last_expanded_section_key = expand_key

                self._wait_for_collapse_content()
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

                self._vue_expand_collapse(i)
                self.log.info(f"[章节] 盲目展开 #{i}: {title_text}")

                expand_count_map[expand_key] = expand_count_map.get(expand_key, 0) + 1
                if state:
                    state._expand_count_map = expand_count_map

                self._wait_for_collapse_content()
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
            script_sources = frame.evaluate("""() => {
                const sources = [];
                const scripts = document.querySelectorAll('script');
                for (let i = 0; i < scripts.length; i++) {
                    const script = scripts[i];
                    const content = script.textContent;
                    if (!content) continue;
                    if (!/nonstrMap|callApinext|new\\s+Map/.test(content)) continue;
                    sources.push({
                        source: 'inline:' + i,
                        content,
                    });
                }

                const extScripts = document.querySelectorAll('script[src]');
                for (const script of extScripts) {
                    const src = script.getAttribute('src') || '';
                    if (/jquery|jweixin|crypto|sdk|captcha|tgJCap|apicenext|wx\\.js|video-js|fontRem/i.test(src)) continue;
                    if (!/\\.js(\\?|$)/i.test(src)) continue;
                    try {
                        const xhr = new XMLHttpRequest();
                        xhr.open('GET', src, false);
                        xhr.send();
                        if (xhr.status === 200) {
                            sources.push({
                                source: 'external:' + src.split('/').pop(),
                                content: xhr.responseText || '',
                            });
                        }
                    } catch(e) {}
                }

                return sources;
            }""")
            if not script_sources:
                return {}

            best_map: dict[int, str] = {}
            best_source = ""
            for item in script_sources:
                content = item.get("content") or ""
                source = item.get("source", "unknown")
                parsed = extract_nonstr_map_from_text(content)
                if len(parsed) > len(best_map):
                    best_map = parsed
                    best_source = source
            if best_map:
                self.log.debug(
                    f"[nonstrMap] 提取到 {len(best_map)} 个令牌 ({best_source}): {best_map}"
                )
                return best_map
        except Exception as e:
            self.log.debug(f"[nonstrMap] 提取异常: {e}")
        return {}

    def _analyze_course_structure(self, frame) -> dict:
        """提取课程页数、nonstrMap、JS 资源和 CSS 导航选择器。

        分析课程 HTML 中的三种关键文件：
        1. .html — 获取引入的 JS：sdk.js (finishWxCourse)、item.js、apicenext.js
        2. sdk.js — 调用 finishWxCourse；仅 URL 含 csCapt=true 时才弹出点选验证码
        3. apicenext.js — 按 item.js 定义调用 callApinext("next")

        Returns:
            dict: {
                'total_pages':      int   — .page-item 元素数量
                'nonstr_map':       dict  — apicenext 步骤追踪令牌
                'has_sdk_js':       bool  — sdk.js 是否存在
                'has_apicenext_js': bool  — apicenext.js 是否存在
                'has_item_js':      bool  — item.js 是否存在
                'nav_selectors':    list  — 页面导航按钮 CSS 选择器
                'quiz_selectors':   list  — 答题按钮 CSS 选择器
                'end_selectors':    list  — 末页/完成按钮 CSS 选择器
                'captcha_expected': bool  — URL 含 csCapt=true 且 weiban=weiban
            }
        """
        info = {
            'total_pages': 0,
            'nonstr_map': {},
            'has_sdk_js': False,
            'has_apicenext_js': False,
            'has_item_js': False,
            'nav_selectors': [],
            'quiz_selectors': [],
            'end_selectors': [],
            'captcha_expected': False,
        }
        try:
            # 提取 JS 资源信息和页面结构
            js_info = frame.evaluate("""() => {
                const scripts = document.querySelectorAll('script[src]');
                const result = {
                    hasSdkJs: false,
                    hasApicenextJs: false,
                    hasItemJs: false,
                    navSelectors: [],
                    quizSelectors: [],
                    endSelectors: [],
                    captchaExpected: false,
                };

                // 检测引入的 JS 文件
                for (const s of scripts) {
                    const src = (s.getAttribute('src') || '').toLowerCase();
                    if (/sdk\\.js/.test(src)) result.hasSdkJs = true;
                    if (/apicenext\\.js/.test(src)) result.hasApicenextJs = true;
                    if (/item\\.js/.test(src)) result.hasItemJs = true;
                }

                // 检测内联脚本中的函数定义（webpack 场景）
                const inlineScripts = document.querySelectorAll('script:not([src])');
                for (const s of inlineScripts) {
                    const text = s.textContent || '';
                    if (text.includes('finishWxCourse')) result.hasSdkJs = true;
                    if (text.includes('callApinext')) result.hasApicenextJs = true;
                }

                // 从 HTML 提取导航/答题/结束按钮选择器
                const allElements = document.querySelectorAll('[class*="btn-"]');
                const seenClasses = new Set();
                for (const el of allElements) {
                    const cls = (el.className || '').trim();
                    if (!cls || seenClasses.has(cls)) continue;
                    seenClasses.add(cls);

                    // 导航按钮: btn-next, btn-start, btn-prev 等
                    if (/\\bbtn-(next|start|prev|base|next-prev|next2)\\b/.test(cls)) {
                        const sel = '.' + cls.split(/\\s+/).filter(c => /\\bbtn-/.test(c)).join('.');
                        if (!result.navSelectors.includes(sel)) {
                            result.navSelectors.push(sel);
                        }
                    }
                    // 答题按钮: btn-at, btn-af, btn-ce
                    if (/\\bbtn-(at|af|ce)\\b/.test(cls)) {
                        const sel = '.' + cls.split(/\\s+/).filter(c => /\\bbtn-/.test(c)).join('.');
                        if (!result.quizSelectors.includes(sel)) {
                            result.quizSelectors.push(sel);
                        }
                    }
                    // 结束按钮: btn-next-end
                    if (/\\bbtn-next-end\\b/.test(cls)) {
                        const sel = '.' + cls.split(/\\s+/).filter(c => /\\bbtn-/.test(c)).join('.');
                        if (!result.endSelectors.includes(sel)) {
                            result.endSelectors.push(sel);
                        }
                    }
                }

                // 检测 captcha 触发条件：URL 需同时含 csCapt=true 和 weiban=weiban
                const href = window.location.href.toLowerCase();
                result.captchaExpected = /cscapt=true/.test(href) && /weiban=weiban/.test(href);

                // 精确计数各类导航按钮（用于确定 callApinext 中间调用次数）
                result.btnNextCount = document.querySelectorAll('.btn-next').length;
                result.btnStartCount = document.querySelectorAll('.btn-start').length;
                result.btnCeCount = document.querySelectorAll('.btn-ce').length;
                result.btnAtCount = document.querySelectorAll('.btn-at').length;
                result.btnNextEndCount = document.querySelectorAll('.btn-next-end').length;
                // slFn 提交按钮：class 含 "btn-aq-" 但排除 btn-at/btn-af
                // （因为 btn-aq-item 中含 "btn-aq-" 子串，btn-at/btn-af 也有 btn-aq-item class）
                result.btnAqCount = Array.from(
                    document.querySelectorAll('[class*="btn-aq-"]')
                ).filter(function(el) {
                    var cls = el.className || '';
                    return !/\bbtn-at\b/.test(cls) && !/\bbtn-af\b/.test(cls);
                }).length;

                return result;
            }""")

            if js_info:
                info['has_sdk_js'] = bool(js_info.get('hasSdkJs'))
                info['has_apicenext_js'] = bool(js_info.get('hasApicenextJs'))
                info['has_item_js'] = bool(js_info.get('hasItemJs'))
                info['nav_selectors'] = js_info.get('navSelectors', [])
                info['quiz_selectors'] = js_info.get('quizSelectors', [])
                info['end_selectors'] = js_info.get('endSelectors', [])
                info['captcha_expected'] = bool(js_info.get('captchaExpected'))
                # 从 HTML 按钮数量计算 callApinext 中间调用次数
                btn_next  = int(js_info.get('btnNextCount', 0))
                btn_start = int(js_info.get('btnStartCount', 0))
                btn_ce    = int(js_info.get('btnCeCount', 0))
                btn_at    = int(js_info.get('btnAtCount', 0))
                btn_next_end = int(js_info.get('btnNextEndCount', 0))
                btn_aq    = int(js_info.get('btnAqCount', 0))
                info['btn_counts'] = {
                    'next': btn_next, 'start': btn_start, 'ce': btn_ce,
                    'at': btn_at, 'next_end': btn_next_end, 'aq': btn_aq,
                }
                # 中间调用数 = btn-next（每个触发 finish=2）
                #             + btn-start（BtnFn 触发 finish=2）
                #             + btn-ce（BtnFn 触发 finish=2）
                #             + btn-aq-*（slFn 提交触发 finish=2）
                #             + (btn-at - 1)（最后一个 btn-at 是末页，不计入中间）
                intermediate = (btn_next + btn_start + btn_ce + btn_aq
                                + max(0, btn_at - 1))
                info['intermediate_btn_count'] = intermediate if intermediate > 0 else 0

            # 提取页面数和 nonstrMap
            page_classes = frame.evaluate(
                "() => Array.from(document.querySelectorAll('.page-item')).map(el => (el.className || '').trim())"
            )
            raw_total = len(page_classes or [])
            info['total_pages'] = normalize_page_item_count(page_classes or [])
            info['nonstr_map'] = self._extract_nonstr_map(frame)

            # 日志输出
            btn_c = info.get('btn_counts', {})
            self.log.info(
                f"[分析] 页数={info['total_pages']} (原始 {raw_total}) "
                f"中间步骤={info.get('intermediate_btn_count', 0)} "
                f"(btn-next={btn_c.get('next',0)} start={btn_c.get('start',0)} "
                f"ce={btn_c.get('ce',0)} aq={btn_c.get('aq',0)} at={btn_c.get('at',0)}) | "
                f"nonstrMap={len(info['nonstr_map'])}个 | "
                f"SDK={'✓' if info['has_sdk_js'] else '✗'} "
                f"apicenext={'✓' if info['has_apicenext_js'] else '✗'} "
                f"captcha={'✓' if info['captcha_expected'] else '✗'}"
            )
            if info['nav_selectors']:
                self.log.debug(f"[分析] 导航选择器: {info['nav_selectors']}")
            if info['quiz_selectors']:
                self.log.debug(f"[分析] 答题选择器: {info['quiz_selectors']}")
            if info['end_selectors']:
                self.log.debug(f"[分析] 结束选择器: {info['end_selectors']}")

        except Exception as e:
            self.log.debug(f"[分析] 课程结构分析异常: {e}")
        return info

    def _detect_course_archetype(self, frame) -> str:
        """检测课程 iframe 内的课程类型。

        Returns:
            'standard'   — 有 nonstrMap + callApinext (A01-A13, DA, A26 等)
            'animate'    — 使用 animate.public.js / sdk.js，无 callApinext (A14)
            'webpack'    — webpack 打包，PageController 导航 (A23, A32, A33)
            'simple'     — 只有 finishWxCourse，无额外追踪
        """
        try:
            result = frame.evaluate("""() => {
                const hasCallApinext = typeof callApinext === 'function';
                const hasNonstrMap = typeof nonstrMap !== 'undefined' && nonstrMap instanceof Map;
                const hasPageController = typeof PageController === 'function'
                    || document.querySelector('.page-content-common') !== null;
                const hasAnimatePublic = typeof animatePublic !== 'undefined'
                    || document.querySelector('script[src*="animate.public"]') !== null
                    || document.querySelector('.item-animation') !== null;
                const hasMapLiteralHint = Array.from(document.querySelectorAll('script'))
                    .some((script) => {
                        const content = script.textContent || '';
                        return content.includes('callApinext')
                            && /new\\s+Map\\s*\\(\\s*\\[\\s*\\[/.test(content);
                    });
                return {
                    callApinext: hasCallApinext,
                    nonstrMap: hasNonstrMap,
                    pageController: hasPageController,
                    animatePublic: hasAnimatePublic,
                    mapLiteralHint: hasMapLiteralHint,
                };
            }""")
            if result:
                return resolve_course_archetype(
                    has_call_apinext=bool(result.get("callApinext")),
                    has_global_nonstr_map=bool(result.get("nonstrMap")),
                    has_page_controller=bool(result.get("pageController")),
                    has_animate_public=bool(result.get("animatePublic")),
                    has_map_literal_hint=bool(result.get("mapLiteralHint")),
                )
        except Exception:
            pass
        return "simple"

    def _has_inline_quiz(self, frame) -> bool:
        """检测课程是否有内嵌答题页面 (page-aq)。"""
        try:
            return frame.evaluate(
                "() => document.querySelectorAll('[class*=\"page-aq\"]').length > 0"
            )
        except Exception:
            return False

    def _handle_inline_quiz(self, frame) -> bool:
        """处理课程内嵌答题页面 (page-aq)。

        item.js 通过 slFn() 定义答题逻辑，答案硬编码在 slFn 调用的参数中。
        页面上 label 内含 dtyn.png 图片的选项也是正确答案的视觉提示。

        处理流程：
        1. 从 script 源码解析 slFn 定义，提取每题答案
        2. 对每个活动答题页 (page-aq*)：选中正确选项 → 点击提交按钮
        3. 若到达正确结果页 (page-at*)：点击 btn-at 继续
        4. 若到达错误结果页 (page-af*)：点击 btn-af 返回重答
        """
        import json as _json

        try:
            # 1. 从 script 标签中解析 slFn 答案定义
            #    slFn 格式：slFn(btnSel, "input[name='aiXX']:checked", atSel, afSel, [answers...])
            #    或带额外参数：slFn(btnSel, ..., [answers], nextBtnSel, pageSel)
            quiz_meta = frame.evaluate("""() => {
                const answers = {};
                for (const script of document.querySelectorAll('script')) {
                    const text = script.textContent || '';
                    if (!text.includes('slFn')) continue;
                    // 匹配 slFn 调用，提取按钮选择器、input name、答案数组
                    const re = /slFn\\s*\\(\\s*["']([^"']+)["']\\s*,\\s*["']input\\[name=['"](ai\\w+)['"]\\]([^"']*)['"\\]\\s*,\\s*["']([^"']+)["']\\s*,\\s*["']([^"']+)["']\\s*,\\s*(\\[[^\\]]+\\])/g;
                    let m;
                    while ((m = re.exec(text)) !== null) {
                        const btnSel   = m[1];
                        const name     = m[2];
                        const atSel    = m[4];
                        const afSel    = m[5];
                        const arrText  = m[6];
                        try {
                            const vals = JSON.parse(arrText.replace(/'/g, '"'));
                            if (!answers[name]) {
                                answers[name] = { btnSel, atSel, afSel, values: vals };
                            }
                        } catch(e) {}
                    }
                }
                const aqCount = document.querySelectorAll('[class*="page-aq"]').length;
                return { answers, aqCount };
            }""")

            if not quiz_meta or not quiz_meta.get('aqCount', 0):
                return False

            answers = quiz_meta.get('answers', {})
            self.log.debug(f"[答题] 解析到 slFn 答案: {answers}")

            # 2. 逐轮处理答题/结果页，最多循环 20 次避免死循环
            for attempt in range(20):
                state = frame.evaluate(f"""() => {{
                    const answersMap = {_json.dumps(answers)};

                    // 确定当前活动页面类型
                    const active = document.querySelector('.page-active, .page-item[style*="display: block"]');
                    if (!active) return {{ type: 'none' }};
                    const cls = active.className || '';

                    // 末页/倒计时页 → 课程结束
                    if (/page-end|page-reciprocal/.test(cls)) return {{ type: 'end' }};

                    // page-at* (正确结果页) → 点击 btn-at 继续
                    if (/page-at/.test(cls)) {{
                        const btn = active.querySelector('.btn-at, .btn-next, a[class*="btn"]');
                        if (btn) btn.click();
                        return {{ type: 'at', clicked: !!btn }};
                    }}

                    // page-af* (错误结果页) → 点击 btn-af 返回重答
                    if (/page-af/.test(cls)) {{
                        const btn = active.querySelector('.btn-af, .btn-prev, a[class*="btn"]');
                        if (btn) btn.click();
                        return {{ type: 'af', clicked: !!btn }};
                    }}

                    // page-aq* (答题页) → 选答案并提交
                    if (!/page-aq/.test(cls)) return {{ type: 'other', cls }};

                    const inputs = active.querySelectorAll('input[name]');
                    if (!inputs.length) return {{ type: 'aq-no-input', cls }};

                    const inputName = inputs[0].getAttribute('name');
                    const inputType = inputs[0].type;

                    // 确定正确答案值
                    let correctVals = [];
                    if (answersMap[inputName]) {{
                        correctVals = answersMap[inputName].values;
                    }} else {{
                        // 降级：dtyn.png 视觉提示（label 内有 img[src*="dtyn"] 的选项）
                        for (const lbl of active.querySelectorAll('label')) {{
                            if (lbl.querySelector('img[src*="dtyn"]')) {{
                                const inp = lbl.querySelector('input');
                                if (inp) correctVals.push(inp.value);
                            }}
                        }}
                    }}
                    // 再降级：选全部（select-all 题型）
                    if (!correctVals.length) {{
                        inputs.forEach(inp => correctVals.push(inp.value));
                    }}

                    // 先取消所有已选
                    inputs.forEach(inp => {{ inp.checked = false; }});
                    // 选中正确选项
                    let selectedCount = 0;
                    inputs.forEach(inp => {{
                        if (correctVals.includes(inp.value)) {{
                            inp.checked = true;
                            selectedCount++;
                        }}
                    }});

                    // 点击提交按钮
                    // 策略 1：用 slFn 定义的 btnSel
                    let submitted = false;
                    const meta = answersMap[inputName];
                    if (meta && meta.btnSel) {{
                        // btnSel 可能是 ".page-aq01 label"（点任意 label）或 ".btn-aq-01"
                        const candidates = document.querySelectorAll(meta.btnSel);
                        if (candidates.length) {{
                            candidates[candidates.length - 1].click();  // 点最后一个
                            submitted = true;
                        }}
                    }}
                    // 策略 2：找页面内 class 含 btn-aq 的按钮
                    if (!submitted) {{
                        const btns = active.querySelectorAll('[class*="btn-aq"]:not([class*="btn-aq-item"])');
                        if (btns.length) {{ btns[0].click(); submitted = true; }}
                    }}
                    // 策略 3：点页面内任意可见 a/button
                    if (!submitted) {{
                        for (const el of active.querySelectorAll('a, button')) {{
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{ el.click(); submitted = true; break; }}
                        }}
                    }}

                    return {{
                        type: 'aq',
                        inputName,
                        correctVals,
                        selectedCount,
                        submitted,
                        cls,
                    }};
                }}""")

                if not state:
                    break

                stype = state.get('type', 'none')

                if stype in ('none', 'end', 'other'):
                    break

                self.log.debug(f"[答题] 第 {attempt+1} 轮: type={stype} detail={state}")
                time.sleep(0.5)

                if stype == 'aq' and not state.get('submitted'):
                    # 提交失败，停止
                    self.log.debug("[答题] 无法提交，停止")
                    break

            return True

        except Exception as e:
            self.log.debug(f"[答题] 内嵌答题处理异常: {e}")
            return False

    def _call_apicenext(
        self, frame, nextprev: str, finish: int, nonstr_map: dict | None
    ) -> bool:
        """调用 callApinext 追踪课程步骤进度。

        callApinext 内部有 nextnummax 守卫，重复调用同一步骤会被忽略，
        因此在按钮点击（课程 JS 自带 callApinext）之后再调一次是安全的。
        """
        try:
            safe_map = nonstr_map or {}
            map_entries = ", ".join(f'[{k}, "{v}"]' for k, v in safe_map.items())
            map_expr = f"new Map([{map_entries}])" if map_entries else "new Map()"
            result = frame.evaluate(f"""() => {{
                if (typeof callApinext !== 'function') return false;
                const nonstrMap = {map_expr};
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



    def _suppress_course_alert(self, frame) -> None:
        """抑制课程弹窗，包括 alert/confirm 和各种反作弊提示。"""
        try:
            frame.evaluate("""() => {
                const _origAlert = window.alert;
                const _origConfirm = window.confirm;
                // 拦截所有 alert：记录日志后静默吞掉
                window.alert = (msg) => {
                    console.log("[WeBan] alert suppressed:", String(msg || "").substring(0, 120));
                };
                // 拦截 confirm 弹窗：始终返回 true（确认）
                window.confirm = (msg) => {
                    console.log("[WeBan] confirm suppressed:", String(msg || "").substring(0, 120));
                    return true;
                };
                // 60 秒后恢复原始函数
                setTimeout(() => {
                    window.alert = _origAlert;
                    window.confirm = _origConfirm;
                }, 60000);
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
                navigated = self._page.evaluate(self._vue_js("""() => {
                    %s
                    const app = findVueProxy(['$router']) || findVueProxy(null);
                    if (app && app.$router) {
                        app.$router.back();
                        return true;
                    }
                    return false;
                }"""))
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

    def _handle_captcha_if_needed(self, frame) -> bool:
        """检测并处理验证码。

        sdk.js 中的 finishWxCourse() 仅在以下两个条件同时满足时触发 TencentCaptcha：
        1. URL 参数 csCapt=true
        2. URL 参数 weiban=weiban
        其他情况无点选验证码，不要误判。

        Returns:
            True: 验证码处理成功或不需要验证码
            False: 验证码处理失败
        """
        try:
            # 仅当 URL 同时含 csCapt=true 和 weiban=weiban 时才需要处理验证码
            # sdk.js: if (weiban === WEIBAN && csCapt === 'true') { ... captcha ... }
            frame_url = (frame.url or "").lower()
            has_cscapt_true = "cscapt=true" in frame_url
            has_weiban_weiban = "weiban=weiban" in frame_url

            if not has_cscapt_true or not has_weiban_weiban:
                # 无验证码触发条件，无需处理
                if has_cscapt_true and not has_weiban_weiban:
                    self.log.debug("[验证码] csCapt=true 但 weiban≠weiban，不会弹出验证码")
                return True

            # 检查是否有可见的验证码元素
            # 腾讯验证码容器 #tcaptcha_transform_dy 在所有页面预加载，
            # 但通过 opacity:0 + top:-1e+06px 隐藏。仅当 url 含 csCapt=true
            # 且 finishWxCourse() 已被调用时才真正可见
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
            else:
                self.log.debug("[验证码] URL 含 csCapt=true 但未检测到可见验证码元素")
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

    def _trigger_img_text_completion(self, frame, title: str) -> bool:
        """完成课程播放。统一使用 nextapi/callApinext 推进到末页。

        所有类型都会处理内嵌答题页面 (page-aq)。
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

            # 2. 检测课程类型
            archetype = self._detect_course_archetype(frame)
            has_quiz = self._has_inline_quiz(frame)
            self.log.info(f"[播放] 课程类型: {archetype}, 内嵌答题: {has_quiz}")

            # 3. 分析课程结构（页数、JS 资源、nonstrMap、CSS 选择器、captcha 条件）
            course_info = self._analyze_course_structure(frame)
            total_pages = course_info.get("total_pages", 0)
            has_apicenext = course_info.get("has_apicenext_js", False)
            captcha_expected = course_info.get("captcha_expected", False)

            if total_pages <= 0:
                total_pages = 4
                self.log.info(f"[播放] 未检测到页面数，使用默认 {total_pages} 页")
            else:
                self.log.info(f"[播放] 检测到 {total_pages} 页")

            # 4. 根据 JS 资源决定推进策略
            if not has_apicenext:
                self.log.info(
                    "[播放] 无 apicenext.js，仅使用 finishWxCourse 完成课程"
                    + (" (预期弹出点选验证码)" if captcha_expected else "")
                )

            # 5. 设置 API 响应拦截
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
                # 统一采用 nextapi/callApinext 推进流程
                self._complete_standard_course(
                    frame, course_info,
                    api_results, start_time, min_study_time
                )

                # 处理内嵌答题页面
                if has_quiz:
                    self._handle_inline_quiz(frame)

                # 等待至少 study_time 秒
                elapsed = time.time() - start_time
                remaining = min_study_time - elapsed
                if remaining > 0:
                    self.log.info(f"[等待] 等待剩余学时 {int(remaining)} 秒")
                    time.sleep(remaining)

                # 到达末页后，触发页面原生完课逻辑
                return self._finish_course_at_end(
                    frame, api_results, course_info
                )

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

    def _complete_standard_course(
        self, frame, course_info: dict,
        api_results: dict, start_time: float, min_study_time: int
    ) -> None:
        """统一课程推进流程：逐页调用 nextapi/callApinext 追踪步骤。

        使用从 HTML 动态提取的导航选择器。
        """
        nonstr_map = course_info.get("nonstr_map", {})
        total_pages = course_info.get("total_pages", 0)
        # 优先使用从 HTML 按钮计数推算的中间步骤数（精确匹配 item.js 定义）
        intermediate_btn_count = course_info.get("intermediate_btn_count", 0)
        loop_count = intermediate_btn_count if intermediate_btn_count > 0 else total_pages
        has_apicenext = course_info.get("has_apicenext_js", False)
        nav_selectors = course_info.get("nav_selectors", [])
        if not nav_selectors:
            nav_selectors = [".btn-next", ".btn-start", ".btn-ce", ".btn-prev"]
        nav_sel_list = ", ".join(nav_selectors)

        self.log.info(
            f"[播放] nextapi 推进模式，中间步骤={loop_count} "
            f"(HTML 按钮={intermediate_btn_count}, 总页数={total_pages})"
        )
        self.log.debug(f"[播放] 导航选择器: {nav_sel_list}")
        # 对于 monitor.js 守卫：预先对所有 .btn-next 分发点击事件解除守卫
        # monitor.js 只监听 .btn-next 的 click（capture=true）来递增 sum
        # 注意：这也会触发 item.js 的导航处理，但 apicenext 的 nextnummax 守卫
        # 会防止重复步骤被发送到服务器
        if self._check_monitor_guard(frame):
            self.log.debug("[播放] 检测到 monitor.js 守卫，预分发 .btn-next 点击解除守卫")
            try:
                frame.evaluate("""() => {
                    const btns = document.querySelectorAll('.btn-next');
                    btns.forEach(btn => {
                        btn.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                    });
                }""")
                time.sleep(0.2)
            except Exception:
                pass

        for i in range(loop_count):
            if self._check_course_completed(frame):
                self.log.info("[播放] 课程已自动完成")
                return
            # 检查 monitor guard：若仍活跃则尝试点击一个可见按钮
            if self._check_monitor_guard(frame):
                self.log.debug("[播放] monitor guard 仍活跃，尝试单次点击")
                try:
                    frame.evaluate(f"""() => {{
                        const btns = document.querySelectorAll("{nav_sel_list}");
                        for (const btn of btns) {{
                            const r = btn.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {{ btn.click(); break; }}
                        }}
                        if (typeof sumClick !== "undefined") sumClick++;
                    }}""")
                except Exception:
                    pass

            expected_steps = api_results["steps"] + 1
            api_called = False
            if has_apicenext:
                api_called = self._call_apicenext(frame, "next", 2, nonstr_map)
            # 同步递增课程内部计数器
            try:
                frame.evaluate("""() => {
                    try { if (typeof pageNums !== 'undefined') pageNums++; } catch(e) {}
                    try { if (typeof sumClick !== 'undefined') sumClick++; } catch(e) {}
                    try { if (typeof atsum !== 'undefined') atsum++; } catch(e) {}
                }""")
            except Exception:
                pass
            # 等待服务端响应（仅在 nextapi 实际可调用时）
            if api_called:
                for _ in range(30):
                    if api_results["steps"] >= expected_steps:
                        break
                    time.sleep(0.1)
            elapsed = int(time.time() - start_time)
            self.log.info(
                f"[推进] nextapi {i + 1}/{loop_count} "
                f"({elapsed}s/{min_study_time}s)"
            )

    def _finish_course_at_end(
        self, frame, api_results: dict, course_info: dict
    ) -> bool:
        """End-of-course completion flow using nextapi + finishWxCourse.

        根据 item.js 定义的模式：
        - 末页 .btn-at 点击 → finishWxCourse() + callApinext("next", 1, nonstrMap)
        - sdk.js 仅需 finishWxCourse；csCapt=true 时弹出点选验证码
        - apicenext.js 存在时需额外调用 callApinext

        1. Check if already completed (popup / comment page / API response)
        2. Ensure pageNums >= total_pages (anti-cheat guard)
        3. Try to trigger native end-page buttons using dynamic selectors
        4. Call final nextapi with finish=1 (if apicenext present)
        5. Call finishWxCourse() via frame.evaluate
        6. Handle captcha if csCapt=true (only when both conditions met)
        7. Wait for completion signals: .pop-jsv popup, frame detach, or API response
        """
        nonstr_map = course_info.get("nonstr_map", {})
        total_pages = course_info.get("total_pages", 0)
        has_apicenext = course_info.get("has_apicenext_js", False)
        end_selectors = course_info.get("end_selectors", [])

        # 构建末页按钮选择器（动态 + 回退）
        all_end_selectors = list(end_selectors)
        for sel in [".btn-next-end", ".btn-at", ".btn-ce"]:
            if sel not in all_end_selectors:
                all_end_selectors.append(sel)
        end_sel_list = ", ".join(all_end_selectors)
        # ── Step 1: Already completed? ──
        if self._check_course_completed(frame):
            return True

        if api_results.get("finished"):
            self.log.info("[完成] API 响应已确认课程完成")
            self._return_from_comment_page()
            return True

        # ── Step 2: Anti-cheat guard — ensure pageNums >= total_pages ──
        try:
            frame.evaluate(f"""() => {{
                try {{
                    if (typeof pageNums !== 'undefined' && pageNums < {total_pages}) {{
                        pageNums = {total_pages};
                    }}
                }} catch(e) {{}}
            }}""")
        except Exception:
            pass

        # ── Step 3: 尝试触发原生末页按钮 ──
        # item.js 末页按钮的处理逻辑：
        #   btn-next-end: callApinext("next", 1) 立即执行 + setTimeout(finishWxCourse, 2000)
        #   最后 btn-at:  callApinext("next", 1, nonstrMap) 立即执行 + setTimeout(finishWxCourse, 2000)
        # 因此：如果原生点击成功，callApinext 和 finishWxCourse 均由 JS 自行处理
        btn_clicked = False
        try:
            clicked = frame.evaluate(f"""() => {{
                const endBtns = document.querySelectorAll("{end_sel_list}");
                for (const btn of endBtns) {{
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {{
                        btn.click();
                        return {{ clicked: true, selector: btn.className }};
                    }}
                }}
                return {{ clicked: false }};
            }}""")
            if clicked and clicked.get("clicked"):
                btn_clicked = True
                self.log.info(f"[完成] 触发末页按钮: {clicked.get('selector')}")
        except Exception as e:
            self.log.debug(f"[完成] 末页按钮触发异常: {e}")

        if btn_clicked:
            # 原生按钮已点击：
            # JS 会立即调用 callApinext("next", 1) 并在 2s 后调用 finishWxCourse()
            # 我们只需等待 JS 执行完成，不再重复调用
            self.log.info("[完成] 等待原生 JS 执行 finishWxCourse（~2s 延迟）...")
            time.sleep(2.5)
            # 处理可能弹出的验证码（finishWxCourse 触发）
            self._handle_captcha_if_needed(frame)
            # 等待完成信号
            for poll_round in range(20):
                if self._check_course_completed(frame):
                    self.log.info("[完成] 原生按钮触发课程完成")
                    return True
                try:
                    pop_visible = frame.evaluate("""() => {
                        const pop = document.querySelector('.pop-jsv');
                        if (pop) {
                            const r = pop.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return true;
                        }
                        return false;
                    }""")
                    if pop_visible:
                        self.log.info("[完成] 检测到完成弹窗 pop-jsv（原生触发）")
                        return True
                except Exception:
                    pass
                time.sleep(0.5)
            # 原生按钮触发后未检测到完成弹窗，降级走后续流程
            self.log.debug("[完成] 原生触发后未检测到弹窗，降级手动调用")

        # ── Step 4: 手动调用 callApinext("next", 1) ──
        # 仅在原生按钮未成功点击时才需要（避免重复步骤）
        # item.js 模式：callApinext("next", 1, nonstrMap) 立即执行（finish=1 信号）
        if not btn_clicked and has_apicenext and total_pages > 0:
            try:
                self._call_apicenext(frame, "next", 1, nonstr_map)
                self.log.debug("[完成] 已发送最终 nextapi 信号 (finish=1)")
                time.sleep(0.5)
            except Exception as e:
                self.log.debug(f"[完成] 最终 nextapi 异常: {e}")

        # ── Step 5: 调用 finishWxCourse() + 处理验证码 ──
        # sdk.js 的 finishWxCourse()：
        #   仅 weiban=weiban 且 csCapt=true 时触发 TencentCaptcha 点选验证码
        #   其他情况直接发送完成请求，无验证码
        self.log.info("[完成] 调用 finishWxCourse()...")
        finish_ok = False
        finish_reason = ""
        max_finish_retries = 3
        for finish_attempt in range(max_finish_retries):
            if finish_attempt > 0:
                retry_delay = 2 + finish_attempt * 2
                self.log.info(f"[完成] finishWxCourse 重试 {finish_attempt}/{max_finish_retries - 1}，等待 {retry_delay}s...")
                time.sleep(retry_delay)

            # 检查 monitor guard 是否仍在阻止完成
            if self._check_monitor_guard(frame):
                self.log.warning("[完成] monitor guard 阻止 finishWxCourse，尝试额外点击")
                for click_i in range(5):
                    try:
                        frame.evaluate("""() => {
                            const btns = document.querySelectorAll(".btn-next, .btn-start, .btn-ce, .btn-next-end");
                            for (const btn of btns) {
                                const r = btn.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) { btn.click(); break; }
                            }
                        }""")
                    except Exception:
                        break
                    time.sleep(0.3)
                    if not self._check_monitor_guard(frame):
                        self.log.info("[完成] monitor guard 已解除")
                        break
                if self._check_monitor_guard(frame):
                    self.log.warning("[完成] monitor guard 未能解除，finishWxCourse 可能为 null")

            # 5a. 调用 finishWxCourse()（可能触发 captcha.show()）
            try:
                has_func = frame.evaluate("typeof finishWxCourse === 'function'")
                if not has_func:
                    self.log.warning("[完成] finishWxCourse 不是函数")
                    break
                frame.evaluate("finishWxCourse()")
                self.log.debug("[完成] finishWxCourse 已调用")
            except Exception as e:
                if self._is_frame_detached_error(e):
                    self.log.info("[完成] frame 已分离 → 课程 JS 已完成流程")
                    self._return_from_comment_page()
                    return True
                finish_reason = str(e)
                self.log.debug(f"[完成] finishWxCourse 调用异常: {e}")
                continue

            # 5b. 处理 finishWxCourse 触发的验证码（csCapt=true 时）
            time.sleep(1)
            captcha_handled = self._handle_captcha_if_needed(frame)
            if captcha_handled:
                self.log.info("[完成] 验证码已处理，等待 JS 回调完成...")
            else:
                self.log.debug("[完成] 无需验证码，继续等待完成信号")

            # 5c. 等待完成弹窗
            for poll_round in range(16):
                if self._check_course_completed(frame):
                    self.log.info("[完成] finishWxCourse 成功")
                    finish_ok = True
                    break
                try:
                    pop_visible = frame.evaluate("""() => {
                        const pop = document.querySelector('.pop-jsv');
                        if (pop) { const r = pop.getBoundingClientRect(); if (r.width > 0 && r.height > 0) return true; }
                        return false;
                    }""")
                    if pop_visible:
                        self.log.info("[完成] 检测到完成弹窗 pop-jsv")
                        finish_ok = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            if finish_ok:
                break

        if finish_ok:
            return True

        if finish_reason:
            self.log.warning(f"[完成] finishWxCourse 失败 (原因: {finish_reason})，共尝试 {max_finish_retries} 次")

        # ── Step 6: Final check — frame detach / completion state ──
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

    def _vue_router_back(self, label: str = "") -> bool:
        """尝试 Vue Router back()，失败则回退到 history.back()。"""
        if not self._page:
            return False
        try:
            navigated = self._page.evaluate(self._vue_js("""() => {
                %s
                const app = findVueProxy(['$router']) || findVueProxy(null);
                if (app && app.$router) { app.$router.back(); return true; }
                return false;
            }"""))
            if navigated:
                self.log.debug(f"[导航] Vue Router back() {label}".strip())
                time.sleep(1)
                return True
        except Exception:
            pass
        try:
            self._page.evaluate("() => { history.back(); }")
            self.log.debug(f"[导航] history.back() {label}".strip())
            time.sleep(1)
            return True
        except Exception:
            return False

    def _return_to_chapter_list(self) -> bool:
        """返回章节列表。优先使用 Vue Router / history.back()，回退到 click。"""
        if not self._page or self._page.is_closed():
            return False

        for attempt in range(3):
            ctx = self._detect_page_context()
            if ctx == PageContext.COURSE_LIST:
                return True

            if ctx == PageContext.PROJECT_LIST:
                self.log.debug("[导航] 当前在项目列表页，尝试重新进入项目...")
                try:
                    task_block = self._page.locator(SEL_TASK_BLOCK).first
                    if task_block.count() > 0 and task_block.is_visible():
                        task_block.click(timeout=5000)
                        time.sleep(2)
                        try:
                            self._page.wait_for_selector(
                                SEL_COURSE_LIST_WAIT_TARGETS,
                                state="attached",
                                timeout=8000,
                            )
                        except Exception:
                            pass
                        if self._detect_page_context() == PageContext.COURSE_LIST:
                            self.log.info("[导航] 成功从项目列表进入课程列表")
                            return True
                except Exception as e:
                    self.log.debug(f"[导航] 项目列表恢复异常: {e}")
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
                if self._vue_router_back():
                    continue

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
                        self._vue_expand_collapse(i)
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
                    if not self._return_to_chapter_list():
                        self.log.debug("[重试] 返回课程列表失败，尝试刷新恢复...")
                        try:
                            self._page.reload(
                                wait_until="domcontentloaded", timeout=15000
                            )
                            time.sleep(1)
                        except Exception:
                            pass

                if not flow_ok:
                    fail_reason = "课程播放/交互流程未完成"

                # 尝试返回课程列表；失败时尝试恢复而非直接中止
                returned = self._return_to_chapter_list()
                if not returned:
                    self.log.warning(
                        f"[{idx + 1}/{total_tasks}] 课后返回课程列表失败，尝试恢复..."
                    )
                    # 恢复策略：先尝试刷新页面
                    recovered = False
                    for recovery_attempt in range(3):
                        try:
                            self._page.reload(
                                wait_until="domcontentloaded", timeout=15000
                            )
                            time.sleep(1)
                            ctx = self._detect_page_context()
                            if ctx == PageContext.COURSE_LIST:
                                self.log.info("[恢复] 刷新后回到课程列表")
                                recovered = True
                                break
                            if ctx == PageContext.PROJECT_LIST:
                                self.log.info("[恢复] 刷新后落在项目列表，尝试重新进入")
                                # 尝试通过 Vue router 返回
                                try:
                                    self._vue_router_back()
                                    time.sleep(1)
                                except Exception:
                                    pass
                                ctx2 = self._detect_page_context()
                                if ctx2 == PageContext.COURSE_LIST:
                                    recovered = True
                                    break
                            # 尝试点击导航返回按钮
                            try:
                                nav_back = self._page.locator(SEL_NAV_BAR_LEFT).first
                                if nav_back.count() > 0 and nav_back.is_visible():
                                    nav_back.click(timeout=5000)
                                    time.sleep(1)
                                    if self._detect_page_context() == PageContext.COURSE_LIST:
                                        recovered = True
                                        break
                            except Exception:
                                pass
                            time.sleep(1)
                        except Exception as e:
                            self.log.debug(f"[恢复] 尝试 {recovery_attempt + 1} 异常: {e}")

                    if recovered:
                        self.log.info("[恢复] 已恢复到课程列表，跳过当前课程继续")
                        failed.add(title)
                        continue  # 继续处理下一个课程
                    else:
                        self.log.warning(
                            f"[{idx + 1}/{total_tasks}] 恢复失败，中止当前批次"
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
