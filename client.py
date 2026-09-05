from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import webbrowser
from pathlib import Path
from random import randint
from typing import Any, Self
from urllib.parse import (
    parse_qs,
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)
from uuid import uuid4

from loguru import logger

from answer_store import AnswerStore, AnswerStoreError
from api import WeBanAPI
from captcha import CaptchaHandler, LoginCaptchaSolver, is_non_interactive
from errors import (
    AccountBlockedError,
    APIResponseError,
    ResponseValidationError,
    WorkflowResult,
)

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(os.path.abspath(sys.executable))
    bundle_path = sys._MEIPASS  # type: ignore[attr-defined]
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    bundle_path = base_path

# 无交互模式判定复用 captcha.is_non_interactive（ENVIRONMENT=docker / stdin 非 TTY）

# 数据目录模式（main.py --data-dir / WB_DATA_DIR）：answer 也放数据目录，便于挂载持久化
_data_dir = os.environ.get("WB_DATA_DIR", "")
if _data_dir:
    answer_dir = os.path.join(_data_dir, "answer")
else:
    answer_dir = os.path.join(base_path, "answer")
answer_path = os.path.join(answer_dir, "answer.json")
root_answer_path = (
    os.path.join(_data_dir, "answer.json")
    if _data_dir
    else os.path.join(base_path, "answer.json")
)
bundle_answer_path = os.path.join(bundle_path, "answer", "answer.json")

# 完课接口返回成功后，showProgress 的计数可能稍后才可见。轮询次数和
# 退避上限必须固定，避免网络异常或服务端永不更新时无限阻塞任务。
PROGRESS_POLL_DELAYS = (0.5, 1.0, 2.0, 4.0)


def clean_text(text):
    """生成保守模糊键，保留会改变语义的正负号和比较符。

    普通标点和空格仍会被忽略，但 ``+ - < > = ≤ ≥ ≠`` 不再被删除，
    避免“正/负”“大于/小于”题目碰撞。
    :param text: 原始文本
    :return: 用于唯一模糊匹配的文本
    """
    normalized = unicodedata.normalize("NFKC", str(text))
    return re.sub(r"[^\w一-龥+\-<>=≤≥≠]", "", normalized)


def _exact_text(text: object) -> str:
    """Unicode 归一化并移除空白，保留其余全部符号。"""

    normalized = unicodedata.normalize("NFKC", str(text)).strip()
    return re.sub(r"\s+", "", normalized)


def _option_signature(question: dict) -> frozenset[str]:
    options = question.get("optionList")
    if not isinstance(options, list):
        return frozenset()
    return frozenset(
        clean_text(option.get("content", ""))
        for option in options
        if isinstance(option, dict) and clean_text(option.get("content", ""))
    )


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def get_source_str(query: dict) -> str:
    """从 URL 参数推断 sourceStr，与 JS 逻辑一致
    :param query: parse_qs 解析后的 URL 查询参数
    :return: sourceStr 值，如 "LYRA"、"MOON"、"WEIBAN" 等
    """
    if query.get("weiban", [None])[0] != "weiban":
        return "LYRA" if query.get("lyra", [None])[0] == "lyra" else "PROTEUS"
    if query.get("source", [None])[0] == "moon":
        return "MOON"
    return "WEIBAN"


def _course_finished(course: dict) -> bool:
    """课程列表对象的 finished 字段是否表示已完成（1=完成，2=未完成）。

    服务端偶发返回 null/字符串，解析失败一律视为未完成，宁可多学一门也不能
    因为 TypeError 让整个项目中断。JSON 里的 1e309 会解析成 inf，
    int(inf) 抛 OverflowError，同样按未完成处理。
    """
    try:
        return int(course.get("finished", 0)) == 1
    except (TypeError, ValueError, OverflowError):
        return False


# 试卷分值的合理上界：官方满分为 100，留出余量拒绝 inf/巨大值。
_MAX_SCORE = 10_000.0


def _finite_score(value: object) -> float | None:
    """把分数字段解析为有限、非负且在合理范围内的 float，否则返回 None。

    JSON 允许 1e309/-1e309 之类字面量，Python 会得到 ±inf；某些代理还可能
    透传 NaN。它们进入 >= 比较会产生错误的跳过/不跳过判定，必须拒收。
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or parsed > _MAX_SCORE:
        return None
    return parsed


def _brief_response(response: object, limit: int = 200) -> str:
    """只提取业务码和短消息用于日志，不回显完整响应正文。

    客户端可能被脱离 main.py 的 LogRedactor 直接使用，因此这里自行去掉
    控制字符并截断长度，避免把 token、个人信息或超长内容写进日志。
    """
    if not isinstance(response, dict):
        return f"<{type(response).__name__}>"
    parts: list[str] = []
    for key in ("code", "detailCode", "msg", "message"):
        if key in response:
            text = re.sub(r"[\x00-\x1f\x7f]", "?", str(response[key]))
            if len(text) > limit:
                text = f"{text[:limit]}…"
            parts.append(f"{key}={text}")
    data = response.get("data")
    parts.append(f"data=<{type(data).__name__}>")
    return ", ".join(parts)


def _check_code_ok(data: dict, allow_200: bool = True) -> bool:
    """接口业务码是否成功（对齐官方 checkCode）

    主站请求封装（app.js request）：Boolean(data) && Number(code)∈{0,1,200}；
    完课 JSONP（sdk.js finishWxCourse）：Boolean(data) && Number(code)∈{0,1}，
    传 allow_200=False 对齐。缺少 code 不是成功响应；显式 code=null 仍按
    Number(null)===0 保留官方兼容语义。
    :param data: 接口响应 dict
    :param allow_200: 是否允许 code=200（主站接口 True，完课 JSONP False）
    :return: 业务成功返回 True
    """
    if not data or "code" not in data:
        return False
    code = data.get("code")
    try:
        num = int(code) if code is not None else 0
    except (TypeError, ValueError):
        return False
    return num in ((0, 1, 200) if allow_200 else (0, 1))


def _extract_map(content: str) -> dict:
    """从 JS 内容中提取 nonstrMap / pageIdMap

    两阶段匹配：先按命名变量精确匹配 nonstrMap/pageIdMap，
    匹配不到再退回到任意 Map，防止误匹配其他 Map 定义。
    :param content: JS 文件内容
    :return: {step_index: nonstr_value} 映射，未找到返回空字典
    """
    for pattern in [
        r"(?:const|var|let)\s+nonstrMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)",
        r"(?:const|var|let)\s+pageIdMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)",
    ]:
        match = re.search(pattern, content)
        if match:
            entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', match.group(1))
            if entries:
                return {int(step): val for step, val in entries}
    # 退而求其次：匹配任意 Map（变量名未知）
    for m in re.finditer(r"new\s+Map\(\[([\s\S]*?)\]\)", content):
        entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', m.group(1))
        if entries:
            return {int(step): val for step, val in entries}
    return {}


def _check_exam(content: str) -> bool:
    """检查 JS 内容中是否包含课后习题相关代码
    :param content: JS 文件内容
    :return: 包含习题相关代码返回 True
    """
    return "saveExamQuestion" in content or "listQuestions" in content


def _count_nav_pages(html: str) -> tuple[int, int]:
    """统计 HTML 中触发向前导航的页面数，以及题目页数。

    统计所有 page-item page-N 区块（排除特殊页），再加回 page-start（点击后触发导航）。
    每个题目页会触发 2 次额外 apinext 调用（提交 → 结果页 → 继续）。

    :return: (nav_pages, question_pages) 基础导航步数 和 题目页数量
    """
    # 统计所有 page-N 区块（排除特殊页面）
    content_pages = 0
    has_start_page = False
    for m in re.finditer(
        r'<section\b[^>]*class="([^"]*\bpage-item\b[^"]*)"',
        html,
    ):
        classes = m.group(1).split()
        if "btn-next-prev" in classes:
            continue  # 集中导航控件，不是内容页
        if {"page-end", "page-success", "page-fail"} & set(classes):
            continue  # 结果页由题目触发，不计入基础导航
        if "page-start" in classes:
            has_start_page = True
            continue  # 单独计数
        page_match = re.search(r"page-(\d+)", m.group(1))
        if page_match:
            content_pages += 1

    # 统计题目页（含 data-all-answer 的 page-options）
    question_pages = 0
    for m in re.finditer(
        r'<section\b[^>]*class="([^"]*\bpage-item\b[^"]*)"[^>]*>'
        r"(?:(?!</section>).)*?(?:data-all-answer|page-commit)",
        html,
        re.DOTALL,
    ):
        page_match = re.search(r"page-(\d+)", m.group(1))
        if page_match:
            question_pages += 1

    # 基础导航步数 = 内容页数 + start（如果有）
    nav_pages = content_pages + (1 if has_start_page else 0)
    return nav_pages, question_pages


def _fetch_text(session, url: str, referer: str | None = None) -> str:
    """从 URL 获取文本内容

    超时 10 秒，异常时返回空串不中断调用方，
    因为 parse_item_js 中的 JS/HTML 获取是辅助性的，宁可缺也不应阻断学习流程。
    :param session: LoggingSession 实例
    :param url: 目标 URL
    :param referer: 自定义 Referer（抓 mcwk 资源时应传课程播放页 URL，
        否则默认 Referer 为 weiban 根域，资源服务器可能拒绝）
    :return: 响应文本，失败返回空字符串
    """
    try:
        headers = {"Referer": referer} if referer else None
        resp = session.get(url, timeout=10, headers=headers)
        return resp.text if resp.status_code == 200 else ""
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# WeBanClient
# ---------------------------------------------------------------------------


class WeBanClient:
    _stdin_lock = threading.Lock()

    def __init__(
        self,
        tenant_name: str,
        account: str | None = None,
        password: str | None = None,
        user: dict[str, str] | None = None,
        log=logger,
        browser_path: str | None = None,
        cdp_host: str | None = None,
        cdp_port: int | None = None,
        debug: bool = False,
        ai_config: dict[str, Any] | None = None,
        video_speed: float = 1.0,
        jupiter_fallback: bool = False,
        data_dir: str | os.PathLike[str] | None = None,
        answer_store: AnswerStore | None = None,
        non_interactive: bool | None = None,
        captcha_debug_dir: str | os.PathLike[str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """
        :param tenant_name: 学校全称
        :param account: 用户名
        :param password: 密码
        :param user: 已有用户凭据 {"userId": ..., "token": ...}，提供则跳过登录
        :param log: logger 实例
        :param browser_path: 浏览器可执行文件路径，用于验证码处理
        :param cdp_host: CDP 远程调试地址
        :param cdp_port: CDP 远程调试端口
        :param debug: 是否启用调试日志
        :param ai_config: AI 搜题配置
        :param video_speed: 视频课程学习倍速，完课前按 视频时长/倍速 等待；
            0 表示不按视频时长等待，只按 study_time 学习时长
        :param jupiter_fallback: 对未加载 apicenext.js 的课程也补发 jupiter
            翻页轨迹。官方页面只有加载 apicenext.js（定义全局 uuid 并调用
            callApinext）的课程才上报轨迹，默认 False 完全对齐官方行为；
            个别学校可能要求该校所有微课都有轨迹，
            实测无轨迹会 10018 时可开启该项
        :param data_dir: 统一数据目录；仅在未显式提供 answer_store 时使用
        :param answer_store: 由入口创建的共享题库存储
        :param non_interactive: 显式无交互策略，禁止回退到 input/可见浏览器
        :param captcha_debug_dir: 当前账号隔离后的验证码调试目录
        :param stop_event: 进程级停止事件，用于中断长等待和验证码流程
        """
        self.log: Any = log
        self.tenant_name = tenant_name.strip()
        self.study_base_time = 20
        self.study_random_upper = 10
        self.study_force = False
        self.exam_mode = "true"
        self.video_speed = video_speed
        self.jupiter_fallback = jupiter_fallback
        # 时间预估状态（按项目累计实测，样本少时渐进信任实测值）
        self._eta_course_state: dict = {}  # project_id -> {"started_at", "start_finished"}
        self._eta_exam_avg: float | None = None  # 每场考试实测平均耗时（秒）
        self.browser_path = browser_path
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.ai_config = ai_config
        self._ai_key_warned = False  # api_key 未配置提醒只打一次
        self.non_interactive = (
            is_non_interactive() if non_interactive is None else bool(non_interactive)
        )
        self.stop_event = stop_event or threading.Event()
        resolved_data_dir = Path(data_dir or _data_dir or base_path).resolve(
            strict=False
        )
        self.data_dir = resolved_data_dir
        self.captcha_debug_dir = Path(
            captcha_debug_dir
            if captcha_debug_dir is not None
            else resolved_data_dir / "logs" / "captcha"
        ).resolve(strict=False)
        self._answer_store_instance = answer_store or self._build_answer_store(
            resolved_data_dir
        )
        if user and all([user.get("userId"), user.get("token")]):
            self.api: Any = WeBanAPI(user=user, debug=debug, log=log)
        elif all([self.tenant_name, account, password]):
            self.api = WeBanAPI(
                account=account, password=password, debug=debug, log=log
            )
        else:
            self.api = WeBanAPI(debug=debug, log=log)
        self._closed = False
        try:
            self.tenant_code = self.get_tenant_code()
            if self.tenant_code:
                self.api.set_tenant_code(self.tenant_code)
            else:
                raise ValueError("学校代码获取失败，请检查学校全称是否正确")
        except BaseException:
            self.api.close()
            self._closed = True
            raise
        self._captcha_handler: Any | None = None

    # ---- properties / helpers ------------------------------------------------

    @property
    def captcha_handler(self):
        """延迟初始化 CaptchaHandler（需要 login 后才有 token）
        :return: CaptchaHandler 实例
        """
        if self._captcha_handler is None:
            self._captcha_handler = CaptchaHandler(
                tenant_code=self.tenant_code,
                user_id=self.api.user["userId"],
                token=self.api.user["token"],
                log=self.log,
                browser_path=self.browser_path,
                cdp_host=self.cdp_host,
                cdp_port=self.cdp_port,
                debug_dir=self.captcha_debug_dir,
                non_interactive=self.non_interactive,
                stop_event=self.stop_event,
            )
        return self._captcha_handler

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """秒数格式化为 XhXXmXXs / XmXXs / Xs"""
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m{sec:02d}s"
        if m:
            return f"{m}m{sec:02d}s"
        return f"{sec}s"

    def _raise_if_stopped(self) -> None:
        stop_event = getattr(self, "stop_event", None)
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("运行已被中断")

    def _sleep(self, seconds: float) -> None:
        """使用共享停止事件替代不可中断的 time.sleep。"""

        self._raise_if_stopped()
        delay = max(0.0, float(seconds))
        stop_event = getattr(self, "stop_event", None)
        if stop_event is None:
            time.sleep(delay)
            return
        if stop_event.wait(delay):
            raise InterruptedError("运行已被中断")

    def close(self) -> None:
        """释放账号客户端持有的网络连接和验证码处理器引用。"""

        if self._closed:
            return
        handler = self._captcha_handler
        self._captcha_handler = None
        close_handler = getattr(handler, "close", None)
        try:
            if close_handler is not None:
                close_handler()
        finally:
            self.api.close()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def simulate_home_page(self) -> None:
        """模拟打开官方 H5 首页：对齐登录后页面初始化请求面

        官方浏览器（weiban.mycourse.cn）登录成功后，首页组件 created 依次发起：
        协议/电子书(getEbook+ebook 记录) → 轮播图 → 用户信息 → 必读公告
        (listMust 弹窗逐条展示，阅读后逐条 viewMust 确认) → 问卷列表 →
        学习任务列表(index/listStudyTask.do，"开始学习"入口) → 项目统计/
        公告红点/功能开关/租户配置/帮助文件等。
        逐个模拟发送（尽力而为），任一失败只记日志不中断主流程。
        """
        batch_code = self.api.user.get("batchCode", "") or ""
        steps: list[tuple[str, Any]] = [
            ("协议内容", self.api.get_ebook),
            ("协议记录", self.api.ebook_record_list),
            ("轮播图", self.api.carousel_list),
            ("用户信息", self.api.my_get_info),
            ("项目统计", self.api.get_project_stat),
            ("公告状态", self.api.notice_index),
            ("公告列表", self.api.notice_list),
            ("功能阀门", self.api.list_valve),
            ("租户配置", self.api.get_simple_config),
            ("帮助文件", self.api.get_help),
            ("学习任务", self.api.list_study_task),
        ]
        for name, fn in steps:
            self._raise_if_stopped()
            try:
                res = fn()
                if not _check_code_ok(res):
                    self.log.debug(f"首页{name}返回异常：{res}")
            except InterruptedError:
                raise
            except PermissionError:
                raise  # Token 失效（被顶号等），立即终止该账号
            except (OSError, APIResponseError) as e:
                self.log.debug(f"首页{name}请求失败：{e}")

        # 必读公告：官方弹窗逐条展示，阅读完成后逐条确认（与浏览器行为一致）
        try:
            must = self.api.notice_list_must(batch_code)
            notices = must.get("data") or []
            if not _check_code_ok(must) or not isinstance(notices, list):
                self.log.debug(f"必读公告返回异常：{must}")
                notices = []
            for n in notices:
                if not isinstance(n, dict) or not n.get("id"):
                    self.log.debug(f"跳过结构无效的必读公告：{n}")
                    continue
                nid = n.get("id", "")
                title = n.get("title", "")
                ntype = n.get("type", "")
                file_url = n.get("fileUrl", "")
                min_read = n.get("minReadLength", 0)
                self.log.info(
                    f"必读公告：{title}（ID={nid}，类型={ntype}，"
                    f"链接={file_url}，阅读时长={min_read}秒）"
                )
                # 官方普通类型公告有阅读倒计时（minReadLength 秒），
                # 倒计时结束才可点击"下一条/关闭"确认；上限 300s 防极端值
                if (
                    ntype not in (3, 4, 5)
                    and isinstance(min_read, (int, float))
                    and min_read > 0
                ):
                    self._sleep(min(min_read, 300))
                try:
                    self.api.view_must_notice(nid)
                except InterruptedError:
                    raise
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except (OSError, APIResponseError) as e:
                    self.log.debug(f"确认必读公告失败：{e}")
        except (InterruptedError, PermissionError):
            raise  # Token 失效，立即终止该账号
        except (OSError, APIResponseError) as e:
            self.log.debug(f"必读公告流程失败：{e}")

        # 问卷：官方在必读公告确认后检查待答问卷，仅拉取并提示，不自动作答
        try:
            q = self.api.questionnaire_list_by_user_id()
            qlist = q.get("data") if isinstance(q.get("data"), list) else []
            if _check_code_ok(q) and qlist:
                self.log.info(
                    f"存在 {len(qlist)} 个待答问卷（官方会弹窗提示，请前往网页完成）"
                )
        except InterruptedError:
            raise
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except (OSError, APIResponseError) as e:
            self.log.debug(f"问卷列表请求失败：{e}")

    def _prompt(self, message: str) -> str:
        """线程安全的 input 封装，多线程下避免 input 输出交错
        :param message: 提示信息
        :return: 去除首尾空白的用户输入
        """
        self._raise_if_stopped()
        if self.non_interactive:
            raise RuntimeError("无交互模式禁止读取终端输入")
        with self._stdin_lock:
            return input(message).strip()

    @classmethod
    def _build_answer_store(cls, data_dir: Path) -> AnswerStore:
        """在统一数据目录内选择写入路径，并保留历史/打包题库作恢复源。"""

        legacy_path = data_dir / "answer.json"
        current_path = data_dir / "answer" / "answer.json"
        target = legacy_path if legacy_path.exists() else current_path
        bundled_path = Path(bundle_path) / "answer" / "answer.json"
        fallbacks = tuple(
            path
            for path in (legacy_path, current_path, bundled_path)
            if os.path.normcase(str(path.resolve(strict=False)))
            != os.path.normcase(str(target.resolve(strict=False)))
        )
        return AnswerStore(
            target,
            fallbacks=fallbacks,
            validator=cls._is_valid_answers,
        )

    def _answer_store(self) -> AnswerStore:
        store = getattr(self, "_answer_store_instance", None)
        if store is not None:
            return store

        # 兼容只构造最小测试替身和旧式嵌入调用；正常客户端始终在
        # __init__ 中使用显式 data_dir 创建实例。
        target = root_answer_path if os.path.exists(root_answer_path) else answer_path
        fallbacks = tuple(
            path
            for path in (root_answer_path, answer_path, bundle_answer_path)
            if os.path.normcase(os.path.abspath(path))
            != os.path.normcase(os.path.abspath(target))
        )
        store = AnswerStore(
            target,
            fallbacks=fallbacks,
            validator=self._is_valid_answers,
        )
        self._answer_store_instance = store
        return store

    def _load_answers_json(self, warn_on_fail: bool = False) -> dict:
        """通过安全存储加载并规范化原始题库。

        :param warn_on_fail: True 时加载失败只警告不抛异常（学习模式容错），
            False 时抛出异常（考试模式必须要有题库）
        :return: 原始题目标题 → 规范化题目对象
        """
        try:
            return self._normalize_answers(self._answer_store().load())
        except (AnswerStoreError, OSError, UnicodeError, ValueError):
            if warn_on_fail:
                self.log.warning("题库加载失败，课后习题将随机作答")
                return {}
            else:
                raise

    @staticmethod
    def _match_answer_contents(
        answers_json: dict,
        question: dict,
    ) -> set[str] | None:
        """按精确标题优先、唯一兼容模糊标题兜底查找答案。"""

        title = str(question.get("title", ""))
        current_signature = _option_signature(question)
        if not title or not current_signature:
            return None

        # 兼容旧调用方传入的 clean_title -> set 映射。
        legacy = answers_json.get(clean_text(title))
        if isinstance(legacy, (set, frozenset, list, tuple)):
            values = {clean_text(item) for item in legacy if clean_text(item)}
            return values or None

        entries = [
            (str(stored_title), stored)
            for stored_title, stored in answers_json.items()
            if isinstance(stored, dict)
        ]

        def compatible(entry: dict) -> bool:
            return (
                bool(_option_signature(entry))
                and _option_signature(entry) == current_signature
            )

        raw_matches = [
            entry for stored_title, entry in entries if stored_title == title
        ]
        if raw_matches:
            return (
                WeBanClient._correct_answer_contents(raw_matches[0])
                if len(raw_matches) == 1 and compatible(raw_matches[0])
                else None
            )

        exact_key = _exact_text(title)
        exact_matches = [
            entry
            for stored_title, entry in entries
            if _exact_text(stored_title) == exact_key
        ]
        if exact_matches:
            candidates = [entry for entry in exact_matches if compatible(entry)]
            return (
                WeBanClient._correct_answer_contents(candidates[0])
                if len(candidates) == 1
                else None
            )

        fuzzy_key = clean_text(title)
        candidates = [
            entry
            for stored_title, entry in entries
            if clean_text(stored_title) == fuzzy_key and compatible(entry)
        ]
        if len(candidates) != 1:
            return None
        return WeBanClient._correct_answer_contents(candidates[0])

    @staticmethod
    def _correct_answer_contents(entry: dict) -> set[str] | None:
        answers = {
            clean_text(option.get("content", ""))
            for option in entry.get("optionList", [])
            if isinstance(option, dict)
            and option.get("isCorrect") == 1
            and clean_text(option.get("content", ""))
        }
        return answers or None

    @classmethod
    def _answer_ids_for_question(
        cls,
        answers_json: dict,
        question: dict,
    ) -> list[str]:
        """仅在全部答案都能映射为当前试卷合法 ID 时返回结果。"""

        answer_contents = cls._match_answer_contents(answers_json, question)
        options = question.get("optionList")
        if not answer_contents or not isinstance(options, list):
            return []
        answer_ids = [
            str(option.get("id", ""))
            for option in options
            if isinstance(option, dict)
            and clean_text(option.get("content", "")) in answer_contents
            and option.get("id")
        ]
        if len(set(answer_ids)) != len(answer_ids):
            return []
        if len(answer_ids) != len(answer_contents):
            return []
        try:
            question_type = int(question.get("type", 1))
        except (TypeError, ValueError):
            return []
        if question_type == 2:
            return answer_ids if answer_ids else []
        return answer_ids if len(answer_ids) == 1 else []

    @staticmethod
    def get_project_type(project_category: int) -> str:
        """获取项目类型
        :param project_category: 1.新生安全教育 2.安全课程 3.专题学习 4.军事理论 9.实验室
        :return: "special" (专题), "lab" (实验室), 或 "" (其他)
        """
        if project_category == 3:
            return "special"
        if project_category == 9:
            return "lab"
        return ""

    @staticmethod
    def _project_startable(task: dict) -> tuple[bool, str]:
        """判断项目当前是否可学习（对齐官方 H5 项目入口拦截逻辑）

        官方 H5 首页 navToProject：
        - completion.grey 用 Number(grey)===1 判定（字符串 "1" 同样拦截），
          命中则 alert(completion.message) 并禁止进入；
        - 各分类导航（pre/normal/military）用 active===1 严格相等判定，
          active 不为数字 1 时 alert(message) 并禁止进入；
        - 学习任务列表过滤 grey!==1 && active===1 才纳入可进入列表。
        :param task: listMyProject/listStudyTask 返回的项目 dict
        :return: (是否可学, 服务端提示信息 message，未开始时非空)
        """
        completion = task.get("completion") or {}
        grey = completion.get("grey", 2)  # 1=灰色不可用（未开放/未开始），2=正常
        active = completion.get("active", 1)  # 1=可进入，2=不可进入
        # Number(grey)===1：数字 1 或字符串 "1" 都视为灰色拦截
        try:
            grey_blocked = int(grey) == 1
        except (TypeError, ValueError):
            grey_blocked = False
        active_ok = active == 1  # 官方 === 严格相等，字符串 "1" 不放行
        if not grey_blocked and active_ok:
            return True, ""
        # 官方仅弹 completion.message；studyStateLabel 只是 message 为空时的兜底
        message = completion.get("message") or task.get("studyStateLabel") or ""
        return False, message

    def _build_course_url(self, course: dict, task: dict) -> str:
        """根据课程和任务信息构建完整的课程 URL

        硬编码的 query 参数（projectType=special 等）为 Web 播放器前端所需，
        缺失会导致页面白屏或功能异常。
        :param course: 课程数据（含 resourceId）
        :param task: 任务数据（含 userProjectId）
        :return: 完整的课程播放 URL
        """
        response = self.api.get_course_url(course["resourceId"], task["userProjectId"])
        url = response.get("data") if _check_code_ok(response) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ResponseValidationError(f"课程链接响应无效：{response}")
        link = course.get("praiseNum", "")
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.extend(
            [
                ("userProjectId", str(task["userProjectId"])),
                ("userId", str(self.api.user["userId"])),
                ("courseId", str(course["resourceId"])),
                (
                    "userName",
                    str(
                        self.api.user.get("userName", self.api.user.get("realName", ""))
                    ),
                ),
                ("projectType", "special"),
                ("projectId", "undefined"),
                ("protocol", "true"),
                ("link", str(link)),
                ("weiban", "weiban"),
                ("certificateId", "undefined"),
                ("userActivityState", "undefined"),
                ("step", "undefined"),
                ("index", "undefined"),
                ("viewStep", "undefined"),
            ]
        )
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    # ---- tenant / progress --------------------------------------------------

    def get_tenant_code(self) -> str:
        """获取学校代码
        :return: 学校代码（tenant_code），找不到返回空字符串
        """
        if not self.tenant_name:
            self.log.error("学校全称不能为空")
            return ""
        tenant_list = self.api.get_tenant_list_with_letter()
        groups = tenant_list.get("data") if _check_code_ok(tenant_list) else None
        if not isinstance(groups, list):
            self.log.error(
                f"获取学校列表失败或结构无效：{_brief_response(tenant_list)}"
            )
            return ""
        self.log.info("获取学校列表成功")
        tenant_names = []
        maybe_names = []
        for item in groups:
            entries = item.get("list") if isinstance(item, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                code = str(entry.get("code") or "")
                if not name or not code:
                    continue
                tenant_names.append(name)
                if self.tenant_name == name.strip():
                    self.log.success(f"找到学校代码: {code}")
                    return code
                if self.tenant_name in name:
                    maybe_names.append(name)
        self.log.error(f"{tenant_names}")
        self.log.error(
            f"没找到你的学校代码，请检查学校全称是否正确"
            f"（上面是有效的学校名称）: {self.tenant_name}"
        )
        if maybe_names:
            self.log.error(f"可能的学校名称: {maybe_names}")
        return ""

    def get_progress(
        self, user_project_id: str, project_prefix: str | None, output: bool = True
    ) -> dict[str, Any]:
        """获取学习进度
        :param user_project_id: 项目 ID
        :param project_prefix: 日志前缀（如项目名）
        :param output: 是否输出进度日志
        :return: show_progress API 原始响应
        """
        progress = self.api.show_progress(user_project_id)
        if not _check_code_ok(progress):
            if output:
                self.log.warning(f"{project_prefix} 获取进度失败：{progress}")
            return progress
        data = progress.get("data")
        required_keys = (
            "requiredNum",
            "requiredFinishedNum",
            "optionalNum",
            "optionalFinishedNum",
            "pushNum",
            "pushFinishedNum",
            "examNum",
            "examFinishedNum",
        )
        if not isinstance(data, dict):
            message = "进度响应 data 不是对象"
            if output:
                self.log.error(f"{project_prefix} {message}")
            return {"code": "-1", "detailCode": "client_validation", "msg": message}
        try:
            counts = {key: int(data[key]) for key in required_keys}
        except (KeyError, TypeError, ValueError):
            message = "进度响应缺少合法计数字段"
            if output:
                self.log.error(f"{project_prefix} {message}：{progress}")
            return {"code": "-1", "detailCode": "client_validation", "msg": message}
        if any(value < 0 for value in counts.values()):
            message = "进度响应包含负数计数"
            if output:
                self.log.error(f"{project_prefix} {message}：{progress}")
            return {"code": "-1", "detailCode": "client_validation", "msg": message}
        data = {**data, **counts}
        progress = {**progress, "data": data}
        if self.study_force:
            # force 模式会重新学习所有课程，剩余量按总数计算
            required = data["requiredNum"]
            optional = data["optionalNum"]
            push = data["pushNum"]
        else:
            required = data["requiredNum"] - data["requiredFinishedNum"]
            optional = data["optionalNum"] - data["optionalFinishedNum"]
            push = data["pushNum"] - data["pushFinishedNum"]
        exam_left = data["examNum"] - data["examFinishedNum"]

        # 每门课耗时：项目内累计实测均值，样本少时与理论值渐进混合，
        # 避免首门课带验证码或单次网络波动让 ETA 大幅跳变。
        finished = (
            data["requiredFinishedNum"]
            + data["pushFinishedNum"]
            + data["optionalFinishedNum"]
        )
        now = time.time()
        state = self._eta_course_state.setdefault(
            user_project_id, {"started_at": now, "start_finished": finished}
        )
        completed = max(0, int(finished) - int(state["start_finished"]))
        measured_avg = None
        if completed > 0:
            elapsed = now - float(state["started_at"])
            if elapsed > 900:
                # 长时间中断/卡死后从当前进度重新起算，避免污染后续估算
                state.update(started_at=now, start_finished=finished)
                completed = 0
            else:
                measured_avg = elapsed / completed

        # 每门课：等待时长理论均值 + 固定开销（翻页 step 发送/课后习题/完课 API/验证码等）
        theoretical_est = self.study_base_time + self.study_random_upper / 2 + 6
        if measured_avg is None:
            course_est = theoretical_est
        else:
            trust = completed / (completed + 10)
            course_est = theoretical_est + (measured_avg - theoretical_est) * trust
            course_est = max(theoretical_est, course_est)
        eta = course_est * (required + optional + push)
        # 每场考试：默认 50 题 × 每题 4.5s + 固定开销 ≈ 4 分钟（有实测后自动替换）
        if exam_left > 0 and self.exam_mode != "false":
            exam_est = self._eta_exam_avg or (50 * 4.5 + 15)
            eta += exam_est * exam_left
        eta = max(0, int(eta))
        if output:
            eta_str = self._format_duration(eta)
            self.log.info(
                f"{project_prefix} 进度：必修课 {data['requiredFinishedNum']}/{data['requiredNum']}，"
                f"推送课 {data['pushFinishedNum']}/{data['pushNum']}，"
                f"自选课 {data['optionalFinishedNum']}/{data['optionalNum']}，"
                f"考试 {data['examFinishedNum']}/{data['examNum']}，预计剩余 {eta_str}"
            )
        return progress

    # ---- login --------------------------------------------------------------

    def login(self) -> dict | None:
        """登录并获取 token

        重试策略：前 10 次尝试用 CNN 模型自动识别验证码，
        失败 10 次后转为手动输入（打开图片浏览器），再额外给 3 次机会。
        :return: 成功返回 self.api.user，失败返回 None
        """
        self._raise_if_stopped()
        if self.api.user.get("userId"):
            return self.api.user
        retry_limit = 10
        # 前 10 次 OCR 自动识别，后 3 次手动输入
        for i in range(retry_limit + 3):
            self._raise_if_stopped()
            if i > 0:
                self.log.warning(f"登录失败，正在重试 {i}/{retry_limit + 2} 次")
            verify_time = self.api.get_timestamp(13, 0)
            verify_image = self.api.rand_letter_image(verify_time)
            if i < retry_limit:
                verify_code = LoginCaptchaSolver.recognize(verify_image, self.log)
                if not verify_code:
                    continue
            elif self.non_interactive:
                # 无交互模式：不阻塞等待手动输入，直接判定失败
                self.log.error(
                    "验证码 OCR 连续失败且处于无交互模式，无法手动输入验证码，"
                    "登录失败（可在宿主机浏览器配合 CDP 或稍后重试）"
                )
                break
            else:
                self.captcha_debug_dir.mkdir(parents=True, exist_ok=True)
                captcha_path = self.captcha_debug_dir / "verify_code.png"
                with captcha_path.open("wb") as f:
                    f.write(verify_image)
                try:
                    webbrowser.open(captcha_path.as_uri())
                    verify_code = self._prompt(
                        f"请在 {captcha_path} 查看验证码图片并输入验证码："
                    )
                finally:
                    try:
                        captcha_path.unlink(missing_ok=True)
                    except OSError as exc:
                        # 图片可能仍被浏览器/杀毒软件占用；清理失败不应
                        # 吞掉已经输入的验证码，更不能阻止登录请求。
                        self.log.debug(f"验证码图片清理失败：{exc}")
            res = self.api.login(verify_code, int(verify_time))
            if res.get("detailCode") == "67":
                self.log.warning("验证码识别失败，正在重试")
                continue
            if self.api.user.get("userId"):
                return self.api.user
            self.log.error(
                f"登录出错，请检查 config.toml 内账号密码，或删除文件后重试: {res}"
            )
            break
        return None

    # ---- project list & per-project cycle ----------------------------------

    def _get_project_list(self) -> list[dict] | None:
        """获取账号全部进行中的项目列表（含实验室课程合并）"""
        response = self.api.list_my_project()
        my_project = response.get("data") if _check_code_ok(response) else None
        if not isinstance(my_project, list) or not all(
            isinstance(project, dict) for project in my_project
        ):
            self.log.error(f"获取任务列表失败或结构无效：{response}")
            return None
        my_project = list(my_project)

        completion = self.api.list_completion()
        modules = completion.get("data") if _check_code_ok(completion) else None
        if not isinstance(modules, list) or not all(
            isinstance(item, dict) and "module" in item and "showable" in item
            for item in modules
        ):
            self.log.error(f"获取模块完成情况失败或结构无效：{completion}")
            return None
        showable_modules = [item["module"] for item in modules if item["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            data = lab_project.get("data") if _check_code_ok(lab_project) else None
            if not isinstance(data, dict):
                self.log.error(f"获取实验室课程失败：{lab_project}")
                return None
            current = data.get("current") or {}
            if current:
                if not isinstance(current, dict):
                    self.log.error(f"实验室课程结构无效：{lab_project}")
                    return None
                my_project.append(current)
        return my_project

    def run_project_cycle(
        self,
        study_time: str | int,
        study_mode: str,
        exam_mode: str,
        random_answer: bool,
        exam_question_time: str,
        exam_submit_match_rate: int,
    ) -> WorkflowResult:
        """按项目交替执行：每个项目先完成课程学习，再完成考试，然后
        切换到下一个项目（用户要求的顺序：项目 A 学习+考试 → 项目 B 学习+考试）。

        考试模式/学习模式为 "false" 时对应阶段整体跳过。
        """
        study = study_mode != "false"
        exam = exam_mode != "false"
        if not study and not exam:
            self.log.info("学习与考试均未开启，跳过")
            return WorkflowResult.success("学习与考试均未开启", skipped=1)

        if study:
            mode_desc = {"true": "正常", "force": "强制重新学习"}.get(
                study_mode, study_mode
            )
            self.log.info(f"学习模式: {mode_desc}")
        if exam:
            mode_desc = {
                "true": "正常",
                "perfect": "追求满分",
                "force": "强制重考",
            }.get(exam_mode, exam_mode)
            self.log.info(f"考试模式: {mode_desc}")

        projects = self._get_project_list()
        if projects is None:
            return WorkflowResult.failed_result("项目列表加载失败")
        if not projects:
            self.log.warning("当前没有进行中的项目。")
            return WorkflowResult.success("当前没有进行中的项目", skipped=1)

        overall = WorkflowResult.success()
        for project in projects:
            self._raise_if_stopped()
            project_name = project.get("projectName", "未知项目")
            user_project_id = project.get("userProjectId", "")
            self.log.info(f"===== 开始处理项目：{project_name} =====")
            if not user_project_id:
                self.log.warning(f"{project_name}：缺少 userProjectId，跳过")
                overall = overall.combine(
                    WorkflowResult.incomplete(
                        f"{project_name} 缺少 userProjectId",
                        skipped=1,
                    )
                )
                continue
            study_result = WorkflowResult.success()
            if study:
                study_result = self.run_study(
                    study_time,
                    study_mode,
                    only_project=project,
                )
                overall = overall.combine(study_result)
            if exam:
                if study and not study_result.ok:
                    self.log.error(
                        f"{project_name} 学习阶段未完整确认，安全跳过该项目考试"
                    )
                    overall = overall.combine(
                        WorkflowResult.incomplete(
                            f"{project_name} 因学习不完整跳过考试",
                            skipped=1,
                        )
                    )
                    continue
                exam_result = self.run_exam(
                    exam_mode=exam_mode,
                    random_answer=random_answer,
                    exam_question_time=exam_question_time,
                    exam_submit_match_rate=exam_submit_match_rate,
                    only_project=project,
                )
                overall = overall.combine(exam_result)
        return overall

    # ---- study --------------------------------------------------------------

    def run_study(
        self,
        study_time: str | int,
        study_mode: str = "true",
        only_project: dict | None = None,
    ) -> WorkflowResult:
        """主学习流程入口：遍历所有项目 → 分类 → 课程，逐门学习
        :param study_time: 每门课学习时长 "基础时间,随机上限"（秒），如 "20,10"
        :param study_mode: 学习模式，"force" 时忽略完成状态全部重新学习
        :param only_project: 只学习指定项目（按项目交替调用时传入，含
            projectName/userProjectId 等字段）；None 时学习全部项目
        """
        # 解析学习时长
        try:
            parts = str(study_time).split(",")
            self.study_base_time = max(0, int(parts[0]))
            self.study_random_upper = max(0, int(parts[1])) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            self.study_base_time = 20
            self.study_random_upper = 10

        self.log.info(
            f"每门课学习时长: {self._format_duration(self.study_base_time)}~{self._format_duration(self.study_base_time + self.study_random_upper)}"
        )

        force_restudy = study_mode == "force"
        self.study_force = force_restudy

        answers_json = self._load_answers_json(warn_on_fail=True)

        if only_project is not None:
            my_project = [only_project]
        else:
            my_project = self._get_project_list()
        if my_project is None:
            return WorkflowResult.failed_result("学习项目列表加载失败")
        if not my_project:
            self.log.warning("当前没有进行中的学习项目。")
            return WorkflowResult.success("当前没有学习项目", skipped=1)

        completed = 0
        failed = 0
        skipped = 0
        details: list[str] = []
        for task in my_project:
            self._raise_if_stopped()
            if not isinstance(task, dict) or not all(
                task.get(key) for key in ("projectName", "userProjectId")
            ):
                failed += 1
                details.append("项目结构无效")
                self.log.error(f"项目响应缺少必要字段：{task}")
                continue
            project_prefix = str(task["projectName"])
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(task)
            if not startable:
                self.log.warning(
                    f"{project_prefix}：{notice or '项目尚未开放，暂不可学习'}，跳过"
                )
                skipped += 1
                failed += 1
                details.append(f"{project_prefix}: 项目尚不可学习")
                continue
            self.log.info(f"开始处理任务：{project_prefix}")
            # 对齐官方 H5：进入学习项目页即发 initIndex（项目详情初始化），
            # 不依赖课程是否加载 apicenext.js
            try:
                init_response = self.api.init_index(task["userProjectId"])
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except (OSError, APIResponseError) as exc:
                self.log.error(f"初始化学习索引失败：{exc}")
                failed += 1
                details.append(f"{project_prefix}: 初始化失败")
                continue
            if not _check_code_ok(init_response):
                self.log.error(f"初始化学习索引失败：{init_response}")
                failed += 1
                details.append(f"{project_prefix}: 初始化响应失败")
                continue
            progress = self.get_progress(task["userProjectId"], project_prefix)
            if not _check_code_ok(progress):
                failed += 1
                details.append(f"{project_prefix}: 进度响应失败")
                continue
            progress_data = progress["data"]

            choose_types = [
                (3, "必修课", "requiredNum", "requiredFinishedNum"),
                (1, "推送课", "pushNum", "pushFinishedNum"),
                (2, "自选课", "optionalNum", "optionalFinishedNum"),
            ]
            project_ok = True
            for choose_type in choose_types:
                # 只跳过"项目无该类型需求"（需求数=0）的类型：
                # - need > 0（如项目确实要完成 5 门自选课）→ 正常学习该类型；
                # - need == 0（未配置该类型，如本例自选课 optionalNum=0）→
                #   整体跳过，避免把可选课池里未报名的课程当任务学
                #   （v3.9.8 #147 让自选课可真学，此前会把整个课池学完）
                need = int(progress_data.get(choose_type[2], 0) or 0)
                if need <= 0:
                    self.log.info(
                        f"{project_prefix} 无{choose_type[1]}需求"
                        f"（{choose_type[2]}={need}），跳过该类型"
                    )
                    continue
                # 官方 H5 课程主页按项目 projectMode 分流课程列表：
                # mode==1 折叠分类（listCategory+listCourse）；mode≠1 扁平分页
                # （listFlatCourse.do）。结构不明时不猜测分流。
                try:
                    simple = self.api.get_project_simple(task["userProjectId"])
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except (OSError, APIResponseError) as exc:
                    self.log.error(f"获取项目模式失败：{exc}")
                    project_ok = False
                    break
                simple_data = simple.get("data") if _check_code_ok(simple) else None
                if (
                    not isinstance(simple_data, dict)
                    or "projectMode" not in simple_data
                ):
                    self.log.error(f"获取项目模式响应无效：{simple}")
                    project_ok = False
                    break
                try:
                    project_mode = int(simple_data["projectMode"])
                except (TypeError, ValueError):
                    self.log.error(f"项目模式值无效：{simple_data['projectMode']}")
                    project_ok = False
                    break
                if project_mode != 1:
                    if not self._study_flat_courses(
                        task,
                        choose_type,
                        project_prefix,
                        answers_json,
                        force_restudy,
                    ):
                        project_ok = False
                        break
                    continue

                try:
                    categories = self.api.list_category(
                        task["userProjectId"], choose_type[0]
                    )
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except (OSError, APIResponseError) as exc:
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{exc}")
                    project_ok = False
                    break
                if not _check_code_ok(categories):
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    project_ok = False
                    break
                category_list = categories.get("data")
                if not isinstance(category_list, list) or not all(
                    isinstance(category, dict) for category in category_list
                ):
                    self.log.error(f"获取 {choose_type[1]} 分类结构无效")
                    project_ok = False
                    break

                for category in category_list:
                    if not all(
                        key in category
                        for key in (
                            "categoryName",
                            "categoryCode",
                            "finishedNum",
                            "totalNum",
                        )
                    ):
                        self.log.error(f"课程分类缺少必要字段：{category}")
                        project_ok = False
                        break
                    category_prefix = (
                        f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    )
                    try:
                        category_finished = int(category["finishedNum"])
                        category_total = int(category["totalNum"])
                    except (TypeError, ValueError):
                        self.log.error(f"课程分类计数无效：{category}")
                        project_ok = False
                        break
                    if not force_restudy and category_finished >= category_total:
                        continue

                    try:
                        courses = self.api.list_course(
                            task["userProjectId"],
                            category["categoryCode"],
                            choose_type[0],
                        )
                    except PermissionError:
                        raise
                    except (OSError, APIResponseError) as exc:
                        self.log.error(f"获取课程列表失败：{exc}")
                        project_ok = False
                        break
                    course_list = (
                        courses.get("data") if _check_code_ok(courses) else None
                    )
                    if not isinstance(course_list, list) or not all(
                        isinstance(course, dict) for course in course_list
                    ):
                        self.log.error(f"获取课程列表失败或结构无效：{courses}")
                        project_ok = False
                        break
                    for course in course_list:
                        if not force_restudy and _course_finished(course):
                            continue
                        if not self._learn_course(
                            course,
                            task,
                            category_prefix,
                            project_prefix,
                            answers_json,
                            force_restudy,
                        ):
                            project_ok = False
                            break
                    if not project_ok:
                        break
                if not project_ok:
                    break

            if project_ok and self._check_project_course_done(task, project_prefix):
                self.log.success(f"{project_prefix} 课程学习已完整确认")
                completed += 1
            else:
                self.log.error(f"{project_prefix} 课程学习未完整确认")
                failed += 1
                details.append(f"{project_prefix}: 学习未完整确认")

        if failed:
            return WorkflowResult.incomplete(
                "部分学习项目未完成",
                completed=completed,
                failed=failed,
                skipped=skipped,
                details=tuple(details),
            )
        return WorkflowResult.success(
            "学习阶段完成",
            completed=completed,
            skipped=skipped,
        )

    def _check_project_course_done(self, task: dict, project_prefix: str) -> bool:
        """校验项目各类型课程完成数是否达到需求数，不足时告警。

        服务端进度更新可能有延迟，因此在有界退避窗口内重读；覆盖折叠/扁平
        两种列表路径学完后的盲区（如扁平分页提前结束导致漏学）。
        """
        try:
            for attempt in range(len(PROGRESS_POLL_DELAYS) + 1):
                progress = self.get_progress(
                    task["userProjectId"], project_prefix, output=False
                )
                if not _check_code_ok(progress):
                    return False
                data = progress.get("data", {})
                completed = True
                for _, label, need_key, finished_key in [
                    (3, "必修课", "requiredNum", "requiredFinishedNum"),
                    (1, "推送课", "pushNum", "pushFinishedNum"),
                    (2, "自选课", "optionalNum", "optionalFinishedNum"),
                ]:
                    need = int(data.get(need_key, 0) or 0)
                    finished = int(data.get(finished_key, 0) or 0)
                    if need > 0 and finished < need:
                        completed = False
                        if attempt == len(PROGRESS_POLL_DELAYS):
                            self.log.warning(
                                f"{project_prefix} {label}完成 {finished}/{need}，"
                                f"未达到需求数，请检查是否漏学"
                            )
                if completed:
                    return True
                if attempt < len(PROGRESS_POLL_DELAYS):
                    self._sleep(PROGRESS_POLL_DELAYS[attempt])
            return False
        except (InterruptedError, PermissionError):
            raise  # Token 失效，立即终止该账号
        except (OSError, APIResponseError, ResponseValidationError) as e:
            self.log.debug(f"校验学习完成进度失败：{e}")
            return False

    def _learn_course(
        self,
        course: dict,
        task: dict,
        category_prefix: str,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> bool:
        """学习单门课程并校验进度是否更新（折叠/扁平两条列表路径共用）。

        :return: True 表示本门已确认完成；False 表示本门不完整
        """
        if not all(course.get(key) for key in ("resourceName", "resourceId")):
            self.log.error(f"{category_prefix}：课程响应缺少必要字段：{course}")
            return False
        course_prefix = f"{category_prefix}/{course['resourceName']}"
        try:
            progress_before = self.get_progress(
                task["userProjectId"], project_prefix, output=False
            )
            if not _check_code_ok(progress_before):
                self.log.error(f"{course_prefix}：学习前进度响应无效")
                return False
            d = progress_before["data"]
            finished_before = (
                d["requiredFinishedNum"]
                + d["pushFinishedNum"]
                + d["optionalFinishedNum"]
            )
            ok = self._study_one_course(
                course,
                task,
                category_prefix,
                project_prefix,
                answers_json,
                force_restudy,
            )
            if not ok:
                return False
            progress_after = None
            finished_after = finished_before
            for attempt in range(len(PROGRESS_POLL_DELAYS) + 1):
                progress_after = self.get_progress(
                    task["userProjectId"], project_prefix
                )
                if not _check_code_ok(progress_after):
                    self.log.error(f"{course_prefix}：学习后进度响应无效")
                    return False
                d = progress_after["data"]
                finished_after = (
                    d["requiredFinishedNum"]
                    + d["pushFinishedNum"]
                    + d["optionalFinishedNum"]
                )
                if force_restudy or finished_after > finished_before:
                    break
                if attempt < len(PROGRESS_POLL_DELAYS):
                    self.log.debug(
                        f"{course_prefix}：完课接口成功但进度尚未更新，"
                        f"{PROGRESS_POLL_DELAYS[attempt]:g}s 后重试"
                    )
                    self._sleep(PROGRESS_POLL_DELAYS[attempt])
            if not force_restudy and finished_after <= finished_before:
                self.log.warning(
                    f"{course_prefix}：完课接口成功但进度未更新，标记为不完整"
                )
                return False
            self.log.success(f"{course_prefix} 完成")
            return True
        except (InterruptedError, PermissionError):
            raise  # Token 失效，立即终止该账号
        except (OSError, APIResponseError, ResponseValidationError) as e:
            self.log.warning(f"{course_prefix}：学习未完成（{e}）")
            return False

    def _study_flat_courses(
        self,
        task: dict,
        choose_type: tuple,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> bool:
        """官方 projectMode≠1 的扁平分页课程列表路径（listFlatCourse.do）

        官方 H5 课程主页按 project/getSimple.do 的 projectMode 分流：
        mode==1 走折叠分类（listCategory + listCourse），mode≠1 全部 tab
        走 listFlatCourse.do 分页列表（平铺渲染，无分类层级）。日志前缀的
        分类名取课程对象的 categoryName（若服务端返回该字段），否则回退到
        tab 名（如"自选课"）；仅用于日志展示，不影响学习逻辑。

        对齐官方前端（app.js loadCourseDataByPage）：pageSize=12、pageNo
        从 1 递增、`finished = totalPages <= pageNo` 翻到最后一页为止，
        把全部页的 paginateData 拼接成完整课程列表。**先翻完所有页收集
        完整列表，再逐门学习**——不能在"边学边翻页"时翻页：listFlatCourse
        的排序会随课程完成状态变化（未完成优先），学完一页后排序漂移会让
        原本靠后的未完成课程沉到已翻过的页里，导致漏学（实测 25 门按
        12/页边学边翻只学到 13 门）。先收集再学则列表在一次翻页窗口内
        稳定，不会漏。
        """
        label = choose_type[1]
        page_size = 12  # 与官方前端一致
        page_no = 1
        courses_all: list[dict] = []
        while True:
            try:
                res = self.api.list_flat_course(
                    task["userProjectId"], choose_type[0], page_no, page_size
                )
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except (OSError, APIResponseError) as e:
                self.log.error(f"获取 {label} 课程失败：{e}")
                return False
            if not _check_code_ok(res):
                self.log.error(f"获取 {label} 课程失败：{res}")
                return False
            data = res.get("data")
            if not isinstance(data, dict):
                self.log.error(f"获取 {label} 课程响应 data 无效")
                return False
            page_courses = data.get("paginateData")
            if not isinstance(page_courses, list) or not all(
                isinstance(course, dict) for course in page_courses
            ):
                self.log.error(f"获取 {label} 课程分页列表结构无效")
                return False
            courses_all.extend(page_courses)
            try:
                total_pages = int(data["totalPages"])
            except (KeyError, TypeError, ValueError):
                self.log.error(f"获取 {label} 课程总页数无效")
                return False
            if total_pages < 1:
                self.log.error(f"获取 {label} 课程总页数无效：{total_pages}")
                return False
            # 官方结束条件：totalPages <= pageNo
            if total_pages <= page_no:
                break
            page_no += 1

        for course in courses_all:
            if not force_restudy and _course_finished(course):
                continue
            category_name = course.get("categoryName") or label
            category_prefix = f"{label} {project_prefix}/{category_name}"
            if not self._learn_course(
                course,
                task,
                category_prefix,
                project_prefix,
                answers_json,
                force_restudy,
            ):
                return False
        return True

    @staticmethod
    def _is_account_blocked(res: dict) -> bool:
        """完课/接口返回是否表示行为异常或账号锁定，应立即停跑。"""
        if not res:
            return False
        detail = str(res.get("detailCode", ""))
        msg = str(res.get("msg", ""))
        raw = str(res.get("raw", ""))
        if detail in {"10018", "701"}:
            return True
        return (
            "行为存在异常" in msg or "Account locked" in raw or "Account locked" in msg
        )

    def _study_one_course(
        self,
        course: dict,
        task: dict,
        category_prefix: str,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> bool:
        """处理单门课程：加载 apicenext.js 的走 jupiter 翻页轨迹；
        无 apicenext 的默认只答题+完课（对齐官方页面行为），配置
        jupiter_fallback=true 时也补翻页轨迹。再答题，最后完课。

                :return: True 表示完课接口成功；False 表示流程不完整
        """
        course_prefix = f"{category_prefix}/{course['resourceName']}"

        if not force_restudy and _course_finished(course):
            return True

        self.log.info(f"学习： {course_prefix}")
        # 官方 navToDetail 先 study.do，成功才进入课程页/取课程 URL；
        # 失败（含课程未开始/未开放）toast 服务端 msg 并跳过本门课
        study_res = self.api.study(course["resourceId"], task["userProjectId"])
        if not _check_code_ok(study_res):
            msg = study_res.get("message") or study_res.get("msg") or "课程暂时无法学习"
            self.log.warning(f"{course_prefix}：{msg}，跳过")
            return False
        study_start = time.time()

        # 官方 H5 完课不依赖列表对象的 userCourseId：课程页 URL 由
        # getCourseUrl.do 返回（CourseDetail/navToDetail 均走它），userCourseId
        # 由服务端填入 URL query，sdk.js finishWxCourse 从页面 URL 读取。
        # 列表对象是否带 userCourseId 取决于服务端：实测本租户
        # listCourse.do（chooseType=2 自选课）对象不含该字段（80/80 日志实证，
        # 必修课 chooseType=3 含）。照官方逻辑从课程 URL 提取，取不到才跳过
        # （不再假报"完成"）。
        course_url = self._build_course_url(course, task)
        self.log.info(f"{course_prefix}：{course_url.split('?')[0]}")
        query = parse_qs(urlparse(course_url).query)
        source_str = get_source_str(query)
        if "userCourseId" not in course:
            uid = query.get("userCourseId")
            if uid and uid[0]:
                course["userCourseId"] = uid[0]
            else:
                self.log.warning(
                    f"{course_prefix}：未获取到学习记录（userCourseId 为空），跳过"
                )
                return False

        course_code = ""
        url_path = urlparse(course_url).path
        # 三级路径课程（/course/DAGJAQ/DAGJAQ001/DAGJAQ001.html）取文件名
        code_match = re.search(r"/course/(?:[^/]+/)*([^/]+)\.html$", url_path)
        if not code_match:
            code_match = re.search(r"/course/([^/]+)/", url_path)
        if code_match:
            course_code = code_match.group(1)
        item_info = (
            self.parse_item_js(course_code, course_url=course_url)
            if course_code
            else {
                "uses_apinext": False,
                "nonstr_map": {},
                "has_exam": False,
                "total_step": 0,
            }
        )

        nonstr_map = item_info.get("nonstr_map", {})
        total_step = item_info.get("total_step", 0)
        uses_apinext = item_info.get("uses_apinext", False)

        # 1. jupiter finish=2 翻页轨迹：官方仅在加载 apicenext.js 的课程里上报
        # （页面 item.js 调 callApinext）；非 apicnext 课程默认不发，除非配置
        # jupiter_fallback=true（个别学校要求全部微课都有轨迹）
        trace_enabled = uses_apinext or self.jupiter_fallback
        # 同一次 apicenext 学习只创建一个 UUID，翻页、完成轨迹和 JSONP 完课
        # 全程复用。普通课程仍不向完课接口发送 uniqueNo。
        trace_unique_no = str(uuid4()) if trace_enabled else ""
        finish_unique_no = trace_unique_no if uses_apinext else None
        if total_step and trace_enabled:
            self.log.info(
                f"total_step={total_step} ({item_info.get('total_step_source', '')})"
            )
            if not uses_apinext:
                self.log.info("  课程未加载 apicenext.js，补充翻页轨迹上报")
            self.handle_apinext(
                course["userCourseId"],
                course["resourceId"],
                task["userProjectId"],
                nonstr_map,
                total_step,
                unique_no=trace_unique_no,
                finish=2,
            )

        # 2. 获取并回答题目（翻页后题目才可用）
        question_data = self.api.list_question(course["resourceId"])
        if not _check_code_ok(question_data):
            self.log.error(f"{course_prefix} 获取课程题目失败：{question_data}")
            return False
        question_payload = question_data.get("data")
        if not isinstance(question_payload, dict):
            self.log.error(f"{course_prefix} 课程题目响应结构无效")
            return False
        for key, label, save_func in [
            (
                "viewpointQuestionList",
                "观点题",
                self.api.save_question,
            ),
            (
                "examQuestionList",
                "课后习题",
                self.api.save_exam_question,
            ),
        ]:
            qlist = question_payload.get(key, [])
            if not isinstance(qlist, list):
                self.log.error(f"{course_prefix} {label}列表结构无效")
                return False
            if qlist:
                self.log.info(f"  {label} {len(qlist)} 道")
            for i, question in enumerate(qlist):
                if (
                    not isinstance(question, dict)
                    or not question.get("id")
                    or not isinstance(question.get("optionList"), list)
                    or not question["optionList"]
                ):
                    self.log.error(f"{course_prefix} {label}题目结构无效")
                    return False
                try:
                    self._answer_question(
                        question,
                        answers_json,
                        course["resourceId"],
                        save_func,
                        source_str,
                    )
                except ResponseValidationError as exc:
                    self.log.error(f"{course_prefix} {label}作答失败：{exc}")
                    return False
                self.log.info(f"    {i + 1}/{len(qlist)} 已完成")
                self._sleep(0.5)
        if item_info.get("has_exam") and not question_payload.get("examQuestionList"):
            self.log.info("  检测到题目标记但 list_question 无课后习题，可能为内联题目")

        # 3. 确保满足最低学习时长（服务端要求 study 后至少学习 study_time 秒才接受完课）
        elapsed = time.time() - study_start
        study_time = self.study_base_time + randint(0, self.study_random_upper)
        # 视频课程按配置倍速播放对齐（video_speed=0 表示不按视频时长等待）
        video_duration = item_info.get("video_duration", 0)
        if self.video_speed > 0 and video_duration > 0:
            if video_duration > 3600:
                self.log.warning("视频超过60分钟，按60分钟处理")
                video_duration = 3600
            video_need = video_duration / self.video_speed
            if video_need > study_time:
                self.log.info(
                    f"视频课程按 {self.video_speed:g} 倍速对齐：等待 "
                    f"{self._format_duration(video_need)}"
                    f"（视频时长 {self._format_duration(video_duration)}）"
                )
                study_time = video_need
        remaining = study_time - elapsed
        if remaining > 0:
            self.log.info(
                f"等待学习时长 {self._format_duration(remaining)} (已用 {self._format_duration(elapsed)}/{self._format_duration(study_time)})"
            )
            if self.video_speed > 0 and video_duration > 0:
                deadline = time.monotonic() + remaining
                while remaining > 0:
                    self._sleep(min(30, remaining))
                    remaining = max(0, deadline - time.monotonic())
                    if remaining > 0:
                        self.log.info(f"视频剩余 {self._format_duration(remaining)}")
            else:
                self._sleep(remaining)

        # 4. jupiter finish=1 完成标记（提交前上报学习完成，与翻页轨迹同条件）
        if total_step and trace_enabled:
            self.handle_apinext(
                course["userCourseId"],
                course["resourceId"],
                task["userProjectId"],
                nonstr_map,
                total_step,
                unique_no=trace_unique_no,
                finish=1,
            )
            self._sleep(2)

        # 5. 完课
        res = self._finish_course(
            course,
            task,
            query,
            course_url,
            finish_unique_no,
        )
        # 完课走 JSONP（sdk.js finishWxCourse）：checkCode 只认 code∈{0,1}
        if not _check_code_ok(res, allow_200=False):
            self.log.error(f"{course_prefix} 完成失败：{res}")
            if self._is_account_blocked(res):
                raise AccountBlockedError(
                    str(res.get("msg") or "系统检测到行为异常或账号已锁定"),
                    detail_code=str(res.get("detailCode", "")),
                )
            return False
        return True

    def _finish_course(
        self,
        course: dict,
        task: dict,
        query: dict,
        course_url: str,
        unique_no: str | None,
    ) -> dict:
        """调用正确的完课接口并返回响应

        四种完课模式按 URL 参数分发：
        - lyra → finish_lyra（LYRA 平台）
        - weiban 不存在 → finish_by_token(course_type="open")（PROTEUS 平台）
        - source=moon → finish_by_token(course_type="moon")
        - weiban 标准 → finish_by_token（WEIBAN，含可选 captcha 校验）
        :param course: 课程数据
        :param task: 任务数据
        :param query: URL 查询参数（parse_qs 格式）
        :param course_url: 完整课程 URL（用于 captcha）
        :param unique_no: 仅 apinext 课程传入；None 表示不传 uniqueNo
        :return: 完课 API 响应
        """
        if query.get("lyra", [None])[0] == "lyra":
            return self.api.finish_lyra(query.get("userActivityId", [None])[0])
        if query.get("weiban", [None])[0] != "weiban":
            return self.api.finish_by_token(course["userCourseId"], course_type="open")
        if query.get("source", [None])[0] == "moon":
            return self.api.finish_by_token(course["userCourseId"], course_type="moon")

        finish_kwargs: dict = {
            "referer": "https://mcwk.mycourse.cn/",
        }
        if unique_no:
            finish_kwargs["unique_no"] = unique_no
        if query.get("csCapt", [None])[0] == "true":
            try:
                captcha_result = self.captcha_handler.handle_course_captcha(
                    course_url=course_url
                )
                check_res = self.api.course_check(
                    course["userCourseId"],
                    task["userProjectId"],
                    course["resourceId"],
                    captcha_result["randstr"],
                    captcha_result["ticket"],
                )
                if not _check_code_ok(check_res):
                    self.log.error(f"课程验证码校验失败：{check_res}")
                    return check_res
                self.log.success("课程验证码校验通过")
                finish_kwargs["token"] = check_res.get("data", "")
            except InterruptedError:
                raise
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except Exception as e:  # noqa: BLE001 -- 浏览器自动化可能抛任意异常，降级为完成失败
                self.log.error(f"课程验证码处理异常: {e}")
                return {"code": "-1"}
        return self.api.finish_by_token(course["userCourseId"], **finish_kwargs)

    # ---- exam ---------------------------------------------------------------

    def run_exam(
        self,
        exam_mode: str = "true",
        random_answer: bool = True,
        exam_question_time: str = "3,3",
        exam_submit_match_rate: int = 90,
        only_project: dict | None = None,
    ) -> WorkflowResult:
        """考试主入口

        流程：加载题库 → 遍历项目/计划 → 无感验证码 → 获取试卷 →
        作答（根据 random_answer 决定手动/自动）→ 提交试卷。

        :param exam_mode: 考试模式
            - "false": 跳过所有考试
            - "true": 正常考试，已及格/已完成的考试默认跳过
            - "perfect": 达到满分为止，只剩一次机会且题库无法完全匹配则停止
            - "force": 强制考试，即使已及格也继续，除非没有考试机会
        :param random_answer: True=单选随机多选全选，False=终端手动输入
        :param exam_question_time: 每道题答题等待时长 "基础时间,随机上限"（秒）
        :param exam_submit_match_rate: 允许提交的最低题库匹配率（%）
        :param only_project: 只考试指定项目（按项目交替调用时传入）；
            None 时考试全部项目
        """
        # 解析每题等待时间
        try:
            parts = exam_question_time.split(",")
            question_base_time = int(parts[0])
            question_random_upper = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            question_base_time = 3
            question_random_upper = 3
        self.exam_mode = exam_mode

        answers_json = self._load_answers_json()
        completed = 0
        failed = 0
        skipped = 0
        details: list[str] = []

        def mark_failed(message: str) -> None:
            nonlocal failed
            failed += 1
            details.append(message)

        if only_project is not None:
            projects = [only_project]
        else:
            projects = self._get_project_list()
        if projects is None:
            return WorkflowResult.failed_result("考试项目列表加载失败")
        if not projects:
            self.log.warning("当前没有进行中的项目可考试。")
            return WorkflowResult.success("当前没有考试项目", skipped=1)

        for project in projects:
            self._raise_if_stopped()
            if not isinstance(project, dict) or not all(
                project.get(key) for key in ("projectName", "userProjectId")
            ):
                self.log.error(f"考试项目结构无效：{project}")
                mark_failed("考试项目结构无效")
                continue
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(project)
            if not startable:
                self.log.warning(
                    f"{project['projectName']}：{notice or '项目尚未开放，暂不可考试'}，跳过"
                )
                skipped += 1
                mark_failed(f"{project['projectName']}: 项目尚不可考试")
                continue
            self.log.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]

            exam_plans = self.api.exam_list_plan(user_project_id)
            if not _check_code_ok(exam_plans):
                self.log.error(f"获取考试计划失败：{exam_plans}")
                mark_failed(f"{project['projectName']}: 考试计划响应失败")
                continue
            exam_plans = exam_plans.get("data")
            if not isinstance(exam_plans, list) or not all(
                isinstance(plan, dict) for plan in exam_plans
            ):
                self.log.error(f"考试计划列表结构无效：{exam_plans}")
                mark_failed(f"{project['projectName']}: 考试计划结构无效")
                continue

            for plan in exam_plans:
                self._raise_if_stopped()
                required_plan_keys = {
                    "id",
                    "examPlanId",
                    "examPlanName",
                    "examOddNum",
                    "examFinishNum",
                    "examScore",
                    "passScore",
                }
                if not required_plan_keys.issubset(plan):
                    self.log.error(f"考试计划缺少必要字段：{plan}")
                    mark_failed(f"{project['projectName']}: 考试计划字段缺失")
                    continue
                plan_name = f"{project['projectName']}/{plan['examPlanName']}"
                try:
                    exam_odd_num = int(plan["examOddNum"])
                    exam_finish_num = int(plan["examFinishNum"])
                except (TypeError, ValueError, OverflowError):
                    self.log.error(f"{plan_name} 考试计划计数字段无效")
                    mark_failed(f"{plan_name}: 考试计划计数无效")
                    continue
                exam_score = _finite_score(plan["examScore"])
                pass_score = _finite_score(plan["passScore"])
                if exam_score is None or pass_score is None:
                    # 非有限分数会让 >= 判定失真（如 -inf 被当成"已及格"），
                    # 宁可跳过该计划也不能基于它决定是否交卷。
                    self.log.error(f"{plan_name} 考试计划分数字段无效")
                    mark_failed(f"{plan_name}: 考试计划分数无效")
                    continue

                # ── 已考过的考试，显示历史成绩 ──
                # 满分仅用于日志和 perfect 模式的跳过判断；preparePaper 在此
                # 处失败或结构异常时按默认 100 分继续，不能拖垮整个账号。
                full_score = 100.0
                if exam_finish_num > 0:
                    try:
                        pp = self.api.exam_prepare_paper(plan["id"])
                    except PermissionError:
                        raise  # Token 失效，立即终止该账号
                    except (OSError, APIResponseError) as exc:
                        self.log.debug(f"{plan_name} 读取试卷总分失败：{exc}")
                    else:
                        pp_data = pp.get("data") if _check_code_ok(pp) else None
                        if isinstance(pp_data, dict):
                            parsed_full = _finite_score(pp_data.get("paperScore"))
                            # 0 分满分同样不可信，保持默认 100
                            if parsed_full:
                                full_score = parsed_full
                    self.log.info(
                        f"{plan_name} 已考过 {exam_finish_num}/{exam_odd_num} 次，"
                        f"最高 {exam_score:g}/{full_score:g}（及格线 {pass_score:g}）"
                    )
                elif exam_odd_num > 0:
                    self.log.info(f"{plan_name} 未考试，可考 {exam_odd_num} 次")

                # ── 根据 exam_mode 判断是否跳过 ──
                if exam_odd_num <= 0:
                    self.log.info(f"{plan_name} 无剩余考试机会，跳过")
                    skipped += 1
                    if exam_finish_num <= 0 or exam_score < pass_score:
                        mark_failed(f"{plan_name}: 未完成且无剩余机会")
                    continue

                if (
                    exam_mode == "true"
                    and exam_finish_num > 0
                    and exam_score >= pass_score
                ):
                    self.log.info(
                        f"{plan_name} 已及格 ({exam_score}分 >= {pass_score}分)，跳过"
                    )
                    skipped += 1
                    continue

                if (
                    exam_mode == "perfect"
                    and exam_finish_num > 0
                    and exam_score >= full_score
                ):
                    self.log.info(f"{plan_name} 已满分 ({exam_score}分)，跳过")
                    skipped += 1
                    continue

                # perfect 模式：只剩 1 次机会时，检查题库是否能全覆盖
                if exam_mode == "perfect" and exam_odd_num <= 1:
                    # 先获取题目列表检查匹配率
                    warning_msg = (
                        f"{plan_name} 只剩 {exam_odd_num} 次考试机会，"
                        f"但 perfect 模式需要满分"
                    )
                    self.log.warning(warning_msg)

                if exam_mode == "true" and exam_finish_num > 0:
                    self.log.info(
                        f"{plan_name} 已完成 {exam_finish_num} 次，{plan_name} 继续考试以争取更好成绩"
                    )

                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]

                before_paper = self.api.exam_before_paper(plan["id"])
                if not _check_code_ok(before_paper):
                    self.log.error(
                        f"考试项目 {plan_name} 获取考试记录失败：{before_paper}"
                    )
                    mark_failed(f"{plan_name}: beforePaper 失败")
                    continue
                before_data = before_paper.get("data")
                if (
                    not isinstance(before_data, dict)
                    or "isExistedNotSubmit" not in before_data
                ):
                    self.log.error(
                        f"考试项目 {plan_name} 获取考试记录结构无效：{before_paper}"
                    )
                    mark_failed(f"{plan_name}: beforePaper 结构无效")
                    continue

                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if not _check_code_ok(prepare_paper):
                    if prepare_paper.get("detailCode") == "14":
                        self.log.warning(
                            f"{plan_name} 课程学习未完成，无法考试；"
                            f"请先完成该项目的课程学习"
                        )
                    else:
                        self.log.error(f"获取考试信息失败：{prepare_paper}")
                    mark_failed(f"{plan_name}: preparePaper 失败")
                    continue
                prepare_paper = prepare_paper.get("data")
                prepare_keys = {
                    "questionNum",
                    "paperScore",
                    "answerTime",
                    "realName",
                    "userIDLabel",
                }
                if not isinstance(prepare_paper, dict) or not prepare_keys.issubset(
                    prepare_paper
                ):
                    self.log.error(f"获取考试信息结构无效：{prepare_paper}")
                    mark_failed(f"{plan_name}: preparePaper 结构无效")
                    continue
                try:
                    question_num = int(prepare_paper["questionNum"])
                except (TypeError, ValueError):
                    question_num = 0
                if question_num <= 0:
                    self.log.error(f"{plan_name} 题目数无效，禁止开始考试")
                    mark_failed(f"{plan_name}: 题目数无效")
                    continue
                self.log.info(
                    f"考试信息：用户：{prepare_paper['realName']}，ID：{prepare_paper['userIDLabel']}，"
                    f"题目数：{question_num}，试卷总分：{prepare_paper['paperScore']}，"
                    f"限时 {prepare_paper['answerTime']} 分钟"
                )

                # 无感验证码
                plan_start_ts = time.time()
                try:
                    captcha_result = self.captcha_handler.handle_exam_captcha(
                        user_exam_plan_id
                    )
                    check_res = self.api.exam_check(
                        user_exam_plan_id,
                        captcha_result["randstr"],
                        captcha_result["ticket"],
                    )
                    if not _check_code_ok(check_res):
                        self.log.error(f"无感验证码校验失败：{check_res}")
                        mark_failed(f"{plan_name}: 验证码校验失败")
                        continue
                    self.log.success("无感验证码校验通过")
                except InterruptedError:
                    raise
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except Exception as e:  # noqa: BLE001 -- 浏览器自动化可能抛任意异常
                    self.log.error(f"无感验证码处理异常: {e}")
                    mark_failed(f"{plan_name}: 验证码处理异常")
                    continue

                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if not _check_code_ok(exam_paper):
                    self.log.error(f"获取考试题目失败：{exam_paper}")
                    mark_failed(f"{plan_name}: startPaper 失败")
                    continue

                exam_paper = exam_paper.get("data")
                if not isinstance(exam_paper, dict):
                    self.log.error(f"{plan_name} 试卷响应 data 无效，禁止交卷")
                    mark_failed(f"{plan_name}: 试卷 data 无效")
                    continue
                question_list = exam_paper.get("questionList")
                if not isinstance(question_list, list) or not question_list:
                    self.log.error(f"{plan_name} 空试卷或题目列表无效，禁止交卷")
                    mark_failed(f"{plan_name}: 空试卷")
                    continue
                if len(question_list) != question_num:
                    self.log.error(
                        f"{plan_name} 试卷题数 {len(question_list)} 与声明题数 "
                        f"{question_num} 不符，禁止交卷"
                    )
                    mark_failed(f"{plan_name}: 题数不符")
                    continue
                paper_valid = True
                for question in question_list:
                    if not isinstance(question, dict) or not all(
                        key in question for key in ("id", "title", "type", "optionList")
                    ):
                        paper_valid = False
                        break
                    options = question["optionList"]
                    if not isinstance(options, list) or not options:
                        paper_valid = False
                        break
                    option_ids = [
                        str(option.get("id", ""))
                        for option in options
                        if isinstance(option, dict) and option.get("id")
                    ]
                    if len(option_ids) != len(options) or len(set(option_ids)) != len(
                        option_ids
                    ):
                        paper_valid = False
                        break
                if not paper_valid:
                    self.log.error(f"{plan_name} 试卷题目/选项结构无效，禁止交卷")
                    mark_failed(f"{plan_name}: 试卷结构无效")
                    continue

                have_answer: list[tuple[dict, list[str]]] = []
                no_answer: list[dict] = []
                for question in question_list:
                    mapped_ids = self._answer_ids_for_question(
                        answers_json,
                        question,
                    )
                    if mapped_ids:
                        have_answer.append((question, mapped_ids))
                    else:
                        no_answer.append(question)

                match_rate = (
                    len(have_answer) / len(question_list) * 100 if question_list else 0
                )
                self.log.info(
                    f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，"
                    f"无答案的题目数：{len(no_answer)}，题库匹配率：{match_rate:.1f}%"
                )

                # 安全门按“已映射到当前试卷合法选项 ID”的题数计算，
                # 与是否启用随机答案无关。
                if match_rate < exam_submit_match_rate:
                    self.log.error(
                        f"题库匹配率 {match_rate:.1f}% 低于阈值 {exam_submit_match_rate}%，"
                        "禁止记录答案和交卷"
                    )
                    mark_failed(f"{plan_name}: 匹配率未达安全阈值")
                    continue
                if exam_odd_num <= 1 and match_rate < 100:
                    self.log.error(
                        f"{plan_name} 只剩最后一次机会且题库未 100% 合法映射，"
                        "禁止记录答案和交卷"
                    )
                    mark_failed(f"{plan_name}: 最后一次机会安全门")
                    continue

                # ── 处理无答案题目 ──
                recorded_count = 0
                answer_failed = False
                for i, question in enumerate(no_answer):
                    type_label = question.get("typeLabel", "未知")

                    # 优先尝试 AI 搜题
                    ai_answers_ids = []
                    if self.ai_config and self.ai_config.get("enable"):
                        ai_answers_ids = self._ai_search_question(question)

                    if ai_answers_ids:
                        answers_ids = ai_answers_ids
                        use_time = question_base_time + randint(
                            0, question_random_upper
                        )
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] AI 搜题作答成功 "
                            f"({type_label})，等待 {self._format_duration(use_time)}: "
                            f"{question['title'][:40]}..."
                        )
                        self._sleep(use_time)
                    elif random_answer or self.non_interactive:
                        # 自动随机作答：单选随机选一个，多选全选
                        # （无交互模式即使配置 random_answer=false 也走随机，
                        #   避免阻塞等待终端输入）
                        answers_ids = self._auto_select_answer(question)
                        use_time = question_base_time + randint(
                            0, question_random_upper
                        )
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 随机作答 "
                            f"({type_label})，等待 {self._format_duration(use_time)}: "
                            f"{question['title'][:40]}..."
                        )
                        self._sleep(use_time)
                    else:
                        # 手动输入
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 题目不在题库中，请手动选择答案"
                        )
                        self.log.info(
                            f"题目类型：{type_label}，题目标题：{question['title']}"
                        )
                        for j, opt in enumerate(question["optionList"]):
                            self.log.info(f"{j + 1}. {opt['content']}")

                        opt_count = len(question["optionList"])
                        start_time = time.time()
                        answers_ids = []

                        while not answers_ids:
                            answer = (
                                self._prompt(
                                    f"[{self.api.user.get('realName', '未知')}] "
                                    "请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）："
                                )
                                .replace(" ", "")
                                .replace("，", ",")
                            )
                            candidates = [
                                ans.strip() for ans in answer.split(",") if ans.strip()
                            ]
                            if all(
                                ans.isdigit() and 1 <= int(ans) <= opt_count
                                for ans in candidates
                            ):
                                answers_ids = [
                                    question["optionList"][int(ans) - 1]["id"]
                                    for ans in candidates
                                ]
                                for ans in candidates:
                                    self.log.info(
                                        f"选择答案：{ans}，"
                                        f"内容：{question['optionList'][int(ans) - 1]['content']}"
                                    )
                            else:
                                self.log.error(
                                    "输入无效，请重新输入（序号需为数字且在选项范围内）"
                                )

                        use_time = round(time.time() - start_time)

                    valid_option_ids = {
                        str(option["id"]) for option in question["optionList"]
                    }
                    selected_ids = [str(answer_id) for answer_id in answers_ids]
                    try:
                        question_type = int(question.get("type", 1))
                    except (TypeError, ValueError):
                        question_type = 0
                    cardinality_ok = (
                        bool(selected_ids)
                        if question_type == 2
                        else len(selected_ids) == 1
                    )
                    if (
                        not cardinality_ok
                        or len(set(selected_ids)) != len(selected_ids)
                        or not set(selected_ids).issubset(valid_option_ids)
                    ):
                        self.log.error(
                            f"{plan_name} 题目 {question['id']} 产生空答案、"
                            "重复答案或非法答案 ID，禁止交卷"
                        )
                        answer_failed = True
                        break
                    self.log.info("正在提交当前答案")
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        answer_failed = True
                        break
                    recorded_count += 1

                if answer_failed:
                    mark_failed(f"{plan_name}: 答案记录失败")
                    continue

                # ── 题库作答 ──
                if have_answer:
                    self.log.info(f"开始答题库中的题目，共 {len(have_answer)} 道题目")
                for i, (question, answers_ids) in enumerate(have_answer):
                    self.log.info(
                        f"[{i + 1}/{len(have_answer)}] 题目在题库中，开始答题"
                    )
                    self.log.info(
                        f"题目类型：{question.get('typeLabel', '未知')}，"
                        f"题目标题：{question['title']}"
                    )
                    use_time = question_base_time + randint(0, question_random_upper)
                    self.log.info(
                        f"等待 {self._format_duration(use_time)}，模拟答题中..."
                    )
                    self._sleep(use_time)
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        answer_failed = True
                        break
                    recorded_count += 1

                if answer_failed:
                    mark_failed(f"{plan_name}: 题库答案记录失败")
                    continue
                if recorded_count != len(question_list):
                    self.log.error(
                        f"{plan_name} 仅成功记录 {recorded_count}/{len(question_list)} "
                        "道题，禁止交卷"
                    )
                    mark_failed(f"{plan_name}: 答案记录数不完整")
                    continue

                self.log.info("完成考试，正在提交试卷...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if not _check_code_ok(submit_res):
                    self.log.error(f"提交试卷失败：{submit_res}")
                    mark_failed(f"{plan_name}: 提交试卷失败")
                    continue
                submit_data = submit_res.get("data")
                if not isinstance(submit_data, dict) or "score" not in submit_data:
                    self.log.error(f"提交试卷响应结构无效：{submit_res}")
                    mark_failed(f"{plan_name}: 交卷响应结构无效")
                    continue
                self.log.success(
                    f"试卷提交成功，考试完成，成绩：{submit_data['score']} 分"
                )
                self._update_exam_eta(time.time() - plan_start_ts)
                completed += 1

        if failed:
            return WorkflowResult.incomplete(
                "部分考试计划未完成",
                completed=completed,
                failed=failed,
                skipped=skipped,
                details=tuple(details),
            )
        return WorkflowResult.success(
            "考试阶段完成",
            completed=completed,
            skipped=skipped,
        )

    def _update_exam_eta(self, elapsed: float) -> None:
        """用实测考试耗时更新每场考试的自适应估计（EMA）"""
        if elapsed <= 0:
            return
        if self._eta_exam_avg is None:
            self._eta_exam_avg = elapsed
        else:
            self._eta_exam_avg = 0.7 * self._eta_exam_avg + 0.3 * elapsed

    # ---- item.js parsing ----------------------------------------------------

    def parse_item_js(
        self, course_code: str, course_url: str | None = None
    ) -> dict[str, Any]:
        """解析课程 JS，检测是否使用 apinext 并提取 nonstrMap/total_step。

        关键判断：HTML 是否加载 apicenext.js。
        不加载 → 不需要任何 apinext 调用，直接返回 uses_apinext=False。
        加载 → 从 item.js 注释/HTML btn-next 推导 total_step。

        :param course_code: 课程代码（用于拼接 mcwk 资源 URL）
        :param course_url: 课程播放页 URL，作为抓 mcwk HTML 的 Referer。
            缺失时 mcwk 资源服务器可能 403。
        """
        result = {
            "uses_apinext": False,
            "nonstr_map": {},
            "has_exam": False,
            "total_step": 0,
            "total_step_source": "",
            "has_video": False,
            "video_duration": 0.0,
        }

        try:
            # 直接复用播放 URL 的路径，兼容二级/三级路径课程
            # （/course/A25005/A25005.html 与 /course/DAGJAQ/DAGJAQ001/DAGJAQ001.html）
            url_path = urlparse(course_url).path if course_url else ""
            html_url = (
                f"https://mcwk.mycourse.cn{url_path}"
                if "/course/" in url_path
                else f"https://mcwk.mycourse.cn/course/{course_code}/{course_code}.html"
            )
            html = _fetch_text(self.api.session, html_url, referer=course_url)
            if not html:
                return result

            # 视频课程：提取 <video>/<source> 源，解析实际时长供完课前按 2 倍速等待
            video_match = None
            video_block = re.search(r"<video\b[^>]*>(.*?)</video>", html, re.DOTALL)
            if video_block:
                # 去掉注释（被注释掉的备选 m3u8 源不算数），再找 <source>
                clean = re.sub(r"<!--.*?-->", "", video_block.group(1), flags=re.DOTALL)
                video_match = re.search(
                    r"<source\b[^>]*\bsrc=[\"']([^\"']+)[\"']", clean
                )
            if not video_match:
                video_match = re.search(r"<video\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html)
            if video_match:
                video_url = urljoin(html_url, video_match.group(1))
                result["has_video"] = True
                video_duration = 0.0
                try:
                    if video_url.endswith(".m3u8") or "/m3u8/" in video_url:
                        # m3u8：累加 EXTINF 时长
                        playlist = self.api.session.get(video_url, timeout=10)
                        if playlist.status_code == 200:
                            segments = re.findall(
                                r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)",
                                playlist.text,
                            )
                            if not segments:
                                variant = re.search(
                                    r"#EXT-X-STREAM-INF:[^\n]*\n\s*(\S+)",
                                    playlist.text,
                                )
                                if variant:
                                    playlist = self.api.session.get(
                                        urljoin(video_url, variant.group(1)),
                                        timeout=10,
                                    )
                                    if playlist.status_code == 200:
                                        segments = re.findall(
                                            r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)",
                                            playlist.text,
                                        )
                            video_duration = sum(float(s) for s in segments)
                    else:
                        # mp4：Range 抓文件头/尾各 512KB，解析 moov 内 mvhd
                        head_size = 512 * 1024
                        head = self.api.session.get(
                            video_url,
                            headers={"Range": f"bytes=0-{head_size - 1}"},
                            timeout=10,
                        )
                        buffers: list[bytes] = []
                        if head.status_code in (200, 206):
                            buffers.append(head.content[:head_size])
                            if head.status_code == 206:
                                match = re.search(
                                    r"/(\d+)\s*$",
                                    head.headers.get("Content-Range", ""),
                                )
                                if match:
                                    total = int(match.group(1))
                                    if total > head_size:
                                        tail = self.api.session.get(
                                            video_url,
                                            headers={
                                                "Range": (
                                                    f"bytes={total - head_size}-"
                                                    f"{total - 1}"
                                                )
                                            },
                                            timeout=10,
                                        )
                                        if tail.status_code == 206:
                                            buffers.append(tail.content)
                        for buf in buffers:
                            pos = 0
                            while pos + 8 <= len(buf) and not video_duration:
                                size = int.from_bytes(buf[pos : pos + 4], "big")
                                box_type = buf[pos + 4 : pos + 8]
                                if size == 1:  # largesize（64 位）
                                    if pos + 16 > len(buf):
                                        break
                                    size = int.from_bytes(
                                        buf[pos + 8 : pos + 16], "big"
                                    )
                                    header = 16
                                elif size == 0:  # 延伸到文件尾
                                    size = len(buf) - pos
                                    header = 8
                                else:
                                    header = 8
                                if size < header:
                                    break
                                if box_type == b"moov":
                                    q = pos + header
                                    end = min(pos + size, len(buf))
                                    while q + 8 <= end:
                                        inner_size = int.from_bytes(
                                            buf[q : q + 4], "big"
                                        )
                                        if inner_size < 8:
                                            break
                                        if buf[q + 4 : q + 8] == b"mvhd":
                                            version = buf[q + 8]
                                            if version == 0 and q + 28 <= len(buf):
                                                timescale = int.from_bytes(
                                                    buf[q + 20 : q + 24],
                                                    "big",
                                                )
                                                duration = int.from_bytes(
                                                    buf[q + 24 : q + 28],
                                                    "big",
                                                )
                                            elif q + 40 <= len(buf):
                                                timescale = int.from_bytes(
                                                    buf[q + 28 : q + 32],
                                                    "big",
                                                )
                                                duration = int.from_bytes(
                                                    buf[q + 32 : q + 40],
                                                    "big",
                                                )
                                            else:
                                                timescale = 0
                                                duration = 0
                                            if timescale:
                                                video_duration = duration / timescale
                                            break
                                        q += inner_size
                                    break
                                pos += size
                except OSError:
                    pass  # 视频元数据获取失败按普通课程处理
                result["video_duration"] = video_duration
                self.log.info(
                    f"视频课程，视频时长 "
                    f"{self._format_duration(video_duration) if video_duration else '未知'}"
                )

            # 不加载 apicenext.js 的课程：JS 无 nonstrMap。仍解析 nav 步数供
            # jupiter_fallback=true 时使用（默认关闭，完全对齐官方不发轨迹）
            if "apicenext.js" not in html:
                result["has_exam"] = (
                    "saveExamQuestion" in html or "listQuestions" in html
                )
                nav_pages, _ = _count_nav_pages(html)
                if nav_pages:
                    result["total_step"] = nav_pages
                    result["total_step_source"] = f"html nav={nav_pages}"
                return result

            result["uses_apinext"] = True
            script_urls = [
                urljoin(html_url, src)
                for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', html)
                if "item.js" in src or f"{course_code}.js" in src
            ]
            script_urls.extend(
                [
                    f"{html_url.rsplit('/', 1)[0]}/js/item.js",
                    f"{html_url.rsplit('/', 1)[0]}/build/js/{course_code}.js",
                ]
            )

            seen_urls: set[str] = set()
            for item_url in script_urls:
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                # JS 由 HTML 加载，Referer 是 HTML 自身的 URL
                content = _fetch_text(self.api.session, item_url, referer=html_url)
                if not content:
                    continue
                extracted = _extract_map(content)
                if extracted:
                    result["nonstr_map"].update(extracted)
                result["has_exam"] = result["has_exam"] or _check_exam(content)

            # 推导 total_step（finish=2 的调用次数 = finish=1 的 step - 1）
            # 每个题目页会产生 2 次额外 apinext 调用（提交 → 结果页 → 继续）
            nav_pages, question_pages = _count_nav_pages(html)
            max_nonstr = max(result["nonstr_map"].keys()) if result["nonstr_map"] else 0
            extra_steps = question_pages * 2
            if nav_pages or max_nonstr:
                base = max(nav_pages, max_nonstr)
                result["total_step"] = base + extra_steps
                parts = []
                if nav_pages:
                    parts.append(f"html nav={nav_pages}")
                if max_nonstr and max_nonstr > nav_pages:
                    parts.append(f"nonstr max={max_nonstr}")
                if extra_steps:
                    parts.append(f"+{extra_steps}题")
                result["total_step_source"] = " ".join(parts)

        except Exception as e:  # noqa: BLE001 -- 解析边界，尽力而为，失败返回默认结构
            self.log.warning(f"解析课程 JS 失败：{e}")
        return result

    # ---- apinext / answer helpers -------------------------------------------

    def handle_apinext(
        self,
        user_course_id: str,
        course_id: str,
        user_project_id: str,
        nonstr_map: dict[int, str],
        total_step: int,
        unique_no: str = "",
        finish: int = 2,
        step_delay: float = 1,
    ) -> str:
        """调用 apinext 接口模拟翻页学习过程

        finish=2：逐页发送 step=1..total_step 模拟中间翻页（nonstr 来自 nonstr_map）。
        finish=1：发送 step=total_step+1 标记学习完成（nonstr 为空，因为 nonstr_map
        中不包含完成步，所以需要偏移 +1）。

        :param user_course_id: 用户课程 ID
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :param nonstr_map: nonstr 值映射（step → nonstr 值）
        :param total_step: finish=2 的调用次数
        :param unique_no: 本次学习的唯一标识
        :param finish: 2=中间步骤, 1=完成标记
        :param step_delay: 每步之间的延迟（秒）
        :return: unique_no
        """
        if unique_no == "":
            unique_no = str(uuid4())
        if not total_step:
            return unique_no

        def _send_step(step: int, finish: int, nonstr: str, label: str) -> None:
            """Jupiter 是状态写入；单步只发送一次并严格校验响应。"""

            resp = self.api.apinext(
                user_course_id,
                course_id,
                user_project_id,
                step=step,
                finish=finish,
                nonstr=nonstr,
                unique_no=unique_no,
            )
            if not _check_code_ok(resp) or resp.get("success") is not True:
                raise ResponseValidationError(f"apinext [{label}] 返回异常：{resp}")
            self.log.info(f"apinext [{label}] finish={finish} 已发送")

        if finish == 2:
            self.log.info(f"apinext 发送中间步骤，共 {total_step} 步")
            for step in range(1, total_step + 1):
                if step_delay:
                    self._sleep(step_delay)
                # nonstr_map 的 key 对应 finish=2 的 step，完成步 (finish=1) 不在 map 中
                _send_step(step, 2, nonstr_map.get(step, ""), f"{step}/{total_step}")
        else:
            if step_delay:
                self._sleep(step_delay)
            # finish=1 的 step 需要偏移 total_step + 1（nonstr_map 不含此步）
            _send_step(total_step + 1, 1, "", f"完成标记 step={total_step + 1}")
        return unique_no

    @staticmethod
    def _auto_select_answer(question: dict) -> list:
        """自动选择答案：单选随机选一个，多选全选

        :param question: 题目数据（含 type 和 optionList）
        :return: 选中选项的 ID 列表
        """
        option_list = question.get("optionList", [])
        if not option_list:
            return []
        question_type = question.get("type", 1)
        if question_type == 2:
            # 多选题 → 全选
            return [opt["id"] for opt in option_list]
        # 单选题 → 随机选一个
        return [option_list[randint(0, len(option_list) - 1)]["id"]]

    def _answer_question(
        self,
        question: dict,
        answers_json: dict,
        course_id: str,
        save_func,
        source: str,
    ) -> bool:
        """答题通用逻辑，返回是否通过题库命中

        题库未命中时使用 fallback 策略：先提交第一个错误选项，
        从响应中提取 answerLabel（如 "A-B-D"），再据此提交正确答案。
        观点题返回列表（无 answerLabel），无法使用此策略。
        :param question: 题目数据（含 title、optionList）
        :param answers_json: 题库映射
        :param course_id: 课程 ID
        :param save_func: 提交函数（save_question 或 save_exam_question）
        :param source: sourceStr 值
        :return: 题库命中返回 True，fallback/失败返回 False
        """
        option_list = question.get("optionList", [])
        if not option_list:
            return False

        # 题库命中，直接提交正确答案
        answer_ids = self._answer_ids_for_question(answers_json, question)
        if answer_ids:
            result = save_func(
                course_id,
                question["id"],
                json.dumps(answer_ids),
                source,
            )
            if not _check_code_ok(result):
                raise ResponseValidationError(f"课程答题响应失败：{result}")
            return True

        # 题库未命中：先提交第一个选项，从响应中提取正确 answerLabel
        res = save_func(
            course_id,
            question["id"],
            json.dumps([option_list[0]["id"]]),
            source,
        )
        if not _check_code_ok(res):
            raise ResponseValidationError(f"课程试探答题响应失败：{res}")
        data = res.get("data", {})
        # 观点题返回投票统计列表，无 answerLabel
        if isinstance(data, list):
            return False

        answer_label = data.get("answerLabel", "")
        if not answer_label:
            return False

        correct_letters = {ch for ch in answer_label.replace("-", "") if ch.isalpha()}
        if not correct_letters:
            return False

        letter_to_opt = {chr(65 + idx): opt for idx, opt in enumerate(option_list)}
        answer_ids = [
            letter_to_opt[ch]["id"] for ch in correct_letters if ch in letter_to_opt
        ]
        if answer_ids:
            result = save_func(
                course_id,
                question["id"],
                json.dumps(answer_ids),
                source,
            )
            if not _check_code_ok(result):
                raise ResponseValidationError(f"课程纠正答题响应失败：{result}")
        return False

    def record_answer(
        self,
        user_exam_plan_id: str,
        question_id: str,
        per_time: int,
        answers_ids: list,
        exam_plan_id: str,
    ) -> bool:
        """记录答题
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param per_time: 答题耗时（秒，用于模拟真实答题行为）
        :param answers_ids: 选中选项的 ID 列表
        :param exam_plan_id: 考试计划 ID
        :return: 成功返回 True，失败返回 False
        """
        if (
            not answers_ids
            or any(
                not isinstance(answer_id, str) or not answer_id
                for answer_id in answers_ids
            )
            or len(set(answers_ids)) != len(answers_ids)
        ):
            self.log.error("拒绝记录空答案、重复答案或无效答案 ID")
            return False
        res = self.api.exam_record_question(
            user_exam_plan_id,
            question_id,
            per_time,
            answers_ids,
            exam_plan_id,
        )
        if not _check_code_ok(res):
            self.log.error(f"答题失败，请重新开启考试：{res}")
            return False
        self.log.info("保存答案成功")
        return True

    def _ai_search_question(self, question: dict) -> list:
        """使用 AI 服务获取题目答案

        :param question: 题目字典
        :return: 选中的选项 ID 列表
        """
        if not self.ai_config or not self.ai_config.get("enable"):
            return []

        api_key = self.ai_config.get("api_key", "").strip()
        base_url = self.ai_config.get("base_url", "https://api.deepseek.com").strip()
        model = self.ai_config.get("model", "deepseek-v4-pro").strip()
        timeout = int(self.ai_config.get("timeout", 60))
        max_retries = int(self.ai_config.get("max_retries", 2))

        if not api_key and not self._ai_key_warned:
            self.log.warning("AI 搜题已启用，但未配置 api_key")
            self._ai_key_warned = True

        title = question.get("title", "")
        type_label = question.get("typeLabel", "未知")
        options = question.get("optionList", [])
        opt_count = len(options)

        options_str = "\n".join(
            [f"{i + 1}. {opt['content']}" for i, opt in enumerate(options)]
        )

        prompt = f"""你是一个在线教育考试答题助手。请根据题目和选项，给出正确答案。

【题目类型】{type_label}

【题目】{title}

【选项】
{options_str}

【规则】
1. 单选题/判断题：answers 只能包含 1 个选项序号
2. 多选题：answers 包含所有正确选项的序号
3. 选项序号为 1-based，有效范围：1~{opt_count}

【输出格式】严格输出一个 JSON 对象，不要输出任何其他内容：
{{"answers":[1],"reason":"理由"}}"""

        self.log.info(f"AI 搜题：{title[:40]}...")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

        url = f"{base_url.rstrip('/')}/chat/completions"
        content = None

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.api.session.post(
                    url, headers=headers, json=payload, timeout=timeout
                )
                resp.raise_for_status()
                res_data = resp.json()

                usage = res_data.get("usage", {})
                if usage:
                    self.log.debug(
                        f"AI token 用量 — prompt: {usage.get('prompt_tokens', '?')}, "
                        f"completion: {usage.get('completion_tokens', '?')}, "
                        f"total: {usage.get('total_tokens', '?')}"
                    )

                content = res_data["choices"][0]["message"]["content"].strip()
                break

            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                if attempt < max_retries:
                    wait = attempt * 2
                    self.log.warning(
                        f"AI 搜题第 {attempt} 次请求失败，{wait}s 后重试：{e}"
                    )
                    self._sleep(wait)
                else:
                    self.log.error(f"AI 搜题请求失败（已重试 {max_retries} 次）：{e}")
                    return []

        if not content:
            return []

        # 解析 AI 返回的 JSON
        raw_indices = self._parse_ai_answer(content)
        if raw_indices is None:
            self.log.warning(f"AI 返回内容解析失败，原始内容：{content[:200]}")
            return []

        # 校验并映射为选项 ID
        valid_ids = []
        for idx in raw_indices:
            try:
                val = int(idx)
            except (ValueError, TypeError):
                continue
            if 1 <= val <= opt_count:
                valid_ids.append(options[val - 1]["id"])
                self.log.info(f"AI 推荐：{val}. {options[val - 1]['content']}")
            else:
                self.log.warning(
                    f"AI 返回的选项序号 {val} 超出范围 1~{opt_count}，忽略"
                )

        if not valid_ids:
            self.log.warning("AI 返回的答案未能匹配任何有效选项")

        return valid_ids

    @staticmethod
    def _parse_ai_answer(content: str) -> list | None:
        """从 AI 返回内容中提取 answers 序号列表

        :param content: AI 返回的原始文本
        :return: 答案序号列表，解析失败返回 None
        """
        # 去除 markdown 代码块包裹
        json_str = content
        if "```" in json_str:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", json_str, re.DOTALL)
            if match:
                json_str = match.group(1).strip()

        # 方式一：直接解析 JSON
        try:
            data = json.loads(json_str)
            answers = data.get("answers", [])
            if isinstance(answers, list) and answers:
                return answers
        except (json.JSONDecodeError, TypeError):
            pass

        # 方式二：用正则从文本中提取第一个 {"answers": [...]} 结构
        match = re.search(r'"answers"\s*:\s*\[([\d\s,]+)\]', json_str)
        if match:
            try:
                return [int(x) for x in match.group(1).split(",") if x.strip()]
            except (ValueError, TypeError):
                pass

        return None

    # ---- sync answers -------------------------------------------------------

    @staticmethod
    def _is_valid_answers(answers_json: Any) -> bool:
        """至少包含一道能在规范化后保留的题目。"""

        if not isinstance(answers_json, dict):
            return False
        for title, question in answers_json.items():
            if (
                not isinstance(title, str)
                or not title
                or not isinstance(question, dict)
            ):
                continue
            raw_options = question.get("optionList")
            if not isinstance(raw_options, list):
                continue
            if any(
                isinstance(option, dict)
                and isinstance(option.get("content"), str)
                and bool(_exact_text(option["content"]))
                for option in raw_options
            ):
                return True
        return False

    @staticmethod
    def _normalize_answers(answers_json: dict) -> dict:
        """清理字段但不再按模糊键合并题目或并集单选答案。"""

        normalized: dict[str, dict[str, Any]] = {}
        for title, question in answers_json.items():
            if (
                not isinstance(title, str)
                or not title
                or not isinstance(question, dict)
            ):
                continue
            raw_options = question.get("optionList")
            if not isinstance(raw_options, list):
                continue
            options: dict[str, dict[str, Any]] = {}
            for option in raw_options:
                if not isinstance(option, dict):
                    continue
                content = option.get("content")
                if not isinstance(content, str) or not content:
                    continue
                option_key = _exact_text(content)
                if not option_key:
                    continue
                # 同一精确选项由后一次完整记录替换，不对 isCorrect 取并集。
                options[option_key] = {
                    "content": content,
                    "isCorrect": 1 if option.get("isCorrect") == 1 else 2,
                }
            if not options:
                continue
            normalized[title] = {
                "type": question.get("type"),
                "optionList": list(options.values()),
            }
        return normalized

    @staticmethod
    def _extract_history_list(response: dict) -> list[dict] | None:
        """兼容 data 为列表和 data.examHistoryList 两种历史响应。"""

        if not _check_code_ok(response):
            return None
        data = response.get("data")
        if isinstance(data, list):
            histories = data
        elif isinstance(data, dict):
            histories = data.get("examHistoryList")
        else:
            return None
        if not isinstance(histories, list) or not all(
            isinstance(history, dict) for history in histories
        ):
            return None
        return histories

    @classmethod
    def _merge_reviewed_answer(
        cls,
        answers: dict,
        reviewed: dict,
    ) -> bool:
        """以完整复盘结果替换唯一兼容项，不合并正确答案集合。"""

        title = reviewed.get("title")
        if not isinstance(title, str) or not title:
            return False
        normalized = cls._normalize_answers({title: reviewed})
        incoming = normalized.get(title)
        if incoming is None:
            return False

        target_key: str | None = title if title in answers else None
        incoming_signature = _option_signature(incoming)
        if target_key is None:
            exact_candidates = [
                key
                for key, value in answers.items()
                if isinstance(value, dict)
                and _exact_text(key) == _exact_text(title)
                and _option_signature(value) == incoming_signature
            ]
            if len(exact_candidates) == 1:
                target_key = exact_candidates[0]
        if target_key is None:
            fuzzy_candidates = [
                key
                for key, value in answers.items()
                if isinstance(value, dict)
                and clean_text(key) == clean_text(title)
                and _option_signature(value) == incoming_signature
            ]
            if len(fuzzy_candidates) == 1:
                target_key = fuzzy_candidates[0]

        if target_key is not None and target_key != title:
            del answers[target_key]
        answers[title] = incoming
        return True

    def sync_answers(self) -> WorkflowResult:
        """从考试复盘增量同步题库，并通过 AnswerStore 原子提交。"""

        store = self._answer_store()
        remote_baseline: dict[str, Any] = {}
        try:
            answers_json = self._normalize_answers(store.load())
        except AnswerStoreError:
            self.log.info("题库不存在或格式错误，正在下载...")
            try:
                remote = self.api.download_answer()
                downloaded = json.loads(remote)
            except (OSError, TypeError, ValueError, APIResponseError) as exc:
                self.log.error(f"题库下载或解析失败：{exc}")
                return WorkflowResult.failed_result("没有可用题库")
            if not self._is_valid_answers(downloaded):
                self.log.error("下载的题库格式无效，应为非空 JSON 对象")
                return WorkflowResult.failed_result("下载题库格式无效")
            answers_json = self._normalize_answers(downloaded)
            if not self._is_valid_answers(answers_json):
                self.log.error("下载的题库格式无效，应包含至少一道有效题目")
                return WorkflowResult.failed_result("下载题库格式无效")
            remote_baseline = answers_json
            self.log.info("题库已从远程下载，待同步事务中合并保存")

        failures = 0
        reviewed_questions: list[dict] = []
        user_project_ids: list[str] = []

        # 题库同步是辅助阶段：项目/模块列表的网络或协议错误只计入 failures
        # 并降级为 incomplete，不能让整个账号在学习开始前就失败。
        for ended in (2, 1):
            self._raise_if_stopped()
            try:
                response = self.api.list_my_project(ended=ended)
            except PermissionError:
                raise
            except (OSError, APIResponseError) as exc:
                self.log.warning(f"获取项目列表失败（ended={ended}）：{exc}")
                failures += 1
                continue
            projects = response.get("data") if _check_code_ok(response) else None
            if not isinstance(projects, list):
                self.log.error(f"获取项目列表失败：{response}")
                failures += 1
                continue
            for project in projects:
                if isinstance(project, dict) and project.get("userProjectId"):
                    user_project_ids.append(str(project["userProjectId"]))
                else:
                    self.log.warning(f"跳过结构无效的项目：{project}")
                    failures += 1

        completion_failed = False
        try:
            completion = self.api.list_completion()
        except PermissionError:
            raise
        except (OSError, APIResponseError) as exc:
            self.log.warning(f"获取模块完成情况失败：{exc}")
            failures += 1
            completion_failed = True
            completion = {}
        modules = completion.get("data") if _check_code_ok(completion) else None
        if isinstance(modules, list):
            show_lab = any(
                isinstance(item, dict)
                and item.get("module") == "labProject"
                and item.get("showable") == 1
                for item in modules
            )
            if show_lab:
                lab_failed = False
                try:
                    lab_project = self.api.lab_index()
                except PermissionError:
                    raise
                except (OSError, APIResponseError) as exc:
                    self.log.warning(f"获取实验室项目失败：{exc}")
                    failures += 1
                    lab_failed = True
                    lab_project = {}
                lab_data = (
                    lab_project.get("data") if _check_code_ok(lab_project) else None
                )
                current = (
                    lab_data.get("current") if isinstance(lab_data, dict) else None
                )
                if isinstance(current, dict) and current.get("userProjectId"):
                    user_project_ids.append(str(current["userProjectId"]))
                elif not lab_failed:
                    self.log.warning(f"跳过无效实验室项目：{lab_project}")
                    failures += 1
        elif not completion_failed:
            self.log.warning(f"获取模块完成情况失败：{completion}")
            failures += 1

        # 去重但保留服务端顺序。
        user_project_ids = list(dict.fromkeys(user_project_ids))
        for user_project_id in user_project_ids:
            self._raise_if_stopped()
            try:
                plan_response = self.api.exam_list_plan(user_project_id)
            except PermissionError:
                raise
            except (OSError, APIResponseError) as exc:
                self.log.warning(f"项目 {user_project_id} 考试计划同步失败：{exc}")
                failures += 1
                continue
            plans = plan_response.get("data") if _check_code_ok(plan_response) else None
            if not isinstance(plans, list):
                self.log.warning(f"项目考试计划响应无效：{plan_response}")
                failures += 1
                continue
            for plan in plans:
                if not isinstance(plan, dict) or not all(
                    key in plan for key in ("examPlanId", "examType")
                ):
                    self.log.warning(f"跳过无效考试计划：{plan}")
                    failures += 1
                    continue
                try:
                    history_response = self.api.exam_list_history(
                        plan["examPlanId"],
                        plan["examType"],
                    )
                except PermissionError:
                    raise
                except (OSError, APIResponseError) as exc:
                    self.log.warning(f"考试历史同步失败：{exc}")
                    failures += 1
                    continue
                histories = self._extract_history_list(history_response)
                if histories is None:
                    self.log.warning(f"考试历史响应无效：{history_response}")
                    failures += 1
                    continue
                for history in histories:
                    history_id = history.get("examId") or history.get("id")
                    if not history_id:
                        self.log.warning(f"跳过缺少 examId/id 的历史记录：{history}")
                        failures += 1
                        continue
                    try:
                        review = self.api.exam_review_paper(
                            str(history_id),
                            int(history.get("isRetake", 2)),
                        )
                    except PermissionError:
                        raise
                    except (OSError, ValueError, APIResponseError) as exc:
                        self.log.warning(f"考试复盘同步失败：{exc}")
                        failures += 1
                        continue
                    review_data = review.get("data") if _check_code_ok(review) else None
                    questions = (
                        review_data.get("questions")
                        if isinstance(review_data, dict)
                        else None
                    )
                    if not isinstance(questions, list):
                        self.log.warning(f"考试复盘响应无效：{review}")
                        failures += 1
                        continue
                    for question in questions:
                        if isinstance(question, dict) and self._normalize_answers(
                            {str(question.get("title", "")): question}
                        ):
                            reviewed_questions.append(question)
                        else:
                            self.log.warning(f"跳过无效复盘题目：{question}")
                            failures += 1

        def merge_latest(current: dict[str, Any]) -> dict[str, Any]:
            merged = self._normalize_answers(current)
            # 远程题库只是缺失本地题库时的基线。将它放入最终 update
            # 事务内合并，避免锁外的独立 write 覆盖其他进程刚写入的答案。
            for title, question in self._normalize_answers(remote_baseline).items():
                merged.setdefault(title, question)
            for reviewed in reviewed_questions:
                if not self._merge_reviewed_answer(merged, reviewed):
                    self.log.warning(f"跳过无效复盘题目：{reviewed}")
            return self._normalize_answers(merged)

        try:
            merged = store.update(merge_latest, default=answers_json)
        except (OSError, AnswerStoreError) as exc:
            self.log.error(f"题库原子写入失败：{exc}")
            return WorkflowResult.failed_result("题库写入失败")

        self.log.success(
            f"题库同步完成：复盘 {len(reviewed_questions)} 题，现有 {len(merged)} 题"
        )
        if failures:
            return WorkflowResult.incomplete(
                "题库已保存，但部分项目同步失败",
                completed=len(reviewed_questions),
                failed=failures,
            )
        return WorkflowResult.success(
            "题库同步完成",
            completed=len(reviewed_questions),
        )
