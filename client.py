import json
import os
import time
import webbrowser
from random import randint
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse, urljoin
from uuid import uuid4

from loguru import logger
import re
import threading

from api import WeBanAPI
from captcha import CaptchaHandler, LoginCaptchaSolver

exe_path = os.environ.get("PYFUZE_EXECUTABLE_PATH")
if exe_path:
    base_path = os.path.dirname(os.path.abspath(exe_path))
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
answer_dir = os.path.join(base_path, "answer")
answer_path = os.path.join(answer_dir, "answer.json")


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


def _extract_map(content: str) -> dict:
    """从 JS 内容中提取 nonstrMap / pageIdMap

    两阶段匹配：先按命名变量精确匹配 nonstrMap/pageIdMap，
    匹配不到再退回到任意 Map，防止误匹配其他 Map 定义。
    :param content: JS 文件内容
    :return: {step_index: nonstr_value} 映射，未找到返回空字典
    """
    for pattern in [
        r'(?:const|var|let)\s+nonstrMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)',
        r'(?:const|var|let)\s+pageIdMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)',
    ]:
        match = re.search(pattern, content)
        if match:
            entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', match.group(1))
            if entries:
                return {int(step): val for step, val in entries}
    # 退而求其次：匹配任意 Map（变量名未知）
    for m in re.finditer(r'new\s+Map\(\[([\s\S]*?)\]\)', content):
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
        page_match = re.search(r'page-(\d+)', m.group(1))
        if page_match:
            content_pages += 1

    # 统计题目页（含 data-all-answer 的 page-options）
    question_pages = 0
    for m in re.finditer(
        r'<section\b[^>]*class="([^"]*\bpage-item\b[^"]*)"[^>]*>'
        r'(?:(?!</section>).)*?(?:data-all-answer|page-commit)',
        html, re.DOTALL,
    ):
        page_match = re.search(r'page-(\d+)', m.group(1))
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
    except Exception:
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
        user: Dict[str, str] | None = None,
        log=logger,
        browser_path: str | None = None,
        debug: bool = False,
    ) -> None:
        """
        :param tenant_name: 学校全称
        :param account: 用户名
        :param password: 密码
        :param user: 已有用户凭据 {"userId": ..., "token": ...}，提供则跳过登录
        :param log: logger 实例
        :param browser_path: 浏览器可执行文件路径，用于验证码处理
        :param debug: 是否启用调试日志
        """
        self.log = log
        self.tenant_name = tenant_name.strip()
        self.study_time = 30
        self.browser_path = browser_path
        if user and all([user.get("userId"), user.get("token")]):
            self.api = WeBanAPI(user=user, debug=debug, log=log)
        elif all([self.tenant_name, account, password]):
            self.api = WeBanAPI(account=account, password=password, debug=debug, log=log)
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
            )
        return self._captcha_handler

    def _prompt(self, message: str) -> str:
        """线程安全的 input 封装，多线程下避免 input 输出交错
        :param message: 提示信息
        :return: 去除首尾空白的用户输入
        """
        with self._stdin_lock:
            return input(message).strip()

    def _load_answers_json(self, warn_on_fail: bool = False) -> dict:
        """加载题库，返回 {clean_text(题目): [正确选项的 clean_text(content), ...]}

        :param warn_on_fail: True 时加载失败只警告不抛异常（学习模式容错），
            False 时抛出异常（考试模式必须要有题库）
        :return: 清洗后的题目标题 → 正确答案内容列表的映射
        """
        answers: dict = {}
        try:
            with open(answer_path, encoding="utf-8") as f:
                for title, options in json.load(f).items():
                    title = clean_text(title)
                    answers.setdefault(title, []).extend(
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

    def _build_course_url(self, course: dict, task: dict) -> str:
        """根据课程和任务信息构建完整的课程 URL

        硬编码的 query 参数（projectType=special 等）为 Web 播放器前端所需，
        缺失会导致页面白屏或功能异常。
        :param course: 课程数据（含 resourceId）
        :param task: 任务数据（含 userProjectId）
        :return: 完整的课程播放 URL
        """
        url = self.api.get_course_url(course["resourceId"], task["userProjectId"])["data"]
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
    ) -> Dict[str, Any]:
        """获取学习进度
        :param user_project_id: 项目 ID
        :param project_prefix: 日志前缀（如项目名）
        :param output: 是否输出进度日志
        :return: show_progress API 原始响应
        """
        progress = self.api.show_progress(user_project_id)
        if progress.get("code", -1) != "0":
            if output:
                self.log.warning(f"{project_prefix} 获取进度失败：{progress}")
            return progress
        data = progress.get("data", {})
        required = data["requiredNum"] - data["requiredFinishedNum"]
        optional = data["optionalNum"] - data["optionalFinishedNum"]
        push = data["pushNum"] - data["pushFinishedNum"]
        eta = max(0, self.study_time * (required + optional + push))
        if output:
            self.log.info(
                f"{project_prefix} 进度：必修课：{data['requiredFinishedNum']}/{data['requiredNum']}，"
                f"推送课：{data['pushFinishedNum']}/{data['pushNum']}，"
                f"自选课：{data['optionalFinishedNum']}/{data['optionalNum']}，"
                f"考试：{data['examFinishedNum']}/{data['examNum']}，预计剩余时间：{eta} 秒"
            )
        return progress

    # ---- login --------------------------------------------------------------

    def login(self) -> Dict | None:
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
            else:
                account_id = self.api.account or self.api.user.get("userId") or "unknown"
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
                except Exception:
                    pass
            res = self.api.login(verify_code, int(verify_time))
            if res.get("detailCode") == "67":
                self.log.warning("验证码识别失败，正在重试")
                continue
            if self.api.user.get("userId"):
                return self.api.user
            self.log.error(f"登录出错，请检查 config.toml 内账号密码，或删除文件后重试: {res}")
            break
        return None

    # ---- study --------------------------------------------------------------

    def run_study(self, study_time: int, study_mode: str = "true") -> None:
        """主学习流程入口：遍历所有项目 → 分类 → 课程，逐门学习
        :param study_time: 每门课学习秒数（0 使用默认值 20）
        :param study_mode: 学习模式，"force" 时忽略完成状态全部重新学习
        """
        if study_time:
            self.study_time = study_time

        force_restudy = study_mode == "force"
        if force_restudy:
            self.log.info(f"重新学习模式已开启，所有课程将重新学习，每门课程学习 {self.study_time} 秒")

        answers_json = self._load_answers_json(warn_on_fail=True)

        my_project = self.api.list_my_project()
        if my_project.get("code", -1) != "0":
            self.log.error(f"获取任务列表失败：{my_project}")
            return

        my_project = my_project.get("data", [])
        completion = self.api.list_completion()
        if completion.get("code", -1) != "0":
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [d["module"] for d in completion.get("data", []) if d["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            my_project.append(lab_project.get("data", {}).get("current", {}))

        for task in my_project:
            project_prefix = task["projectName"]
            self.log.info(f"开始处理任务：{project_prefix}")
            self.get_progress(task["userProjectId"], project_prefix)

            choose_types = [
                (3, "必修课", "requiredNum", "requiredFinishedNum"),
                (1, "推送课", "pushNum", "pushFinishedNum"),
                (2, "自选课", "optionalNum", "optionalFinishedNum"),
            ]
            for choose_type in choose_types:
                categories = self.api.list_category(task["userProjectId"], choose_type[0])
                if categories.get("code") != "0":
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    continue

                for category in categories.get("data", []):
                    category_prefix = f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    if not force_restudy and category["finishedNum"] >= category["totalNum"]:
                        continue

                    courses = self.api.list_course(
                        task["userProjectId"], category["categoryCode"], choose_type[0]
                    )
                    for course in courses.get("data", []):
                        if not force_restudy and int(course.get("finished", 0)) == 1:
                            continue
                        course_prefix = f"{category_prefix}/{course['resourceName']}"
                        progress_before = self.get_progress(task["userProjectId"], project_prefix, output=False)
                        finished_before = 0
                        if progress_before.get("code", -1) == "0":
                            d = progress_before["data"]
                            finished_before = d["requiredFinishedNum"] + d["pushFinishedNum"] + d["optionalFinishedNum"]
                        self._study_one_course(
                            course, task, category_prefix, project_prefix,
                            answers_json, force_restudy,
                        )
                        progress_after = self.get_progress(task["userProjectId"], project_prefix)
                        if progress_after.get("code", -1) == "0":
                            d = progress_after["data"]
                            finished_after = d["requiredFinishedNum"] + d["pushFinishedNum"] + d["optionalFinishedNum"]
                            if finished_after <= finished_before:
                                self.log.warning(f"{course_prefix}：完课成功但进度未更新，请手动检查")

            self.log.success(f"{project_prefix} 课程学习完成")

    def _study_one_course(
        self, course: dict, task: dict, category_prefix: str,
        project_prefix: str, answers_json: dict, force_restudy: bool,
    ) -> None:
        """处理单门课程：有 apinext 的走翻页流程，没 apinext 的直接答题+完课"""
        course_prefix = f"{category_prefix}/{course['resourceName']}"

        if not force_restudy and int(course.get("finished", 0)) == 1:
            return

        self.log.info(f"学习： {course_prefix}")
        self.api.study(course["resourceId"], task["userProjectId"])
        study_start = time.time()

        if "userCourseId" not in course:
            self.log.success(f"{course_prefix} 完成")
            return

        course_url = self._build_course_url(course, task)
        self.log.info(f"{course_prefix}：{course_url.split('?')[0]}")
        query = parse_qs(urlparse(course_url).query)
        source_str = get_source_str(query)

        course_code = ""
        url_path = urlparse(course_url).path
        code_match = re.search(r'/course/([^/]+)/', url_path)
        if code_match:
            course_code = code_match.group(1)
        item_info = (
            self.parse_item_js(course_code, course_url=course_url)
            if course_code
            else {"nonstr_map": {}, "has_exam": False, "total_step": 0}
        )

        unique_no = str(uuid4())
        nonstr_map = item_info.get("nonstr_map", {})
        total_step = item_info.get("total_step", 0)
        uses_apinext = item_info.get("uses_apinext", False)

        if uses_apinext:
            self.api.init_index(task["userProjectId"])

        # 1. apinext finish=2 翻页（先翻页解锁题目）
        if uses_apinext and total_step:
            self.log.info(f"total_step={total_step} ({item_info.get('total_step_source', '')})")
            self.handle_apinext(
                course["userCourseId"], course["resourceId"],
                task["userProjectId"], nonstr_map, total_step,
                unique_no=unique_no, finish=2,
            )

        # 2. 获取并回答题目（翻页后题目才可用）
        question_data = self.api.list_question(course["resourceId"])
        if question_data and question_data.get("code") == "0":
            data = question_data.get("data", {})
            for qlist, label, save_func in [
                (data.get("viewpointQuestionList", []), "观点题", self.api.save_question),
                (data.get("examQuestionList", []), "课后习题", self.api.save_exam_question),
            ]:
                if qlist:
                    self.log.info(f"  {label} {len(qlist)} 道")
                    for i, q in enumerate(qlist):
                        hit = self._answer_question(
                            q, answers_json, course["resourceId"], save_func, source_str,
                        )
                        self.log.info(f"    {i + 1}/{len(qlist)} {'✓' if hit else '未命中'}")
                        time.sleep(0.5)
        elif question_data:
            self.log.info(f"  list_question: code={question_data.get('code')}")
        if item_info.get("has_exam") and not question_data.get("data", {}).get("examQuestionList"):
            self.log.info("  检测到题目标记但 list_question 无课后习题，可能为内联题目")

        # 3. 确保满足最低学习时长（服务端要求 study 后至少学习 study_time 秒才接受完课）
        elapsed = time.time() - study_start
        remaining = self.study_time - elapsed
        if remaining > 0:
            self.log.info(f"等待学习时长 {remaining:.0f}s (已用 {elapsed:.0f}s/{self.study_time}s)")
            time.sleep(remaining)

        # 4. apinext finish=1（仅加载 apicenext.js 的课程）
        if uses_apinext and total_step:
            self.handle_apinext(
                course["userCourseId"], course["resourceId"],
                task["userProjectId"], nonstr_map, total_step,
                unique_no=unique_no, finish=1,
            )
            time.sleep(2)

        # 5. 完课
        res = self._finish_course(course, task, query, course_url, unique_no)
        if res.get("code", "-1") != "0":
            self.log.error(f"{course_prefix} 完成失败：{res}")
            return

        self.log.success(f"{course_prefix} 完成")

    def _finish_course(
        self, course: dict, task: dict, query: dict, course_url: str, unique_no: str,
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
        :param unique_no: apinext 使用的唯一标识
        :return: 完课 API 响应
        """
        if query.get("lyra", [None])[0] == "lyra":
            return self.api.finish_lyra(query.get("userActivityId", [None])[0])
        if query.get("weiban", [None])[0] != "weiban":
            return self.api.finish_by_token(course["userCourseId"], course_type="open")
        if query.get("source", [None])[0] == "moon":
            return self.api.finish_by_token(course["userCourseId"], course_type="moon")

        finish_kwargs = {"unique_no": unique_no}
        if query.get("csCapt", [None])[0] == "true":
            try:
                captcha_result = self.captcha_handler.handle_course_captcha(course_url=course_url)
                check_res = self.api.course_check(
                    course["userCourseId"], task["userProjectId"], course["resourceId"],
                    captcha_result["randstr"], captcha_result["ticket"],
                )
                if check_res.get("code", -1) != "0":
                    self.log.error(f"课程验证码校验失败：{check_res}")
                    return check_res
                self.log.success("课程验证码校验通过")
                finish_kwargs["token"] = check_res.get("data", "")
            except Exception as e:
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
        """
        # 解析每题等待时间
        try:
            parts = exam_question_time.split(",")
            question_base_time = int(parts[0])
            question_random_upper = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            question_base_time = 3
            question_random_upper = 3

        answers_json = self._load_answers_json()

        projects = self.api.list_my_project()
        if projects.get("code", -1) != "0":
            self.log.error(f"获取考试列表失败：{projects}")
            return
        projects = projects.get("data", [])

        completion = self.api.list_completion()
        if completion.get("code", -1) != "0":
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [d["module"] for d in completion.get("data", []) if d["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            projects.append(lab_project.get("data", {}).get("current", {}))

        for project in projects:
            self.log.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]

            exam_plans = self.api.exam_list_plan(user_project_id)
            if exam_plans.get("code", -1) != "0":
                self.log.error(f"获取考试计划失败：{exam_plans}")
                return
            exam_plans = exam_plans["data"]

            for plan in exam_plans:
                plan_name = f"{project['projectName']}/{plan['examPlanName']}"
                exam_odd_num = plan.get("examOddNum", 0)
                exam_finish_num = plan.get("examFinishNum", 0)
                exam_score = plan.get("examScore", 0)
                pass_score = plan.get("passScore", 0)

                # ── 根据 exam_mode 判断是否跳过 ──
                if exam_odd_num <= 0:
                    self.log.info(f"{plan_name} 无剩余考试机会，跳过")
                    continue

                if exam_mode == "true" and exam_finish_num > 0 and exam_score >= pass_score:
                    self.log.info(
                        f"{plan_name} 已及格 ({exam_score}分 >= {pass_score}分)，跳过"
                    )
                    continue

                if exam_mode == "perfect" and exam_score >= 100:
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
                    self.log.info(
                        f"{plan_name} 已完成 {exam_finish_num} 次，"
                        f"最高 {exam_score} 分，继续考试以争取更好成绩"
                    )

                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]

                before_paper = self.api.exam_before_paper(plan["id"])
                if before_paper.get("code", -1) != "0":
                    self.log.error(
                        f"考试项目 {plan_name} 获取考试记录失败：{before_paper}"
                    )

                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if prepare_paper.get("code", -1) != "0":
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
                try:
                    captcha_result = self.captcha_handler.handle_exam_captcha(user_exam_plan_id)
                    check_res = self.api.exam_check(
                        user_exam_plan_id, captcha_result["randstr"], captcha_result["ticket"],
                    )
                    if check_res.get("code", -1) != "0":
                        self.log.error(f"无感验证码校验失败：{check_res}")
                        continue
                    self.log.success("无感验证码校验通过")
                except Exception as e:
                    self.log.error(f"无感验证码处理异常: {e}")
                    continue

                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if exam_paper.get("code", -1) != "0":
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
                    target = have_answer if clean_text(question["title"]) in answers_json else no_answer
                    target.append(question)

                match_rate = (
                    len(have_answer) / len(question_list) * 100
                    if question_list else 0
                )
                self.log.info(
                    f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，"
                    f"无答案的题目数：{len(no_answer)}，题库匹配率：{match_rate:.1f}%"
                )

                # perfect 模式：匹配率不足且 random_answer=False 时警告
                if exam_mode == "perfect" and match_rate < 100:
                    if not random_answer:
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
                    if random_answer:
                        # 自动随机作答：单选随机选一个，多选全选
                        answers_ids = self._auto_select_answer(question)
                        use_time = question_base_time + randint(0, question_random_upper)
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 随机作答 "
                            f"({type_label})，等待 {use_time}s: "
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
                            answer = self._prompt(
                                f"[{self.api.user.get('realName', '未知')}] "
                                "请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）："
                            ).replace(" ", "").replace("，", ",")
                            candidates = [ans.strip() for ans in answer.split(",") if ans.strip()]
                            if all(ans.isdigit() and 1 <= int(ans) <= opt_count for ans in candidates):
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
                        user_exam_plan_id, question["id"],
                        use_time, answers_ids, exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                # ── 题库作答 ──
                if have_answer:
                    self.log.info(
                        f"开始答题库中的题目，共 {len(have_answer)} 道题目"
                    )
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
                    self.log.info(f"等待 {use_time} 秒，模拟答题中...")
                    time.sleep(use_time)
                    if not self.record_answer(
                        user_exam_plan_id, question["id"], use_time,
                        answers_ids, exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                self.log.info("完成考试，正在提交试卷...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if submit_res.get("code", -1) != "0":
                    raise RuntimeError(f"提交试卷失败，请重新考试：{submit_res}")
                self.log.success(
                    f"试卷提交成功，考试完成，成绩：{submit_res['data']['score']} 分"
                )

    # ---- item.js parsing ----------------------------------------------------

    def parse_item_js(self, course_code: str, course_url: str | None = None) -> Dict[str, Any]:
        """解析课程 JS，检测是否使用 apinext 并提取 nonstrMap/total_step。

        关键判断：HTML 是否加载 apicenext.js。
        不加载 → 不需要任何 apinext 调用，直接返回 uses_apinext=False。
        加载 → 从 item.js 注释/HTML btn-next 推导 total_step。

        :param course_code: 课程代码（用于拼接 mcwk 资源 URL）
        :param course_url: 课程播放页 URL，作为抓 mcwk HTML 的 Referer。
            缺失时 mcwk 资源服务器可能 403。
        """
        result = {
            "uses_apinext": False, "nonstr_map": {}, "has_exam": False,
            "total_step": 0, "total_step_source": "",
        }

        try:
            html_url = f"https://mcwk.mycourse.cn/course/{course_code}/{course_code}.html"
            html = _fetch_text(self.api.session, html_url, referer=course_url)
            if not html:
                return result

            # 不加载 apicenext.js 的课程不需要 apinext
            if "apicenext.js" not in html:
                result["has_exam"] = "saveExamQuestion" in html or "listQuestions" in html
                return result

            result["uses_apinext"] = True
            script_urls = [
                urljoin(html_url, src)
                for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', html)
                if "item.js" in src or f"{course_code}.js" in src
            ]
            script_urls.extend([
                f"https://mcwk.mycourse.cn/course/{course_code}/js/item.js",
                f"https://mcwk.mycourse.cn/course/{course_code}/build/js/{course_code}.js",
            ])

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

        except Exception as e:
            self.log.warning(f"解析课程 JS 失败：{e}")
        return result

    # ---- apinext / answer helpers -------------------------------------------

    def handle_apinext(
        self,
        user_course_id: str,
        course_id: str,
        user_project_id: str,
        nonstr_map: Dict[int, str],
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

        if finish == 2:
            self.log.info(f"apinext 发送中间步骤，共 {total_step} 步")
            for step in range(1, total_step + 1):
                if step_delay:
                    time.sleep(step_delay)
                # nonstr_map 的 key 对应 finish=2 的 step，完成步 (finish=1) 不在 map 中
                nonstr = nonstr_map.get(step, "")
                try:
                    resp = self.api.apinext(
                        user_course_id, course_id, user_project_id,
                        step=step, finish=2, nonstr=nonstr, unique_no=unique_no,
                    )
                    self.log.info(f"apinext [{step}/{total_step}] finish=2 已发送")
                    if not resp.get("success"):
                        self.log.warning(f"apinext [{step}/{total_step}] 返回异常：{resp}")
                except Exception as e:
                    self.log.warning(f"apinext [{step}/{total_step}] 失败：{e}")
        else:
            if step_delay:
                time.sleep(step_delay)
            try:
                # finish=1 的 step 需要偏移 total_step + 1（nonstr_map 不含此步）
                resp = self.api.apinext(
                    user_course_id, course_id, user_project_id,
                    step=total_step + 1, finish=1, nonstr="", unique_no=unique_no,
                )
                if not resp.get("success"):
                    self.log.warning(f"apinext 完成请求返回异常：{resp}")
                self.log.info(f"apinext 完成标记 step={total_step + 1} finish=1 已发送")
            except Exception as e:
                self.log.warning(f"apinext 完成请求失败：{e}")
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
        self, question: dict, answers_json: Dict, course_id: str, save_func, source: str,
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
            course_id, question["id"], json.dumps([option_list[0]["id"]]), source,
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
        answer_ids = [letter_to_opt[ch]["id"] for ch in correct_letters if ch in letter_to_opt]
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
            user_exam_plan_id, question_id, per_time, answers_ids, exam_plan_id,
        )
        if res.get("code", -1) != "0":
            self.log.error(f"答题失败，请重新开启考试：{res}")
            return False
        self.log.info("保存答案成功")
        return True

    # ---- sync answers -------------------------------------------------------

    def sync_answers(self) -> None:
        """同步答案
        :return: 无返回值
        """
        os.makedirs(answer_dir, exist_ok=True)
        if not os.path.exists(answer_path):
            self.log.info("题库不存在，正在下载...")
            with open(answer_path, "w", encoding="utf-8") as f:
                f.write(self.api.download_answer())
        try:
            with open(answer_path, encoding="utf-8") as f:
                answers_json = json.load(f)
        except Exception as e:
            self.log.error(f"读取题库失败，请重新下载题库：{e}")
            return

        user_project_ids = [
            p["userProjectId"] for p in self.api.list_my_project().get("data", [])
        ]
        user_project_ids.extend(
            p["userProjectId"] for p in self.api.list_my_project(ended=1).get("data", [])
        )
        completion = self.api.list_completion()
        if completion.get("code", -1) != "0":
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [d["module"] for d in completion.get("data", []) if d["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            user_project_ids.append(
                lab_project.get("data", {}).get("current", {}).get("userProjectId")
            )
        for user_project_id in user_project_ids:
            for plan in self.api.exam_list_plan(user_project_id).get("data", []):
                for history in self.api.exam_list_history(
                    plan["examPlanId"], plan["examType"]
                ).get("data", []):
                    questions = self.api.exam_review_paper(history["id"], history["isRetake"])[
                        "data"
                    ].get("questions", [])
                    for answer in questions:
                        title = answer["title"]
                        old_opts = {
                            o["content"]: o["isCorrect"]
                            for o in answers_json.get(title, {}).get("optionList", [])
                        }
                        new_opts = old_opts | {
                            o["content"]: o["isCorrect"]
                            for o in answer.get("optionList", [])
                        }
                        for content in new_opts.keys() - old_opts.keys():
                            self.log.info(f"发现题目：{title} 新选项：{content}")
                        answers_json[title] = {
                            "type": answer["type"],
                            "optionList": [
                                {"content": content, "isCorrect": is_correct}
                                for content, is_correct in new_opts.items()
                            ],
                        }

        with open(answer_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(answers_json, indent=2, ensure_ascii=False, sort_keys=True))
            f.write("\n")
