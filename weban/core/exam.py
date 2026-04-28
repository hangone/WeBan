import re
import sys
import time
import random
import threading
import logging
from typing import TYPE_CHECKING, Any, Dict, List, cast

from .const import (
    SEL_EXAM_TAB,
    SEL_EXAM_ITEM,
    SEL_EXAM_ITEM_TITLE,
    SEL_EXAM_ITEM_PASS,
    SEL_EXAM_RESULT_SCORE,
    SEL_EXAM_SUBMIT_AREA,
    SEL_EXAM_PREPARE_POPUPS,
    SEL_EXAM_PREPARE_NEXT,
    SEL_EXAM_PREPARE_CONFIRM,
    SEL_EXAM_INTERMEDIATE_PROJECT,
    SEL_START_BTN,
    SEL_DIALOG,
    SEL_CONFIRM_BTN,
    SEL_QUEST_STEM,
    SEL_QUEST_STEM_SUB,
    SEL_QUEST_OPTIONS,
    SEL_NEXT_BTN,
    SEL_SUBMIT_BTN,
    SEL_SUBMIT_CONFIRM,
    SEL_TASK_BLOCK,
    SEL_TASK_BLOCK_TITLE,
    SEL_QUEST_CATEGORY,
    SEL_QUEST_INDICATOR,
    SEL_ANSWER_CARD_BTN,
    SEL_COURSE_LIST_MARKERS,
    SEL_JOIN_BTN,
    SEL_EXAM_SHEET,
    SEL_EXAM_CONFIRM_SHEET,
    SEL_EXAM_CONFIRM_SHEET_BOTTOM_CTRLS,
    SEL_EXAM_BOTTOM_CTRLS,
    SEL_EXAM_SHEET_SUBMIT,
    SEL_EXAM_CONFIRM_SUBMIT,
    SEL_EXAM_NEXT_BTN_IN_BOTTOM,
    SEL_EXAM_CARD_BTN_IN_BOTTOM,
    SEL_EXAM_QUEST_INDEX_ITEM_TEMPLATE,
)
from .captcha import handle_tencent_captcha, has_captcha
from .base import BaseMixin, PageContext
from playwright._impl._errors import TargetClosedError
from weban.app.runtime import clean_text, ignore_symbols

_terminal_lock = threading.Lock()
logger = logging.getLogger(__name__)


class ExamMixin(BaseMixin):
    """在线考试流程 Mixin。"""

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
        """按索引点击选项。

        Args:
            page_options: 页面选项定位器
            indices: 要点击的选项索引列表
            question_type: 题型 (1=单选, 2=多选)

        Returns:
            成功点击的选项数量
        """
        clicked = 0
        for idx in indices:
            try:
                opt = page_options.nth(idx)
                opt.click(force=True, timeout=5000)
                clicked += 1

                for _ in range(15):
                    cls = (opt.get_attribute("class") or "").lower()
                    if "selected" in cls or "active" in cls:
                        break
                    time.sleep(0.1)
            except Exception:
                pass
        return clicked

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
        """尝试前进到下一题。

        优先尝试"下一题"按钮，失败则通过答题卡跳转。

        Returns:
            是否成功推进
        """
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

        try:
            next_btn = self._page.locator(SEL_NEXT_BTN).first
            if next_btn.count() > 0 and next_btn.is_visible():
                next_btn.scroll_into_view_if_needed()
                next_btn.click(force=True, timeout=8000)
                if self._wait_for_question_change(prev_title, prev_indicator, 8):
                    return True
        except Exception:
            pass

        try:
            next_btn2 = (
                self._page.locator(SEL_EXAM_BOTTOM_CTRLS)
                .locator(SEL_EXAM_NEXT_BTN_IN_BOTTOM)
                .first
            )
            if next_btn2.count() > 0 and next_btn2.is_visible():
                next_btn2.scroll_into_view_if_needed()
                next_btn2.click(force=True, timeout=8000)
                if self._wait_for_question_change(prev_title, prev_indicator, 8):
                    return True
        except Exception:
            pass

        return self._jump_via_answer_card(prev_title, prev_indicator)

    def _jump_via_answer_card(self, prev_title: str, prev_indicator: str) -> bool:
        """通过答题卡跳转到下一题。"""
        if not self._page:
            return False

        current_idx = self._get_current_question_index()
        next_idx = current_idx + 1

        try:
            card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).last
            if not (card_btn.count() > 0 and card_btn.is_visible()):
                card_btn = (
                    self._page.locator(SEL_EXAM_BOTTOM_CTRLS)
                    .locator(SEL_EXAM_CARD_BTN_IN_BOTTOM)
                    .first
                )

            if card_btn.count() > 0 and card_btn.is_visible():
                card_btn.scroll_into_view_if_needed()
                card_btn.click(force=True, timeout=5000)
                time.sleep(1.2)

                for num_str in [str(next_idx), f"{next_idx:02d}"]:
                    jump_target = self._page.locator(
                        SEL_EXAM_QUEST_INDEX_ITEM_TEMPLATE.format(num=num_str)
                    ).first
                    if jump_target.count() > 0 and jump_target.is_visible():
                        jump_target.click(force=True, timeout=5000)
                        if self._wait_for_question_change(
                            prev_title, prev_indicator, 6
                        ):
                            return True
        except Exception:
            pass

        return False

    # ========================================================================
    # 提交流程
    # ========================================================================

    def _dismiss_result_popup(self) -> str:
        """等待并关闭提交成绩弹窗，返回捕捉到的分值文本。"""
        if not self._page:
            return ""

        score_text = ""
        deadline = time.time() + 20

        while time.time() < deadline:
            # ---- 方案1：Mint UI MessageBox (普通考试) ----
            try:
                msgbox = self._page.locator(".mint-msgbox-wrapper").first
                if msgbox.count() > 0 and msgbox.is_visible():
                    msg = msgbox.locator(".mint-msgbox-message").first
                    if msg.count() > 0:
                        try:
                            score_text = msg.inner_text(timeout=1000).strip()
                        except Exception:
                            pass
                        # INFO 级别让用户直接看到考试结果
                        self.log.info(f"🎯 考试结果: {score_text}")

                    # 等待确认按钮（可能有延迟渲染）
                    for _ in range(10):
                        confirm = msgbox.locator(
                            ".mint-msgbox-confirm, .mint-msgbox-btn"
                        ).first
                        if confirm.count() > 0 and confirm.is_visible():
                            confirm.click(force=True, timeout=5000)
                            self.log.debug("[成绩弹窗] 已关闭 Mint MessageBox")
                            time.sleep(1.5)
                            return score_text
                        time.sleep(0.3)
            except Exception:
                pass

            # ---- 方案2：自定义 confirm-sheet (安全测评) ----
            try:
                sheet = self._page.locator(".confirm-sheet").first
                if sheet.count() > 0 and sheet.is_visible():
                    # Try multiple sources for score text
                    for msg_sel in [
                        ".confirm-message",
                        ".sheet-message",
                        ".result-score",
                        ".score",
                    ]:
                        msg = sheet.locator(msg_sel).first
                        if msg.count() > 0:
                            try:
                                score_text = msg.inner_text(timeout=1000).strip()
                                if score_text:
                                    break
                            except Exception:
                                pass
                    self.log.info(f"[成绩弹窗] confirm-sheet: {score_text}")

                    confirm = sheet.locator(
                        ".bottom-ctrls button, .bottom-ctrls .mint-button, button"
                    ).first
                    if confirm.count() > 0 and confirm.is_visible():
                        confirm.click(force=True, timeout=5000)
                        self.log.debug("[成绩弹窗] confirm-sheet 已关闭")
                        time.sleep(1.5)
                        return score_text
            except Exception:
                pass

            # ---- 方案3：van-dialog 通用弹窗 ----
            try:
                vdialog = self._page.locator(".van-dialog").first
                if vdialog.count() > 0 and vdialog.is_visible():
                    msg = vdialog.locator(".van-dialog__message").first
                    if msg.count() > 0:
                        try:
                            score_text = msg.inner_text(timeout=1000).strip()
                        except Exception:
                            pass
                        self.log.info(f"[成绩弹窗] van-dialog: {score_text}")

                    confirm = vdialog.locator(".van-dialog__confirm").first
                    if confirm.count() > 0 and confirm.is_visible():
                        confirm.click(force=True, timeout=5000)
                        time.sleep(1.5)
                        return score_text
            except Exception:
                pass

            # ---- 方案4：van-popup 结果弹窗 ----
            try:
                vpopup = self._page.locator(".van-popup, .van-popup--center").first
                if vpopup.count() > 0 and vpopup.is_visible():
                    # 尝试读取弹窗内文本
                    try:
                        score_text = vpopup.inner_text(timeout=1000).strip()[:200]
                        self.log.info(f"[成绩弹窗] van-popup: {score_text}")
                    except Exception:
                        pass
                    # 尝试关闭弹窗
                    for close_sel in [
                        "button:has-text('确定')",
                        "button:has-text('确认')",
                        ".van-popup__close",
                        ".van-overlay",
                    ]:
                        close_btn = vpopup.locator(close_sel).first
                        if close_btn.count() > 0 and close_btn.is_visible():
                            close_btn.click(force=True, timeout=5000)
                            time.sleep(1.5)
                            return score_text
                    # 尝试点击覆盖层关闭
                    overlay = self._page.locator(".van-overlay").first
                    if overlay.count() > 0 and overlay.is_visible():
                        overlay.click(force=True, timeout=5000)
                        time.sleep(1.5)
                        return score_text
            except Exception:
                pass

            time.sleep(0.5)

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
        """点击各级交卷按钮（正确的多级弹窗提交流程）。

        ExamPage.vue 提交流程：
        1. 点击主页面底部「答题卡」按钮 → 弹出 .sheet 弹窗
        2. 在 .sheet 弹窗中点击「交卷」→ 弹出 .confirm-sheet 确认弹窗
        3. 在 .confirm-sheet 弹窗中点击「交卷」/「确 认」→ 真正提交

        备用路径：部分页面可能直接有「交卷」按钮，无需答题卡。
        """
        assert self._page is not None

        def _click_btn(selector, label: str, max_attempts: int = 3) -> bool:
            """通用重试点击辅助函数。"""
            for attempt in range(max_attempts):
                try:
                    btn = self._page.locator(selector).first
                    if btn.count() > 0 and btn.is_visible():
                        if btn.is_enabled():
                            self.log.debug(f"[提交流程] 点击{label}")
                            btn.click(force=True, timeout=5000)
                            time.sleep(1.5)
                            return True
                        else:
                            self.log.debug(f"[提交流程] {label}已禁用，跳过")
                            return False
                except Exception:
                    pass
                time.sleep(0.5)
            return False

        def _wait_element(selector: str, timeout: float = 5.0) -> bool:
            """等待元素出现并可见。"""
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

        def _click_first_visible(
            selectors: list[str], label: str, max_attempts: int = 3
        ) -> bool:
            """尝试多个选择器，点击第一个可见的。"""
            for sel in selectors:
                if _click_btn(sel, label, max_attempts=max_attempts):
                    return True
            return False

        # Step 0: 检查是否可以直接提交（某些页面有直接的提交按钮）
        direct_submit_selectors = [
            SEL_EXAM_SUBMIT_AREA,
            "button:has-text('交卷'):visible",
            "button:has-text('确认交卷'):visible",
            ".mint-button:has-text('交卷'):visible",
        ]
        for sel in direct_submit_selectors:
            try:
                btn = self._page.locator(sel).first
                if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                    # 确保不是 sheet 弹窗内的按钮（那种需要走三步流程）
                    parent_sheet = btn.locator("..").locator(".sheet, .confirm-sheet")
                    if parent_sheet.count() == 0:
                        self.log.info("[提交流程] 发现直接交卷按钮，尝试直接提交")
                        btn.click(force=True, timeout=5000)
                        time.sleep(3)
                        return ""
            except Exception:
                pass

        # Step 1: 打开答题卡 sheet 弹窗
        self.log.debug("[提交流程] Step 1: 打开答题卡")
        if not _wait_element(SEL_EXAM_SHEET, timeout=1.0):
            answer_card_selectors = [
                SEL_ANSWER_CARD_BTN,
                SEL_EXAM_CARD_BTN_IN_BOTTOM,
                "button:has-text('答题卡'):visible",
                ".mint-button:has-text('答题卡'):visible",
                ".van-button:has-text('答题卡'):visible",
            ]
            _click_first_visible(answer_card_selectors, "答题卡", max_attempts=3)
            if not _wait_element(SEL_EXAM_SHEET, timeout=5.0):
                self.log.warning("[提交流程] 答题卡弹窗未出现，尝试直接提交...")
                # 最终兜底：尝试页面上所有可能的交卷按钮
                all_btns = self._page.locator("button:visible, .mint-button:visible")
                for i in range(all_btns.count()):
                    btn = all_btns.nth(i)
                    txt = (btn.inner_text() or "").strip()
                    if txt and any(kw in txt for kw in ["交卷", "提交", "确认"]):
                        if "取消" not in txt and "返回" not in txt:
                            try:
                                btn.click(force=True, timeout=5000)
                                self.log.info(f"[提交流程] 兜底点击按钮: {txt}")
                                time.sleep(3)
                                return ""
                            except Exception:
                                pass
                return ""

        # Step 2: 在 sheet 弹窗中点击「交卷」
        self.log.debug("[提交流程] Step 2: Sheet 内点击交卷")
        sheet_submit_selectors = [
            SEL_EXAM_SHEET_SUBMIT,
            SEL_SUBMIT_BTN,
            ".sheet .mint-button--danger",
            ".sheet .bottom-ctrls button:last-child",
            ".sheet button:has-text('交卷')",
            ".sheet .mint-button:has-text('交卷')",
        ]
        _click_first_visible(sheet_submit_selectors, "Sheet交卷", max_attempts=3)

        # Step 3: 等待确认弹窗出现并点击提交
        self.log.debug("[提交流程] Step 3: 等待确认弹窗并提交")
        if not _wait_element(SEL_EXAM_CONFIRM_SHEET, timeout=8.0):
            # 检查是否已经提交成功（可能直接进到结果页）
            if self._is_in_context(PageContext.EXAM_RESULT):
                self.log.info("[提交流程] 已在结果页，无需确认弹窗")
                return ""
            self.log.warning("[提交流程] 确认弹窗未出现，尝试等待结果...")
            time.sleep(5)
            if self._is_in_context(PageContext.EXAM_RESULT):
                return ""
        else:
            confirm_selectors = [
                SEL_EXAM_CONFIRM_SUBMIT,
                SEL_SUBMIT_CONFIRM,
                f"{SEL_EXAM_CONFIRM_SHEET_BOTTOM_CTRLS} {SEL_SUBMIT_BTN}",
                ".confirm-sheet button:has-text('交卷')",
                ".confirm-sheet .mint-button:has-text('交卷')",
                ".confirm-sheet button:has-text('确 认')",
                ".confirm-sheet button:has-text('确认')",
            ]
            if not _click_first_visible(confirm_selectors, "确认交卷", max_attempts=5):
                # Last resort: click any visible button in confirm sheet
                try:
                    all_btns = self._page.locator(
                        f"{SEL_EXAM_CONFIRM_SHEET} button:visible"
                    )
                    for i in range(all_btns.count()):
                        btn = all_btns.nth(i)
                        txt = (btn.inner_text() or "").strip()
                        if txt and "取消" not in txt and "返回" not in txt:
                            btn.click(force=True, timeout=5000)
                            self.log.info(f"[提交流程] 兜底点击确认弹窗按钮: {txt}")
                            time.sleep(2)
                            break
                except Exception:
                    pass

        return ""

    # ========================================================================
    # 主考试流程
    # ========================================================================

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """处理考试相关弹窗。"""
        time.sleep(2)
        if not self._page:
            raise RuntimeError("Page is not initialized")

        dialogs = self._page.locator(SEL_DIALOG)
        for i in range(dialogs.count()):
            d = dialogs.nth(i)
            if not d.is_visible():
                continue
            text = d.inner_text().strip().replace("\n", " ")

            if any(
                k in text
                for k in [
                    "未开放",
                    "已关闭",
                    "不允许",
                    "暂无考试机会",
                    "次数已用",
                    "课程学习未完成",
                ]
            ):
                self.log.warning(f"无法考试: {text}")
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            if any(k in text for k in ["未提交", "重新进入", "继续考试", "清除"]):
                self.log.info(f"流程提示: {text}")
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                continue

            is_passed = any(k in text for k in ["已合格", "已及格", "考试通过"]) or (
                "合格" in text and "最高成绩" in text
            )
            if is_passed and exam_mode == "true":
                self.log.info(f"及格后不考试: {text}")
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            btn = d.locator(SEL_CONFIRM_BTN).first
            if btn.is_visible():
                btn.click(timeout=5000)

        return "continue"

    def _handle_exam_intermediate_pages(self) -> bool:
        """处理进入考试前的中间页面。"""
        assert self._page is not None
        for _ in range(6):
            time.sleep(2)
            if self._page.locator(SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
                return True

            agree = self._page.locator(
                "#agree, .agree-checkbox input, input[type='checkbox']"
            )
            next_btn = self._page.locator(SEL_EXAM_PREPARE_NEXT)
            if agree.count() > 0 and next_btn.is_visible():
                if not agree.first.is_checked():
                    agree.first.click()
                next_btn.first.click()
                time.sleep(2)
                confirm = self._page.locator(SEL_EXAM_PREPARE_CONFIRM)
                if confirm.count() > 0:
                    confirm.first.click()
                continue

            sub = self._page.locator(SEL_EXAM_INTERMEDIATE_PROJECT)
            if sub.count() > 0:
                sub.first.click()
                continue

            ok = self._page.locator(SEL_CONFIRM_BTN)
            if ok.count() > 0 and ok.first.is_visible():
                ok.first.click()
                continue

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
                        options.nth(idx).click(force=True, timeout=5000)

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
            time.sleep(1.2)

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
                        time.sleep(0.8)
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
                    options.first.click(force=True, timeout=5000)
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
                    "[缺失] 题库无此题，需手动选择\n"
                    + "\n".join(
                        f"    {chr(65 + i)}. {opt_texts[i]}"
                        for i in range(options_count)
                    )
                )
                ok = self._interactive_answering(title, options, options_count, [])
                if ok:
                    matched += 1
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    self._advance_to_next_question()
                    continue

                self.log.warning("   尝试通过答题卡跳转...")
                if self._jump_via_answer_card("", ""):
                    continue

                self.log.warning("   无法跳转，停止作答。")
                break

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
        time.sleep(2)

        proj_count = self._page.locator(SEL_TASK_BLOCK).count()
        completed = set()

        for i in range(proj_count):
            self._page.goto(
                f"{self.base_url}/#/learning-task-list", wait_until="domcontentloaded"
            )
            time.sleep(1.5)

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

            tab = self._page.locator(SEL_EXAM_TAB).first
            if tab.is_visible():
                tab.click(force=True)
                time.sleep(2)

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
                        time.sleep(1.5)
                        proj_retry = self._page.locator(SEL_TASK_BLOCK).nth(i)
                        proj_retry.click()
                        if not self._handle_exam_intermediate_pages():
                            self.log.warning("重试: 无法进入项目课件页")
                            break
                        tab_retry = self._page.locator(SEL_EXAM_TAB).first
                        if tab_retry.is_visible():
                            tab_retry.click(force=True)
                            time.sleep(2)
                        items_retry = self._page.locator(SEL_EXAM_ITEM)
                        it_retry = items_retry.nth(j)
                        join_retry = it_retry.locator(SEL_JOIN_BTN).first
                        if not join_retry.is_visible():
                            self.log.warning(
                                f"重试: 按钮未就绪，跳过 {exam_title_text}"
                            )
                            break
                        join = join_retry

                    self.log.info(f"[答题] 尝试点击：{exam_title_text} (第{attempt}次)")
                    join.click(force=True)
                    time.sleep(2)

                    dialog_result = self._handle_exam_dialog(exam_mode)
                    if dialog_result == "return":
                        self.log.warning(f"[答题] 弹窗提示中止：{exam_title_text}")
                        break

                    self.log.info("[答题] 正在处理启动流程...")
                    start_time = time.time()
                    entered = False
                    captcha_handled = False
                    while time.time() - start_time < 40:
                        try:
                            self._handle_exam_dialog(exam_mode)

                            start_btn = self._page.locator(SEL_START_BTN).first
                            if start_btn.count() > 0 and start_btn.is_visible():
                                self.log.debug("[答题] 点击开始考试按钮")
                                start_btn.click(force=True)
                                time.sleep(3)
                                continue

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
                                time.sleep(3)
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
                                time.sleep(3)
                                continue

                        except Exception as e:
                            self.log.debug(f"[探测过程异常] {e}")

                        time.sleep(2)

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
                time.sleep(3)

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
