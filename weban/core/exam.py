import logging
import random
import re
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, cast

from playwright._impl._errors import TargetClosedError

from weban.app.runtime import clean_text, ignore_symbols

from .base import BaseMixin, PageContext
from .captcha import handle_tencent_captcha, has_captcha
from .const import (
    SEL_COURSE_LIST_MARKERS,
    SEL_EXAM_CONFIRM_SHEET,
    SEL_EXAM_ITEM,
    SEL_EXAM_ITEM_PASS,
    SEL_EXAM_ITEM_TITLE,
    SEL_EXAM_PREPARE_POPUPS,
    SEL_EXAM_RESULT_SCORE,
    SEL_EXAM_SHEET,
    SEL_EXAM_SUBMIT_AREA,
    SEL_EXAM_TAB,
    SEL_JOIN_BTN,
    SEL_QUEST_CATEGORY,
    SEL_QUEST_INDICATOR,
    SEL_QUEST_OPTIONS,
    SEL_QUEST_STEM,
    SEL_QUEST_STEM_SUB,
    SEL_TASK_BLOCK,
    SEL_TASK_BLOCK_TITLE,
)

_terminal_lock = threading.Lock()
logger = logging.getLogger(__name__)


class ExamMixin(BaseMixin):
    """在线考试流程 Mixin。"""

    if TYPE_CHECKING:
        import logging as _logging
        from typing import Union as _Union

        from playwright.sync_api import Browser, BrowserContext, Page, Playwright

        from .browser import BrowserConfig

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

    def _match_answer_in_bank(self, title: str) -> dict | None:
        """从题库中匹配答案。

        Args:
            title: 题目标题

        Returns:
            匹配到的答案项，或 None
        """
        ctitle = ignore_symbols(title)
        for k, v in self.answers.items():
            if ignore_symbols(k) == ctitle:
                return v
        return None

    def _extract_correct_options(self, answer_item: dict | None) -> List[str]:
        """从答案项中提取正确选项文本列表。

        Args:
            answer_item: 题库中的答案项

        Returns:
            正确选项文本列表
        """
        if not answer_item or "optionList" not in answer_item:
            return []
        return [
            opt["content"]
            for opt in answer_item["optionList"]
            if opt.get("isCorrect") == 1
        ]

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
        cleaned_ans = ignore_symbols(option_text)
        for i in range(options_count):
            try:
                page_opt_text = ignore_symbols(page_options.nth(i).inner_text())
                if cleaned_ans and page_opt_text:
                    if cleaned_ans in page_opt_text or page_opt_text in cleaned_ans:
                        return i
            except Exception:
                continue
        return -1

    def _click_options_by_indices(
        self, page_options: Any, indices: List[int], question_type: int = 1
    ) -> int:
        """直接 JS click 选项，不经过 Playwright。"""
        if not self._page:
            return 0
        try:
            selectors = SEL_QUEST_OPTIONS
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
        return 0

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
                stem = self._page.locator(SEL_QUEST_STEM).first
                if stem.count() > 0 and stem.is_visible():
                    raw = stem.inner_text().strip()
                    current_title = re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()
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

    def _advance_to_next_question(self) -> bool:
        """直接 JS click 下一题按钮。"""
        if not self._page:
            return False

        prev_title = ""
        prev_indicator = ""
        try:
            stem = self._page.locator(SEL_QUEST_STEM).first
            if stem.count() > 0 and stem.is_visible():
                raw = stem.inner_text().strip()
                prev_title = re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()

            ind = self._page.locator(SEL_QUEST_INDICATOR).first
            if ind.count() > 0 and ind.is_visible():
                prev_indicator = ind.inner_text().strip()
        except Exception:
            pass

        # JS 直接 click 下一题按钮
        try:
            clicked = self._page.evaluate("""() => {
                const sels = [
                    '.btn-start', '.btn-next', '.btn-ce', '.btn-aq', '.btn-at',
                    '.btn-af', '.btn-base', '.back-list',
                    'button', '.mint-button', '.van-button'
                ];
                for (const sel of sels) {
                    const els = document.querySelectorAll(sel);
                    for (const btn of els) {
                        const txt = (btn.textContent || '').trim();
                        const r = btn.getBoundingClientRect();
                        if (txt.includes('下一题') && r.width > 0 && r.height > 0) {
                            btn.click();
                            return sel;
                        }
                    }
                }
                return null;
            }""")
            if clicked and self._wait_for_question_change(prev_title, prev_indicator, 8):
                return True
        except Exception:
            pass

        return self._jump_via_answer_card(prev_title, prev_indicator)

    def _jump_via_answer_card(self, prev_title: str, prev_indicator: str) -> bool:
        """通过答题卡 JS click 跳转到下一题。"""
        if not self._page:
            return False

        current_idx = self._get_current_question_index()
        next_idx = current_idx + 1

        try:
            result = self._page.evaluate("""(nextIdx) => {
                // 打开答题卡
                const cardSels = [
                    'button', '.mint-button', '.van-button'
                ];
                let opened = false;
                for (const sel of cardSels) {
                    const els = document.querySelectorAll(sel);
                    for (const btn of els) {
                        const txt = (btn.textContent || '').trim();
                        const r = btn.getBoundingClientRect();
                        if ((txt.includes('答题卡') || txt.includes('查看答题卡'))
                            && r.width > 0 && r.height > 0) {
                            btn.click();
                            opened = true;
                            break;
                        }
                    }
                    if (opened) break;
                }
                if (!opened) return false;

                // 点击目标题号
                const nums = [String(nextIdx), String(nextIdx).padStart(2, '0')];
                for (const num of nums) {
                    const items = document.querySelectorAll('.sheet .quest-indexs-list li');
                    for (const item of items) {
                        const span = item.querySelector('span');
                        if (span && span.textContent.trim() === num) {
                            item.click();
                            return true;
                        }
                    }
                }
                return false;
            }""", next_idx)

            if result and self._wait_for_question_change(prev_title, prev_indicator, 6):
                return True
        except Exception:
            pass

        return False

    # ========================================================================
    # 提交流程
    # ========================================================================

    def _dismiss_result_popup(self) -> str:
        """直接 JS 处理成绩弹窗，返回分值文本。"""
        if not self._page:
            return ""

        score_text = ""
        deadline = time.time() + 20

        while time.time() < deadline:
            result = self._page.evaluate("""() => {
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }

                // Mint UI MessageBox
                const msgbox = document.querySelector('.mint-msgbox-wrapper');
                if (isVisible(msgbox)) {
                    const msg = msgbox.querySelector('.mint-msgbox-message');
                    const score = msg ? msg.textContent.trim() : '';
                    const btn = msgbox.querySelector('.mint-msgbox-confirm, .mint-msgbox-btn');
                    if (btn) { btn.click(); return { type: 'mint', score, clicked: true }; }
                    return { type: 'mint', score, clicked: false };
                }

                // confirm-sheet
                const sheet = document.querySelector('.confirm-sheet');
                if (isVisible(sheet)) {
                    let score = '';
                    for (const sel of ['.confirm-message', '.sheet-message', '.result-score', '.score']) {
                        const el = sheet.querySelector(sel);
                        if (el && el.textContent.trim()) { score = el.textContent.trim(); break; }
                    }
                    // 优先「确 认」，其次「交卷」
                    const btns = sheet.querySelectorAll('button');
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim();
                        if (txt.includes('确 认') || txt === '确认') {
                            btn.click(); return { type: 'confirm-sheet', score, clicked: true };
                        }
                    }
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim();
                        if (txt.includes('交卷')) {
                            btn.click(); return { type: 'confirm-sheet-submit', score, clicked: true };
                        }
                    }
                    // 兜底
                    if (btns.length > 0) { btns[0].click(); return { type: 'confirm-sheet-fallback', score, clicked: true }; }
                }

                // van-dialog
                const vdialog = document.querySelector('.van-dialog');
                if (isVisible(vdialog)) {
                    const msg = vdialog.querySelector('.van-dialog__message');
                    const score = msg ? msg.textContent.trim() : '';
                    const btn = vdialog.querySelector('.van-dialog__confirm');
                    if (btn) { btn.click(); return { type: 'van-dialog', score, clicked: true }; }
                }

                // van-popup
                const vpopup = document.querySelector('.van-popup, .van-popup--center');
                if (isVisible(vpopup)) {
                    const score = vpopup.textContent.trim().substring(0, 200);
                    for (const sel of ['button']) {
                        const btns = vpopup.querySelectorAll(sel);
                        for (const btn of btns) {
                            const txt = (btn.textContent || '').trim();
                            if (txt.includes('确定') || txt.includes('确认')) {
                                btn.click(); return { type: 'van-popup', score, clicked: true };
                            }
                        }
                    }
                    const overlay = document.querySelector('.van-overlay');
                    if (overlay) { overlay.click(); return { type: 'van-popup-overlay', score, clicked: true }; }
                }

                return null;
            }""")

            if result:
                score_text = result.get("score", "") or score_text
                popup_type = result.get("type", "")
                clicked = result.get("clicked", False)

                if score_text:
                    self.log.info(f"[成绩弹窗] {popup_type}: {score_text}")

                if clicked:
                    if popup_type == "confirm-sheet-submit":
                        time.sleep(3)
                        continue
                    return score_text

            time.sleep(0.5)

        # 兜底：隐藏残留遮罩层
        try:
            self._page.evaluate("""() => {
                document.querySelectorAll('.v-modal, .van-overlay').forEach(el => el.style.display = 'none');
            }""")
        except Exception:
            pass

        return score_text

    def _submit_exam(self) -> str:
        """交卷并返回结果信息。"""
        if not self._page:
            raise RuntimeError("Page is not initialized")

        # 如果已经在结算页（之前点过交卷），直接读分
        try:
            score_el = self._page.locator(SEL_EXAM_RESULT_SCORE).first
            if score_el.count() > 0 and score_el.is_visible():
                txt = score_el.inner_text().strip()
                if txt:
                    self.log.info(f"[提交流程] 已在结算页，得分: {txt}")
                    return f"已在结算页: {txt}"
        except Exception:
            pass

        # 执行逐级交卷按钮点击
        self._click_submit_buttons()

        # 等待并关闭成绩弹窗
        popup_score = self._dismiss_result_popup()
        if popup_score:
            result_text = f"交卷完成: {popup_score}"
        else:
            result_text = "交卷完成（未捕获弹窗分数）"

        # 弹窗关闭后尝试从页面读分
        try:
            score_el = self._page.locator(SEL_EXAM_RESULT_SCORE).first
            if score_el.count() > 0 and score_el.is_visible(timeout=3000):
                final_score = score_el.inner_text().strip()
                if final_score:
                    result_text = f"交卷完成: {final_score}"
                    self.log.info(f"[提交流程] 成功捕获分值：{final_score}")
        except Exception:
            pass

        return result_text

    def _click_submit_buttons(self) -> str:
        """直接 JS click 交卷按钮，多级弹窗流程。"""
        assert self._page is not None

        def _js_click_button(keywords: list[str], scope: str = "") -> bool:
            """在指定范围内 JS click 包含关键词的按钮。"""
            try:
                result = self._page.evaluate("""(args) => {
                    const [kws, scope] = args;
                    const root = scope ? document.querySelector(scope) : document;
                    if (!root) return null;
                    const btns = root.querySelectorAll('button, .mint-button, .van-button');
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim();
                        if (!txt) continue;
                        const r = btn.getBoundingClientRect();
                        if (r.width === 0 && r.height === 0) continue;
                        if (kws.some(kw => txt.includes(kw))
                            && !['取消', '退出', '暂不', '返回', '继续考试'].some(s => txt.includes(s))) {
                            btn.click();
                            return txt;
                        }
                    }
                    return null;
                }""", [keywords, scope])
                if result:
                    self.log.info(f"[提交流程] JS click: {result}")
                    return True
            except Exception:
                pass
            return False

        def _wait_element(selector: str, timeout: float = 5.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    el = self._page.locator(selector).first
                    if el.count() > 0 and el.is_visible():
                        return True
                except Exception:
                    pass
                time.sleep(0.3)
            return False

        # Step 0: 直接交卷按钮（不在 sheet 内）
        try:
            direct = self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button, .mint-button');
                for (const btn of btns) {
                    const txt = (btn.textContent || '').trim();
                    if (!txt) continue;
                    const r = btn.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    if (txt.includes('交卷') || txt.includes('确认交卷')) {
                        const parent = btn.closest('.sheet, .confirm-sheet');
                        if (!parent) { btn.click(); return txt; }
                    }
                }
                return null;
            }""")
            if direct:
                self.log.info(f"[提交流程] 直接交卷: {direct}")
                time.sleep(3)
                return ""
        except Exception:
            pass

        # Step 1: 打开答题卡
        self.log.debug("[提交流程] Step 1: 打开答题卡")
        if not _wait_element(SEL_EXAM_SHEET, timeout=1.0):
            _js_click_button(["答题卡", "查看答题卡"])
            if not _wait_element(SEL_EXAM_SHEET, timeout=5.0):
                self.log.warning("[提交流程] 答题卡弹窗未出现，尝试直接交卷...")
                _js_click_button(["交卷", "提交", "确认"])
                time.sleep(3)
                return ""

        # Step 2: Sheet 内交卷
        self.log.debug("[提交流程] Step 2: Sheet 内交卷")
        _js_click_button(["交卷"], ".sheet")

        # Step 3: 确认弹窗
        self.log.debug("[提交流程] Step 3: 确认弹窗")

        # 检测"请作答"类拦截提示
        try:
            page_text = self._page.evaluate("""() => {
                const body = document.body;
                return body ? body.innerText.substring(0, 2000) : '';
            }""")
            if page_text and "请作答" in page_text:
                self.log.warning("[提交流程] 检测到'请作答'提示，存在未作答题目")
        except Exception:
            pass

        if not _wait_element(SEL_EXAM_CONFIRM_SHEET, timeout=8.0):
            if self._is_in_context(PageContext.EXAM_RESULT):
                self.log.info("[提交流程] 已在结果页")
                return ""
            # 再次检测是否有拦截提示
            try:
                toast = self._page.evaluate("""() => {
                    const toasts = document.querySelectorAll('.van-toast, .mint-toast, .van-popup');
                    for (const t of toasts) {
                        const r = t.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            const txt = (t.textContent || '').trim();
                            if (txt.includes('请作答')) return txt;
                        }
                    }
                    return null;
                }""")
                if toast:
                    self.log.warning(f"[提交流程] 页面拦截: {toast}")
                    return ""
            except Exception:
                pass
            time.sleep(2)
            if self._is_in_context(PageContext.EXAM_RESULT):
                return ""
        else:
            _js_click_button(["交卷", "确 认", "确认", "提交"], ".confirm-sheet")

        return ""

    # ========================================================================
    # 主考试流程
    # ========================================================================

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """直接 JS 处理考试弹窗。"""
        if not self._page:
            raise RuntimeError("Page is not initialized")

        result = self._page.evaluate(f"""(examMode) => {{
            {self._vue_app_finder_js()}
            const dialogs = document.querySelectorAll('.van-dialog, .mint-msgbox, .mint-toast');
            const actions = [];
            for (const d of dialogs) {{
                // position:fixed 元素 offsetParent 为 null，改用尺寸判断可见性
                const rect = d.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const style = getComputedStyle(d);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const text = (d.textContent || '').replace(/\\n/g, ' ').trim();

                if (/未开放|已关闭|不允许|暂无考试机会|次数已用|课程学习未完成/.test(text)) {{
                    const btn = d.querySelector('button');
                    if (btn) btn.click();
                    return {{ action: 'return', text }};
                }}
                // 未提交/清除数据 → 必须点"确认"而非"取消"
                if (/未提交|重新进入|继续考试|清除/.test(text)) {{
                    let clicked = false;
                    for (const sel of ['.van-dialog__confirm', '.mint-msgbox-confirm']) {{
                        const el = d.querySelector(sel);
                        if (el) {{ el.click(); clicked = true; break; }}
                    }}
                    if (!clicked) {{
                        for (const btn of d.querySelectorAll('button')) {{
                            if (/确[认订]/.test((btn.textContent || '').trim())) {{
                                btn.click(); clicked = true; break;
                            }}
                        }}
                    }}
                    if (!clicked) {{
                        const btn = d.querySelector('button');
                        if (btn) btn.click();
                    }}
                    actions.push({{ action: 'continue', text }});
                    continue;
                }}
                const isPassed = /已合格|已及格|考试通过/.test(text)
                    || (/合格/.test(text) && /最高成绩/.test(text));
                if (isPassed && examMode === 'true') {{
                    const btn = d.querySelector('button');
                    if (btn) btn.click();
                    return {{ action: 'return', text }};
                }}
                const btn = d.querySelector('button');
                if (btn) btn.click();
            }}
            // ExamPopup.vue 的“开始考试”按钮内部先创建 TencentCaptcha；
            // 自动化环境中验证码脚本未必存在，直接调用 CourseIndex.onPop()
            // 等价于通过弹窗校验后进入 ExamPage，避免按钮点击后无响应。
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
            return actions.length > 0 ? actions[0] : {{ action: 'continue' }};
        }}""", exam_mode)

        if not result:
            return "continue"

        action = result.get("action", "continue")
        text = result.get("text", "")
        if action == "return":
            self.log.warning(f"无法考试: {text}")
        elif text:
            self.log.info(f"流程提示: {text}")
        return action

    def _click_exam_join_button(self, exam_title: str) -> bool:
        """点击当前考试卡片内的“参加考试”，避免多考试时误点第一项。"""
        if not self._page:
            return False
        try:
            clicked = self._page.evaluate("""(examTitle) => {
                const cards = document.querySelectorAll('.exam-item');
                for (const card of cards) {
                    const titleEl = card.querySelector('.exam-item-title, .exam-info h3');
                    const title = (titleEl?.textContent || '').trim();
                    if (examTitle && title && !title.includes(examTitle) && !examTitle.includes(title)) {
                        continue;
                    }
                    const btns = card.querySelectorAll('button.exam-button, .exam-button, button');
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim();
                        const r = btn.getBoundingClientRect();
                        if (txt.includes('参加考试') && r.width > 0 && r.height > 0) {
                            btn.click();
                            return { clicked: true, title, text: txt };
                        }
                    }
                }
                return { clicked: false };
            }""", exam_title)
            if clicked and clicked.get("clicked"):
                self.log.info(
                    f"[答题] JS click 当前考试卡片按钮: "
                    f"{clicked.get('title') or exam_title}"
                )
                return True
        except Exception as e:
            self.log.debug(f"[答题] 当前考试卡片按钮点击异常: {e}")
        return False

    def _click_exam_start_popup(self) -> bool:
        """处理 ExamPopup 的开始按钮，优先调用 Vue onPop 绕过失效 DOM 点击。"""
        if not self._page:
            return False
        try:
            state = self._page.evaluate("""() => {
                %s
                const popup = document.querySelector('.popup, .popup-wrapper');
                const visible = popup && popup.getBoundingClientRect().width > 0
                    && popup.getBoundingClientRect().height > 0;
                if (!visible) return { clicked: false, reason: 'no-popup' };

                const app = findVueProxy(['onPop']);
                if (app && typeof app.onPop === 'function') {
                    app.onPop();
                    return { clicked: true, method: 'vue.onPop' };
                }

                const btns = document.querySelectorAll('.popup-btn, button');
                for (const btn of btns) {
                    const txt = (btn.textContent || '').trim();
                    const r = btn.getBoundingClientRect();
                    if (/开始考试/.test(txt) && r.width > 0 && r.height > 0) {
                        btn.click();
                        return { clicked: true, method: 'dom-click', text: txt };
                    }
                }
                return { clicked: false, reason: 'no-start-button' };
            }""" % self._vue_app_finder_js())
            if state and state.get("clicked"):
                self.log.info(f"[答题] 启动考试: {state.get('method')}")
                return True
        except Exception as e:
            self.log.debug(f"[答题] 启动弹窗处理异常: {e}")
        return False

    def _handle_exam_intermediate_pages(self) -> bool:
        """直接 JS 处理进入考试前的中间页面。"""
        assert self._page is not None
        for _ in range(6):
            if self._page.locator(SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return True

            try:
                self._page.evaluate("""() => {
                    // 勾选同意框
                    const cb = document.querySelector('#agree, .agree-checkbox input, input[type="checkbox"]');
                    if (cb && !cb.checked) cb.click();
                    // 下一步按钮
                    const nextBtn = document.querySelector('button');
                    if (nextBtn && /下一步|同意/.test(nextBtn.textContent)) nextBtn.click();
                    // 确认按钮
                    const okBtns = document.querySelectorAll('button');
                    for (const btn of okBtns) {
                        const r = btn.getBoundingClientRect();
                        if (/确认|确定/.test(btn.textContent) && r.width > 0 && r.height > 0) {
                            btn.click(); break;
                        }
                    }
                    // 子项目
                    const sub = document.querySelector('.img-text-block, .task-block');
                    if (sub) sub.click();
                }""")
            except Exception:
                pass
            time.sleep(0.5)

        return self._page.locator(SEL_EXAM_TAB).count() > 0

    def _interactive_answering(
        self, title: str, options: Any, options_count: int, opt_texts: list[str]
    ) -> bool:
        """多线程安全的手工干预命令行交互。"""

        if not sys.stdin or not sys.stdin.isatty():
            self.log.warning("当前环境不可交互，跳过终端人工答题。")
            return False

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

            except Exception as e:
                print(f"   交互出错: {e}")
                return False
            finally:
                print("=" * 62 + "\n")

    def _do_answering(
        self, q_time: int, q_offset: int, rand: bool, rate_limit: int
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
        max_questions = 500
        try:
            indicator = page.locator(SEL_QUEST_INDICATOR).first
            if indicator.count() > 0 and indicator.is_visible():
                m = re.search(r"(\d+)\s*/\s*(\d+)", indicator.inner_text())
                if m:
                    max_questions = min(int(m.group(2)), 5000) + 10
        except Exception:
            pass

        for q_index in range(max_questions):
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

            try:
                popups = page.locator(SEL_EXAM_PREPARE_POPUPS)
                popup_detected = False
                for k in range(popups.count()):
                    p = popups.nth(k)
                    if p.is_visible():
                        txt = p.inner_text()
                        if any(
                            x in txt for x in ["未作答", "共", "道题", "完成", "交卷"]
                        ):
                            self.log.info("探测到结算/交卷层")
                            popup_detected = True
                            break
                        break
                if popup_detected:
                    break
            except TargetClosedError:
                self.log.warning("检测到页面已关闭，提前结束。")
                break

            stem = page.locator(SEL_QUEST_STEM).first
            if not stem.is_visible():
                if self._is_in_context(PageContext.EXAM_RESULT):
                    self.log.info("题目区域不可见，已切换到结果页。")
                    break

                submit_area = page.locator(SEL_EXAM_SUBMIT_AREA)
                if submit_area.count() > 0 and submit_area.first.is_visible():
                    self.log.info("检测到交卷按钮，准备结束。")
                    break
                continue

            raw_title = ""
            for sub_sel in [s.strip() for s in SEL_QUEST_STEM_SUB.split(",")]:
                t_el = stem.locator(sub_sel).first
                if t_el.count() > 0 and t_el.is_visible():
                    raw_title = t_el.inner_text().strip()
                    break

            if not raw_title:
                raw_title = stem.inner_text().strip()

            title = re.sub(r"^\s*\d+[\.、\s]+", "", raw_title)
            if not title:
                continue

            if title == last_title:
                same_count += 1
                if same_count > 6:
                    if self._advance_to_next_question():
                        same_count = 0
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
            q_label = f"{prefix}{total}. {title}"
            self.log.info(q_label)

            # 收集所有选项文本
            opt_texts = []
            for i in range(options_count):
                try:
                    txt = options.nth(i).inner_text().strip()
                    # 去除选项文本中自带的字母标签（如 "A\n..." → "..."）
                    txt = re.sub(r'^[A-Z][.\s、\n]+', '', txt)
                    opt_texts.append(txt)
                except Exception:
                    opt_texts.append("?")

            found = False
            if correct_opts:
                question_type = answer_item.get("type", 1) if answer_item else 1
                indices_to_click = []

                for opt_text in correct_opts:
                    idx = self._find_option_index(opt_text, options, options_count)
                    if idx >= 0:
                        indices_to_click.append(idx)

                if indices_to_click:
                    clicked = self._click_options_by_indices(
                        options, indices_to_click, question_type
                    )
                    found = clicked > 0
                    matched += 1

                    # 一次性输出所有选项及匹配结果
                    lines = []
                    for i in range(options_count):
                        label = chr(65 + i)
                        marker = " ✓" if i in indices_to_click else "  "
                        lines.append(f"    {marker} {label}. {opt_texts[i]}")
                    self.log.info(
                        f"[匹配] 题库命中 → 已选: "
                        f"{'/'.join(chr(65 + i) for i in indices_to_click)}\n"
                        + "\n".join(lines)
                    )

                    wait_time = random.randint(q_time, q_time + q_offset)
                    if question_type == 2 and clicked > 1:
                        wait_time = max(wait_time, 3)
                    time.sleep(wait_time)

                    self._advance_to_next_question()
                    time.sleep(0.5)
                else:
                    self.log.warning(
                        "[匹配失败] 题库有答案但无法在页面定位\n"
                        + f"  正确答案: {correct_opts}\n"
                        + "\n".join(
                            f"    {chr(65 + i)}. {opt_texts[i]}"
                            for i in range(options_count)
                        )
                    )

            if not found and not correct_opts:
                if rand:
                    try:
                        self._page.evaluate(
                            f"""() => {{
                                const opts = document.querySelectorAll('{SEL_QUEST_OPTIONS}');
                                if (opts.length > 0) opts[0].click();
                            }}"""
                        )
                    except Exception:
                        pass
                    # 输出选项供参考
                    self.log.debug(
                        "[随机] 题库无答案，随机选 A\n"
                        + "\n".join(
                            f"    {chr(65 + i)}. {opt_texts[i]}"
                            for i in range(min(options_count, 6))
                        )
                    )
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    self._advance_to_next_question()
                    continue

                self.log.warning(
                    "[缺失] 题库无此题\n"
                    + "\n".join(
                        f"    {chr(65 + i)}. {opt_texts[i]}"
                        for i in range(options_count)
                    )
                )

                # 优先尝试手工交互
                if self._interactive_answering(title, options, options_count, []):
                    matched += 1
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    self._advance_to_next_question()
                    continue

                # 兜底：随机选一个，避免页面拦截"请作答当前题目"
                self.log.info("[兜底] 随机选择，避免未作答拦截")
                try:
                    self._page.evaluate(
                        f"""() => {{
                            const opts = document.querySelectorAll('{SEL_QUEST_OPTIONS}');
                            if (opts.length > 0) opts[0].click();
                        }}"""
                    )
                except Exception:
                    pass
                time.sleep(random.randint(q_time, q_time + q_offset))
                self._advance_to_next_question()

        match_rate = (matched / max(1, total)) * 100
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
                self._page.evaluate("""() => {
                    const tabs = document.querySelectorAll('.van-tab');
                    for (const tab of tabs) {
                        if (/在线考试/.test(tab.textContent)) { tab.click(); return; }
                    }
                }""")
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
                        self._page.evaluate(f"""() => {{
                            const blocks = document.querySelectorAll('.task-block');
                            if ({i} < blocks.length) blocks[{i}].click();
                        }}""")
                        if not self._handle_exam_intermediate_pages():
                            self.log.warning("重试: 无法进入项目课件页")
                            break
                        try:
                            self._page.evaluate("""() => {
                                const tabs = document.querySelectorAll('.van-tab');
                                for (const tab of tabs) {
                                    if (/在线考试/.test(tab.textContent)) { tab.click(); return; }
                                }
                            }""")
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
                        try:
                            should_submit = self._do_answering(
                                q_time=exam_question_time,
                                q_offset=exam_question_time_offset,
                                rand=random_answer,
                                rate_limit=exam_submit_match_rate,
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
                            self.log.info(f"答题结束，准备交卷：{title}")
                            try:
                                result_info = self._submit_exam()
                            except Exception as e:
                                self.log.error(f"[考试异常] 交卷过程中出错: {e}")
                                import traceback

                                self.log.debug(
                                    f"[考试异常] 堆栈:\n{traceback.format_exc()}"
                                )
                                result_info = f"交卷异常: {e}"
                                # 不退出浏览器
                        else:
                            self.log.warning(
                                f"匹配率过低且未开启随机，放弃交卷：{title}"
                            )
                            result_info = "放弃提交"
                            break

                    # 从结果信息中提取分数和通过状态
                    score_match = re.search(r"(\d+)\s*分", result_info)
                    final_score = int(score_match.group(1)) if score_match else 0
                    # 从弹窗文本判断是否通过
                    popup_passed = "通过" in result_info or "合格" in result_info

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
