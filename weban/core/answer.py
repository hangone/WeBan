"""
answer.py — 题库加载与合并模块

职责：
  - _load_answers         : 从本地 answer/answer.json 加载题库
  - _merge_cloud_answers  : 从远端 GitHub / CDN 拉取最新 answer/answer.json 并合并到内存
  - _merge_history_answers: 通过页面点击历史考试记录，从 reviewPaper 接口捕获答案
"""

import json
import os
import logging
import time

from typing import TYPE_CHECKING, Any, Dict

logger = logging.getLogger(__name__)

_SEL_TASK_BLOCK = ".task-block"
_SEL_TASK_BLOCK_TITLE = ".task-block-title"
_SEL_COURSE_READY = ".van-tab, .van-collapse-item, .img-texts-item, .fchl-item"
_SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
_SEL_EXAM_BUTTON = "button.exam-button"
_SEL_EXAM_RECORD_BTN = 'button.exam-button:has-text("考试记录")'
_SEL_REVIEW_LIST_ITEM = ".examPeviewListp-item"
_SEL_REVIEW_DETAIL_LINK = "div.examPeviewListp-item-color"
_SEL_REVIEW_RESULT_READY = ".quest-stem, .quest-option-item"
_SEL_REVIEW_BACK_BTN = "button:has-text('返回'), .van-nav-bar__left, .back-btn"


class AnswerMixin:
    if TYPE_CHECKING:
        from typing import Union as _Union
        from playwright.sync_api import Page, BrowserContext, Browser, Playwright
        from .browser import BrowserConfig
        import logging as _logging

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

    # ------------------------------------------------------------------
    # 工具方法：等待页面选择器出现，失败时静默返回 False
    # ------------------------------------------------------------------

    def _wait_for(self, selector: str, timeout: int = 8000) -> bool:
        """等待页面中指定选择器出现，在 timeout 毫秒内未出现则返回 False。"""
        try:
            self._page.wait_for_selector(selector, state="attached", timeout=timeout)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 本地题库
    # ------------------------------------------------------------------

    def _load_answers(self) -> Dict[str, Any]:
        """从项目根目录的 answer/answer.json 加载本地题库；文件不存在或损坏时返回空字典。"""
        from weban.app.runtime import get_base_path

        base_path = get_base_path()
        answer_path = os.path.join(base_path, "answer", "answer.json")
        if not os.path.exists(answer_path):
            self.log.warning("题库文件不存在，考试将默认按兜底策略作答")
            return {}
        try:
            with open(answer_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.log.warning(f"读取题库失败: {e}")
        return {}

    # ------------------------------------------------------------------
    # 云端题库合并
    # ------------------------------------------------------------------

    def _merge_cloud_answers(self) -> None:
        """从 GitHub / CDN 镜像拉取最新 answer/answer.json，与本地题库合并（新答案优先）。
        依次尝试多个 URL，任意一个成功即停止。
        """
        import urllib.request

        urls = [
            "https://github.com/hangone/WeBan/raw/main/answer/answer.json",
            "https://ghfast.top/https://github.com/hangone/WeBan/raw/main/answer/answer.json",
            "https://fastly.jsdelivr.net/gh/hangone/WeBan@main/answer/answer.json",
        ]
        for url in urls:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        data = json.loads(response.read().decode("utf-8"))
                        for title, item in data.items():
                            old_opts = {
                                o["content"]: o["isCorrect"]
                                for o in self.answers.get(title, {}).get(
                                    "optionList", []
                                )
                            }
                            new_opts = old_opts | {
                                o["content"]: o["isCorrect"]
                                for o in item.get("optionList", [])
                            }
                            self.answers[title] = {
                                "type": item.get("type", ""),
                                "optionList": [
                                    {"content": content, "isCorrect": is_correct}
                                    for content, is_correct in new_opts.items()
                                ],
                            }
                        self.log.info("成功从云端合并最新题库")
                        return
            except Exception:
                pass
        self.log.info("未能从云端合并题库")

    # ------------------------------------------------------------------
    # 历史考试答案合并
    # ------------------------------------------------------------------

    def _merge_history_answers(self) -> None:
        """通过点击页面历史考试记录捕获 reviewPaper 接口响应，将答案合并到本地题库。

        实际页面流程（依据前端源码）：
          1. CourseIndex.vue 在线考试 Tab 中，ExamList.vue 渲染考试列表
             - 已考过的考试计划显示 button.exam-button 文案“考试记录”
             - 点击后 navToExamReviewList() 路由跳转到 /#/exam/review/list
          2. ExamReviewList.vue 列出历史作答记录
             - 仅当 query.displayState === "1" 时显示 div.examPeviewListp-item-color（“作答明细>”）
             - 点击后跳转到 /#/courses/exam-result?examId=...&isRetake=...
          3. ExamResult.vue created 时 dispatch courseExam/getExplain
             - services.getExplain() 实际调用 POST /exam/reviewPaper.do
             - 响应中 data.questions 包含题目、选项、正确答案标记
        """
        try:
            self.log.info("开始通过页面点击合并历史考试答案...")
            merged_count = 0
            responses: list[dict] = []

            def handle_response(response: Any) -> None:
                """拦截 /exam/reviewPaper.do 响应并缓存 JSON 数据。"""
                try:
                    if "reviewPaper.do" not in response.url or response.status != 200:
                        return
                    payload = response.json()
                    if isinstance(payload, dict):
                        responses.append(payload)
                except Exception:
                    pass

            self._page.on("response", handle_response)

            # 先进入任务列表
            self._page.goto(
                f"{self.base_url}/#/learning-task-list",
                wait_until="domcontentloaded",
            )
            self._wait_for(_SEL_TASK_BLOCK, timeout=10000)

            project_count = self._page.locator(_SEL_TASK_BLOCK).count()
            self.log.info(f"[历史记录] 共发现 {project_count} 个学习项目")

            for proj_idx in range(project_count):
                # 每轮重新回到任务列表，避免 DOM 失效
                self._page.goto(
                    f"{self.base_url}/#/learning-task-list",
                    wait_until="domcontentloaded",
                )
                if not self._wait_for(_SEL_TASK_BLOCK, timeout=10000):
                    self.log.info("[历史记录] 未加载到任务列表，结束历史记录合并")
                    break

                projects = self._page.locator(_SEL_TASK_BLOCK)
                if proj_idx >= projects.count():
                    break

                try:
                    title_el = projects.nth(proj_idx).locator(_SEL_TASK_BLOCK_TITLE)
                    proj_title = (
                        title_el.first.inner_text().strip()
                        if title_el.count() > 0
                        else projects.nth(proj_idx)
                        .inner_text()
                        .strip()
                        .split("\n")[0]
                        .strip()
                    )
                except Exception:
                    proj_title = f"项目{proj_idx + 1}"

                self.log.info(f"[历史记录] 进入项目：{proj_title}")
                try:
                    projects.nth(proj_idx).click(force=True)
                except Exception:
                    continue

                # 进入课程页后等待在线考试 Tab 或课程内容
                if not self._wait_for(
                    _SEL_COURSE_READY,
                    timeout=8000,
                ):
                    self.log.info(f"[历史记录] [{proj_title}] 未进入课程页，跳过")
                    continue

                # 切到在线考试 Tab
                exam_tab = self._page.locator(_SEL_EXAM_TAB)
                if exam_tab.count() == 0:
                    self.log.info(f"[历史记录] [{proj_title}] 未找到在线考试 Tab，跳过")
                    continue

                try:
                    exam_tab.first.click(force=True)
                except Exception:
                    continue

                if not self._wait_for(_SEL_EXAM_BUTTON, timeout=8000):
                    self.log.info(f"[历史记录] [{proj_title}] 在线考试区域未渲染，跳过")
                    continue

                # ExamList.vue：已考过的考试计划会出现“考试记录”按钮
                record_btns = self._page.locator(_SEL_EXAM_RECORD_BTN)
                record_count = record_btns.count()
                if record_count == 0:
                    self.log.info(f"[历史记录] [{proj_title}] 无考试记录入口")
                    continue

                self.log.info(
                    f"[历史记录] [{proj_title}] 有 {record_count} 个考试记录入口"
                )

                for exam_idx in range(record_count):
                    # 每个考试计划都重新定位一次，避免页面跳转导致引用失效
                    self._page.goto(
                        f"{self.base_url}/#/learning-task-list",
                        wait_until="domcontentloaded",
                    )
                    if not self._wait_for(_SEL_TASK_BLOCK, timeout=10000):
                        break

                    projects = self._page.locator(_SEL_TASK_BLOCK)
                    if proj_idx >= projects.count():
                        break

                    try:
                        projects.nth(proj_idx).click(force=True)
                    except Exception:
                        break

                    if not self._wait_for(_SEL_EXAM_TAB, timeout=8000):
                        break

                    exam_tab = self._page.locator(_SEL_EXAM_TAB)
                    if exam_tab.count() == 0:
                        break

                    try:
                        exam_tab.first.click(force=True)
                    except Exception:
                        break

                    if not self._wait_for(_SEL_EXAM_BUTTON, timeout=8000):
                        break

                    record_btns = self._page.locator(_SEL_EXAM_RECORD_BTN)
                    if exam_idx >= record_btns.count():
                        break

                    # 点击“考试记录”进入 ExamReviewList
                    try:
                        record_btns.nth(exam_idx).click(force=True)
                    except Exception:
                        continue

                    # ExamReviewList.vue 列表项
                    if not self._wait_for(_SEL_REVIEW_LIST_ITEM, timeout=8000):
                        self.log.info(
                            f"[历史记录] [{proj_title}] 第 {exam_idx + 1} 个考试记录未加载成功"
                        )
                        try:
                            self._page.go_back(wait_until="domcontentloaded")
                        except Exception:
                            pass
                        continue

                    # 只有 displayState=1 才有“作答明细>”
                    detail_links = self._page.locator(_SEL_REVIEW_DETAIL_LINK)
                    detail_count = detail_links.count()
                    if detail_count == 0:
                        self.log.info(
                            f"[历史记录] [{proj_title}] 第 {exam_idx + 1} 个考试记录无作答明细入口"
                        )
                        try:
                            self._page.go_back(wait_until="domcontentloaded")
                        except Exception:
                            pass
                        continue

                    self.log.info(
                        f"[历史记录] [{proj_title}] 第 {exam_idx + 1} 个考试记录有 {detail_count} 条作答明细"
                    )

                    for review_idx in range(detail_count):
                        # 重新定位详情入口，避免 go_back 之后 DOM 更新
                        detail_links = self._page.locator(_SEL_REVIEW_DETAIL_LINK)
                        if review_idx >= detail_links.count():
                            break

                        prev_resp_count = len(responses)

                        try:
                            detail_links.nth(review_idx).click(force=True)
                        except Exception:
                            continue

                        # ExamResult.vue
                        self._wait_for(_SEL_REVIEW_RESULT_READY, timeout=8000)

                        # 等待 reviewPaper.do 响应进入 responses
                        for _ in range(16):
                            time.sleep(0.5)
                            if len(responses) > prev_resp_count:
                                break

                        got_count = len(responses) - prev_resp_count
                        self.log.info(
                            f"[历史记录] [{proj_title}] 第 {exam_idx + 1} 个考试记录 "
                            f"第 {review_idx + 1} 次作答，捕获 {got_count} 条 reviewPaper 响应"
                        )

                        # 返回 ExamReviewList
                        back_btn = self._page.locator(_SEL_REVIEW_BACK_BTN)
                        try:
                            if back_btn.count() > 0 and back_btn.first.is_visible():
                                back_btn.first.click(force=True)
                            else:
                                self._page.go_back(wait_until="domcontentloaded")
                        except Exception:
                            try:
                                self._page.go_back(wait_until="domcontentloaded")
                            except Exception:
                                pass

                        self._wait_for(_SEL_REVIEW_LIST_ITEM, timeout=6000)

            # 移除监听
            try:
                self._page.remove_listener("response", handle_response)
            except Exception:
                pass

            # 解析并合并捕获到的所有 reviewPaper 数据
            seen_exam_keys = set()
            for rev_data in responses:
                try:
                    if not isinstance(rev_data, dict):
                        continue
                    if str(rev_data.get("code")) != "0":
                        continue

                    data = rev_data.get("data") or {}
                    questions = data.get("questions") or []
                    if not isinstance(questions, list):
                        continue

                    # 尽量构造一个去重 key，避免同一试卷重复合并太多次
                    exam_key = (
                        data.get("examId"),
                        data.get("userExamId"),
                        len(questions),
                    )
                    if exam_key in seen_exam_keys:
                        continue
                    seen_exam_keys.add(exam_key)

                    for q in questions:
                        if not isinstance(q, dict):
                            continue

                        title = q.get("title", "")
                        if not title:
                            continue

                        old_opts = {
                            o["content"]: o["isCorrect"]
                            for o in self.answers.get(title, {}).get("optionList", [])
                            if isinstance(o, dict)
                            and "content" in o
                            and "isCorrect" in o
                        }
                        new_opts = old_opts | {
                            o.get("content", ""): o.get("isCorrect", 0)
                            for o in q.get("optionList", [])
                            if isinstance(o, dict) and o.get("content", "")
                        }

                        for content in new_opts.keys() - old_opts.keys():
                            self.log.info(f"发现题目：{title} 新选项：{content}")
                            merged_count += 1

                        self.answers[title] = {
                            "type": q.get("type", ""),
                            "optionList": [
                                {"content": content, "isCorrect": is_correct}
                                for content, is_correct in new_opts.items()
                            ],
                        }
                except Exception:
                    pass

            if merged_count > 0:
                self.log.info(f"成功从历史考试记录中合并 {merged_count} 道题目答案")
            else:
                self.log.info("历史记录中没有发现新的题目答案")

        except Exception as e:
            self.log.warning(f"通过点击获取历史记录时出错: {e}")
            try:
                self._page.remove_listener("response", handle_response)  # type: ignore[name-defined]
            except Exception:
                pass
