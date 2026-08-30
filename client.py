import json
import os
import re
import sys
import threading
import time
import webbrowser
from random import randint
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from uuid import uuid4

from loguru import logger

from api import WeBanAPI
from captcha import CaptchaHandler, LoginCaptchaSolver, is_non_interactive

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
root_answer_path = os.path.join(_data_dir, "answer.json") if _data_dir else os.path.join(base_path, "answer.json")
bundle_answer_path = os.path.join(bundle_path, "answer", "answer.json")


def clean_text(text):
    """只保留字母、数字和汉字，自动去除所有符号和空格

    去除标点/空格后做模糊匹配，确保如「以下说法正确的是（）」能命中
    题库中「以下说法正确的是」。
    :param text: 原始文本
    :return: 仅含字母、数字和汉字的文本
    """
    return re.sub(r"[^\w一-龥]", "", text)


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


def read_first_existing(paths: list[str]) -> str | None:
    """按序读取第一个存在的本地文件内容（模板/题库的打包版兜底共用）。

    模板（config.example.toml）与题库（answer.json）的下载都会先尝试
    jsDelivr 远程源；失败时回退到本地候选文件（打包内置 _MEIPASS 或
    可执行文件旁），两者共用本函数读取兜底内容。
    :param paths: 候选路径，按优先级排列（如 bundle 内置优先）
    :return: 文件文本；全部不存在或不可读返回 None
    """
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return None


def _check_code_ok(data: dict, allow_200: bool = True) -> bool:
    """接口业务码是否成功（对齐官方 checkCode）

    主站请求封装（app.js request）：Boolean(data) && Number(code)∈{0,1,200}；
    完课 JSONP（sdk.js finishWxCourse）：Boolean(data) && Number(code)∈{0,1}，
    传 allow_200=False 对齐。注意 Number(null)===0，code 为 null 时官方同样视为成功。
    :param data: 接口响应 dict
    :param allow_200: 是否允许 code=200（主站接口 True，完课 JSONP False）
    :return: 业务成功返回 True
    """
    if not data:
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
        """
        self.log = log
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
        if user and all([user.get("userId"), user.get("token")]):
            self.api = WeBanAPI(user=user, debug=debug, log=log)
        elif all([self.tenant_name, account, password]):
            self.api = WeBanAPI(
                account=account, password=password, debug=debug, log=log
            )
        else:
            self.api = WeBanAPI(debug=debug, log=log)
        self.tenant_code = self.get_tenant_code()
        if self.tenant_code:
            self.api.set_tenant_code(self.tenant_code)
        else:
            raise ValueError("学校代码获取失败，请检查学校全称是否正确")
        self._captcha_handler = None

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
            try:
                res = fn()
                if not _check_code_ok(res):
                    self.log.debug(f"首页{name}返回异常：{res}")
            except PermissionError:
                raise  # Token 失效（被顶号等），立即终止该账号
            except OSError as e:  # 网络异常（DNS/连接/SSL）忽略，不影响主流程
                self.log.debug(f"首页{name}请求失败（网络异常）：{e}")

        # 必读公告：官方弹窗逐条展示，阅读完成后逐条确认（与浏览器行为一致）
        try:
            must = self.api.notice_list_must(batch_code)
            notices = must.get("data") or []
            if not _check_code_ok(must) or not isinstance(notices, list):
                self.log.debug(f"必读公告返回异常：{must}")
                notices = []
            for n in notices:
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
                if ntype not in (3, 4, 5) and isinstance(min_read, (int, float)) and min_read > 0:
                    time.sleep(min(min_read, 300))
                try:
                    self.api.view_must_notice(nid)
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:
                    self.log.debug(f"确认必读公告失败（网络异常）：{e}")
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"必读公告流程失败（网络异常）：{e}")

        # 问卷：官方在必读公告确认后检查待答问卷，仅拉取并提示，不自动作答
        try:
            q = self.api.questionnaire_list_by_user_id()
            qlist = q.get("data") if isinstance(q.get("data"), list) else []
            if q.get("code", "-1") == "0" and qlist:
                self.log.info(f"存在 {len(qlist)} 个待答问卷（官方会弹窗提示，请前往网页完成）")
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"问卷列表请求失败（网络异常）：{e}")

    def _prompt(self, message: str) -> str:
        """线程安全的 input 封装，多线程下避免 input 输出交错
        :param message: 提示信息
        :return: 去除首尾空白的用户输入
        """
        with self._stdin_lock:
            return input(message).strip()

    def _load_answers_json(self, warn_on_fail: bool = False) -> dict:
        """加载题库，返回 {clean_text(题目): {clean_text(正确选项), ...}}

        :param warn_on_fail: True 时加载失败只警告不抛异常（学习模式容错），
            False 时抛出异常（考试模式必须要有题库）
        :return: 清洗后的题目标题 → 正确答案内容集合的映射
        """
        answers: dict = {}
        # 优先级: 根目录 answer.json > answer/answer.json > 打包内置
        if os.path.exists(root_answer_path):
            load_path = root_answer_path
        elif os.path.exists(answer_path):
            load_path = answer_path
        else:
            load_path = bundle_answer_path
        try:
            with open(load_path, encoding="utf-8") as f:
                for title, options in json.load(f).items():
                    title = clean_text(title)
                    answers.setdefault(title, set()).update(
                        clean_text(a["content"])
                        for a in options.get("optionList", [])
                        if a["isCorrect"] == 1
                    )
        except Exception:
            if warn_on_fail:
                self.log.warning("题库加载失败，课后习题将随机作答")
            else:
                raise
        return answers

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
        url = self.api.get_course_url(course["resourceId"], task["userProjectId"])[
            "data"
        ]
        url += f"&userProjectId={task['userProjectId']}"
        url += f"&userId={self.api.user['userId']}"
        url += f"&courseId={course['resourceId']}"
        url += f"&userName={self.api.user.get('userName', self.api.user.get('realName', ''))}"
        link = course.get("praiseNum", "")
        url += (
            f"&projectType=special&projectId=undefined&protocol=true&link={link}"
            "&weiban=weiban&certificateId=undefined&userActivityState=undefined"
            "&step=undefined&index=undefined&viewStep=undefined"
        )
        return url

    # ---- tenant / progress --------------------------------------------------

    def get_tenant_code(self) -> str:
        """获取学校代码
        :return: 学校代码（tenant_code），找不到返回空字符串
        """
        if not self.tenant_name:
            self.log.error("学校全称不能为空")
            return ""
        tenant_list = self.api.get_tenant_list_with_letter()
        if tenant_list.get("code", -1) == "0":
            self.log.info("获取学校列表成功")
        tenant_names = []
        maybe_names = []
        for item in tenant_list.get("data", []):
            for entry in item.get("list", []):
                name = entry.get("name", "")
                tenant_names.append(name)
                if self.tenant_name == name.strip():
                    self.log.success(f"找到学校代码: {entry['code']}")
                    return entry["code"]
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
        data = progress.get("data", {})
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
        if self.api.user.get("userId"):
            return self.api.user
        retry_limit = 10
        # 前 10 次 OCR 自动识别，后 3 次手动输入
        for i in range(retry_limit + 3):
            if i > 0:
                self.log.warning(f"登录失败，正在重试 {i}/{retry_limit + 2} 次")
            verify_time = self.api.get_timestamp(13, 0)
            verify_image = self.api.rand_letter_image(verify_time)
            if i < retry_limit:
                verify_code = LoginCaptchaSolver.recognize(verify_image, self.log)
                if not verify_code:
                    continue
            elif is_non_interactive():
                # 无交互模式：不阻塞等待手动输入，直接判定失败
                self.log.error(
                    "验证码 OCR 连续失败且处于无交互模式，无法手动输入验证码，"
                    "登录失败（可在宿主机浏览器配合 CDP 或稍后重试）"
                )
                break
            else:
                account_id = (
                    self.api.account or self.api.user.get("userId") or "unknown"
                )
                captcha_dir = os.path.join(base_path, "logs", account_id)
                os.makedirs(captcha_dir, exist_ok=True)
                captcha_path = os.path.join(captcha_dir, "verify_code.png")
                with open(captcha_path, "wb") as f:
                    f.write(verify_image)
                webbrowser.open(f"file://{captcha_path}")
                verify_code = self._prompt(
                    f"[{account_id}] 请在 {captcha_path} 查看验证码图片输入验证码："
                )
                try:
                    os.remove(captcha_path)
                except OSError:
                    pass
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

    def _get_project_list(self) -> list[dict]:
        """获取账号全部进行中的项目列表（含实验室课程合并）"""
        my_project = self.api.list_my_project()
        if not _check_code_ok(my_project):
            self.log.error(f"获取任务列表失败：{my_project}")
            return []
        my_project = my_project.get("data", [])

        completion = self.api.list_completion()
        if not _check_code_ok(completion):
            self.log.error(f"获取模块完成情况失败：{completion}")
        else:
            showable_modules = [
                d["module"] for d in completion.get("data", []) if d["showable"] == 1
            ]
            if "labProject" in showable_modules:
                self.log.info("加载实验室课程")
                lab_project = self.api.lab_index()
                if not _check_code_ok(lab_project):
                    self.log.error(f"获取实验室课程失败：{lab_project}")
                current = lab_project.get("data", {}).get("current") or {}
                if current:
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
    ) -> None:
        """按项目交替执行：每个项目先完成课程学习，再完成考试，然后
        切换到下一个项目（用户要求的顺序：项目 A 学习+考试 → 项目 B 学习+考试）。

        考试模式/学习模式为 "false" 时对应阶段整体跳过。
        """
        study = study_mode != "false"
        exam = exam_mode != "false"
        if not study and not exam:
            self.log.info("学习与考试均未开启，跳过")
            return

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
        if not projects:
            self.log.warning("当前没有进行中的项目。")
            return

        for project in projects:
            project_name = project.get("projectName", "未知项目")
            user_project_id = project.get("userProjectId", "")
            self.log.info(f"===== 开始处理项目：{project_name} =====")
            if not user_project_id:
                self.log.warning(f"{project_name}：缺少 userProjectId，跳过")
                continue
            if study:
                self.run_study(study_time, study_mode, only_project=project)
            if exam:
                self.run_exam(
                    exam_mode=exam_mode,
                    random_answer=random_answer,
                    exam_question_time=exam_question_time,
                    exam_submit_match_rate=exam_submit_match_rate,
                    only_project=project,
                )

    # ---- study --------------------------------------------------------------

    def run_study(
        self,
        study_time: str | int,
        study_mode: str = "true",
        only_project: dict | None = None,
    ) -> None:
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
        if not my_project:
            self.log.warning("当前没有进行中的学习项目。")

        for task in my_project:
            project_prefix = task["projectName"]
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(task)
            if not startable:
                self.log.warning(
                    f"{project_prefix}：{notice or '项目尚未开放，暂不可学习'}，跳过"
                )
                continue
            self.log.info(f"开始处理任务：{project_prefix}")
            # 对齐官方 H5：进入学习项目页即发 initIndex（项目详情初始化），
            # 不依赖课程是否加载 apicenext.js
            try:
                self.api.init_index(task["userProjectId"])
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except OSError as e:  # 网络异常不阻断学习
                self.log.warning(f"初始化学习索引失败（网络异常）：{e}")
            progress = self.get_progress(task["userProjectId"], project_prefix)
            progress_data = (
                progress.get("data", {}) if _check_code_ok(progress) else {}
            )

            choose_types = [
                (3, "必修课", "requiredNum", "requiredFinishedNum"),
                (1, "推送课", "pushNum", "pushFinishedNum"),
                (2, "自选课", "optionalNum", "optionalFinishedNum"),
            ]
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
                # （listFlatCourse.do）。取不到 mode 时按折叠路径（原行为）兜底。
                try:
                    simple = self.api.get_project_simple(task["userProjectId"])
                    project_mode = int(
                        simple.get("data", {}).get("projectMode", 1) or 1
                    )
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:
                    self.log.debug(f"获取项目模式失败（网络异常）：{e}")
                    project_mode = 1
                if project_mode != 1:
                    self._study_flat_courses(
                        task,
                        choose_type,
                        project_prefix,
                        answers_json,
                        force_restudy,
                    )
                    continue

                try:
                    categories = self.api.list_category(
                        task["userProjectId"], choose_type[0]
                    )
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:  # 网络异常（DNS/连接/SSL）跳过本分类，不中断整个账号
                    self.log.error(f"获取 {choose_type[1]} 分类失败（网络异常）：{e}")
                    continue
                if not _check_code_ok(categories):
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    continue

                for category in categories.get("data", []):
                    category_prefix = (
                        f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    )
                    if (
                        not force_restudy
                        and category["finishedNum"] >= category["totalNum"]
                    ):
                        continue

                    courses = self.api.list_course(
                        task["userProjectId"], category["categoryCode"], choose_type[0]
                    )
                    for course in courses.get("data", []):
                        if not force_restudy and int(course.get("finished", 0)) == 1:
                            continue
                        if not self._learn_course(
                            course,
                            task,
                            category_prefix,
                            project_prefix,
                            answers_json,
                            force_restudy,
                        ):
                            return

            self.log.success(f"{project_prefix} 课程学习完成")
            self._check_project_course_done(task, project_prefix)

    def _check_project_course_done(
        self, task: dict, project_prefix: str
    ) -> None:
        """校验项目各类型课程完成数是否达到需求数，不足时告警。

        服务端进度更新可能有延迟，因此只告警不重试；覆盖折叠/扁平两种
        列表路径学完后的盲区（如扁平分页提前结束导致漏学）。
        """
        try:
            progress = self.get_progress(
                task["userProjectId"], project_prefix, output=False
            )
            if not _check_code_ok(progress):
                return
            data = progress.get("data", {})
            for _, label, need_key, finished_key in [
                (3, "必修课", "requiredNum", "requiredFinishedNum"),
                (1, "推送课", "pushNum", "pushFinishedNum"),
                (2, "自选课", "optionalNum", "optionalFinishedNum"),
            ]:
                need = int(data.get(need_key, 0) or 0)
                finished = int(data.get(finished_key, 0) or 0)
                if need > 0 and finished < need:
                    self.log.warning(
                        f"{project_prefix} {label}完成 {finished}/{need}，"
                        f"未达到需求数，请检查是否漏学"
                    )
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"校验学习完成进度失败（网络异常）：{e}")

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

        :return: True 可继续下一门；False 表示账号异常/锁定，应停止本账号
        """
        course_prefix = f"{category_prefix}/{course['resourceName']}"
        try:
            progress_before = self.get_progress(
                task["userProjectId"], project_prefix, output=False
            )
            finished_before = 0
            if progress_before.get("code", -1) == "0":
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
                self.log.error("检测到行为异常或账号锁定，已停止本账号后续学习")
                return False
            progress_after = self.get_progress(task["userProjectId"], project_prefix)
            if progress_after.get("code", -1) == "0":
                d = progress_after["data"]
                finished_after = (
                    d["requiredFinishedNum"]
                    + d["pushFinishedNum"]
                    + d["optionalFinishedNum"]
                )
                if finished_after <= finished_before:
                    self.log.warning(
                        f"{course_prefix}：完课成功但进度未更新，请手动检查"
                    )
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            # 网络异常（DNS/连接/SSL）跳过本门课程，不中断整个账号；
            # 未完成的课程下次运行会自动重学
            self.log.warning(f"{course_prefix}：网络异常，跳过本门课程（{e}）")
        return True

    def _study_flat_courses(
        self,
        task: dict,
        choose_type: tuple,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> None:
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
            except OSError as e:  # 网络异常跳过本类型，不中断整个账号
                self.log.error(f"获取 {label} 课程失败（网络异常）：{e}")
                return
            if not _check_code_ok(res):
                self.log.error(f"获取 {label} 课程失败：{res}")
                return
            data = res.get("data") or {}
            courses_all.extend(data.get("paginateData") or [])
            total_pages = int(data.get("totalPages", 1) or 1)
            # 官方结束条件：totalPages <= pageNo
            if total_pages <= page_no:
                break
            page_no += 1

        for course in courses_all:
            if not force_restudy and int(course.get("finished", 0)) == 1:
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
                return

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
        return ("行为存在异常" in msg or "Account locked" in raw or "Account locked" in msg)

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

        :return: True 可继续下一门；False 表示账号异常/锁定，应停止本账号
        """
        course_prefix = f"{category_prefix}/{course['resourceName']}"

        if not force_restudy and int(course.get("finished", 0)) == 1:
            return True

        self.log.info(f"学习： {course_prefix}")
        # 官方 navToDetail 先 study.do，成功才进入课程页/取课程 URL；
        # 失败（含课程未开始/未开放）toast 服务端 msg 并跳过本门课
        study_res = self.api.study(course["resourceId"], task["userProjectId"])
        if not _check_code_ok(study_res):
            msg = study_res.get("message") or study_res.get("msg") or "课程暂时无法学习"
            self.log.warning(f"{course_prefix}：{msg}，跳过")
            return True
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
                self.log.warning(f"{course_prefix}：未获取到学习记录（userCourseId 为空），跳过")
                return True

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
        # jupiter 学习轨迹上报始终带本次会话 uuid（浏览器每次学习都上报）
        apinext_no = str(uuid4())
        # sdk.js 仅在 apicenext.js 定义了全局 uuid 时才带 uniqueNo；
        # 无 apinext 的课传 uniqueNo 会被判行为异常 (10018)
        unique_no = str(uuid4()) if uses_apinext else None

        # 1. jupiter finish=2 翻页轨迹：官方仅在加载 apicenext.js 的课程里上报
        # （页面 item.js 调 callApinext）；非 apicnext 课程默认不发，除非配置
        # jupiter_fallback=true（个别学校要求全部微课都有轨迹）
        trace_enabled = uses_apinext or self.jupiter_fallback
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
                unique_no=apinext_no,
                finish=2,
            )

        # 2. 获取并回答题目（翻页后题目才可用）
        question_data = self.api.list_question(course["resourceId"])
        if question_data and question_data.get("code") == "0":
            data = question_data.get("data", {})
            for qlist, label, save_func in [
                (
                    data.get("viewpointQuestionList", []),
                    "观点题",
                    self.api.save_question,
                ),
                (
                    data.get("examQuestionList", []),
                    "课后习题",
                    self.api.save_exam_question,
                ),
            ]:
                if qlist:
                    self.log.info(f"  {label} {len(qlist)} 道")
                    for i, q in enumerate(qlist):
                        # 无论题库命中还是 fallback 都已提交答案，
                        # 对用户而言课程题目作答流程已完成
                        self._answer_question(
                            q,
                            answers_json,
                            course["resourceId"],
                            save_func,
                            source_str,
                        )
                        self.log.info(f"    {i + 1}/{len(qlist)} 已完成")
                        time.sleep(0.5)
        elif question_data:
            self.log.info(f"  list_question: code={question_data.get('code')}")
        if item_info.get("has_exam") and not question_data.get("data", {}).get(
            "examQuestionList"
        ):
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
                    time.sleep(min(30, remaining))
                    remaining = max(0, deadline - time.monotonic())
                    if remaining > 0:
                        self.log.info(f"视频剩余 {self._format_duration(remaining)}")
            else:
                time.sleep(remaining)

        # 4. jupiter finish=1 完成标记（提交前上报学习完成，与翻页轨迹同条件）
        if total_step and trace_enabled:
            self.handle_apinext(
                course["userCourseId"],
                course["resourceId"],
                task["userProjectId"],
                nonstr_map,
                total_step,
                unique_no=apinext_no,
                finish=1,
            )
            time.sleep(2)

        # 5. 完课
        res = self._finish_course(course, task, query, course_url, unique_no)
        # 完课走 JSONP（sdk.js finishWxCourse）：checkCode 只认 code∈{0,1}
        if not _check_code_ok(res, allow_200=False):
            self.log.error(f"{course_prefix} 完成失败：{res}")
            return not self._is_account_blocked(res)

        self.log.success(f"{course_prefix} 完成")
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
    ):
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

        if only_project is not None:
            projects = [only_project]
        else:
            projects = self._get_project_list()
        if not projects:
            self.log.warning("当前没有进行中的项目可考试。")

        for project in projects:
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(project)
            if not startable:
                self.log.warning(
                    f"{project['projectName']}：{notice or '项目尚未开放，暂不可考试'}，跳过"
                )
                continue
            self.log.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]

            exam_plans = self.api.exam_list_plan(user_project_id)
            if not _check_code_ok(exam_plans):
                self.log.error(f"获取考试计划失败：{exam_plans}")
                return
            exam_plans = exam_plans["data"]

            for plan in exam_plans:
                plan_name = f"{project['projectName']}/{plan['examPlanName']}"
                exam_odd_num = plan.get("examOddNum", 0)
                exam_finish_num = plan.get("examFinishNum", 0)
                exam_score = plan.get("examScore", 0)
                pass_score = plan.get("passScore", 0)

                # ── 已考过的考试，显示历史成绩 ──
                full_score = 100
                if exam_finish_num > 0:
                    try:
                        pp = self.api.exam_prepare_paper(plan["id"])
                        full_score = pp.get("data", {}).get("paperScore", 100)
                    except PermissionError:
                        raise  # Token 失效，立即终止该账号
                    except OSError:
                        pass
                    self.log.info(
                        f"{plan_name} 已考过 {exam_finish_num}/{exam_odd_num} 次，"
                        f"最高 {exam_score}/{full_score}（及格线 {pass_score}）"
                    )
                elif exam_odd_num > 0:
                    self.log.info(
                        f"{plan_name} 未考试，可考 {exam_odd_num} 次"
                    )

                # ── 根据 exam_mode 判断是否跳过 ──
                if exam_odd_num <= 0:
                    self.log.info(f"{plan_name} 无剩余考试机会，跳过")
                    continue

                if (
                    exam_mode == "true"
                    and exam_finish_num > 0
                    and exam_score >= pass_score
                ):
                    self.log.info(f"{plan_name} 已及格 ({exam_score}分 >= {pass_score}分)，跳过")
                    continue

                if (
                    exam_mode == "perfect"
                    and exam_finish_num > 0
                    and exam_score >= full_score
                ):
                    self.log.info(f"{plan_name} 已满分 ({exam_score}分)，跳过")
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
                    self.log.info(f"{plan_name} 已完成 {exam_finish_num} 次，{plan_name} 继续考试以争取更好成绩")

                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]

                before_paper = self.api.exam_before_paper(plan["id"])
                if not _check_code_ok(before_paper):
                    self.log.error(
                        f"考试项目 {plan_name} 获取考试记录失败：{before_paper}"
                    )

                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if not _check_code_ok(prepare_paper):
                    if prepare_paper.get("detailCode") == "14":
                        self.log.warning(
                            f"{plan_name} 课程学习未完成，无法考试；"
                            f"请先完成该项目的课程学习"
                        )
                    else:
                        self.log.error(f"获取考试信息失败：{prepare_paper}")
                    continue
                prepare_paper = prepare_paper["data"]
                question_num = prepare_paper["questionNum"]
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
                        continue
                    self.log.success("无感验证码校验通过")
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except Exception as e:  # noqa: BLE001 -- 浏览器自动化可能抛任意异常
                    self.log.error(f"无感验证码处理异常: {e}")
                    continue

                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if not _check_code_ok(exam_paper):
                    self.log.error(f"获取考试题目失败：{exam_paper}")
                    if exam_paper.get("detailCode") == "10018":
                        self.log.warning(
                            f"考试项目 {plan_name} 需要手动处理，"
                            f"请在网站上开启一次考试后重试"
                        )
                    continue

                exam_paper = exam_paper.get("data", {})
                question_list = exam_paper.get("questionList", [])
                have_answer, no_answer = [], []
                for question in question_list:
                    target = (
                        have_answer
                        if clean_text(question["title"]) in answers_json
                        else no_answer
                    )
                    target.append(question)

                match_rate = (
                    len(have_answer) / len(question_list) * 100 if question_list else 0
                )
                self.log.info(
                    f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，"
                    f"无答案的题目数：{len(no_answer)}，题库匹配率：{match_rate:.1f}%"
                )

                # perfect 模式：匹配率不足且 random_answer=False 时警告
                if exam_mode == "perfect" and match_rate < 100 and not random_answer:
                    self.log.warning(
                        f"题库匹配率 {match_rate:.1f}% 不足 100%，"
                        f"perfect 模式下手动作答可能存在风险"
                    )

                # 检查提交匹配率
                if match_rate < exam_submit_match_rate and not random_answer:
                    self.log.error(
                        f"题库匹配率 {match_rate:.1f}% 低于阈值 {exam_submit_match_rate}%，"
                        f"且 random_answer=false，放弃交卷"
                    )
                    continue

                # ── 处理无答案题目 ──
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
                        time.sleep(use_time)
                    elif random_answer or is_non_interactive():
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
                        time.sleep(use_time)
                    else:
                        # 手动输入
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 题目不在题库中，请手动选择答案"
                        )
                        print(f"题目类型：{type_label}，题目标题：{question['title']}")
                        for j, opt in enumerate(question["optionList"]):
                            print(f"{j + 1}. {opt['content']}")

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

                    self.log.info("正在提交当前答案")
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                # ── 题库作答 ──
                if have_answer:
                    self.log.info(f"开始答题库中的题目，共 {len(have_answer)} 道题目")
                for i, question in enumerate(have_answer):
                    self.log.info(
                        f"[{i + 1}/{len(have_answer)}] 题目在题库中，开始答题"
                    )
                    self.log.info(
                        f"题目类型：{question['typeLabel']}，"
                        f"题目标题：{question['title']}"
                    )
                    answers = answers_json[clean_text(question["title"])]
                    answers_ids = [
                        opt["id"]
                        for opt in question["optionList"]
                        if clean_text(opt["content"]) in answers
                    ]
                    use_time = question_base_time + randint(0, question_random_upper)
                    self.log.info(
                        f"等待 {self._format_duration(use_time)}，模拟答题中..."
                    )
                    time.sleep(use_time)
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                self.log.info("完成考试，正在提交试卷...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if not _check_code_ok(submit_res):
                    raise RuntimeError(f"提交试卷失败，请重新考试：{submit_res}")
                self.log.success(
                    f"试卷提交成功，考试完成，成绩：{submit_res['data']['score']} 分"
                )
                self._update_exam_eta(time.time() - plan_start_ts)

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
                clean = re.sub(
                    r"<!--.*?-->", "", video_block.group(1), flags=re.DOTALL
                )
                video_match = re.search(
                    r"<source\b[^>]*\bsrc=[\"']([^\"']+)[\"']", clean
                )
            if not video_match:
                video_match = re.search(
                    r"<video\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html
                )
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
                                size = int.from_bytes(
                                    buf[pos : pos + 4], "big"
                                )
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
                                            if (
                                                version == 0
                                                and q + 28 <= len(buf)
                                            ):
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
                                                video_duration = (
                                                    duration / timescale
                                                )
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
                result["nonstr_map"] = _extract_map(content)
                result["has_exam"] = result["has_exam"] or _check_exam(content)
                if result["nonstr_map"] or result["has_exam"]:
                    break

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
            """单步发送，网络异常最多重试 3 次（含首次）。

            WeBanAPI 的 session 层已有 HTTPAdapter 全局重试（连接类错误/429/5xx
            自动退避），这里对重试耗尽后剩余的网络异常再兜底 2 次，避免偶发
            抖动导致翻页轨迹断步。
            """
            for attempt in range(1, 4):
                try:
                    resp = self.api.apinext(
                        user_course_id,
                        course_id,
                        user_project_id,
                        step=step,
                        finish=finish,
                        nonstr=nonstr,
                        unique_no=unique_no,
                    )
                    if not resp.get("success"):
                        self.log.warning(f"apinext [{label}] 返回异常：{resp}")
                    else:
                        self.log.info(f"apinext [{label}] finish={finish} 已发送")
                    return
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:
                    if attempt < 3:
                        self.log.warning(
                            f"apinext [{label}] 网络异常，重试 {attempt}/2：{e}"
                        )
                        time.sleep(attempt)
                    else:
                        self.log.warning(f"apinext [{label}] 失败：{e}")

        if finish == 2:
            self.log.info(f"apinext 发送中间步骤，共 {total_step} 步")
            for step in range(1, total_step + 1):
                if step_delay:
                    time.sleep(step_delay)
                # nonstr_map 的 key 对应 finish=2 的 step，完成步 (finish=1) 不在 map 中
                _send_step(step, 2, nonstr_map.get(step, ""), f"{step}/{total_step}")
        else:
            if step_delay:
                time.sleep(step_delay)
            # finish=1 的 step 需要偏移 total_step + 1（nonstr_map 不含此步）
            _send_step(
                total_step + 1, 1, "", f"完成标记 step={total_step + 1}"
            )
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
        title = clean_text(question.get("title", ""))
        option_list = question.get("optionList", [])
        if not option_list:
            return False

        # 题库命中，直接提交正确答案
        if title in answers_json:
            answer_ids = [
                opt["id"]
                for opt in option_list
                if clean_text(opt["content"]) in answers_json[title]
            ]
            if answer_ids:
                save_func(course_id, question["id"], json.dumps(answer_ids), source)
                return True

        # 题库未命中：先提交第一个选项，从响应中提取正确 answerLabel
        res = save_func(
            course_id,
            question["id"],
            json.dumps([option_list[0]["id"]]),
            source,
        )
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
            save_func(course_id, question["id"], json.dumps(answer_ids), source)
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
            "temperature": 0
        }

        # 用户自定义 body：兼容单行 JSON 字符串（config.toml）与 dict（TOML 内联表）
        user_payload = self.ai_config.get("custom_body") or {}
        if isinstance(user_payload, str):
            try:
                user_payload = json.loads(user_payload)
            except json.JSONDecodeError as e:
                self.log.error(f"AI 搜题 - [ai].custom_body 不是合法 JSON（{e}），已忽略")
                user_payload = {}
        if not isinstance(user_payload, dict):
            self.log.error("AI 搜题 - [ai].custom_body 必须是 JSON 对象（{...}），已忽略")
            user_payload = {}

        reserved_keys = ("model", "messages", "temperature") # 禁止用户设置这些body，因为这会和系统内置的产生冲突

        url = f"{base_url.rstrip('/')}/chat/completions"
        content = None
        resp = None

        for k,v in user_payload.items():
            if k in reserved_keys:
                self.log.warning(f"AI 搜题 - {k} 为系统保留字段，已忽略")
                continue
            payload[k] = v

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
                api_detail = (
                    f"，响应体：{resp.text[:300]}" if resp is not None else ""
                )
                if attempt < max_retries:
                    wait = attempt * 2
                    self.log.warning(
                        f"AI 搜题第 {attempt} 次请求失败，{wait}s 后重试：{e}{api_detail}"
                    )
                    time.sleep(wait)
                else:
                    self.log.error(
                        f"AI 搜题请求失败（已重试 {max_retries} 次）：{e}{api_detail}"
                    )
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
        """校验题库是否为有效字典且非空"""
        return isinstance(answers_json, dict) and bool(answers_json)

    @staticmethod
    def _normalize_answers(answers_json: dict) -> dict:
        """合并 clean_text 后相同的题目与选项条目，保留原始标点。

        标题与选项文本取各组内最长（标点最完整）的原文，选项标记取并集
        （任一变体标 1 则保留 1），合并前后经 clean_text 匹配的运行时
        行为不变。
        """
        merged: dict = {}
        for title, question in answers_json.items():
            clean_title = clean_text(title)
            entry = merged.get(clean_title)
            if entry is None:
                entry = merged[clean_title] = {
                    "title": title,
                    "type": question.get("type"),
                    "options": {},
                }
            elif len(title) > len(entry["title"]):
                entry["title"] = title
            for option in question.get("optionList", []):
                content = clean_text(option["content"])
                old = entry["options"].get(content)
                if old is None:
                    entry["options"][content] = {
                        "content": option["content"],
                        "isCorrect": option["isCorrect"],
                    }
                else:
                    if len(option["content"]) > len(old["content"]):
                        old["content"] = option["content"]
                    if option["isCorrect"] == 1:
                        old["isCorrect"] = 1
        return {
            entry["title"]: {
                "type": entry["type"],
                "optionList": list(entry["options"].values()),
            }
            for entry in merged.values()
        }

    def sync_answers(self) -> None:
        """同步答案
        :return: 无返回值
        """
        os.makedirs(answer_dir, exist_ok=True)
        # 按优先级查找已有题库: 根目录 > answer/ > 打包内置
        existing_path: str | None = None
        for p in [root_answer_path, answer_path, bundle_answer_path]:
            if os.path.exists(p):
                existing_path = p
                break
        need_download = existing_path is None

        answers_json: dict | None = None
        if not need_download:
            assert existing_path is not None
            try:
                with open(existing_path, encoding="utf-8") as f:
                    answers_json = json.load(f)
                if not self._is_valid_answers(answers_json):
                    need_download = True
            except (json.JSONDecodeError, OSError):
                need_download = True

        if need_download:
            # 与模板下载同构：远程 jsDelivr 失败后回退打包内置/本地文件兜底
            self.log.info("题库不存在或格式错误，正在下载...")
            try:
                remote = self.api.download_answer()
                self.log.success("题库已从远程下载")
            except Exception as e:  # noqa: BLE001 -- 网络失败不应中断整个账号流程
                self.log.warning(f"题库下载失败：{e}，回退本地内置题库")
                remote = read_first_existing(
                    [bundle_answer_path, answer_path, root_answer_path]
                )
                if remote is None:
                    self.log.warning("本地无可用题库，本次跳过题库同步")
                    return
            with open(answer_path, "w", encoding="utf-8") as f:
                f.write(remote)
            try:
                with open(answer_path, encoding="utf-8") as f:
                    answers_json = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self.log.error(f"读取题库失败：{e}")
                return
            if not self._is_valid_answers(answers_json):
                self.log.error("下载的题库格式无效，应为非空 JSON 对象")
                return

        if answers_json is None:
            self.log.error("题库加载失败")
            return

        # 合并变体：clean_text 相同的题目/选项仅保留一条，保留原始标点
        answers_json = self._normalize_answers(answers_json)
        # clean 标题 → 原始标题索引，服务器标题按 clean 语义匹配
        key_by_clean = {clean_text(k): k for k in answers_json}

        user_project_ids = [
            p["userProjectId"] for p in self.api.list_my_project().get("data", [])
        ]
        user_project_ids.extend(
            p["userProjectId"]
            for p in self.api.list_my_project(ended=1).get("data", [])
        )
        completion = self.api.list_completion()
        if not _check_code_ok(completion):
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [
            d["module"] for d in completion.get("data", []) if d["showable"] == 1
        ]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            if not _check_code_ok(lab_project):
                self.log.error(f"获取实验室课程失败：{lab_project}")
            user_project_ids.append(
                lab_project.get("data", {}).get("current", {}).get("userProjectId")
            )
        for user_project_id in user_project_ids:
            for plan in self.api.exam_list_plan(user_project_id).get("data", []):
                for history in self.api.exam_list_history(
                    plan["examPlanId"], plan["examType"]
                ).get("data", []):
                    questions = self.api.exam_review_paper(
                        history["id"], history["isRetake"]
                    )["data"].get("questions", [])
                    for answer in questions:
                        server_title = answer["title"]
                        clean_title = clean_text(server_title)
                        old_key = key_by_clean.get(clean_title)
                        if old_key is None:
                            # 新题：直接以服务器原文入库，并提醒用户
                            answers_json[server_title] = {
                                "type": answer["type"],
                                "optionList": [
                                    {
                                        "content": o["content"],
                                        "isCorrect": o["isCorrect"],
                                    }
                                    for o in answer.get("optionList", [])
                                ],
                            }
                            key_by_clean[clean_title] = server_title
                            self.log.info(f"发现新题：{server_title}")
                            for option in answer.get("optionList", []):
                                self.log.info(
                                    f"发现题目：{server_title} 新选项：{option['content']}"
                                )
                            continue
                        entry = answers_json[old_key]
                        # 标题有变化则以服务器原文更新
                        if old_key != server_title:
                            del answers_json[old_key]
                            answers_json[server_title] = entry
                            key_by_clean[clean_title] = server_title
                        # 选项追加合并：新选项追加；已有选项标记取并集
                        # （任一变体标 1 则保留 1），文本保留较长原文，同题
                        # 不同答案的变体互不覆盖（与 _normalize_answers 一致）
                        options = {
                            clean_text(o["content"]): o for o in entry["optionList"]
                        }
                        for option in answer.get("optionList", []):
                            content = clean_text(option["content"])
                            old = options.get(content)
                            if old is None:
                                options[content] = {
                                    "content": option["content"],
                                    "isCorrect": option["isCorrect"],
                                }
                                self.log.info(
                                    f"发现题目：{server_title} 新选项：{option['content']}"
                                )
                                continue
                            merged = False
                            if option["isCorrect"] == 1 and old["isCorrect"] != 1:
                                old["isCorrect"] = 1
                                merged = True
                            if len(option["content"]) > len(old["content"]):
                                old["content"] = option["content"]
                                merged = True
                            if merged:
                                self.log.info(
                                    f"发现题目：{server_title} 答案合并：{old['content']}"
                                )
                        entry["optionList"] = list(options.values())
                        entry["type"] = answer["type"]

        # 所有入库路径统一走规范化，确保只保留 content/isCorrect
        answers_json = self._normalize_answers(answers_json)

        # 写回读取来源（打包内置路径只读，退回可写的 answer/ 目录），
        # 与 _load_answers_json 的加载优先级保持一致，避免同步结果不被加载
        write_path = (
            existing_path
            if existing_path is not None and existing_path != bundle_answer_path
            else answer_path
        )
        os.makedirs(os.path.dirname(write_path), exist_ok=True)
        with open(write_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(answers_json, indent=2, ensure_ascii=False, sort_keys=True)
            )
            f.write("\n")
