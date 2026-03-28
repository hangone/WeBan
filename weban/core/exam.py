import json
import os
import re
import time
import random
import threading

from typing import TYPE_CHECKING, Any, Dict
import logging

from .captcha import handle_click_captcha, has_captcha

_terminal_lock = threading.Lock()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selector constants (derived from ExamList.vue / ExamPage.vue / ExamPopup.vue)
# ---------------------------------------------------------------------------
_SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
_SEL_JOIN_BTN = 'button.exam-button:has-text("参加考试")'
_SEL_START_BTN = 'a.popup-btn:has-text("开始考试")'
_SEL_DIALOG = ".van-dialog, .van-toast, .van-dialog__message"
_SEL_CONFIRM_BTN = '.van-dialog__confirm, button:has-text("确认"), button:has-text("确定")'
_SEL_QUESTION_TITLE = ".quest-stem"
_SEL_OPTIONS = ".quest-option-item"
_SEL_NEXT_BTN = (
    ".bottom-ctrls button:has-text('下一题'), "
    "button:has-text('下一题'), span:text-is('下一题')"
)
_SEL_SUBMIT_BTN = (
    ".sheet .bottom-ctrls button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls button:has-text('交卷')"
)
_SEL_ANSWER_CARD_BTN = ".bottom-ctrls button:has-text('答题卡')"
_SEL_SUBMIT_CONFIRM = (
    ".confirm-sheet .bottom-ctrls button:has-text('确 认'), "
    ".confirm-sheet .bottom-ctrls button:has-text('确认'), "
    ".van-dialog__confirm, button:has-text('确定')"
)


class ExamMixin:
    if TYPE_CHECKING:
        import logging as _logging
        from typing import Union as _Union
        from playwright.sync_api import Page, BrowserContext, Browser, Playwright
        from .browser import BrowserConfig

        _page: Page
        _context: BrowserContext
        _browser: Browser
        _playwright: Playwright
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        continue_on_invalid_token: bool
        browser_config: BrowserConfig
        answers: Dict[str, Any]

        # 以下方法定义在 AnswerMixin，通过多重继承在运行时可用
        def _merge_history_answers(self) -> None: ...
        def _merge_cloud_answers(self) -> None: ...

    @staticmethod
    def _clean_text(text: str) -> str:
        text = text or ""
        text = re.sub(r"^\s*[A-Z0-9]+[\.、\s]+", "", text)
        return re.sub(r"[^\w\u4e00-\u9fa5]", "", text)

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """Handle dialog after clicking 参加考试.

        Returns:
            'return'   - skip the exam
            'continue' - proceed past dialog
        """
        time.sleep(1)
        dialog = self._page.locator(_SEL_DIALOG)
        if dialog.count() == 0 or not dialog.first.is_visible():
            return "continue"

        text = dialog.first.inner_text()
        is_completed_prompt = "重新" in text or "完成" in text or "次数" in text

        if is_completed_prompt:
            self.log.info(
                f"检测到考试已完成/重考提示: {text.strip().replace(chr(10), ' ')}"
            )
            if exam_mode == "true":
                self.log.info("考试模式为[及格后不考试]，跳过重新考试")
                return "return"
            else:
                self.log.info("考试模式为[及格后也考试]，继续重新考试")
                confirm_btn = dialog.first.locator(_SEL_CONFIRM_BTN)
                if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
                    confirm_btn.first.click(force=True)
                    time.sleep(2)
        elif "未达标" in text or "必须" in text or "才能" in text:
            self.log.warning(
                f"前置条件未满足，无法考试: {text.strip().replace(chr(10), ' ')}"
            )
            return "return"
        else:
            confirm_btn = dialog.first.locator(_SEL_CONFIRM_BTN)
            if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
                self.log.info(
                    f"检测到提示框 ({text.strip().replace(chr(10), ' ')})，尝试点击确认。"
                )
                confirm_btn.first.click(force=True)
                time.sleep(2)

        return "continue"

    def _extract_question_list_from_response(self, payload: Any) -> list[dict[str, Any]]:
        """从 startPaper / refreshPaper 响应中提取完整试题列表。"""
        if not isinstance(payload, dict):
            return []

        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return []

        question_list = data.get("questionList") or data.get("questions") or []
        if not isinstance(question_list, list):
            return []

        return [q for q in question_list if isinstance(q, dict)]

    def _compute_match_stats_from_questions(
        self, question_list: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """基于整张试卷的题目列表，提前计算精确匹配数。"""
        if not question_list:
            return 0, 0

        answer_map = {self._clean_text(k): v for k, v in self.answers.items()}
        matched = 0

        for q in question_list:
            title = self._clean_text(str(q.get("title", "")))
            if not title:
                continue

            item = answer_map.get(title)
            if not item:
                continue

            matched_opts = [
                opt.get("content", "")
                for opt in item.get("optionList", [])
                if opt.get("isCorrect") == 1
            ]
            if matched_opts:
                matched += 1

        return len(question_list), matched

    def _answer_question(
        self,
        options,
        matched_opts,
        is_multiple: bool,
        random_answer: bool,
        title: str,
    ) -> bool:
        """Select answers for one question. Returns True if answered."""
        selected = False

        if matched_opts:
            self.log.info(f"匹配到答案：{matched_opts}")
            for i in range(options.count()):
                opt = options.nth(i)
                clean_opt_text = self._clean_text(opt.inner_text())
                if any(
                    self._clean_text(a) and self._clean_text(a) in clean_opt_text
                    for a in matched_opts
                ):
                    self.log.info(f"点击选项：{clean_opt_text}")
                    opt.click(force=True)
                    selected = True
            return selected

        if options.count() == 0:
            return False

        if random_answer:
            self.log.info(f"题目未找到答案，随机/全选: {self._clean_text(title)}")
            if is_multiple:
                for i in range(options.count()):
                    options.nth(i).click(force=True)
            else:
                options.first.click(force=True)
            return True

        # Ask user in terminal
        with _terminal_lock:
            print(
                f"\n[{self.account if hasattr(self, 'account') and self.account else '账号'}] 遇到未收录题目:"
            )
            print(f"题目: ({'多选' if is_multiple else '单选'}) {title}")
            print("选项:")
            for i in range(options.count()):
                t = options.nth(i).inner_text().strip()
                print(f"{chr(65 + i)}. {t}")
            while True:
                ans = input("请输入答案 (如 A, 或 AB) [多选请连打]: ").strip().upper()
                if ans:
                    break
            for char in ans:
                idx_char = ord(char) - 65
                if 0 <= idx_char < options.count():
                    options.nth(idx_char).click(force=True)
                    selected = True

        return selected

    def _submit_exam(self) -> None:
        """Handle the 交卷 submit logic at the end of an exam."""
        # ExamPage.vue 源码：
        # 1. 底部先点“答题卡”
        # 2. .sheet 弹层底部点“交卷”
        # 3. 若出现 .confirm-sheet，则继续点其中“交卷”或“确 认”
        answer_card_btn = self._page.locator(_SEL_ANSWER_CARD_BTN)
        if answer_card_btn.count() > 0:
            try:
                answer_card_btn.last.scroll_into_view_if_needed()
                answer_card_btn.last.click(force=True)
            except Exception:
                answer_card_btn.last.click(force=True)
            time.sleep(1)

        submit_btn = self._page.locator(_SEL_SUBMIT_BTN)
        if submit_btn.count() > 0:
            for i in range(submit_btn.count() - 1, -1, -1):
                try:
                    btn = submit_btn.nth(i)
                    if btn.is_visible() and btn.is_enabled():
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        break
                except Exception:
                    pass

        # 处理交卷后的确认弹层/确认弹窗
        time.sleep(1)
        confirm_btn = self._page.locator(_SEL_SUBMIT_CONFIRM)
        if confirm_btn.count() > 0:
            for i in range(confirm_btn.count() - 1, -1, -1):
                try:
                    btn = confirm_btn.nth(i)
                    if btn.is_visible() and btn.is_enabled():
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        break
                except Exception:
                    pass

    def _handle_exam_intermediate_pages(self) -> bool:
        """处理从任务列表点击项目后、进入考试 Tab 之前的中间页面。

        可能遇到：
        - SpecialIndex / LabIndex（聚合子项目列表，.img-text-block / .task-block）
        - ProtocolPageWk / ProtocolPage（承诺书签署页）
        - 直接进入 CourseIndex（有在线考试 Tab）

        返回 True 表示已就位（可见在线考试 Tab 或课程内容）。
        """
        for _step in range(6):
            time.sleep(2)

            # 已在课程页——有考试 Tab 或课程内容
            if self._page.locator(_SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(".van-collapse-item, .img-texts-item, .fchl-item").count() > 0:
                return True

            # 承诺书/协议签署页（ProtocolPageWk.vue / ProtocolPage.vue）
            agree_cb = self._page.locator(
                "#agree, .agree-checkbox input, input[type='checkbox']"
            )
            next_btn = self._page.locator(
                "button:has-text('下一步'), a:has-text('下一步')"
            )
            if agree_cb.count() > 0 and next_btn.count() > 0:
                self.log.info("[中间页] 检测到承诺书/协议页，自动同意并继续")
                try:
                    if not agree_cb.first.is_checked():
                        agree_cb.first.click(force=True)
                        time.sleep(0.5)
                    next_btn.first.click(force=True)
                    time.sleep(2)
                    # 签名页：直接点确认/完成
                    confirm = self._page.locator(
                        "button:has-text('确认'), button:has-text('完成'), "
                        "button:has-text('提交'), a:has-text('确认')"
                    )
                    if confirm.count() > 0:
                        confirm.first.click(force=True)
                        time.sleep(2)
                except Exception as e:
                    self.log.warning(f"[中间页] 处理承诺书失败: {e}")
                continue

            # SpecialIndex / LabIndex（.img-text-block 或嵌套 .task-block）
            sub_blocks = self._page.locator(".img-text-block, .task-block")
            if sub_blocks.count() > 0:
                self.log.info(
                    f"[中间页] 检测到子项目列表（{sub_blocks.count()} 个），点击第一个"
                )
                try:
                    sub_blocks.first.click(force=True)
                except Exception as e:
                    self.log.warning(f"[中间页] 点击子项目失败: {e}")
                continue

            # 弹窗（"已签名"等）——确认关掉
            dialog_ok = self._page.locator(_SEL_CONFIRM_BTN)
            if dialog_ok.count() > 0 and dialog_ok.first.is_visible():
                self.log.info("[中间页] 检测到弹窗，自动确认")
                try:
                    dialog_ok.first.click(force=True)
                except Exception:
                    pass
                continue

        self.log.warning("[中间页] 未能进入课程页，当前 URL: " + self._page.url)
        return False

    def run_exam(
        self,
        exam_question_time: int = 5,
        exam_question_time_offset: int = 3,
        random_answer: bool = False,
        exam_mode: str = "true",
        exam_submit_match_rate: int = 90,
    ) -> None:
        self._merge_cloud_answers()
        self._merge_history_answers()

        if exam_mode == "false":
            self.log.info("考试模式为[不考试]，跳过考试")
            return

        self.log.info("开始考试流程")

        try:
            self._page.goto(
                f"{self.base_url}/#/learning-task-list",
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as e:
            self.log.warning(f"导航到任务列表失败: {e}")
            return
        time.sleep(3)

        # 关闭广播公告
        try:
            broadcast = self._page.locator(".broadcast-modal")
            if broadcast.count() > 0 and broadcast.first.is_visible():
                broadcast.first.locator("button").first.click(force=True)
                time.sleep(0.5)
        except Exception:
            pass

        projects = self._page.locator(".task-block")
        if projects.count() == 0:
            self.log.warning("未找到任何学习任务，跳过考试")
            return

        total_proj = projects.count()
        self.log.info(f"共找到 {total_proj} 个学习项目，逐一检查考试")
        completed_projects: set = set()

        for proj_idx in range(total_proj):
            # 每轮重新导航，避免 DOM 失效
            try:
                self._page.goto(
                    f"{self.base_url}/#/learning-task-list",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                time.sleep(2)
            except Exception as e:
                self.log.warning(f"重新导航到任务列表失败: {e}")
                break

            projs = self._page.locator(".task-block")
            if proj_idx >= projs.count():
                break

            proj = projs.nth(proj_idx)
            try:
                title_el = proj.locator(".task-block-title")
                proj_title = (
                    title_el.first.inner_text().strip()
                    if title_el.count() > 0
                    else proj.inner_text().strip().split("\n")[0].strip()
                )
            except Exception:
                proj_title = f"项目{proj_idx + 1}"

            if proj_title in completed_projects:
                continue

            self.log.info(f"[考试] 进入项目：{proj_title}")
            try:
                proj.scroll_into_view_if_needed()
                time.sleep(0.5)
                proj.click(force=True)
            except Exception:
                proj.click(force=True)
            time.sleep(3)

            # 处理中间页
            if not self._handle_exam_intermediate_pages():
                self.log.warning(f"[考试] 无法进入 [{proj_title}] 的课程页，跳过")
                completed_projects.add(proj_title)
                continue

            # 点击"在线考试" Tab
            exam_tab = self._page.locator(_SEL_EXAM_TAB)
            if exam_tab.count() == 0:
                self.log.warning(f"[考试] [{proj_title}] 未找到在线考试 Tab，跳过")
                completed_projects.add(proj_title)
                continue
            exam_tab.first.click(force=True)
            time.sleep(3)

            # --- 1. 点击"参加考试"（ExamList.vue: button.exam-button）---
            list_btn = self._page.locator(_SEL_JOIN_BTN)
            if list_btn.count() > 0:
                try:
                    list_btn.first.scroll_into_view_if_needed()
                    time.sleep(1)
                    list_btn.first.click(force=True)
                except Exception:
                    list_btn.first.click(force=True)
                time.sleep(2)
            else:
                self.log.warning(f"[考试] [{proj_title}] 未找到'参加考试'按钮，跳过")
                completed_projects.add(proj_title)
                continue

            # --- 2. 处理弹窗（已完成/重考提示）---
            if self._handle_exam_dialog(exam_mode) == "return":
                completed_projects.add(proj_title)
                continue

            # --- 3. 点击"开始考试"（ExamPopup.vue: a.popup-btn @click="onConfirm"）---
            exam_paper_responses: list[dict[str, Any]] = []

            def _handle_exam_paper_response(response: Any) -> None:
                try:
                    if response.status != 200:
                        return
                    if not any(
                        key in response.url
                        for key in (
                            "/exam/startPaper.do",
                            "/exam/refreshPaper.do",
                            "/contest/startPaper.do",
                            "safeevaluation/startPaper.do",
                        )
                    ):
                        return
                    payload = response.json()
                    if isinstance(payload, dict):
                        exam_paper_responses.append(payload)
                except Exception:
                    pass

            self._page.on("response", _handle_exam_paper_response)

            start_btn = self._page.locator(_SEL_START_BTN)
            if start_btn.count() > 0:
                try:
                    start_btn.last.scroll_into_view_if_needed()
                    start_btn.last.click(force=True)
                except Exception:
                    start_btn.last.click(force=True)
                time.sleep(2)

                # 腾点选验证码（ExamPopup.vue onConfirm → TencentCaptcha）
                for _cap_try in range(5):
                    if not has_captcha(self._page):
                        break
                    self.log.info("[考试] 检测到点选验证码，开始自动识别...")
                    if handle_click_captcha(self._page, self.log):
                        self.log.info("[考试] 验证码通过")
                        time.sleep(2)
                        break
                    self.log.warning(
                        f"[考试] 验证码识别失败（{_cap_try + 1}/5），重试..."
                    )
                    time.sleep(2)
            else:
                self.log.warning("[考试] 未找到'开始考试'按钮，尝试继续答题流程")

            # --- 4. 拿到整张试卷后，先计算精确匹配率 ---
            time.sleep(2)
            total_questions_expected = 0
            matched_questions_expected = 0

            for payload in reversed(exam_paper_responses):
                question_list = self._extract_question_list_from_response(payload)
                if question_list:
                    (
                        total_questions_expected,
                        matched_questions_expected,
                    ) = self._compute_match_stats_from_questions(question_list)
                    break

            if total_questions_expected == 0:
                try:
                    indicator = self._page.locator(".quest-indicator")
                    if indicator.count() > 0:
                        ind_text = indicator.first.inner_text().strip()
                        parts = ind_text.split("/")
                        if len(parts) == 2 and parts[1].strip().isdigit():
                            total_questions_expected = int(parts[1].strip())
                except Exception:
                    pass

            if total_questions_expected > 0:
                expected_match_rate = (
                    matched_questions_expected / total_questions_expected
                ) * 100
                self.log.info(
                    f"[考试] [{proj_title}] 试卷已获取，共 {total_questions_expected} 题，"
                    f"预计可匹配 {matched_questions_expected} 题，"
                    f"预估匹配率 {expected_match_rate:.2f}%"
                )
                if (
                    expected_match_rate < exam_submit_match_rate
                    and not random_answer
                ):
                    self.log.warning(
                        f"预估匹配率 {expected_match_rate:.2f}% 低于要求 "
                        f"{exam_submit_match_rate}% ，未开启随机答题，放弃本次考试"
                    )
                    completed_projects.add(proj_title)
                    try:
                        self._page.remove_listener(
                            "response", _handle_exam_paper_response
                        )
                    except Exception:
                        pass
                    continue
            else:
                self.log.info(
                    f"[考试] [{proj_title}] 未能从试卷接口提取完整题目，继续按页面答题"
                )

            try:
                self._page.remove_listener("response", _handle_exam_paper_response)
            except Exception:
                pass

            # --- 5. 答题循环 ---
            total_questions = 0
            matched_questions = 0

            for idx in range(300):
                time.sleep(1)
                title_loc = self._page.locator(_SEL_QUESTION_TITLE)
                for _ in range(5):
                    if title_loc.count() > 0:
                        break
                    time.sleep(1)
                if title_loc.count() == 0:
                    break
                title = title_loc.first.inner_text()
                clean_title = self._clean_text(title)

                is_multiple = "多选" in title
                matched_opts = None
                for k, v in self.answers.items():
                    if self._clean_text(k) == clean_title:
                        matched_opts = [
                            opt.get("content", "")
                            for opt in v.get("optionList", [])
                            if opt.get("isCorrect") == 1
                        ]
                        if len(matched_opts) > 1:
                            is_multiple = True
                        break

                options = self._page.locator(_SEL_OPTIONS)
                total_questions += 1
                if matched_opts:
                    matched_questions += 1
                    self.log.info(
                        f"匹配题库（{total_questions} 题中已匹配 {matched_questions}）：{title}"
                    )
                else:
                    self.log.info(
                        f"未匹配题库（第 {total_questions} 题）：{self._clean_text(title)}"
                    )

                self._answer_question(
                    options, matched_opts, is_multiple, random_answer, title
                )

                wait_time = random.randint(
                    exam_question_time, exam_question_time + exam_question_time_offset
                )
                time.sleep(wait_time)

                # 已知总题数时，答完最后一题直接跳出，不再依赖标题变化等不精确判断
                if (
                    total_questions_expected > 0
                    and total_questions >= total_questions_expected
                ):
                    self.log.info(
                        f"已答完全部 {total_questions_expected} 题，退出答题循环"
                    )
                    break

                # 点击"下一题"
                next_btn = self._page.locator(_SEL_NEXT_BTN)
                if next_btn.count() == 0:
                    next_btn = self._page.get_by_text("下一题", exact=True)
                clicked_next = False
                if next_btn.count() > 0:
                    for i in range(next_btn.count() - 1, -1, -1):
                        try:
                            btn = next_btn.nth(i)
                            if btn.is_visible() and btn.is_enabled():
                                btn.click(force=True)
                                clicked_next = True
                                break
                        except Exception:
                            pass
                if not clicked_next:
                    self.log.info("未找到可用的'下一题'按钮，已到最后一题，退出答题循环")
                    break


            # --- 6. 提交 ---
            match_rate = (matched_questions / max(1, total_questions)) * 100
            self.log.info(
                f"[考试] [{proj_title}] 答题结束，共 {total_questions} 题，"
                f"匹配 {matched_questions} 题，匹配率 {match_rate:.2f}%"
            )

            if match_rate < exam_submit_match_rate and not random_answer:
                self.log.warning(
                    f"匹配率 {match_rate:.2f}% 低于要求 {exam_submit_match_rate}%"
                    "，未开启随机答题，放弃提交"
                )
                completed_projects.add(proj_title)
                continue

            self._submit_exam()
            self.log.info(f"[考试] [{proj_title}] 考试已提交")
            completed_projects.add(proj_title)

        # --- 所有项目考试完成，保存题库 ---
        self._merge_history_answers()
        try:
            from weban.app.runtime import get_base_path

            base_path = get_base_path()
            answer_path = os.path.join(base_path, "answer", "answer.json")
            with open(answer_path, "w", encoding="utf-8") as f:
                json.dump(self.answers, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            self.log.info("已将最新题库保存到本地 answer/answer.json")
        except Exception as e:
            self.log.warning(f"保存题库到本地失败: {e}")
        self.log.info("考试流程全部完成")
