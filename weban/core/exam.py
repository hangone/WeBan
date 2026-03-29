import re
import time
import random
import threading

from typing import TYPE_CHECKING, Any, cast
import logging

from .captcha import handle_click_captcha, has_captcha
from .base import BaseMixin
from weban.app.runtime import clean_text

_terminal_lock = threading.Lock()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selector constants (derived from ExamList.vue / ExamPage.vue / ExamPopup.vue)
# ---------------------------------------------------------------------------
_SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
_SEL_JOIN_BTN = 'button.exam-button:has-text("参加考试")'
_SEL_START_BTN = 'a.popup-btn:has-text("开始考试")'
_SEL_DIALOG = ".van-dialog, .van-toast, .mint-msgbox, .mint-toast"
_SEL_CONFIRM_BTN = '.van-dialog__confirm, button:has-text("确认"), button:has-text("确定"), .mint-msgbox-confirm'
_SEL_QUESTION_TITLE = ".quest-stem"
_SEL_OPTIONS = ".quest-option-item"
_SEL_NEXT_BTN = (
    ".bottom-ctrls button:has-text('下一题'), "
    "button:has-text('下一题'), span:text-is('下一题')"
)
_SEL_SUBMIT_BTN = (
    ".sheet .bottom-ctrls button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls button:has-text('交卷'), "
    "button:has-text('交卷'), .mint-button:has-text('交卷')"
)
_SEL_ANSWER_CARD_BTN = ".bottom-ctrls button:has-text('答题卡'), .bottom-ctrls .mint-button:has-text('答题卡')"
_SEL_SUBMIT_CONFIRM = (
    ".confirm-sheet .bottom-ctrls button:has-text('确 认'), "
    ".confirm-sheet .bottom-ctrls button:has-text('确认'), "
    ".van-dialog__confirm, button:has-text('确定'), button:has-text('提交')"
)
_SEL_BROADCAST_MODAL = ".broadcast-modal"
_SEL_BROADCAST_CLOSE_BTN = ".broadcast-modal button"
_SEL_TASK_BLOCK = ".task-block"
_SEL_TASK_BLOCK_TITLE = ".task-block-title"
_SEL_QUEST_INDICATOR = ".quest-indicator"
_SEL_INTERMEDIATE_WAIT_TARGETS = ".van-collapse-item, .img-texts-item, .fchl-item, #agree, .agree-checkbox input, input[type='checkbox'], .img-text-block, .task-block"
_SEL_COURSE_LIST_MARKERS = ".van-collapse-item, .img-texts-item, .fchl-item"
_SEL_COURSE_READY = ".van-tab, .van-collapse-item, .img-texts-item, .fchl-item"


class ExamMixin(BaseMixin):
    """在线考试流程 Mixin。"""

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """根据弹窗内容直接决策业务流程。"""
        time.sleep(2)
        dialogs = self._page.locator(_SEL_DIALOG)

        for i in range(dialogs.count()):
            d = dialogs.nth(i)
            if not d.is_visible():
                continue
            text = d.inner_text().strip().replace("\n", " ")

            # 1. 拒绝类（无法考试）
            if any(
                k in text
                for k in ["未开放", "已关闭", "不允许", "暂无考试机会", "次数已用"]
            ):
                self.log.warning(f"无法考试: {text}")
                btn = d.locator(_SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            # 2. 流程类（清理数据）
            if any(k in text for k in ["未提交", "重新进入", "继续考试", "清除"]):
                self.log.info(f"流程提示: {text}")
                btn = d.locator(_SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                continue

            # 3. 及格判定
            is_passed = any(k in text for k in ["已合格", "已及格", "考试通过"]) or (
                "合格" in text and "最高成绩" in text
            )
            if is_passed and exam_mode == "true":
                self.log.info(f"及格后不考试: 检测到已达标 ({text})")
                btn = d.locator(_SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            # 常规弹窗确认
            btn = d.locator(_SEL_CONFIRM_BTN).first
            if btn.is_visible():
                btn.click(timeout=5000)

        return "continue"

    def _submit_exam(self) -> str:
        """交卷并确认，返回抓取到的得分信息。"""
        # 1. 尝试多次寻找答题卡按钮并点击
        for _ in range(3):
            btn = self._page.locator(_SEL_ANSWER_CARD_BTN).last
            if btn.is_visible():
                self.log.debug("[提交流程] 点击答题卡")
                btn.click(force=True, timeout=5000)
                time.sleep(1)
                break
            time.sleep(1)

        # 2. 点击确认列表中的交卷按钮
        for _ in range(3):
            submit_btn = self._page.locator(_SEL_SUBMIT_BTN).last
            if submit_btn.is_visible():
                self.log.debug("[提交流程] 点击交卷")
                submit_btn.click(force=True, timeout=5000)
                time.sleep(1.5)
                break
            time.sleep(1)

        # 3. 点击最后的确认交卷按钮 (针对最终结算弹窗)
        result_text = ""
        confirm_btn = self._page.locator(_SEL_SUBMIT_CONFIRM).last
        if confirm_btn.is_visible():
            # 捕获弹窗中的得分信息
            result_text = self._page.locator(_SEL_DIALOG).first.inner_text()
            self.log.debug(f"[结果弹出] {result_text}")
            confirm_btn.click(force=True, timeout=5000)
            time.sleep(2)

        return result_text

    def _handle_exam_intermediate_pages(self) -> bool:
        """处理进入考试前的中间页面。"""
        for _ in range(6):
            time.sleep(2)
            if self._page.locator(_SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(_SEL_COURSE_LIST_MARKERS).count() > 0:
                return True

            # 承诺书
            agree = self._page.locator(
                "#agree, .agree-checkbox input, input[type='checkbox']"
            )
            next_btn = self._page.locator(
                "button:has-text('下一步'), a:has-text('下一步')"
            )
            if agree.count() > 0 and next_btn.is_visible():
                if not agree.first.is_checked():
                    agree.first.click()
                next_btn.first.click()
                time.sleep(2)
                confirm = self._page.locator(
                    "button:has-text('确认'), button:has-text('完成'), button:has-text('提交')"
                )
                if confirm.count() > 0:
                    confirm.first.click()
                continue

            # 子项目
            sub = self._page.locator(".img-text-block, .task-block")
            if sub.count() > 0:
                sub.first.click()
                continue

            # 杂项弹窗
            ok = self._page.locator(_SEL_CONFIRM_BTN)
            if ok.count() > 0 and ok.first.is_visible():
                ok.first.click()
                continue

        return self._page.locator(_SEL_EXAM_TAB).count() > 0

    def run_exam(
        self,
        exam_question_time: int = 5,
        exam_question_time_offset: int = 3,
        random_answer: bool = False,
        exam_mode: str = "true",
        exam_submit_match_rate: int = 90,
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

        self.log.info("开始执行考试流程...")
        self._page.goto(f"{self.base_url}/#/learning-task-list")
        time.sleep(2)

        proj_count = self._page.locator(_SEL_TASK_BLOCK).count()
        completed = set()

        for i in range(proj_count):
            self._page.goto(f"{self.base_url}/#/learning-task-list")
            time.sleep(1.5)

            proj = self._page.locator(_SEL_TASK_BLOCK).nth(i)
            title = proj.locator(_SEL_TASK_BLOCK_TITLE).inner_text().strip()
            if title in completed:
                continue

            self.log.info(f"[考试] 正在检查项目：{title}")
            proj.click()
            if not self._handle_exam_intermediate_pages():
                self.log.warning(f"无法进入项目课件页: {title}")
                completed.add(title)
                continue

            # 切换 Tab
            tab = self._page.locator(_SEL_EXAM_TAB).first
            if not tab.is_visible():
                completed.add(title)
                continue
            tab.click()
            time.sleep(1.5)

            # 检查考试项
            items = self._page.locator(".exam-item")
            for j in range(items.count()):
                it = items.nth(j)
                p_span = it.locator(".exam-item-title .exam-pass")
                if exam_mode == "true" and p_span.count() > 0:
                    self.log.info(f"[及格跳过] {title} 子项已合格")
                    continue

                join = it.locator('button.exam-button:has-text("参加考试")').first
                if not join.is_visible():
                    continue
                join.click()
                time.sleep(1.5)

                if self._handle_exam_dialog(exam_mode) == "return":
                    continue

                # --- 进入答题 ---
                self.log.info(f"[答题] 开始作答：{title}")
                resps = []

                def on_resp(r):
                    if "/startPaper.do" in r.url or "/refreshPaper.do" in r.url:
                        try:
                            resps.append(r.json())
                        except Exception:
                            pass

                self._page.on("response", on_resp)
                try:
                    # 弹窗确认后，可能还留在 ExamPopup，需要点【开始考试】
                    time.sleep(1.5)
                    start = self._page.locator(_SEL_START_BTN).first
                    if start.is_visible():
                        start.click(force=True)
                        time.sleep(2)
                        # 处理可能的验证码
                        for _ in range(3):
                            if not has_captcha(self._page):
                                break
                            self.log.info("[验证码] 检测到考试验证码，正在自动处理...")
                            handle_click_captcha(self._page, self.log)
                            time.sleep(2)

                    # 等待进入正式答题页
                    try:
                        self._page.wait_for_selector(
                            _SEL_QUESTION_TITLE, state="visible", timeout=10000
                        )
                    except Exception:
                        self.log.warning("答题页加载超时，尝试继续探测...")

                    self.log.info(f"考试结束，结果如下：{title}")
                    result_info = self._submit_exam()
                finally:
                    self._page.remove_listener("response", on_resp)

                # 返回列表查看最新状态
                self.log.debug("[流程] 正在返回任务列表以确认最终分值...")
                self._page.goto(f"{self.base_url}/#/learning-task-list")
                time.sleep(3)
                self._page.locator(_SEL_EXAM_TAB).first.click()
                time.sleep(2)

                score_match = re.search(r"(\d+)分", result_info)
                final_score = score_match.group(1) if score_match else "待确认"

                # 从列表中再次探测详情
                proj_el = (
                    self._page.locator(_SEL_TASK_BLOCK).filter(has_text=title).first
                )
                pass_score = "未知"
                remain_times = "0"
                if proj_el.is_visible():
                    txt = proj_el.inner_text()
                    ps_m = re.search(r"合格分数.*?(\d+)", txt)
                    if ps_m:
                        pass_score = ps_m.group(1)
                    rt_m = re.search(r"剩.*?(\d+)", txt)
                    if rt_m:
                        remain_times = rt_m.group(1)

                self.log.info(f"【考试报告】项目：{title}")
                self.log.info(f"   - 本次得分：{final_score} 分")
                self.log.info(f"   - 合格标准：{pass_score} 分")
                self.log.info(f"   - 剩余机会：{remain_times} 次")

                break

            completed.add(title)

    def _do_answering(self, q_time, q_offset, rand, rate_limit):
        """具体答题执行。"""
        matched, total = 0, 0
        for _ in range(300):
            time.sleep(1.5)

            # 实时检测是否出现结算/确认弹窗 (Vant 或 Mint-UI 风格)
            # 只要有可见的弹窗包含 "未作答"、"共" 或 "交卷" 字样，即视为已完成题目探测
            popups = self._page.locator(
                ".van-popup, .mint-popup, .confirm-sheet, .sheet"
            )
            found_popup = False
            for k in range(popups.count()):
                p = popups.nth(k)
                if p.is_visible():
                    txt = p.inner_text()
                    if any(x in txt for x in ["未作答", "共", "道题", "完成", "交卷"]):
                        self.log.info(
                            f"检测到最终交卷层: {txt.replace('\\n', ' ')[:50]}..."
                        )
                        found_popup = True
                        break
            if found_popup:
                break

            stem = self._page.locator(_SEL_QUESTION_TITLE).first
            if not stem.is_visible():
                break

            title = stem.inner_text()
            ctitle = clean_text(title)
            options = self._page.locator(_SEL_OPTIONS)

            # 查找匹配
            item = None
            for k, v in self.answers.items():
                if clean_text(k) == ctitle:
                    item = v
                    break

            ans_opts = (
                [o["content"] for o in item["optionList"] if o["isCorrect"] == 1]
                if item
                else []
            )
            total += 1

            # 构建详尽日志
            opt_texts = []
            for i in range(options.count()):
                opt_text = options.nth(i).inner_text().strip()
                cleaned_opt = clean_text(opt_text)
                is_correct = any(clean_text(a) in cleaned_opt for a in ans_opts)
                marker = "[✓] " if is_correct else "[ ] "
                opt_texts.append(f"{marker}{opt_text}")

            status = "[匹配]" if ans_opts else "[随机]"
            self.log.info(f"{status} 第 {total} 题: {title}")
            # 选项明细移至 DEBUG 模式，且不截断输出
            for ot in opt_texts:
                self.log.debug(f"      - {ot}")

            if ans_opts:
                matched += 1
                found = False
                for i in range(options.count()):
                    opt = options.nth(i)
                    octext = clean_text(opt.inner_text())
                    if any(clean_text(a) in octext for a in ans_opts):
                        self.log.debug(f"   [点击选项] {octext}")
                        opt.click(force=True, timeout=5000)
                        found = True
                if not found and rand:
                    self.log.debug("   [随机点选] 首项")
                    options.first.click(force=True, timeout=5000)
            elif rand:
                self.log.debug("   [随机点选] 首项")
                options.first.click(force=True, timeout=5000)
            else:
                next_btn = self._page.locator(
                    "button:has-text('下一题'), .van-button:has-text('下一题')"
                ).first
                if next_btn.is_visible():
                    self.log.debug("   [点击跳过] 下一题")
                    next_btn.click(force=True)

            time.sleep(random.randint(q_time, q_time + q_offset))

            next_btn = self._page.locator(_SEL_NEXT_BTN).first
            if next_btn.is_visible():
                next_btn.click()
            else:
                break

        match_rate = (matched / max(1, total)) * 100
        if match_rate >= rate_limit or rand:
            self.log.info(f"答题结束，匹配率 {match_rate:.2f}%，准备交卷")
            self._submit_exam()
        else:
            self.log.warning(f"匹配率 {match_rate:.2f}% 过低，放弃提交")
