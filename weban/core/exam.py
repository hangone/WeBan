import re
import time
import random
import threading
import logging
from typing import TYPE_CHECKING, Any, cast

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

# 提前导入工具函数
from weban.app.runtime import clean_text, ignore_symbols

_terminal_lock = threading.Lock()
logger = logging.getLogger(__name__)


class ExamMixin(BaseMixin):
    """在线考试流程 Mixin。"""

    def _handle_exam_dialog(self, exam_mode: str) -> str:
        """根据弹窗内容直接决策业务流程。"""
        time.sleep(2)
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
                "   (请输入选项编号，多选可用逗号分隔，如 '1,3'；直接 Enter 跳过此题)"
            )

            try:
                raw_choice = input("   👉 请选择: ").strip()
                if not raw_choice:
                    return False

                # 解析输入
                nums = re.split(r"[,\s，]+", raw_choice)
                indices = []
                for n in nums:
                    if n.isdigit():
                        idx = int(n) - 1
                        if 0 <= idx < options_count:
                            indices.append(idx)

                if not indices:
                    print("   ⚠️ 无效输入，已忽略。")
                    return False

                # 1. 自动执行点击
                for idx in indices:
                    options.nth(idx).click(force=True, timeout=5000)

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

        # 1. 寻找通往结算的关键路径按钮
        # 优先看有没有“交卷”或“提交”按钮（有些系统不用进答题卡也能看到）
        submit_btn = self._page.locator(SEL_SUBMIT_BTN).last
        if not submit_btn.is_visible():
            # 需要点击答题卡才能看到交卷按钮的情况
            for _ in range(3):
                card_btn = self._page.locator(SEL_ANSWER_CARD_BTN).last
                if card_btn.is_visible():
                    self.log.debug("[提交流程] 点击答题卡")
                    card_btn.click(force=True, timeout=5000)
                    time.sleep(1.5)
                    break
                time.sleep(1)

        # 2. 点击“交卷”按钮进入二级确认
        for _ in range(3):
            submit_btn = self._page.locator(SEL_SUBMIT_BTN).last
            if submit_btn.is_visible():
                btn_text = submit_btn.inner_text().strip()
                self.log.debug(f"[提交流程] 点击主要按钮: {btn_text}")
                submit_btn.click(force=True, timeout=5000)
                time.sleep(1.5)
                break
            time.sleep(1)

        # 3. 点击最终业务确认按钮（针对“确定交卷吗？”等弹窗）
        result_text = ""
        # 优先点击满足“确认”文本的选择器
        confirm_btn = self._page.locator(SEL_SUBMIT_CONFIRM).last
        if confirm_btn.is_visible():
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

                    # 等待进入正式答题页
                    try:
                        self._page.wait_for_selector(
                            SEL_QUESTION_TITLE, state="visible", timeout=10000
                        )
                    except Exception:
                        self.log.warning("答题页加载超时，尝试继续探测...")

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
        matched, total = 0, 0
        last_title = ""
        same_count = 0

        for _ in range(500):
            time.sleep(1.2)

            # 1. 检测弹窗
            popups = self._page.locator(
                ".van-popup, .mint-popup, .confirm-sheet, .sheet, .mint-msgbox"
            )
            found_popup = False
            for k in range(popups.count()):
                p = popups.nth(k)
                if p.is_visible():
                    txt = p.inner_text()
                    if any(x in txt for x in ["未作答", "共", "道题", "完成", "交卷"]):
                        self.log.info(
                            f"探测到结算/交卷层: {txt.replace('\\n', ' ')[:50]}..."
                        )
                        found_popup = True
                        break
            if found_popup:
                break

            # 2. 定位题目并提取纯净题干（尽量避开题号节点）
            stem = self._page.locator(SEL_QUESTION_TITLE).first
            if not stem.is_visible():
                # 如果找不到题目且没弹窗，尝试找一遍交卷按钮（可能在最后一题底部）
                submit_area = self._page.locator(
                    "button:has-text('交卷'), button:has-text('完成')"
                )
                if submit_area.count() > 0 and submit_area.first.is_visible():
                    self.log.info(
                        "题目区域由于页面滚动不可见，但探测到交卷按钮，准备结束。"
                    )
                    break
                break

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
                    self.log.warning("持续探测到同一题目，尝试强制点击下一题...")
                    next_btn = self._page.locator(SEL_NEXT_BTN).first
                    if next_btn.is_visible():
                        next_btn.click(force=True)
                    same_count = 0
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

                marker = "[✓] " if is_correct else "[ ] "
                # 恢复并规范展现题目字母：chr(65) = 'A'
                label = f"{chr(65 + i)}."
                opt_texts.append(f"{marker}{label} {cur_text_plain}")

            status = "[匹配]" if ans_opts else "[随机]"
            self.log.info(f"{status} 第 {total} 题: {title}")
            for ot in opt_texts:
                self.log.debug(f"      - {ot}")

            found = False
            if ans_opts:
                for i in range(options_count):
                    opt = options.nth(i)
                    octext = ignore_symbols(opt.inner_text())
                    for a in ans_opts:
                        c_a = ignore_symbols(a)
                        if c_a and octext and (c_a in octext or octext in c_a):
                            self.log.debug(
                                f"   [点击选项] {chr(65 + i)}. {all_opt_texts[i]}"
                            )
                            opt.click(force=True, timeout=5000)
                            found = True

                if found:
                    matched += 1
                    # 作答后点击下一步
                    time.sleep(random.randint(q_time, q_time + q_offset))

                    next_btn = self._page.locator(SEL_NEXT_BTN).first
                    if not next_btn.is_visible():
                        # 为了应对部分页面的动态加载，给按钮 2 秒的重试机会
                        time.sleep(2)
                        next_btn = self._page.locator(SEL_NEXT_BTN).first

                    if next_btn.is_visible():
                        next_btn.click(force=True, timeout=8000)
                        # 为了确保页面刷新，给 1 秒稳定时间
                        time.sleep(1)
                    else:
                        # 既找不到下一题，也没有弹窗，判断为单页考试或最后一小题
                        self.log.info(
                            "未发现更多【下一题】按钮，判定本轮作答已达环节末端。"
                        )
                        break
                    continue
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

                # 路径 B: 未开启随机 -> 尝试手工干预命令行交互
                if self._interactive_answering(
                    title, options, options_count, opt_texts
                ):
                    matched += 1
                    time.sleep(random.randint(q_time, q_time + q_offset))
                    next_btn = self._page.locator(SEL_NEXT_BTN).first
                    if next_btn.is_visible():
                        next_btn.click(force=True)
                        time.sleep(1)
                    continue

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
                                f".van-popup :text-is('{next_q_num}'), "
                                f".mint-popup :text-is('{next_q_num}'), "
                                f".sheet :text-is('{next_q_num}'), "
                                f".answer-sheet :text-is('{next_q_num}'), "
                                f".sheet-item:has-text('{next_q_num}')"
                            ).first

                            if not jump_target.is_visible():
                                jump_target = (
                                    self._page.locator(
                                        f"div:text-is('{next_q_num}'), span:text-is('{next_q_num}')"
                                    )
                                    .filter(has_not=self._page.locator(".quest-stem"))
                                    .last
                                )

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
