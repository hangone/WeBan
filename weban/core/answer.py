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
from weban.app.runtime import clean_text
from .base import BaseMixin

logger = logging.getLogger(__name__)

_SEL_TASK_BLOCK = ".task-block"
_SEL_TASK_BLOCK_TITLE = ".task-block-title"
_SEL_COURSE_READY = ".van-tab, .van-collapse-item, .fchl-item"
_SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
_SEL_EXAM_BUTTON = "button.exam-button"
_SEL_EXAM_RECORD_BTN = 'button.exam-button:has-text("考试记录")'
_SEL_REVIEW_LIST_ITEM = ".examPeviewListp-item"
_SEL_REVIEW_DETAIL_LINK = "div.examPeviewListp-item-color"
_SEL_REVIEW_RESULT_READY = ".quest-stem, .quest-option-item"
_SEL_REVIEW_BACK_BTN = "button:has-text('返回'), .van-nav-bar__left, .back-btn"


class AnswerMixin(BaseMixin):
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
        answers: Dict[str, Any]

    def _wait_for(self, selector: str, timeout: int = 8000) -> bool:
        """等待选择器。"""
        try:
            self._page.wait_for_selector(selector, state="attached", timeout=timeout)
            return True
        except Exception:
            return False

    def _load_answers(self) -> Dict[str, Any]:
        """加载本地题库。"""
        from weban.app.runtime import get_base_path

        base_path = get_base_path()
        answer_path = os.path.join(base_path, "answer", "answer.json")
        if not os.path.exists(answer_path):
            self.log.warning("题库文件不存在")
            return {}
        try:
            with open(answer_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.log.warning(f"读取题库失败: {e}")
        return {}

    def _merge_cloud_answers(self) -> None:
        """从云端合并题库。"""
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
                                    {"content": c, "isCorrect": ic}
                                    for c, ic in new_opts.items()
                                ],
                            }
                        self.log.info("成功从云端合并最新题库")
                        return
            except Exception:
                pass
        self.log.info("未能从云端合并题库")

    def _merge_history_answers(self) -> None:
        """通过点击历史记录合并答案。"""
        try:
            self.log.info("开始通过页面点击合并历史考试答案...")
            merged_count = 0
            responses: list[dict] = []

            def _handle_response(response: Any) -> None:
                try:
                    if "reviewPaper.do" in response.url and response.status == 200:
                        responses.append(response.json())
                except Exception:
                    pass

            self._page.on("response", _handle_response)
            try:
                # 遍历分类 Tab：1-学习项目, 2-结束项目
                tab_names = ["学习项目", "结束项目"]
                for tab_name in tab_names:
                    self.log.info(f"[历史记录] 正在搜集 [{tab_name}] 的历史答案...")
                    self._page.goto(f"{self.base_url}/#/learning-task-list")
                    time.sleep(2)

                    tab_el = self._page.locator(
                        f'.van-tab:has-text("{tab_name}")'
                    ).first
                    if tab_el.is_visible():
                        tab_el.click(force=True)
                        time.sleep(1.5)

                    if not self._wait_for(_SEL_TASK_BLOCK, timeout=8000):
                        continue

                    for proj_idx in range(self._page.locator(_SEL_TASK_BLOCK).count()):
                        # 每轮重回列表并切 Tab
                        self._page.goto(f"{self.base_url}/#/learning-task-list")
                        time.sleep(1)
                        tab_el = self._page.locator(
                            f'.van-tab:has-text("{tab_name}")'
                        ).first
                        if tab_el.is_visible():
                            tab_el.click(force=True)
                        time.sleep(1)

                        proj = self._page.locator(_SEL_TASK_BLOCK).nth(proj_idx)
                        proj_title = (
                            proj.locator(_SEL_TASK_BLOCK_TITLE).inner_text().strip()
                        )
                        self.log.info(f"[历史记录] 进入项目：{proj_title}")

                        proj.click(force=True)
                        self._wait_for(_SEL_COURSE_READY, timeout=8000)

                        # 切换到“在线考试”Tab
                        exam_tab = self._page.locator(_SEL_EXAM_TAB).first
                        if not exam_tab.is_visible():
                            self.log.info(f"[历史记录] [{proj_title}] 无在线考试 Tab")
                            continue
                        exam_tab.click(force=True)
                        time.sleep(1.5)

                        r_btns = self._page.locator(_SEL_EXAM_RECORD_BTN)
                        self.log.info(
                            f"[历史记录] - 项目 [{proj_title}] 找到 {r_btns.count()} 个考试记录入口"
                        )
                        for exam_idx in range(r_btns.count()):
                            # 重新定位记录按钮
                            self._page.goto(self._page.url)
                            self._page.locator(_SEL_EXAM_TAB).first.click()
                            time.sleep(1.5)

                            btn = self._page.locator(_SEL_EXAM_RECORD_BTN).nth(exam_idx)
                            btn.scroll_into_view_if_needed()
                            btn.click(force=True)

                            # 在记录列表页提取明细
                            if self._wait_for(_SEL_REVIEW_LIST_ITEM, timeout=8000):
                                d_links = self._page.locator(_SEL_REVIEW_DETAIL_LINK)
                                for review_idx in range(d_links.count()):
                                    pc = len(responses)
                                    d_links.nth(review_idx).click(force=True)
                                    self._wait_for(
                                        _SEL_REVIEW_RESULT_READY, timeout=8000
                                    )
                                    # 等待拦截
                                    for _ in range(10):
                                        if len(responses) > pc:
                                            break
                                        time.sleep(0.5)
                                    self._page.go_back()
                                    self._wait_for(_SEL_REVIEW_LIST_ITEM, timeout=6000)
                            self._page.go_back()
                            self._wait_for(_SEL_EXAM_TAB, timeout=6000)
            finally:
                self._page.remove_listener("response", _handle_response)

            merged_count = 0
            seen_keys = set()
            for rev in responses:
                if not isinstance(rev, dict) or str(rev.get("code")) != "0":
                    continue
                data = rev.get("data") or {}
                qs = data.get("questions") or []
                match_id = data.get("examId") or data.get("paperId")
                match_key = (match_id, len(qs))
                if match_key in seen_keys:
                    continue
                seen_keys.add(match_key)

                for q in qs:
                    raw_title = q.get("title", "")
                    if not raw_title:
                        continue
                    # 归一化处理标题，避免因题号前缀导致重复
                    title = clean_text(raw_title)

                    new_opts = {
                        clean_text(o.get("content", "")): o.get("isCorrect", 0)
                        for o in q.get("optionList", [])
                        if o.get("content")
                    }

                    if title not in self.answers:
                        self.answers[title] = {
                            "type": q.get("type", ""),
                            "optionList": [],
                        }
                        merged_count += 1

                    # 合并选项逻辑：保留老选项，合并新选项，isCorrect 取最大值
                    current_opts = {
                        clean_text(o["content"]): o
                        for o in self.answers[title]["optionList"]
                    }
                    updated = False
                    for c_opt, is_c in new_opts.items():
                        if c_opt not in current_opts:
                            current_opts[c_opt] = {"content": c_opt, "isCorrect": is_c}
                            updated = True
                        elif is_c > current_opts[c_opt]["isCorrect"]:
                            current_opts[c_opt]["isCorrect"] = is_c
                            updated = True

                    if updated:
                        self.answers[title]["optionList"] = list(current_opts.values())

            if merged_count > 0:
                self.log.info(f"成功从历史考试记录中合并 {merged_count} 道题目答案")
            else:
                self.log.info("历史记录中没有发现新的题目答案")

        except Exception as e:
            self.log.warning(f"历史记录合并过程中断: {e}")
