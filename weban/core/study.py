"""
study.py —— 自动学习流程模块

核心类：StudyMixin
  提供 run_study() 方法，驱动 Playwright 自动完成视频/图文课程的学习。

  通过 _StudyRunState 数据类传递可变状态：
    - _dismiss_broadcast()       关闭广播公告弹窗
    - _return_to_chapter_list()  从详情页返回章节列表
    - _find_course_target()      在 locator 中找下一个未学课程
    - _goto_next_project()       导航到下一个学习项目
    - _try_next_study_tab()      切换到当前项目的下一个课程 Tab
"""

import re
import time
import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List

import logging

from .captcha import has_captcha as _has_captcha, handle_click_captcha as _handle_click_captcha


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# projectCategory → 类型名称映射（源自 LearningTaskList.vue / constants.js）
# ---------------------------------------------------------------------------
_PROJECT_CATEGORY_NAMES = {
    1: "新生安全教育",
    2: "安全课程",
    3: "专题学习",
    4: "军事理论",
    9: "实验室安全",
}

# 课程 Tab 顺序：subjectType 3=必修 2=选修 1=匹配
# CourseIndex.vue initProject 中按 projectType 组装 subjectList
_PROJECT_STUDY_TABS = {
    "pre":      [3, 2],      # 新生安全：必修 + 选修
    "normal":   [3, 1, 2],   # 安全课程：必修 + 匹配 + 选修
    "special":  [3, 2],      # 专题学习：必修 + 选修
    "military": [3],         # 军事理论：必修
    "lab":      [3],         # 实验室安全：必修
    "foods":    [3],         # 食品安全：必修
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
    """
    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""


# ---------------------------------------------------------------------------
# StudyMixin
# ---------------------------------------------------------------------------

class StudyMixin:
    """自动学习流程 Mixin，通过多重继承供 WeBanClient 使用。"""

    if TYPE_CHECKING:
        _page: Any
        _context: Any
        _browser: Any
        _playwright: Any
        log: Any
        base_url: Any
        token: Any
        user_id: Any
        tenant_name: Any
        account: Any
        password: Any
        continue_on_invalid_token: Any
        browser_config: Any
        answers: Any

    # ------------------------------------------------------------------
    # 页面结构辅助方法
    # ------------------------------------------------------------------

    def _detect_project_type(self) -> str:
        """从当前页面 URL query 推断 projectType（pre / normal / special 等）。"""
        try:
            url = self._page.url
            m = re.search(r"projectType=([^&/#]+)", url)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    def _handle_protocol_page(self) -> bool:
        """处理承诺书/协议签署页（ProtocolPageWk.vue / ProtocolPage.vue）。

        自动勾选同意框并点击下一步；若有签名提交按钮也一并点击。
        返回是否处理了该页面（True = 检测到并已处理）。
        """
        try:
            agree_cb = self._page.locator("#agree, input[type='checkbox']")
            next_btn = self._page.locator(
                "button:has-text('下一步'), a:has-text('下一步'), "
                "button:has-text('同意'), a:has-text('同意')"
            )
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
            submit_btn = self._page.locator(
                "button:has-text('提交'), button:has-text('确认提交')"
            )
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
        try:
            blocks = self._page.locator(".task-block, .img-text-block")
            if blocks.count() == 0:
                return False
            self.log.info(f"[专题/实验室] 检测到中间列表页，共 {blocks.count()} 个子项目")
            # 优先点击未完成的子项目
            for i in range(blocks.count()):
                blk = blocks.nth(i)
                if blk.locator(".task-block-done").count() > 0:
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
        for _round in range(5):
            time.sleep(1)

            # 已在课程列表页，直接返回
            if self._page.locator(
                ".van-collapse-item, .img-texts-item, .fchl-item"
            ).count() > 0:
                return

            if self._handle_protocol_page():
                continue

            if self._handle_special_index():
                continue

            # LabIndex（.img-text-block 独立存在）
            lab_blocks = self._page.locator(".img-text-block")
            if lab_blocks.count() > 0:
                self.log.info("[实验室] 检测到 LabIndex 页，点击第一个实验项目")
                lab_blocks.first.click(force=True)
                time.sleep(3)
                continue

            # 等待任意一种课程结构出现，超时则退出
            try:
                self._page.wait_for_selector(
                    ".van-collapse-item, .img-texts-item, .fchl-item, "
                    ".task-block, .img-text-block, #agree",
                    timeout=5000,
                )
            except Exception:
                break

    def _get_current_study_tabs(self) -> List[int]:
        """根据当前页面 URL 的 projectType 返回需要遍历的 subjectType 列表。"""
        pt = self._detect_project_type()
        return list(_PROJECT_STUDY_TABS.get(pt, [3, 2]))

    def _switch_to_study_tab(self, subject_type: int) -> bool:
        """在 CourseIndex 页切换到指定 subjectType 对应的 Tab。

        subjectType 对应关系：3=必修课程  2=选修课程  1=匹配课程
        返回是否切换成功（Tab 不存在时返回 False）。
        """
        _tab_labels = {3: "必修课程", 2: "选修课程", 1: "匹配课程"}
        label = _tab_labels.get(subject_type, "")
        if not label:
            return False
        try:
            tab = self._page.locator(f'.van-tab:has-text("{label}")')
            if tab.count() == 0:
                return False
            # 已经是激活状态，不需要再点击
            if "van-tab--active" in (tab.first.get_attribute("class") or ""):
                return True
            tab.first.click(force=True)
            time.sleep(1.5)
            self.log.info(f"[Tab] 切换到「{label}」")
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
            title_el = item.locator(".title, .fchl-item-content-title")
            if title_el.count() > 0:
                return title_el.first.inner_text().strip()
        except Exception:
            pass
        try:
            return item.inner_text().strip().split("\n")[0].strip()
        except Exception:
            return ""

    def _find_fchl_target(self, study_mode: str, failed_courses: set, completed_courses: set):
        """在 fchl 页面（.fchl-item 结构）中查找下一个未完成的课程项。

        force 模式：从全部项目中查找（含已通过）。
        其他模式：仅查找未通过（:not(.fchl-item-active)）的项目。
        """
        selectors = (
            [".fchl-item:visible", ".fchl-item"]
            if study_mode == "force"
            else [".fchl-item:not(.fchl-item-active):visible", ".fchl-item:not(.fchl-item-active)"]
        )
        for sel in selectors:
            items = self._page.locator(sel)
            for i in range(items.count()):
                item = items.nth(i)
                title = self._extract_item_title(item)
                if title and title not in failed_courses and title not in completed_courses:
                    return item
        return None

    def _count_page_tasks(self) -> tuple:
        """统计当前页面的课程总数和已完成数，返回 (total, finished)。

        支持两种页面结构：
          - .fchl-item 结构（Foods/实验课）
          - .van-collapse-item 折叠章节（普通课程，从进度文本解析）
        """
        fchl_items = self._page.locator(".fchl-item")
        if fchl_items.count() > 0:
            finished = self._page.locator(".fchl-item.fchl-item-active").count()
            return fchl_items.count(), finished

        total, finished = 0, 0
        collapse_items = self._page.locator(".van-collapse-item")
        for i in range(collapse_items.count()):
            title_el = collapse_items.nth(i).locator(".van-cell__title")
            if title_el.count() == 0:
                continue
            text = title_el.inner_text()
            m = re.search(r"(\d+)\s*/\s*(\d+)", text)
            if m:
                f, t = int(m.group(1)), int(m.group(2))
                total += t
                finished += f
        return total, finished

    def _log_round_start(self, current_round: int, all_tasks: int, study_time: int) -> None:
        """输出新一轮强制学习开始的日志（含课程数和预计用时）。"""
        round_seconds = all_tasks * study_time
        m, s = divmod(round_seconds, 60)
        self.log.info(
            f"--- 第 {current_round} 轮开始（共 {all_tasks} 课，预计用时 {m}分{s}秒）---"
        )

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
        study_mode: str,
        completed_courses: set,
        failed_courses: set,
    ) -> bool:
        """尝试展开下一个有未完成课程的折叠章节（.van-collapse-item）。

        非强制模式下跳过已全部完成（完成数 >= 总数）的章节。
        返回 True 表示成功展开了一个新章节，False 表示所有章节均已展开或完成。
        """
        collapse_items = self._page.locator(".van-collapse-item")
        for i in range(collapse_items.count()):
            item = collapse_items.nth(i)
            # aria-expanded="false" 表示该章节处于折叠状态
            btn = item.locator('.van-collapse-item__title[aria-expanded="false"]')
            if btn.count() == 0:
                continue
            # 非强制模式：跳过已全部完成的章节（完成数 >= 总数）
            if study_mode != "force":
                title_el = item.locator(".van-cell__title")
                if title_el.count() > 0:
                    m = re.search(r"(\d+)\s*/\s*(\d+)", title_el.inner_text())
                    if m and int(m.group(1)) >= int(m.group(2)):
                        continue
            btn.first.click()
            time.sleep(1)
            return True
        return False

    # ------------------------------------------------------------------
    # 从 run_study 提升的辅助方法（原为闭包，现为类方法）
    # ------------------------------------------------------------------

    def _dismiss_broadcast(self) -> None:
        """检测并关闭广播公告弹窗（.broadcast-modal），有则点击关闭按钮。"""
        try:
            broadcast = self._page.locator(".broadcast-modal")
            if broadcast.count() > 0 and broadcast.first.is_visible():
                broadcast.first.locator("button").first.click(force=True)
                self.log.info("[公告] 已关闭广播公告弹窗")
                time.sleep(0.5)
        except Exception:
            pass

    def _return_to_chapter_list(self) -> bool:
        """从课程详情页返回章节列表。

        优先点击「返回」按钮，其次点击导航栏左箭头，均不存在则返回 False。
        """
        return_btn = self._page.locator('.comment-footer-button:has-text("返回")')
        if return_btn.count() > 0:
            return_btn.first.click(force=True)
        else:
            back = self._page.locator(".van-nav-bar__left")
            if back.count() > 0:
                back.first.click(force=True)
            else:
                return False
        time.sleep(3)
        url = self._page.url.lower()
        return "detail" not in url and "video" not in url and "play" not in url

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

        projects = self._page.locator(".task-block")
        if projects.count() == 0:
            self.log.warning("学习任务列表中未找到任务项（.task-block）")
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
                state_txt = cat_el.first.inner_text().strip() if cat_el.count() > 0 else ""
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
            if state.study_tabs:
                self._switch_to_study_tab(state.study_tabs[0])

            return True

        self.log.info("所有学习项目已遍历完成。")
        return False

    def _try_next_study_tab(self, state: _StudyRunState) -> bool:
        """尝试切换到当前项目的下一个课程 Tab（选修/匹配等）。

        切换成功后检查该 Tab 下是否有未完成课程。
        返回 True 表示成功切换到有内容的新 Tab，False 表示已无更多 Tab。
        """
        while state.active_tab_index + 1 < len(state.study_tabs):
            state.active_tab_index += 1
            st = state.study_tabs[state.active_tab_index]
            if self._switch_to_study_tab(st):
                time.sleep(1)
                has_content = (
                    self._page.locator(
                        ".img-texts-item, .van-collapse-item, .fchl-item"
                    ).count() > 0
                    and self._page.locator(".img-texts-item:not(.passed)").count() > 0
                )
                if has_content:
                    return True
                self.log.info("[Tab] 切换后该 Tab 无未完成课程，继续下一 Tab")
        return False

    # ------------------------------------------------------------------
    # 主学习流程
    # ------------------------------------------------------------------

    def run_study(
        self,
        study_time: int = 20,
        study_mode: str = "true",
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
        try:
            self._page.wait_for_selector(
                ".van-collapse-item, .img-texts-item, .fchl-item",
                state="attached",
                timeout=10000,
            )
        except Exception:
            pass

        # 统计课程总数并输出预计完成时间
        all_tasks, all_finished = self._count_page_tasks()
        remaining = all_tasks - all_finished
        if all_tasks > 0:
            self.log.info(f"课程进度：{all_finished}/{all_tasks}")
            if study_mode == "force":
                rm, rs = divmod(all_tasks * study_time, 60)
                self.log.info(f"强制模式：每轮预计用时 {rm}分{rs}秒")
            if remaining > 0:
                em, es = divmod(remaining * study_time, 60)
                finish_time = (
                    datetime.datetime.now()
                    + datetime.timedelta(seconds=remaining * study_time)
                )
                self.log.info(
                    f"预计剩余用时：{em}分{es}秒，"
                    f"预计完成时间：{finish_time.strftime('%H:%M:%S')}"
                )
            else:
                self.log.info("所有课程已完成。")
        else:
            self.log.warning("未能统计到课程数量，继续尝试学习。")

        failed_courses: set = set()
        completed_courses: set = set()
        current_round = 1
        round_completed = 0

        if study_mode == "force" and all_tasks > 0:
            self._log_round_start(current_round, all_tasks, study_time)

        # 主循环：最多迭代 500 次防止死循环
        for _ in range(500):
            try:
                # ----------------------------------------------------------
                # 验证码检测
                # ----------------------------------------------------------
                if _has_captcha(self._page):
                    self.log.info("[验证码] 检测到验证码，尝试自动处理")
                    _handle_click_captcha(self._page, self.log)
                    time.sleep(2)
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
                    title = self._extract_item_title(fchl_target)
                    self.log.info(f"[fchl] 开始学习：{title}")
                    fchl_target.click(force=True)
                    time.sleep(study_time)

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
                target = self._find_course_target(img_items, failed_courses, completed_courses)

                if target is not None:
                    title = self._extract_item_title(target)
                    self.log.info(f"[img-texts] 开始学习：{title}")
                    target.click(force=True)
                    time.sleep(study_time)

                    # 调用 JS 接口标记完成
                    try:
                        self._page.evaluate("finishWxCourse()")
                    except Exception:
                        pass

                    completed_courses.add(title)
                    round_completed += 1

                    # 返回章节列表
                    if not self._return_to_chapter_list():
                        self.log.warning(f"[img-texts] 无法返回章节列表，标记失败：{title}")
                        failed_courses.add(title)
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 当前章节无课程：尝试展开下一个折叠章节
                # ----------------------------------------------------------
                if self._expand_next_section(study_mode, completed_courses, failed_courses):
                    time.sleep(1)
                    continue

                # ----------------------------------------------------------
                # 当前 Tab 已无更多内容：切换到下一个 Tab
                # ----------------------------------------------------------
                self.log.info("[Tab] 当前 Tab 已无未完成课程，尝试切换下一个 Tab")
                if self._try_next_study_tab(state):
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
                if not self._goto_next_project(state, completed_projects):
                    self.log.info("所有学习项目已完成，退出学习流程。")
                    return
                time.sleep(1)

            except Exception as e:
                self.log.error(f"学习主循环异常：{e}", exc_info=True)
                time.sleep(3)


