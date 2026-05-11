import logging
import random
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, cast

from playwright._impl._errors import TargetClosedError

from weban.app.runtime import clean_text, get_bool_value

from ..base import BaseMixin, PageContext
from ..captcha import handle_tencent_captcha, has_captcha, _log_captcha_contexts
from ..const import (
    SEL_ANSWER_CARD_BTN,
    SEL_COURSE_LIST_MARKERS,
    SEL_EXAM_ITEM,
    SEL_EXAM_ITEM_PASS,
    SEL_EXAM_ITEM_TITLE,
    SEL_EXAM_SHEET,
    SEL_EXAM_SUBMIT_AREA,
    SEL_EXAM_TAB,
    SEL_JOIN_BTN,
    SEL_NEXT_BTN,
    SEL_QUEST_CATEGORY,
    SEL_QUEST_INDICATOR,
    SEL_QUEST_OPTIONS,
    SEL_QUEST_STEM,
    SEL_QUEST_STEM_SUB,
    SEL_START_BTN,
    SEL_TASK_BLOCK,
    SEL_TASK_BLOCK_TITLE,
)
from .answer_manager import ExamAnswerManager
from .completion_manager import ExamCompletionManager
from .context_detector import ExamContextDetector
from .submit_manager import ExamSubmitManager

_terminal_lock = threading.Lock()
logger = logging.getLogger(__name__)


class ExamMixin(BaseMixin):
    """在线考试流程 Mixin。"""

    if TYPE_CHECKING:
        import logging as _logging
        from typing import Union as _Union

        from playwright.sync_api import Browser, BrowserContext, Page, Playwright

        from ..browser import BrowserConfig

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

    # ========================================================================
    # 答案匹配逻辑
    # ========================================================================

    def _answer_manager(self) -> ExamAnswerManager:
        manager = getattr(self, "_exam_answer_manager", None)
        if manager is None:
            manager = ExamAnswerManager()
            setattr(self, "_exam_answer_manager", manager)
        return manager

    def _submit_manager(self) -> ExamSubmitManager:
        manager = getattr(self, "_exam_submit_manager", None)
        if manager is None:
            manager = ExamSubmitManager()
            setattr(self, "_exam_submit_manager", manager)
        return manager

    def _completion_manager(self) -> ExamCompletionManager:
        manager = getattr(self, "_exam_completion_manager", None)
        if manager is None:
            manager = ExamCompletionManager(
                page_getter=lambda: self._page,
                log=self.log,
                vue_app_finder_js_getter=self._vue_app_finder_js,
                is_in_result_context=lambda: self._is_in_context(PageContext.EXAM_RESULT),
            )
            setattr(self, "_exam_completion_manager", manager)
        return manager

    def _context_detector(self) -> ExamContextDetector:
        detector = getattr(self, "_exam_context_detector", None)
        if detector is None:
            detector = ExamContextDetector()
            setattr(self, "_exam_context_detector", detector)
        return detector

    def _match_answer_in_bank(self, title: str) -> dict | None:
        """从题库中匹配答案。

        Args:
            title: 题目标题

        Returns:
            匹配到的答案项，或 None
        """
        return self._answer_manager().find_answer_item(self.answers, title)

    def _extract_correct_options(self, answer_item: dict | None) -> List[str]:
        """从答案项中提取正确选项文本列表。

        Args:
            answer_item: 题库中的答案项

        Returns:
            正确选项文本列表
        """
        return self._answer_manager().extract_correct_options(answer_item)

    def _find_option_index(
        self, option_text: str, page_options: Any, options_count: int
    ) -> int:
        """在页面选项中查找匹配的选项索引。

        Args:
            option_text: 正确答案文本
            page_options: 页面选项定位器
            options_count: 选项数量

        Returns:
            匹配的选项索引，未找到返回 -1
        """
        return self._answer_manager().find_option_index(
            option_text, page_options, options_count
        )

    def _verify_and_fix_selection(self, page_options: Any, idx: int) -> bool:
        """验证选项是否被选中，未选中则用原生 click 兜底。"""
        if not self._page:
            return False

        def _is_selected() -> bool:
            try:
                opt = page_options.nth(idx)
                cls = (opt.get_attribute("class") or "").lower()
                # ExamPage: .selected; QuestionPage: .answerPg-container-item-active
                return any(k in cls for k in (
                    "selected", "active", "checked", "answerpg-container-item-active"
                ))
            except Exception:
                return False

        if _is_selected():
            return True

        self.log.debug(f"[选择验证] 选项 {idx} 未检测到选中状态，原生 click 兜底")
        try:
            self._page.evaluate("""(idx) => {
                const sels = '.quest-option-item, .answerPg-container-item';
                const opts = document.querySelectorAll(sels);
                if (idx >= opts.length) return;
                opts[idx].click();
            }""", idx)
            time.sleep(0.3)
            selected = _is_selected()
            if not selected:
                self.log.warning(f"[选择验证] 选项 {idx} 原生 click 后仍未选中")
            return selected
        except Exception:
            return False

    def _click_options_by_indices(
        self, page_options: Any, indices: List[int], question_type: int = 1
    ) -> int:
        """点击选项，直接 JS 原生 click，确保触发 Vue 事件。"""
        if not self._page:
            return 0
        selectors = SEL_QUEST_OPTIONS
        try:
            result = self._page.evaluate("""(args) => {
                const [sels, indices] = args;
                const opts = document.querySelectorAll(sels);
                let clicked = 0;
                for (const idx of indices) {
                    if (idx < opts.length) {
                        opts[idx].click();
                        clicked++;
                    }
                }
                return clicked;
            }""", [selectors, indices])
            return result or 0
        except Exception:
            return 0

    # ========================================================================
    # 题目导航逻辑
    # ========================================================================

    def _get_current_question_index(self) -> int:
        """获取当前题号（从1开始）。"""
        if not self._page:
            return 0
        try:
            indicator = self._page.locator(SEL_QUEST_INDICATOR).first
            if indicator.count() > 0 and indicator.is_visible():
                m = re.search(r"(\d+)\s*/\s*(\d+)", indicator.inner_text())
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        try:
            idx = self._page.evaluate(
                """() => {
                %s
                const app = findVueProxy(['$store']) || findVueProxy(null);
                const state = app?.$store?.state?.courseExam;
                const activeIndex = Number(state?.activeIndex);
                if (Number.isFinite(activeIndex) && activeIndex >= 0) {
                    return activeIndex + 1;
                }
                return 0;
            }"""
                % self._vue_app_finder_js()
            )
            if isinstance(idx, int) and idx > 0:
                return idx
        except Exception:
            pass
        return 0

    def _read_total_questions_from_indicator(self) -> int:
        """从题号指示器读取总题数（如 10/50 -> 50）。"""
        if not self._page:
            return 0
        try:
            indicator = self._page.locator(SEL_QUEST_INDICATOR).first
            if indicator.count() > 0 and indicator.is_visible():
                text = indicator.inner_text()
                match = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if match:
                    return int(match.group(2))
        except Exception:
            pass
        try:
            total = self._page.evaluate(
                """() => {
                %s
                const app = findVueProxy(['$store']) || findVueProxy(null);
                const paper = app?.$store?.state?.courseExam?.examPaper;
                return Array.isArray(paper) ? paper.length : 0;
            }"""
                % self._vue_app_finder_js()
            )
            if isinstance(total, int) and total > 0:
                return total
        except Exception:
            pass
        return 0

    def _estimate_unfinished_question_count(self) -> int | None:
        """估算未作答数量，优先 Vue store，失败时按题号差值兜底。"""
        unfinished = self._get_unfinished_question_count()
        if isinstance(unfinished, int):
            return unfinished

        current = self._get_current_question_index()
        total = self._read_total_questions_from_indicator()
        if total > 0 and current > 0 and total >= current:
            return total - current
        return None

    def _wait_for_question_change(
        self, prev_title: str, prev_indicator: str, timeout: float = 6
    ) -> bool:
        """等待题目变化。

        Args:
            prev_title: 之前的题目
            prev_indicator: 之前的题号指示器文本
            timeout: 超时秒数

        Returns:
            是否成功切换到新题目
        """
        if not self._page:
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # 读取当前题目文本（支持 ExamPage + QuestionPage）
                current_title = ""
                for sub_sel in [s.strip() for s in SEL_QUEST_STEM_SUB.split(",")]:
                    try:
                        el = self._page.locator(sub_sel).first
                        if el.count() > 0 and el.is_visible():
                            raw = el.inner_text().strip()
                            current_title = re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()
                            if current_title:
                                break
                    except Exception:
                        continue
                if current_title and current_title != prev_title:
                    return True

                if prev_indicator:
                    ind = self._page.locator(SEL_QUEST_INDICATOR).first
                    if ind.count() > 0 and ind.is_visible():
                        current_ind = ind.inner_text().strip()
                        if current_ind and current_ind != prev_indicator:
                            return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _close_answer_sheet(self) -> None:
        """关闭答题卡弹窗（如果打开的话）。全部用 JS 检测+操作，避免 is_visible() 状态不同步。"""
        if not self._page:
            return
        try:
            closed = self._page.evaluate("""() => {
                function isTrulyVisible(el) {
                    if (!el) return false;
                    if (el.style.display === 'none' || el.style.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    const s = getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden') return false;
                    return true;
                }
                // 支持 mint-popup.sheet (ExamPage) 和 van-popup.sheet (QuestionPage)
                const sheet = document.querySelector('.mint-popup.sheet, .van-popup.sheet');
                if (!isTrulyVisible(sheet)) return 'not-open';

                // 方法1: click 返回按钮（触发 Vue 事件）
                const btn = sheet.querySelector(
                    '.mint-header-button button, .mintui-back, .van-nav-bar__left, .van-icon-cross'
                );
                if (btn) { btn.click(); return 'clicked'; }

                // 方法2: 直接隐藏
                sheet.style.display = 'none';
                sheet.style.visibility = 'hidden';
                document.querySelectorAll('.v-modal, .van-overlay').forEach(el => el.style.display = 'none');
                return 'hidden';
            }""")
            if closed == "clicked":
                time.sleep(0.5)
            elif closed == "hidden":
                time.sleep(0.3)
        except Exception:
            pass

    def _dismiss_msgbox(self) -> None:
        """关闭 mint-msgbox 提示弹窗（如"请作答当前题目"）。"""
        if not self._page:
            return
        try:
            self._page.evaluate("""() => {
                const wrapper = document.querySelector('.mint-msgbox-wrapper');
                if (!wrapper) return;
                const r = wrapper.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return;
                const s = getComputedStyle(wrapper);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                const btn = wrapper.querySelector('.mint-msgbox-confirm, .mint-msgbox-btn');
                if (btn) btn.click();
            }""")
            time.sleep(0.3)
        except Exception:
            pass

    def _jump_to_first_unfinished_question(self) -> bool:
        """通过答题卡跳转到首个未作答题。"""
        if not self._page:
            return False
        try:
            jumped = self._page.evaluate("""() => {
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }

                let sheet = document.querySelector('.sheet');
                if (!isVisible(sheet)) {
                    const cardBtn = Array.from(
                        document.querySelectorAll('button, .mint-button, .van-button, a')
                    ).find((el) => {
                        const txt = (el.textContent || '').trim();
                        return /答题卡|查看答题卡/.test(txt) && isVisible(el);
                    });
                    if (cardBtn) cardBtn.click();
                    sheet = document.querySelector('.sheet');
                }
                if (!isVisible(sheet)) return false;

                const target = sheet.querySelector('.quest-indexs-list li:not(.done)');
                if (!target || !isVisible(target)) return false;
                target.click();
                return true;
            }""")
            if jumped:
                time.sleep(0.6)
            return bool(jumped)
        except Exception:
            return False

    def _recover_from_unfinished_prompt(self) -> bool:
        """从“未作答”拦截弹层恢复到答题流程。"""
        if not self._page:
            return False
        try:
            recovered = self._page.evaluate("""() => {
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }

                const msgbox = document.querySelector('.mint-msgbox-wrapper');
                if (isVisible(msgbox)) {
                    const msg = (msgbox.textContent || '').trim();
                    if (/请作答|未作答/.test(msg)) {
                        const btn = msgbox.querySelector('.mint-msgbox-confirm, .mint-msgbox-btn');
                        if (btn) btn.click();
                    }
                }

                const confirmSheet = document.querySelector('.confirm-sheet');
                if (!isVisible(confirmSheet)) return true;

                const text = (confirmSheet.textContent || '').trim();
                if (!/未作答|请作答/.test(text)) return false;

                const continueBtn = Array.from(
                    confirmSheet.querySelectorAll('button, .mint-button, .van-button, a')
                ).find((el) => /查看答题卡|继续作答|返回/.test((el.textContent || '').trim()));
                if (continueBtn && isVisible(continueBtn)) {
                    continueBtn.click();
                    return true;
                }

                const backBtn = confirmSheet.querySelector(
                    '.mint-header-button button, .mintui-back, .van-nav-bar__left, .van-icon-arrow-left'
                );
                if (backBtn && isVisible(backBtn)) {
                    backBtn.click();
                    return true;
                }
                return false;
            }""")
            if not recovered:
                return False
            time.sleep(0.5)
            return self._jump_to_first_unfinished_question()
        except Exception:
            return False

    def _get_unfinished_question_count(self) -> int | None:
        """读取当前考试未作答数量。优先读取 Vue store。"""
        if not self._page:
            return None
        try:
            count = self._page.evaluate("""() => {
                %s
                const app = findVueProxy(['$store']) || findVueProxy(null);
                const paper = app?.$store?.state?.courseExam?.examPaper;
                if (Array.isArray(paper) && paper.length > 0) {
                    return paper.filter((item) => !item?.isDone).length;
                }
                return null;
            }""" % self._vue_app_finder_js())
            if isinstance(count, int) and count >= 0:
                return count
        except Exception:
            pass
        return None

    def _dismiss_blocking_overlays(self, answered_count: int) -> bool:
        """统一清理答题过程中的遮挡层。

        Returns:
            True 表示检测到应退出的交卷/结算弹窗；False 表示可继续答题。
        """
        if not self._page:
            return False

        # 1. 关闭答题卡弹窗
        self._close_answer_sheet()

        # 2. 关闭 mint-msgbox（"请作答"等提示）
        self._dismiss_msgbox()

        # 3. 用 JS 检测交卷/结算弹窗（比 is_visible() 更可靠）
        try:
            result = self._page.evaluate("""() => {
                function isTrulyVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    if (r.bottom <= 0 || r.top >= window.innerHeight) return false;
                    if (r.right <= 0 || r.left >= window.innerWidth) return false;
                    const s = getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden') return false;
                    // 检查祖先是否有隐藏的
                    let p = el.parentElement;
                    while (p && p !== document.body) {
                        const ps = getComputedStyle(p);
                        if (ps.display === 'none' || ps.visibility === 'hidden') return false;
                        p = p.parentElement;
                    }
                    return true;
                }

                const sels = '.van-popup, .mint-popup, .confirm-sheet, .mint-msgbox';
                const popups = document.querySelectorAll(sels);
                for (const p of popups) {
                    if (!isTrulyVisible(p)) continue;
                    const cls = (p.className || '').toLowerCase();
                    // 排除答题卡弹窗（.sheet 但非 .confirm-sheet）
                    if (cls.includes('sheet') && !cls.includes('confirm')) continue;
                    const txt = (p.textContent || '').trim();
                    // 排除答题卡自身文本（含"答题卡"关键词的 sheet）
                    if (cls.includes('sheet') && txt.includes('答题卡')) continue;
                    // 检测结算/交卷弹窗特征
                    if (/未作答.*道题|道题.*未作答|确认交卷|交卷确认/.test(txt)) {
                        return { found: true, text: txt.substring(0, 120) };
                    }
                }
                return { found: false };
            }""")
            if result and result.get("found"):
                txt = result.get("text", "")
                self.log.info(f"[答题] 探测到结算/交卷层 (已答 {answered_count} 题): {txt[:80]}")
                if self._is_submit_blocked_message(txt):
                    if self._recover_from_unfinished_prompt():
                        self.log.info("[答题] 检测到未作答拦截，已恢复到未作答题继续答题")
                        return False
                return True
        except TargetClosedError:
            self.log.warning("[答题] 页面已关闭")
            return True
        except Exception:
            pass

        return False

    def _handle_captcha(self) -> bool:
        """检测并处理验证码，返回 True 表示处理了验证码（调用方应 continue 重试）。"""
        if not self._page:
            return False
        if not has_captcha(self._page, require_cscapt=False):
            return False
        self.log.info("[答题] 检测到验证码，正在处理...")
        _log_captcha_contexts(self._page, self.log)
        if handle_tencent_captcha(self._page, self.log, require_cscapt=False):
            self.log.info("[答题] 验证码处理成功")
        else:
            self.log.warning("[答题] 验证码处理失败")
        time.sleep(1)
        return True

    def _click_first_option(self) -> bool:
        """点击第一个选项（随机/兜底用）。优先 JS 原生 click 触发 Vue 事件。"""
        if not self._page:
            return False
        try:
            result = self._page.evaluate(f"""() => {{
                const opts = document.querySelectorAll('{SEL_QUEST_OPTIONS}');
                if (opts.length > 0) {{ opts[0].click(); return true; }}
                return false;
            }}""")
            time.sleep(0.3)
            return bool(result)
        except Exception:
            return False

    def _wait_and_advance(self, q_time: int, q_offset: int) -> bool:
        """随机等待后跳到下一题。"""
        delay = random.randint(q_time, q_time + q_offset) if q_offset > 0 else q_time
        if delay > 0:
            time.sleep(delay)
        return self._advance_to_next_question()

    def _advance_to_next_question(self) -> bool:
        """Playwright click 下一题按钮。"""
        if not self._page:
            return False

        prev_title = ""
        prev_indicator = ""
        try:
            # 读取当前题目（支持 ExamPage + QuestionPage）
            for sub_sel in [s.strip() for s in SEL_QUEST_STEM_SUB.split(",")]:
                try:
                    el = self._page.locator(sub_sel).first
                    if el.count() > 0 and el.is_visible():
                        raw = el.inner_text().strip()
                        prev_title = re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()
                        if prev_title:
                            break
                except Exception:
                    continue

            ind = self._page.locator(SEL_QUEST_INDICATOR).first
            if ind.count() > 0 and ind.is_visible():
                prev_indicator = ind.inner_text().strip()
        except Exception:
            pass

        # 先关闭可能残留的答题卡弹窗
        self._close_answer_sheet()

        # Playwright click 下一题按钮
        try:
            btn = self._page.locator(SEL_NEXT_BTN).filter(has_text="下一题").first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                if self._wait_for_question_change(prev_title, prev_indicator, 8):
                    return True
                self.log.warning("[下一题] 按钮已点击但题目未变化")
            else:
                self.log.warning("[下一题] 按钮不可见")
        except Exception as e:
            self.log.warning(f"[下一题] 点击异常: {e}")

        # 兜底：通过答题卡跳转
        result = self._jump_via_answer_card(prev_title, prev_indicator)
        if not result:
            self.log.warning("[下一题] 答题卡跳转也失败")
        return result

    def _jump_via_answer_card(self, prev_title: str, prev_indicator: str, target_num: int | None = None) -> bool:
        """通过答题卡跳转到指定题号（默认下一题）。"""
        if not self._page:
            return False

        if target_num is None:
            current_idx = self._get_current_question_index()
            target_num = current_idx + 1

        self.log.debug(f"[答题卡跳转] 目标题号: {target_num}")

        try:
            # 检查 sheet 是否已经打开
            sheet = self._page.locator(SEL_EXAM_SHEET).first
            sheet_already_open = sheet.count() > 0 and sheet.is_visible()

            if not sheet_already_open:
                card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).first
                if card_btn.count() == 0 or not card_btn.is_visible():
                    self.log.warning("[答题卡跳转] 答题卡按钮不可见")
                    return False
                card_btn.click(timeout=3000)
                time.sleep(0.5)

            # 注意：答题卡按题型分组时每组会从 1 重新编号，不能按显示文本匹配题号
            # 改为按 DOM 顺序（全量 li）定位第 target_num 个题号。
            try:
                clicked = self._page.evaluate("""(targetNum) => {
                    const idx = Number(targetNum) - 1;
                    if (!Number.isFinite(idx) || idx < 0) return false;

                    function isVisible(el) {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }

                    const items = Array.from(
                        document.querySelectorAll('.sheet .quest-indexs-list li')
                    ).filter(isVisible);
                    if (idx >= items.length) return false;
                    items[idx].click();
                    return true;
                }""", target_num)
                if clicked:
                    if self._wait_for_question_change(prev_title, prev_indicator, 6):
                        return True
                    self.log.warning(f"[答题卡跳转] 点击序号 {target_num} 后题目未变化")
                else:
                    self.log.warning(f"[答题卡跳转] 序号 {target_num} 超出答题卡范围")
            except Exception as e:
                self.log.warning(f"[答题卡跳转] 点击序号异常: {e}")
        except Exception as e:
            self.log.warning(f"[答题卡跳转] 异常: {e}")

        return False

    # ========================================================================
    # 提交流程
    # ========================================================================

    def _is_submit_blocked_message(self, text: str) -> bool:
        """判断交卷是否被“未作答”类提示拦截。"""
        return self._submit_manager().is_submit_blocked_message(text)

    def _read_submit_runtime_state(self) -> dict:
        return self._completion_manager().read_submit_runtime_state()

    def _dismiss_result_popup(self, timeout: float = 20.0) -> str:
        return self._completion_manager().dismiss_result_popup(timeout=timeout)

    def _submit_exam(self) -> str:
        return self._completion_manager().submit_exam(
            is_submit_blocked_message=self._is_submit_blocked_message,
            dismiss_msgbox=self._dismiss_msgbox,
        )

    def _click_submit_buttons(self) -> str:
        return self._completion_manager().click_submit_buttons()

    # ========================================================================
    # 主考试流程
    # ========================================================================

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """Playwright click 处理考试弹窗。"""
        if not self._page:
            raise RuntimeError("Page is not initialized")

        # JS 读取弹窗文本和判断类型（仅读取，不 click）
        result = self._page.evaluate(f"""(examMode) => {{
            {self._vue_app_finder_js()}
            const dialogs = document.querySelectorAll('.van-dialog, .mint-msgbox, .mint-toast');
            for (const d of dialogs) {{
                const rect = d.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const style = getComputedStyle(d);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const text = (d.textContent || '').replace(/\\n/g, ' ').trim();

                if (/未开放|已关闭|不允许|暂无考试机会|次数已用|课程学习未完成/.test(text)) {{
                    return {{ action: 'return', text, clickTarget: 'first-button' }};
                }}
                if (/未提交|重新进入|继续考试|清除/.test(text)) {{
                    // 优先点确认按钮
                    const confirmEl = d.querySelector('.van-dialog__confirm, .mint-msgbox-confirm');
                    if (confirmEl) return {{ action: 'continue', text, clickTarget: 'confirm-class' }};
                    for (const btn of d.querySelectorAll('button')) {{
                        if (/确[认订]/.test((btn.textContent || '').trim()))
                            return {{ action: 'continue', text, clickTarget: 'confirm-text' }};
                    }}
                    return {{ action: 'continue', text, clickTarget: 'first-button' }};
                }}
                const isPassed = /已合格|已及格|考试通过/.test(text)
                    || (/合格/.test(text) && /最高成绩/.test(text));
                if (isPassed && examMode === 'true') {{
                    return {{ action: 'return', text, clickTarget: 'first-button' }};
                }}
                return {{ action: 'continue', text, clickTarget: 'first-button' }};
            }}
            // 调用 Vue onPop（不通过 DOM click）
            const popup = document.querySelector('.popup, .popup-wrapper');
            if (popup) {{
                const r = popup.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {{
                    const app = findVueProxy(['onPop']);
                    if (app && typeof app.onPop === 'function') {{
                        app.onPop();
                        return {{ action: 'continue', text: '直接调用 onPop 进入考试' }};
                    }}
                }}
            }}
            return null;
        }}""", exam_mode)

        if not result:
            return "continue"

        action = result.get("action", "continue")
        text = result.get("text", "")
        click_target = result.get("clickTarget", "")

        # Playwright click 关闭弹窗
        try:
            dialog = self._page.locator(".van-dialog, .mint-msgbox").first
            if dialog.count() > 0 and dialog.is_visible():
                if click_target == "confirm-class":
                    btn = dialog.locator(
                        ".van-dialog__confirm, .mint-msgbox-confirm"
                    ).first
                    if btn.count() > 0:
                        btn.click(timeout=2000)
                elif click_target == "confirm-text":
                    btn = dialog.locator("button").filter(
                        has_text=re.compile(r"确[认订]")
                    ).first
                    if btn.count() > 0:
                        btn.click(timeout=2000)
                    else:
                        btn = dialog.locator("button").first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                else:
                    btn = dialog.locator("button").first
                    if btn.count() > 0:
                        btn.click(timeout=2000)
        except Exception:
            pass

        if action == "return":
            self.log.warning(f"无法考试: {text}")
        elif text:
            self.log.info(f"流程提示: {text}")
        return action

    def _click_exam_join_button(self, exam_title: str) -> bool:
        """Playwright click 当前考试卡片内的"参加考试"，避免多考试时误点第一项。"""
        if not self._page:
            return False
        try:
            card_index = self._context_detector().find_exam_card_index(
                self._page, exam_title
            )

            if card_index is not None and card_index >= 0:
                card = self._page.locator(".exam-item").nth(card_index)
                btn = card.locator(SEL_JOIN_BTN).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=3000)
                    self.log.info(f"[答题] Playwright click 考试卡片按钮: {exam_title}")
                    return True
        except Exception as e:
            self.log.debug(f"[答题] 当前考试卡片按钮点击异常: {e}")
        return False

    def _click_exam_start_popup(self) -> bool:
        """处理 ExamPopup 的开始按钮，优先调用 Vue onPop 绕过失效 DOM 点击。"""
        if not self._page:
            return False
        try:
            state = self._context_detector().trigger_start_popup(
                self._page, self._vue_app_finder_js()
            )

            if state and state.get("clicked"):
                self.log.info(f"[答题] 启动考试: {state.get('method')}")
                return True

            # JS 入口都不可用时，直接点击开始考试按钮
            if state and state.get("reason") in {"no-start-handler", "no-popup"}:
                try:
                    btn = self._page.locator(SEL_START_BTN).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=3000)
                        self.log.info("[答题] 启动考试: Playwright click 开始考试按钮")
                        return True
                except Exception:
                    pass
        except Exception as e:
            self.log.debug(f"[答题] 启动弹窗处理异常: {e}")
        return False

    def _handle_exam_intermediate_pages(self) -> bool:
        """Playwright click 处理进入考试前的中间页面。"""
        assert self._page is not None
        for _ in range(6):
            if self._page.locator(SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return True

            try:
                # 勾选同意框
                try:
                    cb = self._page.locator(
                        "#agree, .agree-checkbox input, input[type='checkbox']"
                    ).first
                    if cb.count() > 0 and not cb.is_checked():
                        cb.click(timeout=2000)
                except Exception:
                    pass

                # 下一步按钮
                try:
                    next_btn = self._page.locator("button").filter(
                        has_text=re.compile(r"下一步|同意")
                    ).first
                    if next_btn.count() > 0 and next_btn.is_visible():
                        next_btn.click(timeout=2000)
                except Exception:
                    pass

                # 确认按钮
                try:
                    ok_btn = self._page.locator("button").filter(
                        has_text=re.compile(r"确认|确定")
                    ).first
                    if ok_btn.count() > 0 and ok_btn.is_visible():
                        ok_btn.click(timeout=2000)
                except Exception:
                    pass

                # 子项目
                try:
                    sub = self._page.locator(
                        ".img-text-block, .task-block"
                    ).first
                    if sub.count() > 0 and sub.is_visible():
                        sub.click(timeout=2000)
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(0.5)

        return self._page.locator(SEL_EXAM_TAB).count() > 0

    def _interactive_answering(
        self, title: str, options: Any, options_count: int, opt_texts: list[str]
    ) -> bool:
        """多线程安全的手工干预命令行交互。"""

        with _terminal_lock:
            print("\n" + "=" * 60)
            print(f"   【人工干预请求】 用户: {getattr(self, 'account', '未知')}")
            print(f"   项目: {getattr(self, 'tenant_name', '未知')}")
            print(f"   题目: {title}")
            print("-" * 64)
            for i in range(options_count):
                display_text = (
                    opt_texts[i][4:] if len(opt_texts[i]) > 4 else opt_texts[i]
                )
                print(f"    {i + 1}. {display_text}")
            print("-" * 64)
            print(
                "   (输入选项编号，多选用逗号分隔；直接 Enter 表示网页手动勾选后继续)"
            )

            try:
                raw_choice = input("   请选择: ").strip()
                indices = []

                if raw_choice:
                    nums = re.split(r"[,\s，]+", raw_choice)
                    for n in nums:
                        if n.isdigit():
                            idx = int(n) - 1
                            if 0 <= idx < options_count:
                                indices.append(idx)

                    if not indices:
                        print("   无效输入，已忽略。")
                        return False

                    for idx in indices:
                        try:
                            self._page.evaluate(
                                f"""() => {{
                                    const opts = document.querySelectorAll('{SEL_QUEST_OPTIONS}');
                                    if ({idx} < opts.length) opts[{idx}].click();
                                }}"""
                            )
                        except Exception:
                            pass

                else:
                    input("   请在网页上手动勾选答案后按 Enter 继续: ")

                    for i in range(options_count):
                        try:
                            cls = (options.nth(i).get_attribute("class") or "").lower()
                            if "selected" in cls:
                                indices.append(i)
                        except Exception:
                            pass

                    if not indices:
                        print("   未检测到已选选项，本题未记录。")
                        return False

                new_item = {"optionList": [], "type": "手工输入"}
                for i in range(options_count):
                    raw_opt_text = options.nth(i).inner_text().strip()
                    new_item["optionList"].append(
                        {"content": raw_opt_text, "isCorrect": 1 if i in indices else 2}
                    )

                target_key = title
                c_title = clean_text(title)
                for existing_raw in list(self.answers.keys()):
                    if clean_text(existing_raw) == c_title:
                        target_key = existing_raw
                        break

                self.answers[target_key] = new_item
                if hasattr(self, "_save_answers"):
                    getattr(self, "_save_answers")()

                print("   已成功点选并记录到题库！")
                return True

            except (EOFError, OSError):
                self.log.warning("终端不可用，无法手动答题。")
                return False
            except Exception as e:
                print(f"   交互出错: {e}")
                return False
            finally:
                print("=" * 62 + "\n")

    def _pre_scan_unmatched(self) -> list:
        """预扫描所有题目，返回题库中无答案的题目列表。

        通过答题卡逐题跳转读取，不依赖"下一题"按钮（页面可能要求先作答）。
        """
        if not self._page:
            return []
        page = self._page

        total_questions = 0
        try:
            indicator = page.locator(SEL_QUEST_INDICATOR).first
            if indicator.count() > 0 and indicator.is_visible():
                m = re.search(r"(\d+)\s*/\s*(\d+)", indicator.inner_text())
                if m:
                    total_questions = int(m.group(2))
        except Exception:
            pass

        if total_questions == 0:
            self.log.warning("[预扫描] 无法获取题目总数")
            return []

        self.log.info(f"[预扫描] 共 {total_questions} 题，通过答题卡逐题扫描...")
        unmatched = []
        seen = set()

        # 先打开答题卡
        try:
            page.evaluate("""() => {
                const btns = document.querySelectorAll('button, .mint-button, .van-button');
                for (const btn of btns) {
                    const txt = (btn.textContent || '').trim();
                    const r = btn.getBoundingClientRect();
                    if ((txt.includes('答题卡') || txt.includes('查看答题卡'))
                        && r.width > 0 && r.height > 0) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            time.sleep(1)
        except Exception:
            pass

        for q_num in range(1, total_questions + 1):
            # 通过答题卡跳转到指定题号
            try:
                # 确保答题卡打开
                sheet_visible = page.evaluate("""() => {
                    const sheet = document.querySelector('.sheet');
                    if (!sheet) return false;
                    const r = sheet.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }""")
                if not sheet_visible:
                    page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, .mint-button, .van-button');
                        for (const btn of btns) {
                            const txt = (btn.textContent || '').trim();
                            const r = btn.getBoundingClientRect();
                            if ((txt.includes('答题卡') || txt.includes('查看答题卡'))
                                && r.width > 0 && r.height > 0) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    time.sleep(0.5)

                jumped = page.evaluate("""(targetNum) => {
                    const idx = Number(targetNum) - 1;
                    if (!Number.isFinite(idx) || idx < 0) return false;
                    function isVisible(el) {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }
                    const items = Array.from(
                        document.querySelectorAll('.sheet .quest-indexs-list li')
                    ).filter(isVisible);
                    if (idx >= items.length) return false;
                    items[idx].click();
                    return true;
                }""", q_num)
                if not jumped:
                    self.log.debug(f"[预扫描] 无法跳转到第 {q_num} 题")
                    continue
                time.sleep(0.5)
            except Exception:
                continue

            # 读取当前题目
            try:
                stem = page.locator(SEL_QUEST_STEM).first
                if not stem.is_visible():
                    time.sleep(0.3)
                    if not stem.is_visible():
                        continue
            except Exception:
                continue

            raw_title = ""
            for sub_sel in [s.strip() for s in SEL_QUEST_STEM_SUB.split(",")]:
                try:
                    t_el = page.locator(sub_sel).first
                    if t_el.count() > 0 and t_el.is_visible():
                        raw_title = t_el.inner_text().strip()
                        break
                except Exception:
                    continue
            if not raw_title:
                try:
                    raw_title = stem.inner_text().strip()
                except Exception:
                    continue

            title = re.sub(r"^\s*\d+[\.、\s]+", "", raw_title)
            if not title or title in seen:
                continue
            seen.add(title)

            # 读取选项
            options = page.locator(SEL_QUEST_OPTIONS)
            options_count = options.count()
            opt_texts = []
            for j in range(options_count):
                try:
                    txt = options.nth(j).inner_text().strip()
                    txt = re.sub(r'^[A-Z][.\s、\n]+', '', txt)
                    opt_texts.append(txt)
                except Exception:
                    opt_texts.append("?")

            # 检查题库
            if not self._match_answer_in_bank(title):
                unmatched.append({
                    "title": title,
                    "options": opt_texts,
                })
                self.log.debug(f"[预扫描] 未匹配 #{q_num}: {title[:40]}")

        self.log.info(f"[预扫描] 完成，{len(unmatched)}/{len(seen)} 题未匹配")

        # 关闭答题卡弹窗，避免遮挡后续答题操作
        self._close_answer_sheet()

        return unmatched

    def _batch_select_unmatched(self, unmatched: list) -> None:
        """终端批量展示未匹配题目，让用户手动选择答案。"""
        if not unmatched:
            return

        selected_count = 0
        with _terminal_lock:
            print("\n" + "=" * 60)
            print(f"   【批量答题】 共 {len(unmatched)} 题需要手动选择")
            print("=" * 60)

            for i, q in enumerate(unmatched):
                print(f"\n   ── 第 {i + 1}/{len(unmatched)} 题 ──")
                print(f"   {q['title']}")
                print("   " + "-" * 50)
                for j, opt in enumerate(q["options"]):
                    print(f"     {j + 1}. {opt}")
                print("   " + "-" * 50)

                try:
                    raw = input("   请选择 (编号，多选用逗号分隔，Enter 跳过): ").strip()
                    if not raw:
                        print("   已跳过")
                        continue

                    indices = []
                    for n in re.split(r"[,\s，]+", raw):
                        if n.isdigit():
                            idx = int(n) - 1
                            if 0 <= idx < len(q["options"]):
                                indices.append(idx)

                    if not indices:
                        print("   无效输入，已跳过")
                        continue

                    new_item = {"optionList": [], "type": "手工输入"}
                    for j, opt_text in enumerate(q["options"]):
                        new_item["optionList"].append({
                            "content": opt_text,
                            "isCorrect": 1 if j in indices else 2,
                        })

                    target_key = q["title"]
                    c_title = clean_text(q["title"])
                    for existing_raw in list(self.answers.keys()):
                        if clean_text(existing_raw) == c_title:
                            target_key = existing_raw
                            break

                    self.answers[target_key] = new_item
                    labels = "/".join(chr(65 + idx) for idx in indices)
                    print(f"   ✓ 已记录: {labels}")
                    selected_count += 1

                except (EOFError, OSError):
                    print("\n   终端不可用，停止批量答题。")
                    break
                except Exception as e:
                    print(f"   出错: {e}")

            if hasattr(self, "_save_answers"):
                getattr(self, "_save_answers")()

            print("\n" + "=" * 60)
            print(f"   批量答题完成: 共 {len(unmatched)} 题，已答 {selected_count} 题")
            print("=" * 60 + "\n")

    def _do_answering(
        self,
        q_time: int,
        q_offset: int,
        rand: bool,
        rate_limit: int,
        fixed_total_questions: int = 0,
    ) -> bool:
        """具体答题执行。"""
        if not self._page:
            raise RuntimeError("Page is not initialized")
        page = self._page

        matched, total = 0, 0
        last_title = ""
        same_count = 0
        invalid_context_count = 0

        # 动态上限：从页面题目总数获取，最多 5000
        expected_total = fixed_total_questions if fixed_total_questions > 0 else 0
        if expected_total <= 0:
            expected_total = self._read_total_questions_from_indicator()
        max_questions = (
            min(max(expected_total * 10, expected_total + 20), 5000)
            if expected_total > 0
            else 500
        )

        # 非随机模式：先预扫描所有题目，让用户批量选择未匹配的
        if not rand:
            unmatched = self._pre_scan_unmatched()
            if unmatched:
                self._batch_select_unmatched(unmatched)
                # 回到第 1 题开始正式答题
                self._jump_via_answer_card("", "", 1)
                time.sleep(1)
            else:
                self.log.info("[预扫描] 所有题目均已匹配，直接开始答题")

        for q_index in range(max_questions):
            # ── 1. 清理遮挡层 ──
            if self._dismiss_blocking_overlays(total):
                break

            # ── 2. 处理验证码 ──
            if self._handle_captcha():
                continue

            # ── 3. 检查页面上下文 ──
            if self._is_in_context(PageContext.EXAM_RESULT):
                self.log.info("检测到已进入考试结果页，结束答题循环。")
                break

            if not self._is_in_context(PageContext.EXAM_QUESTION, PageContext.UNKNOWN):
                invalid_context_count += 1
                if invalid_context_count > 30:
                    self.log.warning("持续处于非答题上下文，强制退出。")
                    break
                time.sleep(1)
                continue
            invalid_context_count = 0

            # ── 4. 等待题目加载 ──
            stem = page.locator(SEL_QUEST_STEM).first
            if not stem.is_visible():
                if self._is_in_context(PageContext.EXAM_RESULT):
                    self.log.info("题目区域不可见，已切换到结果页。")
                    break
                stem_appeared = False
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        if stem.is_visible():
                            stem_appeared = True
                            break
                    except Exception:
                        pass
                if stem_appeared:
                    continue
                self.log.debug(f"[答题] stem 不可见 (q_index={q_index})")
                submit_area = page.locator(SEL_EXAM_SUBMIT_AREA)
                if submit_area.count() > 0 and submit_area.first.is_visible():
                    current_idx = self._get_current_question_index()
                    total_questions = (
                        expected_total
                        if expected_total > 0
                        else self._read_total_questions_from_indicator()
                    )
                    if total_questions > 0 and current_idx >= total_questions:
                        self.log.info("检测到已到最后一题，准备结束答题。")
                        break
                    self.log.debug("[答题] 交卷按钮可见，但尚未确认到末题，继续等待题目渲染")
                continue

            # ── 5. 读取题目（支持 ExamPage + QuestionPage 两种 DOM 结构）──
            raw_title = ""
            for sub_sel in [s.strip() for s in SEL_QUEST_STEM_SUB.split(",")]:
                try:
                    t_el = page.locator(sub_sel).first
                    if t_el.count() > 0 and t_el.is_visible():
                        raw_title = t_el.inner_text().strip()
                        break
                except Exception:
                    continue
            if not raw_title:
                try:
                    raw_title = stem.inner_text().strip()
                except Exception:
                    pass
            title = re.sub(r"^\s*\d+[\.、\s]+", "", raw_title)
            if not title:
                continue

            # 同题检测：连续看到同一题则尝试跳过
            if title == last_title:
                same_count += 1
                if same_count > 6:
                    if self._advance_to_next_question():
                        same_count = 0
                    elif self._jump_to_first_unfinished_question():
                        self.log.warning("[答题] 下一题推进失败，改为跳转到首个未作答题")
                        same_count = 0
                    else:
                        time.sleep(0.2)
                continue
            same_count = 0
            last_title = title

            options = page.locator(SEL_QUEST_OPTIONS)
            total += 1
            options_count = options.count()

            answer_item = self._match_answer_in_bank(title)
            correct_opts = self._extract_correct_options(answer_item)

            q_type = ""
            progress = ""
            try:
                q_type = page.locator(SEL_QUEST_CATEGORY).first.inner_text().strip()
            except Exception:
                pass
            try:
                progress = page.locator(SEL_QUEST_INDICATOR).first.inner_text().strip()
            except Exception:
                pass

            head = f"{q_type} {progress}".strip()
            prefix = f"{head} " if head else ""
            self.log.info(f"{prefix}{total}. {title}")

            # 收集选项文本
            opt_texts = []
            for i in range(options_count):
                try:
                    txt = options.nth(i).inner_text().strip()
                    txt = re.sub(r'^[A-Z][.\s、\n]+', '', txt)
                    opt_texts.append(txt)
                except Exception:
                    opt_texts.append("?")

            # ── 6. 选择答案 ──
            found = False
            if correct_opts:
                question_type = answer_item.get("type", 1) if answer_item else 1
                indices_to_click = [
                    self._find_option_index(opt, options, options_count)
                    for opt in correct_opts
                ]
                indices_to_click = [i for i in indices_to_click if i >= 0]

                if indices_to_click:
                    if self._handle_captcha():
                        continue

                    clicked = self._click_options_by_indices(
                        options, indices_to_click, question_type
                    )

                    # 验证选项是否真正被选中
                    if clicked > 0 and not self._verify_and_fix_selection(options, indices_to_click[0]):
                        if self._handle_captcha():
                            continue
                        self.log.warning("[答题] 选项未选中，尝试 JS 兜底")
                        self._click_options_by_indices(options, indices_to_click, question_type)
                        time.sleep(0.3)
                        # 最终兜底：确保至少选了一个选项
                        if not self._verify_and_fix_selection(options, indices_to_click[0]):
                            self._click_first_option()
                            self._verify_and_fix_selection(options, 0)

                    found = clicked > 0
                    if found:
                        matched += 1
                        labels = "/".join(chr(65 + i) for i in indices_to_click)
                        lines = [
                            f"    {' ✓' if i in indices_to_click else '  '} {chr(65 + i)}. {opt_texts[i]}"
                            for i in range(options_count)
                        ]
                        self.log.info(f"[匹配] 题库命中 → 已选: {labels}\n" + "\n".join(lines))
                        self._wait_and_advance(q_time, q_offset)
                    else:
                        self.log.warning("[答题] 选项点击失败，跳过本题")
                        self._wait_and_advance(q_time, q_offset)
                        continue
                else:
                    self.log.warning(
                        "[匹配失败] 题库有答案但无法在页面定位\n"
                        + f"  正确答案: {correct_opts}\n"
                        + "\n".join(f"    {chr(65 + i)}. {opt_texts[i]}" for i in range(options_count))
                    )
                    # 答案存在但无法定位 → 走兜底逻辑，不要卡住
                    found = False

            if not found:
                if self._handle_captcha():
                    continue

                if rand:
                    self._click_first_option()
                    self._verify_and_fix_selection(options, 0)
                    self.log.debug(
                        "[随机] 题库无答案，随机选 A\n"
                        + "\n".join(f"    {chr(65 + i)}. {opt_texts[i]}" for i in range(min(options_count, 6)))
                    )
                    self._wait_and_advance(q_time, q_offset)
                    continue

                self.log.warning(
                    "[缺失] 题库无此题\n"
                    + "\n".join(f"    {chr(65 + i)}. {opt_texts[i]}" for i in range(options_count))
                )

                if self._interactive_answering(title, options, options_count, opt_texts):
                    matched += 1
                    self._wait_and_advance(q_time, q_offset)
                    continue

                # 兜底：必须选一个答案再前进，否则 QuestionPage 会拦截
                self.log.info("[兜底] 随机选择，避免未作答拦截")
                self._click_first_option()
                self._verify_and_fix_selection(options, 0)
                self._wait_and_advance(q_time, q_offset)

        if self._page and self._page.is_closed():
            self.log.warning("[答题] 页面已关闭，判定答题未完成")
            return False

        match_rate = (matched / max(1, total)) * 100
        if expected_total > 0 and total < expected_total:
            self.log.warning(
                f"[答题] 提前结束: 仅处理 {total}/{expected_total} 题，判定为未完成"
            )
            return False
        self.log.info(
            f"答题结束: 总计 {total} 题，匹配 {matched} 题，匹配率 {match_rate:.2f}%"
        )
        return match_rate >= rate_limit or rand

    def _parse_exam_item_dom(self, exam_item) -> dict:
        """从考试列表项的 DOM 中直接提取考试信息。

        直接从页面 DOM 解析，不依赖 Vue 数据。当 Vue 数据不可用时
        作为兜底方案，确保考试及格线等信息始终可用。

        典型 DOM 结构:
        <li class="exam-item">
          <h3 class="exam-item-title">结课考试</h3>
          <section class="exam-item-content">
            <p><span class="exam-fail">记入考核</span>
                <span>合格分数<span class="exam-fail">80</span>分</span></p>
            <p>开放时间 2026-03-06 — 2026-05-31</p>
            <p><span class="exam-fail">未考试</span>
                <span>剩<span class="exam-fail">3</span>次机会</span></p>
          </section>
          <button class="exam-button">参加考试</button>
        </li>

        Returns:
            {
                "title": str,
                "pass_score": int | None,
                "status": str,
                "remaining_attempts": int | None,
                "counts_toward_grade": bool,
            }
        """
        result = {
            "title": "",
            "pass_score": None,
            "status": "",
            "remaining_attempts": None,
            "counts_toward_grade": False,
        }

        if not exam_item or not self._page:
            return result

        try:
            full_text = exam_item.inner_text().strip()

            # 标题
            title_el = exam_item.locator(SEL_EXAM_ITEM_TITLE).first
            if title_el.count() > 0:
                result["title"] = title_el.inner_text().strip()

            # 合格分数：匹配 "合格分数 80 分" 或 "合格分数80分"
            m = re.search(r"合格分数[:\s]*(\d+)\s*分", full_text)
            if m:
                result["pass_score"] = int(m.group(1))

            # 剩余次数：匹配 "剩 3 次机会" 或 "剩余3次"
            m = re.search(r"剩(?:余)?\s*(\d+)\s*次", full_text)
            if m:
                result["remaining_attempts"] = int(m.group(1))

            # 考试状态
            status_patterns = [
                (r"未考试", "未考试"),
                (r"已合格|已及格|考试通过|成绩合格", "已合格"),
                (r"未通过|不合格|未及格", "未通过"),
                (r"已考试|已完成", "已考试"),
            ]
            for pattern, label in status_patterns:
                if re.search(pattern, full_text):
                    result["status"] = label
                    break

            # 是否记入考核
            if "记入考核" in full_text:
                result["counts_toward_grade"] = True

            self.log.debug(
                f"[考试DOM] {result['title']}: "
                f"及格线={result['pass_score']}, "
                f"状态={result['status'] or '未知'}, "
                f"剩余={result['remaining_attempts']}次, "
                f"记入考核={result['counts_toward_grade']}"
            )

        except Exception as e:
            self.log.debug(f"[考试DOM] 解析异常: {e}")

        return result

    def _log_exam_page_diag(self) -> None:
        """输出考试启动流程的页面诊断信息。"""
        if not self._page:
            return
        try:
            diag = self._page.evaluate("""() => {
                const url = location.href;
                const hash = location.hash;

                // 检测 ExamPopup 状态
                const popup = document.querySelector('.popup, .popup-wrapper');
                const popupVisible = popup ? (popup.getBoundingClientRect().width > 0) : false;
                const popupBtn = document.querySelector('.popup-btn');
                const popupBtnText = popupBtn ? popupBtn.textContent.trim() : '';
                const popupBtnVisible = popupBtn ? (popupBtn.getBoundingClientRect().width > 0) : false;

                // 检测 van-dialog / mint-msgbox
                const dialogs = document.querySelectorAll('.van-dialog, .mint-msgbox');
                const dialogTexts = [];
                for (const d of dialogs) {
                    const rect = d.getBoundingClientRect();
                    const style = getComputedStyle(d);
                    if ((rect.width > 0 || rect.height > 0) && style.display !== 'none' && style.visibility !== 'hidden') {
                        dialogTexts.push((d.textContent || '').replace(/\\n/g, ' ').trim().substring(0, 80));
                    }
                }

                // 检测 isPreparing 锁
                const preparingEl = document.querySelector('[disabled], .is-preparing');

                // 可见按钮
                const btns = document.querySelectorAll('button, .popup-btn, .van-button, .mint-button');
                const visibleBtns = [];
                for (const btn of btns) {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        const txt = (btn.textContent || '').trim();
                        if (txt) visibleBtns.push(txt.substring(0, 25));
                    }
                }

                // 验证码 iframe
                const iframes = document.querySelectorAll('iframe');
                const captchaFrames = [];
                for (const f of iframes) {
                    const src = f.src || '';
                    if (src.includes('captcha') || src.includes('tcaptcha')) {
                        captchaFrames.push(src.substring(0, 80));
                    }
                }

                return {
                    url: url.substring(0, 120),
                    hash,
                    popupVisible,
                    popupBtnText,
                    popupBtnVisible,
                    dialogTexts,
                    visibleBtnCount: visibleBtns.length,
                    visibleBtns: visibleBtns.slice(0, 10),
                    captchaFrames,
                    bodyLen: document.body ? document.body.innerHTML.length : 0
                };
            }""")
            self.log.debug(
                f"[诊断] url={diag.get('url', '?')}\n"
                f"  popup: visible={diag.get('popupVisible')}, "
                f"btn='{diag.get('popupBtnText')}', btnVisible={diag.get('popupBtnVisible')}\n"
                f"  dialogs: {diag.get('dialogTexts', [])}\n"
                f"  visibleBtns({diag.get('visibleBtnCount', 0)}): "
                f"{diag.get('visibleBtns', [])}\n"
                f"  captchaFrames: {diag.get('captchaFrames', [])}"
            )
        except Exception as e:
            self.log.debug(f"[诊断] 页面诊断异常: {e}")

    def run_exam(
        self,
        exam_question_time: int,
        exam_question_time_offset: int,
        random_answer: bool,
        exam_mode: str,
        exam_submit_match_rate: int,
    ) -> None:
        """主考试流程。"""
        if TYPE_CHECKING:
            _self = cast(Any, self)
        else:
            _self = self

        random_answer = get_bool_value(random_answer)

        _self._merge_cloud_answers()
        _self._merge_history_answers()
        if exam_mode == "false":
            return

        if not self._page:
            raise RuntimeError("Page is not initialized")

        self.log.info("开始执行考试流程...")
        self._page.goto(
            f"{self.base_url}/#/learning-task-list", wait_until="domcontentloaded"
        )

        # 等待 SPA 渲染完成，否则可能拿到 0 个项目
        try:
            self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=10000)
        except Exception:
            self._page.reload(wait_until="domcontentloaded")
            try:
                self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=10000)
            except Exception:
                pass

        proj_count = self._page.locator(SEL_TASK_BLOCK).count()
        completed = set()

        for i in range(proj_count):
            self._page.goto(
                f"{self.base_url}/#/learning-task-list", wait_until="domcontentloaded"
            )
            try:
                self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=10000)
            except Exception:
                pass

            proj = self._page.locator(SEL_TASK_BLOCK).nth(i)
            title = proj.locator(SEL_TASK_BLOCK_TITLE).inner_text().strip()
            if title in completed:
                continue

            self.log.info(f"[考试] 正在检查项目：{title}")
            proj.click()
            if not self._handle_exam_intermediate_pages():
                self.log.warning(f"无法进入项目课件页: {title}")
                completed.add(title)
                continue

            try:
                exam_tab = self._page.locator(SEL_EXAM_TAB).first
                if exam_tab.count() > 0:
                    exam_tab.click(timeout=3000)
            except Exception:
                pass

            # 等待考试列表渲染完成
            try:
                self._page.wait_for_selector(SEL_EXAM_ITEM, timeout=5000)
            except Exception:
                pass

            items = self._page.locator(SEL_EXAM_ITEM)
            self.log.info(f"[考试] 找到 {items.count()} 个考试项")

            # 从 Vue 数据提取考试及格线
            overview = self._extract_project_overview()
            exam_pass_scores: Dict[str, int] = {}
            if overview and overview.get("exams"):
                for exam in overview["exams"]:
                    name = exam.get("name", "")
                    if name:
                        exam_pass_scores[name] = exam.get("passScore", 60)
                self.log.info(
                    f"[考试] 从 Vue 提取到 {len(exam_pass_scores)} 个考试的及格线"
                )
            else:
                self.log.info("[考试] Vue 数据不可用，将从 DOM 解析考试信息")

            # 兜底：从 DOM 解析所有考试项的信息
            dom_exam_info: Dict[str, dict] = {}
            for j in range(items.count()):
                it = items.nth(j)
                info = self._parse_exam_item_dom(it)
                name = info.get("title", "")
                if name:
                    dom_exam_info[name] = info
                    # DOM 及格线作为 Vue 数据的补充
                    if name not in exam_pass_scores and info.get("pass_score"):
                        exam_pass_scores[name] = info["pass_score"]
                        self.log.info(
                            f"[考试] DOM 解析到 '{name}' 的及格线: "
                            f"{info['pass_score']} 分"
                        )

            for j in range(items.count()):
                it = items.nth(j)
                exam_title_el = it.locator(SEL_EXAM_ITEM_TITLE).first
                exam_title_text = (
                    exam_title_el.inner_text().strip()
                    if exam_title_el.count() > 0
                    else "未知标题"
                )
                self.log.info(f"[考试] 检查子项 {j + 1}: {exam_title_text}")

                # 从 DOM 获取考试信息（状态、剩余次数等）
                dom_info = dom_exam_info.get(exam_title_text, {})
                dom_status = dom_info.get("status", "")
                dom_remaining = dom_info.get("remaining_attempts")

                # 检查是否已合格（同时检查 CSS 类和 DOM 文本）
                p_span = it.locator(SEL_EXAM_ITEM_PASS).first
                is_passed_by_dom = (
                    p_span.count() > 0 and p_span.is_visible()
                ) or dom_status == "已合格"
                if exam_mode == "true" and is_passed_by_dom:
                    self.log.info(f"[及格跳过] {exam_title_text} 已合格")
                    continue

                # 检查是否有剩余次数
                if dom_remaining is not None and dom_remaining <= 0:
                    self.log.warning(f"[考试] {exam_title_text} 无剩余次数，跳过")
                    continue

                join = it.locator(SEL_JOIN_BTN).first
                if not join.is_visible():
                    self.log.debug(f"[考试] 按钮未就绪，跳过 {exam_title_text}")
                    continue

                # 及格线：优先 Vue 数据，其次 DOM（已在上方预扫描中合并）
                pass_score = exam_pass_scores.get(exam_title_text, 60)
                self.log.info(
                    f"[考试] 及格线: {pass_score} 分"
                    + (f", 剩余: {dom_remaining} 次" if dom_remaining else "")
                    + (f", 状态: {dom_status}" if dom_status else "")
                )

                MAX_ATTEMPTS = 3
                final_score = 0
                passed = False
                locked_total_questions = 0
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    if attempt > 1:
                        self.log.info(
                            f"[考试] 第 {attempt}/{MAX_ATTEMPTS} 次重试: "
                            f"{exam_title_text}"
                        )
                        self._page.goto(
                            f"{self.base_url}/#/learning-task-list",
                            wait_until="domcontentloaded",
                        )
                        try:
                            self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=10000)
                        except Exception:
                            pass
                        # 兜底：移除可能残留的遮罩层
                        try:
                            for modal_sel in [".v-modal", ".van-overlay"]:
                                modal = self._page.locator(modal_sel).first
                                if modal.count() > 0 and modal.is_visible():
                                    modal.evaluate("el => el.style.display = 'none'")
                        except Exception:
                            pass
                        try:
                            task = self._page.locator(SEL_TASK_BLOCK).nth(i)
                            if task.count() > 0:
                                task.click(timeout=3000)
                        except Exception:
                            pass
                        if not self._handle_exam_intermediate_pages():
                            self.log.warning("重试: 无法进入项目课件页")
                            break
                        try:
                            exam_tab = self._page.locator(SEL_EXAM_TAB).first
                            if exam_tab.count() > 0:
                                exam_tab.click(timeout=3000)
                        except Exception:
                            pass

                    self.log.info(f"[答题] 尝试点击：{exam_title_text} (第{attempt}次)")
                    if not self._click_exam_join_button(exam_title_text):
                        self.log.warning(f"[答题] 未找到参加考试按钮：{exam_title_text}")
                        break

                    dialog_result = self._handle_exam_dialog(exam_mode)
                    if dialog_result == "return":
                        self.log.warning(f"[答题] 弹窗提示中止：{exam_title_text}")
                        break

                    self.log.info("[答题] 正在处理启动流程...")
                    start_time = time.time()
                    entered = False
                    captcha_handled = False
                    loop_count = 0
                    last_diag_time = 0.0
                    while time.time() - start_time < 40:
                        loop_count += 1
                        elapsed = time.time() - start_time
                        try:
                            # 首次 + 每 10 秒输出一次页面诊断
                            if loop_count == 1 or elapsed - last_diag_time >= 10:
                                self._log_exam_page_diag()
                                last_diag_time = elapsed

                            dialog_result = self._handle_exam_dialog(exam_mode)
                            if dialog_result == "return":
                                self.log.warning("[答题] 启动流程中弹窗提示中止")
                                break

                            if self._click_exam_start_popup():
                                time.sleep(1)
                            elif loop_count <= 3 or loop_count % 5 == 0:
                                try:
                                    btn_state = self._page.evaluate("""() => {
                                        const btns = document.querySelectorAll('.popup-btn, button, .van-button, .mint-button');
                                        const visible = [];
                                        for (const btn of btns) {
                                            const txt = (btn.textContent || '').trim();
                                            if (!txt) continue;
                                            const r = btn.getBoundingClientRect();
                                            if (r.width > 0 && r.height > 0) visible.push(txt.substring(0, 30));
                                        }
                                        return { visible_count: visible.length, visible_sample: visible.slice(0, 8) };
                                    }""")
                                    self.log.debug(
                                        f"[答题] 未找到开始考试入口"
                                        f" (可见按钮 {btn_state.get('visible_count')} 个:"
                                        f" {btn_state.get('visible_sample')})"
                                    )
                                except Exception as e:
                                    self.log.debug(f"[答题] 按钮诊断异常: {e}")

                            if not captcha_handled and has_captcha(
                                self._page, require_cscapt=False
                            ):
                                self.log.info(
                                    "[验证码] 检测到考试验证码，正在自动处理..."
                                )
                                if handle_tencent_captcha(
                                    self._page, self.log, require_cscapt=False
                                ):
                                    self.log.info("[验证码] 验证码处理成功")
                                    captcha_handled = True
                                else:
                                    self.log.warning("[验证码] 验证码处理失败，将重试")
                                continue

                            if self._is_in_context(PageContext.EXAM_QUESTION):
                                self.log.info("[答题] 已成功进入答题页面")
                                entered = True
                                break

                            if has_captcha(self._page, require_cscapt=False):
                                self.log.info(
                                    "[验证码] 考试过程中出现验证码，正在处理..."
                                )
                                handle_tencent_captcha(
                                    self._page, self.log, require_cscapt=False
                                )
                                continue

                            # 等待再进入下一轮探测，避免高频轮询
                            time.sleep(1)

                        except Exception as e:
                            self.log.debug(f"[探测过程异常] {e}")
                            time.sleep(1)

                    if not entered:
                        self.log.warning(
                            f"[答题] 启动流程超时 ({elapsed:.0f}s, 循环 {loop_count} 次)"
                        )

                    result_info = "进入考试页面超时"
                    if entered:
                        if locked_total_questions <= 0:
                            for _ in range(6):
                                locked_total_questions = (
                                    self._read_total_questions_from_indicator()
                                )
                                if locked_total_questions > 0:
                                    break
                                time.sleep(0.5)
                            if locked_total_questions > 0:
                                self.log.info(
                                    f"[答题] 本场考试总题数锁定为: {locked_total_questions}"
                                )
                            else:
                                self.log.warning("[答题] 未能锁定总题数，将按动态兜底流程处理")

                        try:
                            should_submit = self._do_answering(
                                q_time=exam_question_time,
                                q_offset=exam_question_time_offset,
                                rand=random_answer,
                                rate_limit=exam_submit_match_rate,
                                fixed_total_questions=locked_total_questions,
                            )
                        except Exception as e:
                            self.log.error(f"[考试异常] 答题过程中出错: {e}")
                            import traceback

                            self.log.debug(
                                f"[考试异常] 堆栈:\n{traceback.format_exc()}"
                            )
                            result_info = f"答题异常: {e}"
                            # 不退出浏览器，继续处理下一个考试
                            break

                        if should_submit:
                            unfinished_count = self._estimate_unfinished_question_count()
                            if unfinished_count and unfinished_count > 0:
                                self.log.warning(
                                    f"[答题] 检测到仍有 {unfinished_count} 题未作答，先补答再交卷"
                                )
                                if self._jump_to_first_unfinished_question():
                                    should_submit = self._do_answering(
                                        q_time=exam_question_time,
                                        q_offset=exam_question_time_offset,
                                        rand=random_answer,
                                        rate_limit=exam_submit_match_rate,
                                        fixed_total_questions=locked_total_questions,
                                    )
                                    unfinished_count = self._estimate_unfinished_question_count()
                                else:
                                    should_submit = False
                                    result_info = f"未作答题仍存在({unfinished_count}题)"

                            if should_submit and unfinished_count and unfinished_count > 0:
                                should_submit = False
                                result_info = f"未作答题仍存在({unfinished_count}题)"

                        if should_submit:
                            self.log.info(f"答题结束，准备交卷：{title}")
                            try:
                                result_info = self._submit_exam()
                                if self._is_submit_blocked_message(result_info):
                                    self.log.warning(
                                        "[交卷] 仍被未作答拦截，尝试恢复后继续补答一次"
                                    )
                                    if self._recover_from_unfinished_prompt():
                                        should_submit = self._do_answering(
                                            q_time=exam_question_time,
                                            q_offset=exam_question_time_offset,
                                            rand=random_answer,
                                            rate_limit=exam_submit_match_rate,
                                            fixed_total_questions=locked_total_questions,
                                        )
                                        if should_submit:
                                            result_info = self._submit_exam()
                                    else:
                                        result_info = "交卷失败: 未作答拦截且恢复失败"
                                if "页面已关闭" in result_info:
                                    self.log.error("[交卷] 页面已关闭，结束当前考试重试")
                                    break
                            except Exception as e:
                                self.log.error(f"[考试异常] 交卷过程中出错: {e}")
                                import traceback

                                self.log.debug(
                                    f"[考试异常] 堆栈:\n{traceback.format_exc()}"
                                )
                                result_info = f"交卷异常: {e}"
                                # 不退出浏览器
                        else:
                            if random_answer:
                                self.log.warning(f"答题流程未完成，放弃交卷：{title}")
                            else:
                                self.log.warning(
                                    f"匹配率过低且未开启随机，放弃交卷：{title}"
                                )
                            result_info = "放弃提交"
                            break

                    # 从结果信息中提取分数和通过状态
                    final_score = self._submit_manager().extract_score_from_result_text(
                        result_info
                    )
                    # 从弹窗文本判断是否通过
                    popup_passed = self._submit_manager().is_popup_passed(result_info)

                    self.log.info(
                        f"[考试] {exam_title_text} 第{attempt}次得分: "
                        f"{final_score} 分 (及格线: {pass_score} 分)"
                        + (" [弹窗判定: 通过]" if popup_passed else "")
                    )

                    if final_score >= pass_score and final_score > 0:
                        self.log.info(f"[考试] {exam_title_text} 考试通过！")
                        passed = True
                        break
                    elif popup_passed and final_score == 0:
                        # 弹窗显示通过但未提取到分数，信任弹窗
                        self.log.info(
                            f"[考试] {exam_title_text} 弹窗显示通过，判定合格"
                        )
                        passed = True
                        final_score = pass_score
                        break
                    elif final_score > 0:
                        self.log.warning(
                            f"[考试] {exam_title_text} 未通过 "
                            f"({final_score} < {pass_score})"
                        )

                self._page.goto(
                    f"{self.base_url}/#/learning-task-list",
                    wait_until="domcontentloaded",
                )
                try:
                    self._page.wait_for_selector(SEL_TASK_BLOCK, timeout=10000)
                except Exception:
                    pass

                remain_times = "未知"
                try:
                    proj_item = (
                        self._page.locator(SEL_TASK_BLOCK)
                        .filter(
                            has=self._page.locator(SEL_TASK_BLOCK_TITLE).filter(
                                has_text=re.compile(f"^{re.escape(title)}$")
                            )
                        )
                        .first
                    )
                    if proj_item.count() == 0:
                        proj_item = (
                            self._page.locator(SEL_TASK_BLOCK)
                            .filter(has_text=title)
                            .first
                        )
                    if proj_item.count() > 0 and proj_item.is_visible():
                        txt = proj_item.inner_text().strip()
                        rt_m = re.search(r"(?:剩余机会|剩|机会).*?(\d+)", txt)
                        if rt_m:
                            remain_times = rt_m.group(1)
                except Exception:
                    pass

                self.log.info(f"【考试报告】项目：{title} | 子项：{exam_title_text}")
                self.log.info(f"   - 最终得分：{final_score} 分")
                self.log.info(f"   - 合格标准：{pass_score} 分")
                self.log.info(f"   - 是否通过：{'是' if passed else '否'}")
                self.log.info(f"   - 剩余机会：{remain_times} 次")

                break

            completed.add(title)
