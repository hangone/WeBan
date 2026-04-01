import re
import time
import random
import threading
import logging
from typing import TYPE_CHECKING, Any, Dict, cast

from .const import (
    SEL_EXAM_TAB,
    SEL_START_BTN,
    SEL_DIALOG,
    SEL_CONFIRM_BTN,
    SEL_QUESTION_TITLE,
    SEL_OPTIONS,
    SEL_NEXT_BTN,
    SEL_SUBMIT_BTN,
    SEL_SUBMIT_CONFIRM,
    SEL_TASK_BLOCK,
    SEL_TASK_BLOCK_TITLE,
    SEL_QUEST_INDICATOR,
    SEL_ANSWER_CARD_BTN,
    SEL_COURSE_LIST_MARKERS,
)
from .captcha import handle_click_captcha, has_captcha
from .base import BaseMixin
from playwright._impl._errors import TargetClosedError

# 提前导入工具函数
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

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """根据弹窗内容直接决策业务流程。"""
        time.sleep(2)
        if not self._page:
            raise RuntimeError("Page is not initialized")
        dialogs = self._page.locator(SEL_DIALOG)

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
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            # 2. 流程类（清理数据）
            if any(k in text for k in ["未提交", "重新进入", "继续考试", "清除"]):
                self.log.info(f"流程提示: {text}")
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                continue

            # 3. 及格判定
            is_passed = any(k in text for k in ["已合格", "已及格", "考试通过"]) or (
                "合格" in text and "最高成绩" in text
            )
            if is_passed and exam_mode == "true":
                self.log.info(f"及格后不考试: 检测到已达标 ({text})")
                btn = d.locator(SEL_CONFIRM_BTN).first
                if btn.is_visible():
                    btn.click(timeout=5000)
                return "return"

            # 常规弹窗确认
            btn = d.locator(SEL_CONFIRM_BTN).first
            if btn.is_visible():
                btn.click(timeout=5000)

        return "continue"

    def _get_exam_page_context(self) -> str:
        """基于统一页面状态识别考试流程所在上下文。"""
        if not self._page:
            return "unknown"

        page_state = self._ensure_page_state()
        try:
            if (
                self._page.locator(
                    ".score-num, .score, .exam-score, .result-score, .score-text"
                ).count()
                > 0
            ):
                return "result"
            if (
                self._page.locator(SEL_QUESTION_TITLE).count() > 0
                and self._page.locator(SEL_OPTIONS).count() > 0
            ):
                return "question"
            if self._page.locator(".exam-item").count() > 0:
                return "exam-list"
            if self._page.locator(SEL_EXAM_TAB).count() > 0:
                return "course"
        except Exception:
            pass

        state = str(page_state.get("state", "unknown"))
        if state == "exam_question":
            return "question"
        if state == "exam_result":
            return "result"
        if state == "exam":
            return "exam"

        return "unknown"

    def _wait_for_exam_context(
        self, expected_contexts: set[str], timeout_sec: float = 10
    ) -> bool:
        """等待考试页面进入预期上下文，避免 SPA 切页中途误判。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            exam_context = self._get_exam_page_context()
            if exam_context in expected_contexts:
                return True
            time.sleep(0.5)
        return False

    def _interactive_answering(
        self, title: str, options: Any, options_count: int, opt_texts: list[str]
    ) -> bool:
        """多线程安全的手工干预命令行交互。"""

        with _terminal_lock:
            print("\n" + "🚀" + "=" * 60)
            print(f"   【人工干预请求】 用户: {getattr(self, 'account', '未知')}")
            print(f"   项目: {getattr(self, 'tenant_name', '未知')}")
            print(f"   题目: {title}")
            print("-" * 64)
            for i in range(options_count):
                # 剥离展示前缀 [ ]
                display_text = (
                    opt_texts[i][4:] if len(opt_texts[i]) > 4 else opt_texts[i]
                )
                print(f"    {i + 1}. {display_text}")
            print("-" * 64)
            print(
                "   (请输入选项编号，多选可用逗号分隔，如 '1,3'；"
                "直接 Enter 表示你将改为在网页上手动勾选，勾选完后再按 Enter 继续)"
            )

            try:
                raw_choice = input("   请选择: ").strip()

                indices = []

                # A) 终端输入：自动点击并记录
                if raw_choice:
                    nums = re.split(r"[,\s，]+", raw_choice)
                    for n in nums:
                        if n.isdigit():
                            idx = int(n) - 1
                            if 0 <= idx < options_count:
                                indices.append(idx)

                    if not indices:
                        print("   ⚠️ 无效输入，已忽略。")
                        return False

                    for idx in indices:
                        options.nth(idx).click(force=True, timeout=5000)

                # B) 网页手动：等待用户勾选后读取 selected class 并记录
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
                        print("   ⚠️ 未检测到任何已选选项，本题未记录。")
                        return False

                # 2. 将此正确答案记入题库（内存 + 持久化）
                new_item = {
                    "optionList": [],
                    "type": "手工输入",  # 暂时标记，后续合并会归一化
                }
                for i in range(options_count):
                    raw_opt_text = options.nth(i).inner_text().strip()
                    new_item["optionList"].append(
                        {"content": raw_opt_text, "isCorrect": 1 if i in indices else 2}
                    )

                # 找到当前题目的 Key（尝试 cleaned 匹配以防重复）
                target_key = title
                c_title = clean_text(title)
                for existing_raw in list(self.answers.keys()):
                    if clean_text(existing_raw) == c_title:
                        target_key = existing_raw  # 沿用题库中的原始 Key
                        break

                self.answers[target_key] = new_item
                if hasattr(self, "_save_answers"):
                    # 使用并发锁保护的持久化方法
                    getattr(self, "_save_answers")()

                print("   ✅ 已成功点选并记录到题库！")
                return True

            except Exception as e:
                print(f"   ❌ 交互出错: {e}")
                return False
            finally:
                print("=" * 62 + "\n")

    def _submit_exam(self) -> str:
        """交卷并确认，返回抓取到的得分信息。"""
        # 检查 _page 是否已初始化
        if not self._page:
            raise RuntimeError("Page is not initialized")

        # 0. 预检查：是否已经在结算页（看到分数即视为已完成）
        try:
            score_el = self._page.locator(
                ".score-num, .score, .exam-score, .result-score, .score-text"
            ).first
            if score_el.count() > 0 and score_el.is_visible():
                txt = score_el.inner_text().strip()
                if txt:
                    self.log.info(f"[提交流程] 检测到已处于结算页，得分: {txt}")
                    return f"已在结算页: {txt}"
        except Exception:
            pass

        # 1. 进入“答题卡/交卷”路径（参考前端 ExamPage.vue：.sheet / .confirm-sheet）
        # - 交卷按钮可能只出现在答题卡(sheet)里
        # - 点击交卷后，可能进入 confirm-sheet 再次交卷
        # - 也可能弹出 dialog 需要最终确认
        result_text = ""

        # 1.1 尝试打开答题卡(sheet)，确保交卷按钮出现
        for _ in range(3):
            try:
                sheet = self._page.locator(".sheet").first
                if sheet.count() > 0 and sheet.is_visible():
                    break
            except Exception:
                pass

            try:
                card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).last
                if card_btn.count() > 0 and card_btn.is_visible():
                    self.log.debug("[提交流程] 点击答题卡")
                    card_btn.click(force=True, timeout=5000)
                    time.sleep(1.2)
                    continue
            except Exception:
                pass

            break

        # 1.2 点击 sheet 内的“交卷”（优先使用 SEL_SUBMIT_BTN，其次用 src 路径兜底）
        clicked_submit = False
        for _ in range(3):
            try:
                submit_btn = self._page.locator(SEL_SUBMIT_BTN).last
                if submit_btn.count() > 0 and submit_btn.is_visible():
                    btn_text = submit_btn.inner_text().strip()
                    self.log.debug(f"[提交流程] 点击主要按钮: {btn_text}")
                    submit_btn.click(force=True, timeout=5000)
                    time.sleep(1.5)
                    clicked_submit = True
                    break
            except Exception:
                pass

            # src 兜底：.sheet .bottom-ctrls 里的“交卷”
            try:
                sheet_submit = (
                    self._page.locator(".sheet .bottom-ctrls").locator("text=交卷").last
                )
                if sheet_submit.count() > 0 and sheet_submit.is_visible():
                    self.log.debug("[提交流程] 点击答题卡内交卷")
                    sheet_submit.click(force=True, timeout=5000)
                    time.sleep(1.5)
                    clicked_submit = True
                    break
            except Exception:
                pass

            time.sleep(1)

        # 1.3 若进入 confirm-sheet，再次点击 confirm-sheet 内的“交卷”
        if clicked_submit:
            for _ in range(3):
                try:
                    confirm_sheet_submit = (
                        self._page.locator(".confirm-sheet .bottom-ctrls")
                        .locator("text=交卷")
                        .last
                    )
                    if (
                        confirm_sheet_submit.count() > 0
                        and confirm_sheet_submit.is_visible()
                    ):
                        self.log.debug("[提交流程] 点击确认页交卷")
                        confirm_sheet_submit.click(force=True, timeout=5000)
                        time.sleep(2)
                        break
                except Exception:
                    pass
                time.sleep(1)

        # 2. 点击最终业务确认按钮（针对“确定交卷吗？”等弹窗）
        confirm_btn = self._page.locator(SEL_SUBMIT_CONFIRM).last
        if confirm_btn.count() > 0 and confirm_btn.is_visible():
            try:
                # 获取弹窗背景文字内容（如果有）
                dialog_box = (
                    self._page.locator(SEL_DIALOG).filter(has=confirm_btn).first
                )
                if dialog_box.is_visible():
                    result_text = dialog_box.inner_text().strip()
            except Exception:
                pass

            # 异常流程拦截：保护由于网络或页面抖动引起的进度丢失弹窗
            if result_text and any(
                k in result_text for k in ["清除", "重考", "重新进入", "未提交"]
            ):
                self.log.warning(
                    f"[提交流程] 探测到异常进度对话框，拦截点击以保护数据: {result_text}"
                )
                return result_text

            self.log.debug(
                f"[提交流程] 点击最终确认按钮... 弹窗内容概览: {result_text.replace('\\n', ' ')[:60]}..."
            )
            confirm_btn.click(force=True, timeout=5000)
            time.sleep(3)

        # 4. 最后尝试捕获得分（可能在点击确认后才加载出来的结算页面上）
        try:
            score_num_el = self._page.locator(
                ".score-num, .score, .exam-score, .result-score, .score-text"
            ).first
            if score_num_el.count() > 0 and score_num_el.is_visible(timeout=3000):
                final_score_str = score_num_el.inner_text().strip()
                if final_score_str:
                    result_text += f" (最终得分: {final_score_str})"
                    self.log.info(f"[提交流程] 成功捕获分值：{final_score_str}")
        except Exception:
            pass

        return result_text

    def _handle_exam_intermediate_pages(self) -> bool:
        """处理进入考试前的中间页面。"""
        # 检查 _page 是否已初始化
        if not self._page:
            return False

        for _ in range(6):
            time.sleep(2)
            if self._page.locator(SEL_EXAM_TAB).count() > 0:
                return True
            if self._page.locator(SEL_COURSE_LIST_MARKERS).count() > 0:
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
            ok = self._page.locator(SEL_CONFIRM_BTN)
            if ok.count() > 0 and ok.first.is_visible():
                ok.first.click()
                continue

        return self._page.locator(SEL_EXAM_TAB).count() > 0

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

        # 检查 _page 是否已初始化
        if not self._page:
            raise RuntimeError("Page is not initialized")

        self.log.info("开始执行考试流程...")
        self._page.goto(f"{self.base_url}/#/learning-task-list")
        time.sleep(2)

        proj_count = self._page.locator(SEL_TASK_BLOCK).count()
        completed = set()

        for i in range(proj_count):
            self._page.goto(f"{self.base_url}/#/learning-task-list")
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

            # 切换 Tab
            tab = self._page.locator(SEL_EXAM_TAB).first
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
                    start = self._page.locator(SEL_START_BTN).first
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

                    # 等待进入正式答题页或结果页，避免 SPA 尚未切换完成就开始按题目逻辑执行
                    if not self._wait_for_exam_context(
                        {"question", "result"}, timeout_sec=10
                    ):
                        page_state = self._ensure_page_state()
                        self.log.warning(
                            f"答题页加载超时，当前 state={page_state['state']} "
                            f"url={page_state['url'] or '<blank>'}，尝试继续探测..."
                        )

                    # --- 执行题目搜索与勾选 ---
                    should_submit = self._do_answering(
                        q_time=exam_question_time,
                        q_offset=exam_question_time_offset,
                        rand=random_answer,
                        rate_limit=exam_submit_match_rate,
                    )

                    if should_submit:
                        self.log.info(f"答题结束，准备交卷：{title}")
                        result_info = self._submit_exam()
                    else:
                        self.log.warning(f"匹配率过低且未开启随机，放弃交卷：{title}")
                        result_info = "放弃提交"
                finally:
                    self._page.remove_listener("response", on_resp)

                # 返回列表查看最新状态
                self.log.debug("[流程] 正在返回任务列表以确认最终分值...")
                self._page.goto(f"{self.base_url}/#/learning-task-list")
                time.sleep(3)

                # 1. 精确匹配项目卡片并读取信息（合格标准/剩余机会/最新成绩）
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
                    # 模糊兜底
                    proj_item = (
                        self._page.locator(SEL_TASK_BLOCK).filter(has_text=title).first
                    )

                pass_score = "未知"
                remain_times = "0"

                if proj_item.count() > 0 and proj_item.is_visible():
                    txt = proj_item.inner_text().strip()
                    # 增强正则表达式匹配，兼容“合格分数”、“标准”、“及格”等多种描述
                    ps_m = re.search(r"(?:合格分数|合格|标准|及格).*?(\d+)", txt)
                    if ps_m:
                        pass_score = ps_m.group(1)
                    rt_m = re.search(r"(?:剩余机会|剩|机会).*?(\d+)", txt)
                    if rt_m:
                        remain_times = rt_m.group(1)

                    # 2. 点击进入项目，这样才能看到并切换“在线考试”Tab
                    proj_item.click(force=True)
                    time.sleep(2)

                # 3. 尝试切换到“在线考试”Tab（如果存在），以触发可能的页面状态更新
                exam_tab = self._page.locator(SEL_EXAM_TAB).first
                if exam_tab.count() > 0 and exam_tab.is_visible():
                    try:
                        exam_tab.click(timeout=5000)
                        time.sleep(1.5)
                    except Exception:
                        pass

                # 从提交时捕获的对话框文本中解析得分
                score_match = re.search(r"(\d+)分", result_info)
                final_score = score_match.group(1) if score_match else "待确认"

                self.log.info(f"【考试报告】项目：{title}")
                self.log.info(f"   - 本次得分：{final_score} 分")
                self.log.info(f"   - 合格标准：{pass_score} 分")
                self.log.info(f"   - 剩余机会：{remain_times} 次")

                break

            completed.add(title)

    def _do_answering(self, q_time, q_offset, rand, rate_limit):
        """具体答题执行。"""
        # 检查 _page 是否已初始化
        if not self._page:
            raise RuntimeError("Page is not initialized")
        page = self._page

        matched, total = 0, 0
        last_title = ""
        same_count = 0
        force_advance_attempts = 0

        for _ in range(500):
            time.sleep(1.2)

            page_state = self._ensure_page_state()
            exam_context = self._get_exam_page_context()

            if exam_context == "result":
                self.log.info("检测到已进入考试结果页，结束答题循环。")
                break

            if exam_context not in {"question", "exam", "unknown"}:
                self.log.debug(
                    f"[答题] 当前页面上下文为 {exam_context}，"
                    f"state={page_state['state']} url={page_state['url'] or '<blank>'}"
                )
                time.sleep(1)
                continue

            # 1. 检测弹窗
            try:
                popups = page.locator(
                    ".van-popup, .mint-popup, .confirm-sheet, .sheet, .mint-msgbox"
                )
                found_popup = False
                for k in range(popups.count()):
                    p = popups.nth(k)
                    if p.is_visible():
                        txt = p.inner_text()
                        if any(
                            x in txt for x in ["未作答", "共", "道题", "完成", "交卷"]
                        ):
                            self.log.info(
                                f"探测到结算/交卷层: {txt.replace('\\n', ' ')[:50]}..."
                            )
                            found_popup = True
                            break
                if found_popup:
                    break
            except TargetClosedError:
                # 页面/浏览器被关闭时，直接退出答题循环，避免抛栈导致任务失败
                self.log.warning("检测到页面已关闭，提前结束答题循环。")
                break

            # 2. 定位题目并提取纯净题干（尽量避开题号节点）
            stem = self._page.locator(SEL_QUESTION_TITLE).first
            if not stem.is_visible():
                exam_context = self._get_exam_page_context()
                if exam_context == "result":
                    self.log.info("题目区域不可见，且已切换到结果页，准备结束。")
                    break

                # 如果找不到题目且没弹窗，尝试找一遍交卷按钮（可能在最后一题底部）
                submit_area = self._page.locator(
                    "button:has-text('交卷'), button:has-text('完成')"
                )
                if submit_area.count() > 0 and submit_area.first.is_visible():
                    self.log.info(
                        "题目区域由于页面滚动不可见，但探测到交卷按钮，准备结束。"
                    )
                    break

                page_state = self._ensure_page_state()
                self.log.debug(
                    f"[答题] 题目区域暂不可见，当前 context={exam_context} "
                    f"state={page_state['state']} url={page_state['url'] or '<blank>'}"
                )
                continue

            # 尝试从子节点获取纯文本（避开 .index 等）
            raw_title = ""
            for sub_sel in [".title", ".quest-title", ".stem-text"]:
                t_el = stem.locator(sub_sel).first
                if t_el.count() > 0 and t_el.is_visible():
                    raw_title = t_el.inner_text().strip()
                    break

            if not raw_title:
                raw_title = stem.inner_text().strip()

            # 立即清洗：去除冗余的题号前缀（源自 clean_text 逻辑，但在提取处直接生效）
            title = re.sub(r"^\s*\d+[\.、\s]+", "", raw_title)
            if not title:
                continue

            # 检测是否重复
            if title == last_title:
                same_count += 1
                if same_count > 6:
                    force_advance_attempts += 1
                    if force_advance_attempts >= 20:
                        self.log.warning("持续无法推进到下一题，提前结束答题循环。")
                        break

                    self.log.warning("持续探测到同一题目，尝试强制推进到下一题...")
                    advanced = False

                    # 用题号指示器/题干变化来判断是否真正推进成功
                    prev_title = title
                    prev_indicator = ""
                    try:
                        ind0 = page.locator(SEL_QUEST_INDICATOR).first
                        if ind0.count() > 0 and ind0.is_visible():
                            prev_indicator = ind0.inner_text().strip()
                    except Exception:
                        pass

                    def _title_now_quick() -> str:
                        try:
                            stem_now = page.locator(SEL_QUESTION_TITLE).first
                            if stem_now.count() == 0 or not stem_now.is_visible():
                                return ""
                            raw = stem_now.inner_text().strip()
                            return re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()
                        except Exception:
                            return ""

                    def _indicator_now_quick() -> str:
                        try:
                            ind_now = page.locator(SEL_QUEST_INDICATOR).first
                            if ind_now.count() == 0 or not ind_now.is_visible():
                                return ""
                            return ind_now.inner_text().strip()
                        except Exception:
                            return ""

                    def _wait_any_change(timeout_sec: float = 5) -> bool:
                        deadline2 = time.time() + timeout_sec
                        while time.time() < deadline2:
                            cur_t = _title_now_quick()
                            if cur_t and cur_t != prev_title:
                                return True
                            if prev_indicator:
                                cur_i = _indicator_now_quick()
                                if cur_i and cur_i != prev_indicator:
                                    return True
                            time.sleep(0.25)
                        return False

                    # 1) 优先尝试点击【下一题】（先用既有选择器，再用 src 对应的 bottom-ctrls 文本兜底）
                    try:
                        next_btn = page.locator(SEL_NEXT_BTN).first
                        if next_btn.count() > 0:
                            next_btn.scroll_into_view_if_needed()
                            if next_btn.is_visible():
                                next_btn.click(force=True, timeout=8000)
                                advanced = _wait_any_change()

                        if not advanced:
                            next_btn2 = (
                                page.locator(".bottom-ctrls")
                                .locator("text=下一题")
                                .first
                            )
                            if next_btn2.count() > 0 and next_btn2.is_visible():
                                next_btn2.scroll_into_view_if_needed()
                                next_btn2.click(force=True, timeout=8000)
                                advanced = _wait_any_change()
                    except TargetClosedError:
                        self.log.warning("检测到页面已关闭，提前结束答题循环。")
                        break
                    except Exception:
                        pass

                    # 2) 若找不到/点不动，则用【答题卡】跳转兜底（src ExamPage.vue: .sheet .quest-indexs-list li > span 为题号）
                    if not advanced:
                        try:
                            indicator = page.locator(SEL_QUEST_INDICATOR).first
                            current_idx = 0
                            if indicator.count() > 0 and indicator.is_visible():
                                m = re.search(
                                    r"(\d+)\s*/\s*(\d+)", indicator.inner_text()
                                )
                                if m:
                                    current_idx = int(m.group(1))

                            card_btn = page.locator(SEL_ANSWER_CARD_BTN).last
                            if not (card_btn.count() > 0 and card_btn.is_visible()):
                                card_btn = (
                                    page.locator(".bottom-ctrls")
                                    .locator("text=答题卡")
                                    .first
                                )

                            if card_btn.count() > 0 and card_btn.is_visible():
                                card_btn.scroll_into_view_if_needed()
                                card_btn.click(force=True, timeout=5000)
                                time.sleep(1.2)

                                target_nums = [
                                    str(current_idx + 1),
                                    f"{current_idx + 1:02d}",
                                ]
                                for next_q_num in target_nums:
                                    jump_target = page.locator(
                                        f".sheet .quest-indexs-list li:has(span:text-is('{next_q_num}')), "
                                        f".sheet .quest-indexs-list li:has-text('{next_q_num}')"
                                    ).first
                                    if (
                                        jump_target.count() > 0
                                        and jump_target.is_visible()
                                    ):
                                        jump_target.click(force=True, timeout=5000)
                                        advanced = _wait_any_change()
                                        break
                        except TargetClosedError:
                            self.log.warning("检测到页面已关闭，提前结束答题循环。")
                            break
                        except Exception:
                            pass

                    same_count = 0
                    if advanced:
                        time.sleep(0.8)
                continue

            same_count = 0
            last_title = title
            options = self._page.locator(SEL_OPTIONS)
            total += 1

            # 3. 匹配答案：答题环节忽略符号匹配
            ctitle_ignore = ignore_symbols(title)
            item = None

            for k, v in self.answers.items():
                if ignore_symbols(k) == ctitle_ignore:
                    item = v
                    break

            ans_opts = (
                [o["content"] for o in item["optionList"] if o["isCorrect"] == 1]
                if item
                else []
            )

            # 4. 日志并点击
            opt_texts = []
            all_opt_texts = []
            options_count = options.count()
            for i in range(options_count):
                raw_opt = options.nth(i).inner_text().strip()
                # 去除 A. B. C. 前缀后的纯文本（用于日志展示）
                cur_text_plain = re.sub(r"^[A-Z0-9][\s\n\.、]+", "", raw_opt).strip()
                all_opt_texts.append(cur_text_plain)

                is_correct = False
                if ans_opts:
                    cleaned_opt_on_page = ignore_symbols(raw_opt)
                    for a in ans_opts:
                        # 选项匹配忽略符号以确保鲁棒性
                        cleaned_ans = ignore_symbols(a)
                        if (
                            cleaned_ans
                            and cleaned_opt_on_page
                            and (
                                cleaned_ans in cleaned_opt_on_page
                                or cleaned_opt_on_page in cleaned_ans
                            )
                        ):
                            is_correct = True
                            break

                label = f"{chr(65 + i)}. {cur_text_plain}"
                opt_texts.append(f"✓ {label}" if is_correct else label)

            # ---- 输出题目类型与答题进度（参考前端 ExamPage.vue：.quest-category / .quest-indicator）----
            q_type = ""
            progress = ""
            q_no = total
            try:
                q_type = page.locator(".quest-category").first.inner_text().strip()
            except Exception:
                q_type = ""

            try:
                progress = page.locator(SEL_QUEST_INDICATOR).first.inner_text().strip()
                m = re.search(r"(\d+)\s*/\s*(\d+)", progress)
                if m:
                    q_no = int(m.group(1))
            except Exception:
                progress = ""

            head_parts = []
            if q_type:
                head_parts.append(q_type)
            if progress:
                head_parts.append(progress)
            head = " ".join(head_parts).strip()

            if head:
                self.log.info(f"{head} {q_no}. {title}")
            else:
                self.log.info(f"{q_no}. {title}")

            for ot in opt_texts:
                self.log.debug(ot)

            found = False
            if ans_opts:
                for i in range(options_count):
                    opt = options.nth(i)
                    octext = ignore_symbols(opt.inner_text())
                    for a in ans_opts:
                        c_a = ignore_symbols(a)
                        if c_a and octext and (c_a in octext or octext in c_a):
                            opt.click(force=True, timeout=5000)
                            # 确保点击生效：ExamPage.vue 中选中态会给 quest-option-item 增加 selected class
                            try:
                                for _ in range(15):
                                    cls = (opt.get_attribute("class") or "").lower()
                                    if "selected" in cls:
                                        break
                                    time.sleep(0.1)
                            except Exception:
                                pass
                            found = True

                if found:
                    matched += 1
                    time.sleep(random.randint(q_time, q_time + q_offset))

                    # 记录推进前的题目/指示器，用于判断是否真正切到下一题
                    prev_title = title
                    prev_indicator = ""
                    try:
                        ind0 = page.locator(SEL_QUEST_INDICATOR).first
                        if ind0.count() > 0 and ind0.is_visible():
                            prev_indicator = ind0.inner_text().strip()
                    except Exception:
                        pass

                    def _title_now() -> str:
                        try:
                            stem_now = page.locator(SEL_QUESTION_TITLE).first
                            if stem_now.count() == 0 or not stem_now.is_visible():
                                return ""
                            raw = stem_now.inner_text().strip()
                            return re.sub(r"^\s*\d+[\.、\s]+", "", raw).strip()
                        except Exception:
                            return ""

                    def _wait_changed(
                        prev_t: str, prev_ind: str, timeout_sec: float = 6
                    ) -> bool:
                        deadline2 = time.time() + timeout_sec
                        while time.time() < deadline2:
                            cur_t = _title_now()
                            if cur_t and cur_t != prev_t:
                                return True
                            if prev_ind:
                                try:
                                    ind2 = page.locator(SEL_QUEST_INDICATOR).first
                                    if ind2.count() > 0 and ind2.is_visible():
                                        cur_ind = ind2.inner_text().strip()
                                        if cur_ind and cur_ind != prev_ind:
                                            return True
                                except Exception:
                                    pass
                            time.sleep(0.25)
                        return False

                    advanced = False

                    # 1) 尝试点击【下一题】（有些页面按钮存在但点击无效，因此必须等待题目变化）
                    try:
                        next_btn = page.locator(SEL_NEXT_BTN).first
                        if next_btn.count() > 0:
                            next_btn.scroll_into_view_if_needed()
                            if next_btn.is_visible():
                                next_btn.click(force=True, timeout=8000)
                                advanced = _wait_changed(
                                    prev_title, prev_indicator, timeout_sec=6
                                )
                    except Exception:
                        advanced = False

                    if advanced:
                        time.sleep(0.5)
                        continue

                    # 2) 【答题卡】兜底跳转（src ExamPage.vue: .sheet .quest-indexs-list li > span 为题号）
                    current_idx = 0
                    try:
                        indicator = self._page.locator(SEL_QUEST_INDICATOR).first
                        if indicator.count() > 0 and indicator.is_visible():
                            m = re.search(r"(\d+)\s*/\s*(\d+)", indicator.inner_text())
                            if m:
                                current_idx = int(m.group(1))
                    except Exception:
                        current_idx = 0

                    found_jump = False
                    try:
                        card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).last
                        if card_btn.count() > 0 and card_btn.is_visible():
                            card_btn.scroll_into_view_if_needed()
                            card_btn.click(force=True, timeout=5000)
                            time.sleep(1.2)

                            target_nums = [
                                str(current_idx + 1),
                                f"{current_idx + 1:02d}",
                            ]
                            for next_q_num in target_nums:
                                jump_target = self._page.locator(
                                    f".sheet .quest-indexs-list li:has(span:text-is('{next_q_num}')), "
                                    f".sheet .quest-indexs-list li:has-text('{next_q_num}')"
                                ).first
                                if jump_target.count() > 0 and jump_target.is_visible():
                                    jump_target.click(force=True, timeout=5000)
                                    found_jump = _wait_changed(
                                        prev_title, prev_indicator, timeout_sec=6
                                    )
                                    break
                    except Exception:
                        found_jump = False

                    if found_jump:
                        time.sleep(0.5)
                        continue

                    # 3) 仍无法推进：检查是否已到结算/交卷阶段
                    exam_context = self._get_exam_page_context()
                    if exam_context == "result":
                        self.log.info("检测到已进入考试结果页，结束答题循环。")
                        break

                    submit_area = self._page.locator(
                        "button:has-text('交卷'), button:has-text('完成')"
                    )
                    if submit_area.count() > 0 and submit_area.first.is_visible():
                        self.log.info("当前已无下一题，检测到交卷入口，结束答题循环。")
                        break

                    try:
                        popups = page.locator(
                            ".van-popup, .mint-popup, .confirm-sheet, .sheet, .mint-msgbox"
                        )
                        for k in range(popups.count()):
                            p = popups.nth(k)
                            if p.is_visible():
                                txt = p.inner_text()
                                if any(
                                    x in txt
                                    for x in ["未作答", "共", "道题", "完成", "交卷"]
                                ):
                                    self.log.info(
                                        f"探测到结算/交卷层: {txt.replace('\\n', ' ')[:50]}..."
                                    )
                                    break
                        else:
                            # 4) 最后兜底：不直接判定“只有 1 题”，给 SPA 多一点切页时间
                            self.log.warning(
                                "无法定位可用的下一题入口，继续等待页面切换或题目刷新。"
                            )
                            time.sleep(2.0)
                            continue
                        break
                    except TargetClosedError:
                        self.log.warning("检测到页面已关闭，提前结束答题循环。")
                        break
                else:
                    self.log.warning(
                        f"   [警告] 题库有匹配但无法定位到具体选项: {ans_opts}"
                    )

            # 匹配失败时的处理路径
            if not ans_opts:
                # 路径 A: 开启了随机答题 -> 直接点选首项
                if rand:
                    self.log.debug("   [随机点选] A")
                    options.first.click(force=True, timeout=5000)
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    next_btn = self._page.locator(SEL_NEXT_BTN).first
                    if next_btn.is_visible():
                        next_btn.click(force=True, timeout=8000)
                        time.sleep(1)
                    continue

                # 路径 B: 未开启随机 -> 必须人工处理（终端输入 或 网页手动勾选）
                self.log.warning("题库缺失：请在终端输入答案，或在网页手动勾选后继续。")
                ok = self._interactive_answering(
                    title, options, options_count, opt_texts
                )
                if ok:
                    matched += 1
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    next_btn = page.locator(SEL_NEXT_BTN).first
                    if next_btn.count() > 0 and next_btn.is_visible():
                        next_btn.click(force=True)
                        time.sleep(1)
                    continue

                self.log.warning("题库缺失且未完成人工作答，停止作答循环。")
                break

                # 路径 C: 交互跳过或失败 -> 尝试通过答题卡跳转或兜底前进
                msg = "手工交互已跳过"
                self.log.warning(f"   [流程] 第 {total} 题{msg}，尝试通过答题卡跳转...")
                try:
                    # 1. 获取当前题号
                    indicator = self._page.locator(SEL_QUEST_INDICATOR).first
                    current_idx = 0
                    if indicator.is_visible():
                        match = re.search(r"(\d+)\s*/\s*(\d+)", indicator.inner_text())
                        if match:
                            current_idx = int(match.group(1))

                    # 2. 点击答题卡按钮
                    card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).last
                    if card_btn.is_visible():
                        card_btn.click(force=True)
                        time.sleep(1.5)

                        # 3. 寻找下一题的按钮
                        target_nums = [str(current_idx + 1), f"{current_idx + 1:02d}"]
                        found_jump = False
                        for next_q_num in target_nums:
                            jump_target = self._page.locator(
                                f".sheet .quest-indexs-list li:has(span:text-is('{next_q_num}')), "
                                f".sheet .quest-indexs-list li:has-text('{next_q_num}')"
                            ).first

                            if not jump_target.is_visible():
                                jump_target = self._page.locator(
                                    f".sheet span:text-is('{next_q_num}'), .sheet div:text-is('{next_q_num}')"
                                ).first

                            if jump_target.is_visible():
                                self.log.info(
                                    f"   [答题卡] 成功定位下一题按钮({next_q_num})，正在跳转..."
                                )
                                jump_target.click(force=True)
                                time.sleep(1.5)
                                found_jump = True
                                break

                        if found_jump:
                            continue

                    # 4. 兜底策略：强制点击第一个选项作为最后的同步手段
                    if options.count() > 0:
                        self.log.warning(
                            "   [兜底] 交互失败且无法跳转，尝试点选首项并前进..."
                        )
                        options.first.click(force=True)
                        time.sleep(1)
                        next_btn = self._page.locator(SEL_NEXT_BTN).first
                        if next_btn.is_visible():
                            next_btn.click(force=True)
                            continue

                    self.log.warning("   [拦截] 无法跳转也无法推进，停止作答。")
                    break
                except Exception as e:
                    self.log.error(f"   [跳转异常] {e}")
                    break

        match_rate = (matched / max(1, total)) * 100
        self.log.info(
            f"答题循环结束，总计 {total} 题，匹配 {matched} 题，匹配率 {match_rate:.2f}%"
        )
        return match_rate >= rate_limit or rand
