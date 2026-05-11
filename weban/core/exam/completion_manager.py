import re
import time
from typing import Any, Callable

from playwright._impl._errors import TargetClosedError

from ..const import (
    SEL_ANSWER_CARD_BTN,
    SEL_EXAM_CONFIRM_SHEET,
    SEL_EXAM_CONFIRM_SUBMIT,
    SEL_EXAM_RESULT_SCORE,
    SEL_EXAM_SHEET,
    SEL_EXAM_SHEET_SUBMIT,
)

_DEFAULT_STATE = {
    "submit_state": False,
    "submitting": False,
    "score": "",
    "dom_score": "",
    "blocking_text": "",
}


class ExamCompletionManager:
    """完成考试/交卷流程管理器。"""

    def __init__(
        self,
        page_getter: Callable[[], Any],
        log: Any,
        vue_app_finder_js_getter: Callable[[], str],
        is_in_result_context: Callable[[], bool],
    ) -> None:
        self._page_getter = page_getter
        self._log = log
        self._vue_app_finder_js_getter = vue_app_finder_js_getter
        self._is_in_result_context = is_in_result_context

    def _page(self) -> Any:
        return self._page_getter()

    def read_submit_runtime_state(self) -> dict:
        """读取考试提交流程关键状态（Vue store + 页面弹层）。"""
        page = self._page()
        if not page or page.is_closed():
            return dict(_DEFAULT_STATE)
        try:
            return page.evaluate(
                """() => {
                %s
                function toStr(v) {
                    if (v === null || v === undefined) return '';
                    return String(v).trim();
                }
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                function textOf(sel) {
                    const el = document.querySelector(sel);
                    return el && isVisible(el) ? (el.textContent || '').trim() : '';
                }

                const app = findVueProxy(['$store']) || findVueProxy(null);
                const storeState = app?.$store?.state?.courseExam || {};
                const compState = findVueProxy(['submitState']) || app || {};

                const submitState = !!(storeState.submitState ?? compState.submitState);
                const submitting = !!(storeState.submitting ?? compState.submitting);
                const score = toStr(storeState.score ?? compState.score);
                const domScore = textOf('.exam-score, .result-score, .score-text');

                let blockingText = '';
                for (const sel of [
                    '.mint-msgbox-wrapper .mint-msgbox-message',
                    '.van-dialog .van-dialog__message',
                    '.mint-toast-text',
                    '.van-toast'
                ]) {
                    const txt = textOf(sel);
                    if (txt) {
                        blockingText = txt.substring(0, 200);
                        break;
                    }
                }

                return {
                    submit_state: submitState,
                    submitting,
                    score,
                    dom_score: domScore,
                    blocking_text: blockingText
                };
            }"""
                % self._vue_app_finder_js_getter()
            )
        except Exception:
            return dict(_DEFAULT_STATE)

    def dismiss_result_popup(self, timeout: float = 20.0) -> str:
        """处理成绩/拦截弹窗，返回弹窗关键信息。"""
        page = self._page()
        if not page or page.is_closed():
            return ""

        popup_text = ""
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                popup_info = page.evaluate(
                    """() => {
                    function isVisible(el) {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 && r.height === 0) return false;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                    }

                    const msgbox = document.querySelector('.mint-msgbox-wrapper');
                    if (isVisible(msgbox)) {
                        const msg = msgbox.querySelector('.mint-msgbox-message');
                        return { type: 'mint', score: msg ? msg.textContent.trim() : '' };
                    }

                    const sheet = document.querySelector('.confirm-sheet');
                    if (isVisible(sheet)) {
                        let score = '';
                        for (const sel of ['.confirm-message', '.sheet-message', '.result-score', '.score']) {
                            const el = sheet.querySelector(sel);
                            if (el && el.textContent.trim()) {
                                score = el.textContent.trim();
                                break;
                            }
                        }
                        const sheetText = (sheet.textContent || '').trim().substring(0, 200);
                        const btns = sheet.querySelectorAll('button');
                        for (const btn of btns) {
                            const txt = (btn.textContent || '').trim();
                            if (txt.includes('确 认') || txt === '确认') {
                                return { type: 'confirm-sheet', score: score || sheetText };
                            }
                        }
                        for (const btn of btns) {
                            const txt = (btn.textContent || '').trim();
                            if (txt.includes('交卷')) {
                                return { type: 'confirm-sheet-submit', score: score || sheetText };
                            }
                        }
                        if (btns.length > 0) {
                            return { type: 'confirm-sheet-fallback', score: score || sheetText };
                        }
                    }

                    const vdialog = document.querySelector('.van-dialog');
                    if (isVisible(vdialog)) {
                        const msg = vdialog.querySelector('.van-dialog__message');
                        return { type: 'van-dialog', score: msg ? msg.textContent.trim() : '' };
                    }

                    const vpopup = document.querySelector('.van-popup, .van-popup--center');
                    if (isVisible(vpopup)) {
                        return { type: 'van-popup', score: vpopup.textContent.trim().substring(0, 200) };
                    }

                    return null;
                }"""
                )
            except TargetClosedError:
                return popup_text
            except Exception:
                popup_info = None

            if popup_info:
                popup_type = popup_info.get("type", "")
                score = popup_info.get("score", "")
                if score:
                    popup_text = score
                    self._log.info(f"[成绩弹窗] {popup_type}: {score}")

                try:
                    if popup_type == "mint":
                        btn = page.locator(
                            ".mint-msgbox-wrapper .mint-msgbox-confirm, "
                            ".mint-msgbox-wrapper .mint-msgbox-btn"
                        ).first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                    elif popup_type == "confirm-sheet":
                        btn = page.locator(SEL_EXAM_CONFIRM_SUBMIT).filter(
                            has_text=re.compile(r"确\s*认")
                        ).first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                    elif popup_type == "confirm-sheet-submit":
                        btn = page.locator(SEL_EXAM_CONFIRM_SUBMIT).first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                        time.sleep(3)
                        continue
                    elif popup_type == "confirm-sheet-fallback":
                        btn = page.locator(".confirm-sheet button").first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                    elif popup_type == "van-dialog":
                        btn = page.locator(".van-dialog .van-dialog__confirm").first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                    elif popup_type == "van-popup":
                        btn = page.locator(
                            ".van-popup button, .van-popup--center button"
                        ).filter(has_text=re.compile(r"确定|确认")).first
                        if btn.count() > 0:
                            btn.click(timeout=2000)
                        else:
                            overlay = page.locator(".van-overlay").first
                            if overlay.count() > 0:
                                overlay.click(timeout=2000)
                except TargetClosedError:
                    return popup_text
                except Exception:
                    pass

                if popup_type != "confirm-sheet-submit":
                    return popup_text

            time.sleep(0.5)

        try:
            page.evaluate(
                """() => {
                document.querySelectorAll('.v-modal, .van-overlay').forEach(el => {
                    el.style.display = 'none';
                });
            }"""
            )
        except Exception:
            pass

        return popup_text

    def click_submit_buttons(self) -> str:
        """多级交卷按钮点击流程。"""
        page = self._page()
        if not page or page.is_closed():
            return ""

        def _wait_element(selector: str, timeout: float = 5.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    el = page.locator(selector).first
                    if el.count() > 0 and el.is_visible():
                        return True
                except TargetClosedError:
                    return False
                except Exception:
                    pass
                time.sleep(0.3)
            return False

        try:
            btn = page.locator("button, .mint-button").filter(
                has_text=re.compile(r"交卷|确认交卷")
            ).first
            if btn.count() > 0 and btn.is_visible():
                in_sheet = False
                handle = btn.element_handle()
                if handle:
                    in_sheet = bool(
                        page.evaluate(
                            """(el) => {
                            return !!el.closest('.sheet, .confirm-sheet');
                        }""",
                            handle,
                        )
                    )
                if not in_sheet:
                    btn_text = btn.inner_text().strip()
                    btn.click(timeout=3000)
                    self._log.info(f"[提交流程] 直接交卷: {btn_text}")
                    time.sleep(3)
                    return ""
        except TargetClosedError:
            return ""
        except Exception:
            pass

        self._log.debug("[提交流程] Step 1: 打开答题卡")
        if not _wait_element(SEL_EXAM_SHEET, timeout=1.0):
            try:
                card_btn = page.locator(SEL_ANSWER_CARD_BTN).first
                if card_btn.count() > 0 and card_btn.is_visible():
                    card_btn.click(timeout=3000)
            except TargetClosedError:
                return ""
            except Exception:
                pass
            if not _wait_element(SEL_EXAM_SHEET, timeout=5.0):
                self._log.warning("[提交流程] 答题卡弹窗未出现，尝试直接交卷...")
                try:
                    direct_btn = page.locator("button, .mint-button, .van-button").filter(
                        has_text=re.compile(r"交卷|提交|确认")
                    ).first
                    if direct_btn.count() > 0 and direct_btn.is_visible():
                        direct_btn.click(timeout=3000)
                        self._log.info(
                            f"[提交流程] Playwright click: {direct_btn.inner_text().strip()}"
                        )
                except TargetClosedError:
                    return ""
                except Exception:
                    pass
                time.sleep(3)
                return ""

        self._log.debug("[提交流程] Step 2: Sheet 内交卷")
        try:
            sheet_btn = page.locator(SEL_EXAM_SHEET_SUBMIT).first
            if sheet_btn.count() > 0 and sheet_btn.is_visible():
                sheet_btn.click(timeout=3000)
                self._log.info(f"[提交流程] Sheet 内交卷: {sheet_btn.inner_text().strip()}")
        except TargetClosedError:
            return ""
        except Exception:
            pass

        self._log.debug("[提交流程] Step 3: 确认弹窗")
        try:
            page_text = page.evaluate(
                """() => {
                const body = document.body;
                return body ? body.innerText.substring(0, 2000) : '';
            }"""
            )
            if page_text and "请作答" in page_text:
                self._log.warning("[提交流程] 检测到'请作答'提示，存在未作答题目")
        except Exception:
            pass

        if not _wait_element(SEL_EXAM_CONFIRM_SHEET, timeout=8.0):
            if self._is_in_result_context():
                self._log.info("[提交流程] 已在结果页")
                return ""
            try:
                toast = page.evaluate(
                    """() => {
                    const toasts = document.querySelectorAll('.van-toast, .mint-toast, .van-popup');
                    for (const t of toasts) {
                        const r = t.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) {
                            const txt = (t.textContent || '').trim();
                            if (txt.includes('请作答')) return txt;
                        }
                    }
                    return null;
                }"""
                )
                if toast:
                    self._log.warning(f"[提交流程] 页面拦截: {toast}")
                    return ""
            except Exception:
                pass
            time.sleep(2)
            if self._is_in_result_context():
                return ""
        else:
            try:
                confirm_btn = page.locator(SEL_EXAM_CONFIRM_SUBMIT).first
                if confirm_btn.count() > 0 and confirm_btn.is_visible():
                    confirm_btn.click(timeout=3000)
                    self._log.info(
                        f"[提交流程] 确认交卷: {confirm_btn.inner_text().strip()}"
                    )
            except TargetClosedError:
                return ""
            except Exception:
                pass

        return ""

    def submit_exam(
        self,
        is_submit_blocked_message: Callable[[str], bool],
        dismiss_msgbox: Callable[[], None],
    ) -> str:
        """交卷并返回结果信息。"""
        page = self._page()
        if not page or page.is_closed():
            return "交卷失败: 页面已关闭"

        try:
            score_el = page.locator(SEL_EXAM_RESULT_SCORE).first
            if score_el.count() > 0 and score_el.is_visible():
                txt = score_el.inner_text().strip()
                if txt:
                    self._log.info(f"[提交流程] 已在结算页，得分: {txt}")
                    return f"已在结算页: {txt}"
        except Exception:
            pass

        self.click_submit_buttons()

        deadline = time.time() + 25
        last_popup_text = ""
        while time.time() < deadline:
            page = self._page()
            if not page or page.is_closed():
                return "交卷失败: 页面已关闭"

            state = self.read_submit_runtime_state()
            blocking_text = (state.get("blocking_text") or "").strip()
            if blocking_text and is_submit_blocked_message(blocking_text):
                dismiss_msgbox()
                return f"交卷失败: {blocking_text}"

            dom_score = (state.get("dom_score") or "").strip()
            if dom_score:
                return f"交卷完成: {dom_score}"

            if state.get("submit_state"):
                vue_score = (state.get("score") or "").strip()
                if vue_score:
                    return f"交卷完成: {vue_score}分"

                popup_text = self.dismiss_result_popup(timeout=2.0)
                if popup_text:
                    last_popup_text = popup_text
                    if is_submit_blocked_message(popup_text):
                        dismiss_msgbox()
                        return f"交卷失败: {popup_text}"
                    score_match = re.search(r"\d+\s*分", popup_text)
                    if score_match:
                        return f"交卷完成: {score_match.group(0)}"
                return "交卷完成"

            popup_text = self.dismiss_result_popup(timeout=1.2)
            if popup_text:
                last_popup_text = popup_text
                if is_submit_blocked_message(popup_text):
                    dismiss_msgbox()
                    return f"交卷失败: {popup_text}"
                score_match = re.search(r"\d+\s*分", popup_text)
                if score_match:
                    return f"交卷完成: {score_match.group(0)}"
            time.sleep(0.4)

        if last_popup_text:
            return f"交卷未确认成功: {last_popup_text}"
        return "交卷未确认成功"
