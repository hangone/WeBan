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
import threading
import urllib.request
from typing import TYPE_CHECKING, Any, Dict

from weban.app.runtime import clean_text, get_base_path
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

    def _is_better(self, new_val: int, old_val: int) -> bool:
        """比较题库答案状态，判断新记录是否优于或等效于旧记录。

        正确(1) > 错误(2) > 未知(0)。
        使用 >= 意味着当质量相当时，更新为较新的原始文本，实现‘新合并旧’。
        """
        score = {1: 3, 2: 2, 0: 1}
        return score.get(new_val, 0) >= score.get(old_val, 0)

    def _load_answers(self) -> Dict[str, Any]:
        """加载本地题库。"""
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
        urls = [
            "https://github.com/hangone/WeBan/raw/main/answer/answer.json",
            "https://fastly.jsdelivr.net/gh/hangone/WeBan@main/answer/answer.json",
        ]

        for url in urls:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        data = json.loads(response.read().decode("utf-8"))

                        # 使用锁保护字典写入
                        with getattr(self, "_ANSWER_LOCK", threading.Lock()):
                            for raw_title, item in data.items():
                                c_title = clean_text(raw_title)
                                if not c_title:
                                    continue

                                target_title = raw_title
                                for existing_raw in list(self.answers.keys()):
                                    if clean_text(existing_raw) == c_title:
                                        target_title = existing_raw
                                        break

                                if target_title not in self.answers:
                                    self.answers[target_title] = {
                                        "type": item.get("type", ""),
                                        "optionList": [],
                                    }

                                cur_opts_map = {
                                    clean_text(o["content"]): o
                                    for o in self.answers[target_title].get(
                                        "optionList", []
                                    )
                                }
                                for new_o in item.get("optionList", []):
                                    raw_new_o = new_o["content"]
                                    c_new = clean_text(raw_new_o)
                                    if c_new not in cur_opts_map:
                                        cur_opts_map[c_new] = {
                                            "content": raw_new_o,
                                            "isCorrect": new_o["isCorrect"],
                                        }
                                    elif self._is_better(
                                        new_o["isCorrect"],
                                        cur_opts_map[c_new]["isCorrect"],
                                    ):
                                        cur_opts_map[c_new]["isCorrect"] = new_o[
                                            "isCorrect"
                                        ]
                                        cur_opts_map[c_new]["content"] = raw_new_o

                                self.answers[target_title]["optionList"] = list(
                                    cur_opts_map.values()
                                )

                        self.log.info("成功从云端合并最新题库")
                        self._save_answers()
                        return
            except Exception as e:
                self.log.debug(f"云端合并尝试失败 ({url}): {e}")
        self.log.info("未能从云端合并题库")

    _ANSWER_LOCK = threading.Lock()

    def _save_answers(self) -> None:
        """持久化合并后的题库，并执行深度清理与去重（最短文本优先）。"""
        from weban.app.runtime import ignore_symbols, strip_side_symbols

        ans_file = os.path.join(get_base_path(), "answer", "answer.json")
        os.makedirs(os.path.dirname(ans_file), exist_ok=True)

        try:
            with self._ANSWER_LOCK:
                # 1. 加载当前磁盘数据
                disk_data = {}
                if os.path.exists(ans_file):
                    with open(ans_file, "r", encoding="utf-8") as f:
                        try:
                            disk_data = json.load(f)
                            if not isinstance(disk_data, dict):
                                disk_data = {}
                        except Exception:
                            disk_data = {}

                # 2. 聚类池：key 为语义键，value 存储最优原始信息
                cluster_pool = {}

                def _merge_into_pool(raw: str, item: Dict):
                    # 两端去噪：删除空格和末尾符号，但保留中间
                    t_stripped = strip_side_symbols(raw)
                    sem_key = ignore_symbols(t_stripped)
                    if not sem_key:
                        return

                    if sem_key not in cluster_pool:
                        cluster_pool[sem_key] = {"raw": t_stripped, "item": item}
                    else:
                        target = cluster_pool[sem_key]
                        # 规则：题目文本按长度更短的来
                        if len(t_stripped) < len(target["raw"]):
                            target["raw"] = t_stripped

                        # 合并选项
                        opt_clusters = {}  # key 为语义键

                        def _fill_opts(pool_dict, opt_list):
                            for o in opt_list:
                                raw_o = strip_side_symbols(o.get("content", ""))
                                s_key = ignore_symbols(raw_o)
                                if not s_key:
                                    continue
                                if s_key not in pool_dict:
                                    pool_dict[s_key] = {
                                        "content": raw_o,
                                        "isCorrect": o.get("isCorrect", 2),
                                    }
                                else:
                                    # 选项文本也按长度更短的来
                                    if len(raw_o) < len(pool_dict[s_key]["content"]):
                                        pool_dict[s_key]["content"] = raw_o
                                    # 合并正确性
                                    if self._is_better(
                                        o.get("isCorrect", 2),
                                        pool_dict[s_key]["isCorrect"],
                                    ):
                                        pool_dict[s_key]["isCorrect"] = o.get(
                                            "isCorrect", 2
                                        )

                        _fill_opts(opt_clusters, target["item"].get("optionList", []))
                        _fill_opts(opt_clusters, item.get("optionList", []))
                        target["item"]["optionList"] = list(opt_clusters.values())

                # 全量归集
                for k, v in disk_data.items():
                    _merge_into_pool(k, v)
                for k, v in self.answers.items():
                    _merge_into_pool(k, v)

                # 3. 排序并最终格式化
                final_output = {}
                for sem_key in sorted(cluster_pool.keys()):
                    raw_title = cluster_pool[sem_key]["raw"]
                    it = cluster_pool[sem_key]["item"]
                    final_output[raw_title] = {
                        "optionList": it.get("optionList", []),
                        "type": it.get("type", ""),
                    }

                with open(ans_file, "w", encoding="utf-8") as f:
                    json.dump(final_output, f, ensure_ascii=False, indent=2)

                self.log.info(
                    f"题库数据已通过语义合并持久化: {ans_file} (总计 {len(final_output)} 题)"
                )
                self.answers = final_output
        except Exception as e:
            self.log.warning(f"题库持久化合并失败: {e}")

    def _merge_history_answers(self) -> None:
        """通过点击历史记录合并答案。"""
        # 定义兼容不同拼写的选择器（针对 WeBan 前端常见的 Peview/Preview 拼写错误）
        _SEL_REVIEW_ITEM = ".examPreviewListp-item, .examPeviewListp-item"
        _SEL_REVIEW_LINK = ".examPreviewListp-item-color, .examPeviewListp-item-color"
        _SEL_REVIEW_RESULT_READY = ".quest-stem, .quest-option-item"

        try:
            self.log.info("开始通过记录页面提取并合并历史考试答案...")

            # 使用临时状态变量，由回调函数更新
            self._live_total_qs = 0
            self._live_added_qs = 0
            self._live_updated_qs = 0

            def _handle_response(response: Any) -> None:
                try:
                    if "reviewPaper.do" in response.url and response.status == 200:
                        rev = response.json()
                        if not isinstance(rev, dict) or str(rev.get("code")) != "0":
                            return

                        data = rev.get("data") or {}
                        qs = data.get("questions") or []

                        report_total = len(qs)
                        report_added = 0
                        report_updated = 0
                        self._live_total_qs += report_total

                        new_titles = []
                        update_details = []

                        for q in qs:
                            raw_title = q.get("title", "")
                            if not raw_title:
                                continue

                            new_opts_data = [
                                {
                                    "content": o.get("content", ""),
                                    "isCorrect": o.get("isCorrect", 0),
                                }
                                for o in q.get("optionList", [])
                                if o.get("content")
                            ]

                            from weban.app.runtime import ignore_symbols

                            c_title = ignore_symbols(raw_title)

                            target_key = None
                            for existing_raw in list(self.answers.items()):
                                if ignore_symbols(existing_raw[0]) == c_title:
                                    target_key = existing_raw[0]
                                    break

                            is_new_q = False
                            if not target_key:
                                self.answers[raw_title] = {
                                    "optionList": [],
                                    "type": q.get("type", ""),
                                }
                                target_key = raw_title
                                report_added += 1
                                self._live_added_qs += 1
                                is_new_q = True
                                new_titles.append(raw_title)

                            current_opts = {
                                ignore_symbols(o["content"]): o
                                for o in self.answers[target_key]["optionList"]
                            }

                            q_updated_str = []
                            updated_this_q = False

                            # 正确性评分函数
                            score_map = {1: 3, 2: 2, 0: 1}

                            for o_data in new_opts_data:
                                raw_o = o_data["content"]
                                is_c = o_data["isCorrect"]
                                osk = ignore_symbols(raw_o)

                                if osk not in current_opts:
                                    current_opts[osk] = {
                                        "content": raw_o,
                                        "isCorrect": is_c,
                                    }
                                    updated_this_q = True
                                    q_updated_str.append(f"[新增选项] {raw_o}")
                                else:
                                    old_is_c = current_opts[osk]["isCorrect"]
                                    # 只有质量严格提升才视为更新
                                    if score_map.get(is_c, 0) > score_map.get(
                                        old_is_c, 0
                                    ):
                                        current_opts[osk]["isCorrect"] = is_c
                                        current_opts[osk]["content"] = raw_o
                                        updated_this_q = True
                                        q_updated_str.append(
                                            f"[答案更正] {raw_o} ({old_is_c} -> {is_c})"
                                        )

                            if updated_this_q:
                                self.answers[target_key]["optionList"] = list(
                                    current_opts.values()
                                )
                                if not is_new_q:
                                    report_updated += 1
                                    self._live_updated_qs += 1
                                    update_details.append(
                                        f"  题干: {target_key}\n      "
                                        + "\n      ".join(q_updated_str)
                                    )

                        self.log.info(
                            f"   -> 报告解析完成: 获取 {report_total} 题, 新增 {report_added} 题, 补全 {report_updated} 题答案"
                        )
                        if new_titles:
                            self.log.info(
                                "      [新增题干] "
                                + ", ".join(new_titles[:5])
                                + (
                                    f" 等 {len(new_titles)} 题"
                                    if len(new_titles) > 5
                                    else ""
                                )
                            )
                        if update_details:
                            for detail in update_details[
                                :3
                            ]:  # 最多展示前 3 个详细变更，避免刷屏
                                self.log.debug(detail)
                            if len(update_details) > 3:
                                self.log.debug(
                                    f"      ... (其余 {len(update_details) - 3} 处答案补全已静默处理)"
                                )
                except Exception:
                    pass

            self._page.on("response", _handle_response)
            try:
                # 遍历分类 Tab：1-学习项目, 2-结束项目
                tab_names = ["学习项目", "结束项目"]
                for tab_name in tab_names:
                    self.log.info(f"[历史记录] 正在扫描：{tab_name}")
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

                    proj_count = self._page.locator(_SEL_TASK_BLOCK).count()
                    for proj_idx in range(proj_count):
                        # 每轮重回列表并切换 Tab，确保 DOM 状态最新
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

                        exam_tab = self._page.locator(_SEL_EXAM_TAB).first
                        if not exam_tab.is_visible():
                            continue
                        exam_tab.click(force=True)
                        time.sleep(1.5)

                        r_btns = self._page.locator(_SEL_EXAM_RECORD_BTN)
                        for exam_idx in range(r_btns.count()):
                            record_url = self._page.url
                            btn = self._page.locator(_SEL_EXAM_RECORD_BTN).nth(exam_idx)
                            btn.scroll_into_view_if_needed()
                            btn.click(force=True)

                            if self._wait_for(_SEL_REVIEW_ITEM, timeout=8000):
                                d_links = self._page.locator(_SEL_REVIEW_LINK)
                                link_count = d_links.count()
                                if link_count > 0:
                                    self.log.info(
                                        f"[历史记录] -> 发现 {link_count} 份考试报告，准备解析..."
                                    )

                                for review_idx in range(link_count):
                                    link = self._page.locator(_SEL_REVIEW_LINK).nth(
                                        review_idx
                                    )
                                    link.click(force=True)
                                    # 等待内容出现，会自动触发 Response 拦截器并在控制台打印
                                    self._wait_for(
                                        _SEL_REVIEW_RESULT_READY, timeout=8000
                                    )
                                    time.sleep(0.5)

                                    self._page.go_back()
                                    self._wait_for(_SEL_REVIEW_ITEM, timeout=6000)

                            self._page.goto(record_url)
                            self._wait_for(_SEL_EXAM_TAB, timeout=6000)
            finally:
                self._page.remove_listener("response", _handle_response)

            # 最终汇总
            if self._live_added_qs > 0 or self._live_updated_qs > 0:
                self.log.info(
                    f"【汇总】历史记录合并成功：共解析 {self._live_total_qs} 道题目，"
                    f"其中新增 {self._live_added_qs} 题，完善 {self._live_updated_qs} 题信息"
                )
                self._save_answers()
            else:
                self.log.info("历史记录解析完成，本地题库已是最新状态。")

        except Exception as e:
            self.log.warning(f"历史记录合并过程中断: {e}")
