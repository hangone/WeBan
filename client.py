import json
import os
import sys
import time
import webbrowser
from typing import Any, Dict, Optional, TYPE_CHECKING, Union
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from loguru import logger
import re
import threading

from api import WeBanAPI
from captcha import CaptchaHandler

if TYPE_CHECKING:
    from ddddocr import DdddOcr

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
answer_dir = os.path.join(base_path, "answer")
answer_path = os.path.join(answer_dir, "answer.json")


def clean_text(text):
    # 只保留字母、数字和汉字，自动去除所有符号和空格
    return re.sub(r"[^\w\u4e00-\u9fa5]", "", text)


class WeBanClient:
    _stdin_lock = threading.Lock()

    def __init__(self, tenant_name: str, account: str | None = None, password: str | None = None, user: Dict[str, str] | None = None, log=logger) -> None:
        self.log = log
        self.tenant_name = tenant_name.strip()
        self.study_time = 20
        self.ocr = self.get_ocr_instance()
        if user and all([user.get("userId"), user.get("token")]):
            self.api = WeBanAPI(user=user)
        elif all([self.tenant_name, account, password]):
            self.api = WeBanAPI(account=account, password=password)
        else:
            self.api = WeBanAPI()
        self.tenant_code = self.get_tenant_code()
        if self.tenant_code:
            self.api.set_tenant_code(self.tenant_code)
        else:
            raise ValueError("学校代码获取失败，请检查学校全称是否正确")
        self._captcha_handler = None

    @property
    def captcha_handler(self):
        """延迟初始化 CaptchaHandler（需要 login 后才有 token）"""
        if self._captcha_handler is None:
            self._captcha_handler = CaptchaHandler(
                tenant_code=self.tenant_code,
                user_id=self.api.user["userId"],
                token=self.api.user["token"],
                log=self.log,
            )
        return self._captcha_handler

    @staticmethod
    def get_project_type(project_category: int) -> str:
        """
        获取项目类型
        :param project_category: 项目类型 1.新生安全教育 2.安全课程 3.专题学习 4.军事理论 9.实验室
        :return: 项目类型字符串
        """
        if project_category == 3:
            return "special"
        elif project_category == 9:
            return "lab"
        else:
            return ""

    def get_ocr_instance(self, _cache: Dict[str, Any] = {"ocr": None}) -> Optional[Union["DdddOcr", None]]:
        """
        检查是否安装 ddddocr 库，多次调用返回同一个 DdddOcr 实例
        """
        if not _cache.get("ocr"):
            try:
                import ddddocr
                _cache["ocr"] = ddddocr.DdddOcr(show_ad=False)
            except Exception:
                self.log.warning("ddddocr 库未安装，自动验证码识别功能将不可用")

        return _cache["ocr"]

    def get_tenant_code(self) -> str:
        """
        获取学校代码
        :return: code
        """
        if not self.tenant_name:
            self.log.error(f"学校全称不能为空")
            return ""
        tenant_list = self.api.get_tenant_list_with_letter()
        if tenant_list.get("code", -1) == "0":
            self.log.info(f"获取学校列表成功")
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
        self.log.error(f"没找到你的学校代码，请检查学校全称是否正确（上面是有效的学校名称）: {self.tenant_name}")
        if maybe_names:
            self.log.error(f"可能的学校名称: {maybe_names}")
        return ""

    def get_progress(self, user_project_id: str, project_prefix: str | None, output: bool = True) -> Dict[str, Any]:
        """
        获取学习进度
        :param output: 是否输出进度信息
        :param user_project_id: 用户项目 ID
        :param project_prefix: 项目前缀
        :return:
        """
        progress = self.api.show_progress(user_project_id)
        if progress.get("code", -1) == "0":
            progress = progress.get("data", {})
            # 推送课
            push_num = progress["pushNum"]
            push_finished_num = progress["pushFinishedNum"]
            # 自选课
            optional_num = progress["optionalNum"]
            optional_finished_num = progress["optionalFinishedNum"]
            # 必修课
            required_num = progress["requiredNum"]
            required_finished_num = progress["requiredFinishedNum"]
            # 考试
            exam_num = progress["examNum"]
            exam_finished_num = progress["examFinishedNum"]
            eta = max(0, self.study_time * (required_num - required_finished_num + optional_num - optional_finished_num + push_num - push_finished_num))
            if output:
                self.log.info(f"{project_prefix} 进度：必修课：{required_finished_num}/{required_num}，推送课：{push_finished_num}/{push_num}，自选课：{optional_finished_num}/{optional_num}，考试：{exam_finished_num}/{exam_num}，预计剩余时间：{eta} 秒")
        return progress

    def login(self) -> Dict | None:
        if self.api.user.get("userId"):
            return self.api.user
        retry_limit = 3
        for i in range(retry_limit + 2):
            if i > 0:
                self.log.warning(f"登录失败，正在重试 {i}/{retry_limit+2} 次")
            verify_time = self.api.get_timestamp(13, 0)
            verify_image = self.api.rand_letter_image(verify_time)
            if i < retry_limit and self.ocr:
                try:
                    verify_code = self.ocr.classification(verify_image)
                    self.log.info(f"自动验证码识别结果: {verify_code}")
                    if len(verify_code) != 4:
                        self.log.warning(f"验证码识别失败，正在重试")
                        continue
                except Exception as e:
                    self.log.error(f"验证码识别异常: {e}")
                    continue
            else:
                account_id = self.api.account or self.api.user.get("userId") or "unknown"
                captcha_filename = f"verify_code_{account_id}.png"
                captcha_path = os.path.abspath(captcha_filename)
                with self._stdin_lock:
                    open(captcha_path, "wb").write(verify_image)
                    webbrowser.open(f"file://{captcha_path}")
                    verify_code = input(f"[{account_id}] 请查看 {captcha_filename} 输入验证码：")
                # 尝试删除临时验证码图片
                try:
                    os.remove(captcha_path)
                except Exception:
                    pass
            res = self.api.login(verify_code, int(verify_time))
            if res.get("detailCode") == "67":
                self.log.warning(f"验证码识别失败，正在重试")
                continue
            if self.api.user.get("userId"):
                return self.api.user
            self.log.error(f"登录出错，请检查 config.json 内账号密码，或删除文件后重试: {res}")
            break
        return None

    def run_study(self, study_time: int = 20, restudy_time: int = 0) -> None:
        if study_time:
            self.study_time = study_time

        if restudy_time:
            self.study_time = restudy_time
            self.log.info(f"重新学习模式已开启，所有课程将重新学习，每门课程学习 {self.study_time} 秒")

        # 加载题库（用于课后习题自动答题）
        answers_json = {}
        try:
            with open(answer_path, encoding="utf-8") as f:
                for title, options in json.load(f).items():
                    title = clean_text(title)
                    if title not in answers_json:
                        answers_json[title] = []
                    answers_json[title].extend([clean_text(a["content"]) for a in options.get("optionList", []) if a["isCorrect"] == 1])
        except Exception:
            self.log.warning("题库加载失败，课后习题将随机作答")

        my_project = self.api.list_my_project()
        if my_project.get("code", -1) != "0":
            self.log.error(f"获取任务列表失败：{my_project}")
            return

        my_project = my_project.get("data", [])
        if not my_project:
            self.log.error(f"获取任务列表失败")

        completion = self.api.list_completion()
        if completion.get("code", -1) != "0":
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [d["module"] for d in completion.get("data", []) if d["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info(f"加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            my_project.append(lab_project.get("data", {}).get("current", {}))

        for task in my_project:
            project_prefix = task["projectName"]
            self.log.info(f"开始处理任务：{project_prefix}")

            # 获取学习进度
            self.get_progress(task["userProjectId"], project_prefix)

            # 聚合类别 1：推送课，2：自选课，3：必修课
            for choose_type in [(3, "必修课", "requiredNum", "requiredFinishedNum"), (1, "推送课", "pushNum", "pushFinishedNum"), (2, "自选课", "optionalNum", "optionalFinishedNum")]:
                categories = self.api.list_category(task["userProjectId"], choose_type[0])
                if categories.get("code") != "0":
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    continue

                for category in categories.get("data", []):
                    category_prefix = f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    self.log.info(f"开始处理 {category_prefix}")
                    if not restudy_time and category["finishedNum"] >= category["totalNum"]:
                        self.log.success(f"{category_prefix} 已完成")
                        continue

                    # 获取学习进度
                    progress = self.get_progress(task["userProjectId"], project_prefix, False)
                    if not restudy_time and progress[choose_type[3]] >= progress[choose_type[2]]:
                        self.log.info(f"{category_prefix} 已达到要求，跳过")
                        break

                    courses = self.api.list_course(task["userProjectId"], category["categoryCode"], choose_type[0])
                    for course in courses.get("data", []):
                        course_prefix = f"{category_prefix}/{course['resourceName']}"
                        # 获取学习进度
                        progress = self.get_progress(task["userProjectId"], category_prefix)
                        if not restudy_time and progress[choose_type[3]] >= progress[choose_type[2]]:
                            self.log.info(f"{category_prefix} 已达到要求，跳过")
                            break

                        self.log.info(f"开始处理课程：{course_prefix}")
                        if not restudy_time and course["finished"] == 1:
                            self.log.success(f"{course_prefix} 已完成")
                            continue

                        self.api.study(course["resourceId"], task["userProjectId"])
                        if self.api.get_simple_config().get("data", {}).get("isControlSource") == 1:
                            self.log.warning(f"检测到课程需网页端处理（isControlSource=1），建议前往网页版登录处理一下")

                        if "userCourseId" not in course:
                            self.log.success(f"{course_prefix} 完成")
                            continue

                        course_url = self.api.get_course_url(course["resourceId"], task["userProjectId"])["data"] + "&weiban=weiban"
                        self.log.info(f"{course_prefix} URL: {course_url}")
                        query = parse_qs(urlparse(course_url).query)

                        # 从 URL 中提取课程代码，解析 item.js
                        course_code = ""
                        url_path = urlparse(course_url).path
                        code_match = re.search(r'/course/([^/]+)/', url_path)
                        if code_match:
                            course_code = code_match.group(1)
                        item_info = self.parse_item_js(course_code) if course_code else {"nonstr_map": {}, "has_exam": False}

                        sleep = 0
                        while sleep < self.study_time:
                            if sleep % 60 == 0:
                                self.log.info(f"{course_prefix} 等待 {self.study_time - sleep} 秒，模拟学习中...")
                            time.sleep(1)
                            sleep += 1

                        # 生成 uniqueNo，apinext 和 finish 共用同一个
                        unique_no = str(uuid4())

                        # 浏览器流程：apinext finish=2 → 课后习题 → apinext finish=1 → finish_by_token
                        nonstr_map = item_info.get("nonstr_map", {})
                        page_count = item_info.get("page_count", 0)
                        total_step = page_count or (max(nonstr_map.keys()) if nonstr_map else 0)

                        # 中间翻页心跳
                        if total_step:
                            self.handle_apinext(course["userCourseId"], course["resourceId"], task["userProjectId"], nonstr_map, total_step, unique_no=unique_no, finish=2)

                        # 课后习题（必须在 finish=1 / 完课接口之前）
                        if item_info.get("has_exam"):
                            source_str = self.get_source_str(query)
                            self.handle_exam_questions(course["resourceId"], answers_json, course_prefix, source_str)

                        # 完成标记
                        if total_step:
                            self.handle_apinext(course["userCourseId"], course["resourceId"], task["userProjectId"], nonstr_map, total_step, unique_no=unique_no, finish=1)

                        # 完课接口
                        if query.get("lyra", [None])[0] == "lyra":  # 安全实训
                            res = self.api.finish_lyra(query.get("userActivityId", [None])[0])
                        elif query.get("weiban", [None])[0] != "weiban":
                            res = self.api.finish_by_token(course["userCourseId"], course_type="open")
                        elif query.get("source", [None])[0] == "moon":
                            res = self.api.finish_by_token(course["userCourseId"], course_type="moon")
                        else:
                            token = None
                            if query.get("csCapt", [None])[0] == "true":
                                # 通过 Playwright 让用户手动完成滑块验证码 (appId: 195119536)
                                try:
                                    captcha_result = self.captcha_handler.handle_course_captcha(course_url=course_url,)
                                    check_res = self.api.course_check(
                                        course["userCourseId"],
                                        task["userProjectId"],
                                        course["resourceId"],
                                        captcha_result["randstr"],
                                        captcha_result["ticket"],
                                    )
                                    if check_res.get("code", -1) != "0":
                                        self.log.error(f"课程验证码校验失败：{check_res}")
                                        continue
                                    token = check_res.get("data", "")
                                    self.log.success(f"课程验证码校验通过，token: {token}")
                                except Exception as e:
                                    self.log.error(f"课程验证码处理异常: {e}")
                                    continue
                            res = self.api.finish_by_token(course["userCourseId"], token, unique_no=unique_no)
                            if res.get("code", "-1") != "0":
                                self.log.error(f"{course_prefix} 完成失败：{res}")

                        self.log.success(f"{course_prefix} 完成")

            self.log.success(f"{project_prefix} 课程学习完成")

    def run_exam(self, use_time: int = 250):
        # 加载题库
        answers_json = {}

        with open(answer_path, encoding="utf-8") as f:
            for title, options in json.load(f).items():
                title = clean_text(title)
                if title not in answers_json:
                    answers_json[title] = []
                answers_json[title].extend([clean_text(a["content"]) for a in options.get("optionList", []) if a["isCorrect"] == 1])

        # 获取项目
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
            self.log.info(f"加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            projects.append(lab_project.get("data", {}).get("current", {}))

        for project in projects:
            self.log.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]
            # 获取考试计划
            exam_plans = self.api.exam_list_plan(user_project_id)
            if exam_plans.get("code", -1) != "0":
                self.log.error(f"获取考试计划失败：{exam_plans}")
                return
            exam_plans = exam_plans["data"]
            for plan in exam_plans:
                if plan["examFinishNum"] != 0:
                    with self._stdin_lock:
                        self.log.success(f"考试项目 {project['projectName']}/{plan['examPlanName']} 最高成绩 {plan['examScore']} 分。已考试次数 {plan['examFinishNum']} 次，还剩 {plan['examOddNum']} 次。需要重考吗(y/N)？")
                        choice = input().strip().lower()
                    if choice != "y":
                        self.log.info(f"不重考项目 {project['projectName']}")
                        continue
                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]
                # 是否存在完成的考试记录
                before_paper = self.api.exam_before_paper(plan["id"])
                if before_paper.get("code", -1) != "0":
                    self.log.error(f"考试项目 {project['projectName']}/{plan['examPlanName']} 获取考试记录失败：{before_paper}")
                # before_paper = before_paper.get("data", {})
                # if before_paper.get("isExistedNotSubmit"):
                #     self.log.warning(f"考试项目 {project['projectName']}/{plan['examPlanName']} 存在未提交的考试数据，继续将清除未提交数据（Y/n）:")
                #     if input().lower() == "n":
                #         self.log.error(f"用户取消")
                #         return

                # 预请求
                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if prepare_paper.get("code", -1) != "0":
                    self.log.error(f"获取考试信息失败：{prepare_paper}")
                    continue
                prepare_paper = prepare_paper["data"]
                question_num = prepare_paper["questionNum"]
                self.log.info(f"考试信息：用户：{prepare_paper['realName']}，ID：{prepare_paper['userIDLabel']}，题目数：{question_num}，试卷总分：{prepare_paper['paperScore']}，限时 {prepare_paper['answerTime']} 分钟")
                per_time = use_time // prepare_paper["questionNum"]

                # 处理无感验证码 (appId: 190330343)
                try:
                    captcha_result = self.captcha_handler.handle_exam_captcha(user_exam_plan_id)
                    check_res = self.api.exam_check(
                        user_exam_plan_id,
                        captcha_result["randstr"],
                        captcha_result["ticket"],
                    )
                    if check_res.get("code", -1) != "0":
                        self.log.error(f"无感验证码校验失败：{check_res}")
                        continue
                    self.log.success("无感验证码校验通过")
                except Exception as e:
                    self.log.error(f"无感验证码处理异常: {e}")
                    continue

                # 获取考试题目
                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if exam_paper.get("code", -1) != "0":
                    self.log.error(f"获取考试题目失败：{exam_paper}")
                    if exam_paper.get("detailCode") == "10018":
                        self.log.warning(f"考试项目 {project['projectName']}/{plan['examPlanName']} 需要手动处理，请在网站上开启一次考试后重试")
                    continue
                exam_paper = exam_paper.get("data", {})
                question_list = exam_paper.get("questionList", [])
                have_answer = []  # 有答案的题目
                no_answer = []  # 无答案的题目

                for question in question_list:
                    if clean_text(question["title"]) in answers_json:
                        have_answer.append(question)
                    else:
                        no_answer.append(question)

                self.log.info(f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，无答案的题目数：{len(no_answer)}")
                # correct_rate = len(have_answer) / question_num
                # if correct_rate < 0.9:
                #     self.log.warning(f"题库正确率 {correct_rate} 少于 90%，是否继续考试？（Y/n）")
                #     if input().lower() == "n":
                #         self.log.error(f"用户取消")
                #         continue

                for i, question in enumerate(no_answer):
                    self.log.info(f"[{i}/{len(no_answer)}]题目不在题库中或选项不同，请手动选择答案")
                    print(f"题目类型：{question['typeLabel']}，题目标题：{question['title']}")
                    for j, opt in enumerate(question["optionList"]):
                        print(f"{j + 1}. {opt['content']}")

                    opt_count = len(question["optionList"])
                    start_time = time.time()
                    answers_ids = []

                    while not answers_ids:
                        with self._stdin_lock:
                            answer = input(f"[{self.api.user.get('realName', '未知')}] 请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）：").replace(" ", "").replace("，", ",")
                        candidates = [ans.strip() for ans in answer.split(",") if ans.strip()]
                        if all(ans.isdigit() and 1 <= int(ans) <= opt_count for ans in candidates):
                            answers_ids = [question["optionList"][int(ans) - 1]["id"] for ans in candidates]
                            for ans in candidates:
                                self.log.info(f"选择答案：{ans}，内容：{question['optionList'][int(ans)-1]['content']}")
                        else:
                            self.log.error("输入无效，请重新输入（序号需为数字且在选项范围内）")

                    self.log.info(f"正在提交当前答案")
                    end_time = time.time()
                    if not self.record_answer(user_exam_plan_id, question["id"], round(end_time - start_time), answers_ids, exam_plan_id):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                self.log.info(f"手动答题结束，开始答题库中的题目，共 {len(have_answer)} 道题目")
                for i, question in enumerate(have_answer):
                    self.log.info(f"[{i}/{len(have_answer)}]题目在题库中，开始答题")
                    self.log.info(f"题目类型：{question['typeLabel']}，题目标题：{question['title']}")
                    answers = answers_json[clean_text(question["title"])]
                    answers_ids = [option["id"] for option in question["optionList"] if clean_text(option["content"]) in answers]
                    self.log.info(f"等待 {per_time} 秒，模拟答题中...")
                    time.sleep(per_time)
                    if not self.record_answer(user_exam_plan_id, question["id"], per_time + 1, answers_ids, exam_plan_id):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")
                self.log.info(f"完成考试，正在提交试卷...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if submit_res.get("code", -1) != "0":
                    raise RuntimeError(f"提交试卷失败，请重新考试：{submit_res}")
                self.log.success(f"试卷提交成功，考试完成，成绩：{submit_res['data']['score']} 分")

    @staticmethod
    def get_source_str(query: dict) -> str:
        """从 URL 参数推断 sourceStr，与 JS 逻辑一致"""
        weiban = query.get("weiban", [None])[0]
        lyra = query.get("lyra", [None])[0]
        source = query.get("source", [None])[0]
        if weiban != "weiban":
            return "LYRA" if lyra == "lyra" else "PROTEUS"
        elif source == "moon":
            return "MOON"
        return "WEIBAN"

    def parse_item_js(self, course_code: str) -> Dict[str, Any]:
        """
        解析课程的 item.js，提取 nonstrMap 和页面信息
        nonstr_map 从 item.js 提取，page_count 从课程 HTML 提取（更准确）
        :param course_code: 课程代码（如 DA0309018）
        :return: {"nonstr_map": {step: str}, "has_exam": bool, "page_count": int}
        """
        result = {"nonstr_map": {}, "has_exam": False, "page_count": 0}
        try:
            url = f"https://mcwk.mycourse.cn/course/{course_code}/js/item.js"
            resp = self.api.session.get(url, timeout=10)
            if resp.status_code != 200:
                return result
            content = resp.text
            # 提取 nonstrMap
            match = re.search(r'const\s+nonstrMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)', content)
            if match:
                entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', match.group(1))
                result["nonstr_map"] = {int(step): val for step, val in entries}
            # 检查是否有课后习题
            result["has_exam"] = "saveExamQuestion" in content or "listQuestions" in content

            # --- 提取页面数 ---
            # 从课程 HTML 中提取 .page-N 类名（最准确，HTML 包含所有页面）
            # 页面编号从 0 开始，page_count = max(N)
            html_url = f"https://mcwk.mycourse.cn/course/{course_code}/{course_code}.html"
            html_resp = self.api.session.get(html_url, timeout=10)
            if html_resp.status_code == 200:
                html_pages = re.findall(r'page-(\d+)', html_resp.text)
                if html_pages:
                    result["page_count"] = max(int(p) for p in html_pages)

        except Exception as e:
            self.log.warning(f"解析 item.js 失败：{e}")
        return result

    def handle_apinext(self, user_course_id: str, course_id: str, user_project_id: str, nonstr_map: Dict[int, str], total_step: int, unique_no: str = None, finish: int = 2) -> str:
        """
        调用 apinext 接口模拟翻页学习过程
        :param user_course_id: 用户课程 ID
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :param nonstr_map: 从 item.js 提取的 nonstrMap（稀疏映射，仅部分 step 有 nonstr）
        :param total_step: 总步数（finish=2 的次数；finish=1 会发 total_step + 1）
        :param unique_no: 复用的 UUID，为 None 时自动生成
        :param finish: 2=中间步骤, 1=完成（仅发送 finish=1）
        :return: UUID（用于 finish 接口）
        """
        if unique_no is None:
            unique_no = str(uuid4())
        if not total_step:
            return unique_no
        if finish == 2:
            # 发送中间步骤（模拟翻页）
            self.log.info(f"apinext 开始发送中间步骤，共 {total_step} 步，uniqueNo={unique_no[:8]}...")
            for step in range(1, total_step + 1):
                nonstr = nonstr_map.get(step, "")
                try:
                    self.api.apinext(user_course_id, course_id, user_project_id, step=step, finish=2, nonstr=nonstr, unique_no=unique_no)
                    self.log.info(f"apinext [{step}/{total_step}] finish=2 已发送")
                except Exception as e:
                    self.log.warning(f"apinext [{step}/{total_step}] finish=2 失败：{e}")
                time.sleep(0.5)
            self.log.info(f"apinext 中间步骤全部发送完毕")
        else:
            # 发送完成标记（finish=1），step = total_step + 1
            self.log.info(f"apinext 发送完成标记 finish=1，step={total_step + 1}，uniqueNo={unique_no[:8]}...")
            try:
                self.api.apinext(user_course_id, course_id, user_project_id, step=total_step + 1, finish=1, nonstr="", unique_no=unique_no)
                self.log.info("apinext 完成标记已发送")
            except Exception as e:
                self.log.warning(f"apinext 完成请求失败：{e}")
            time.sleep(2)
        return unique_no

    def handle_exam_questions(self, course_id: str, answers_json: Dict, course_prefix: str, source: str = "WEIBAN") -> None:
        """
        处理课后习题
        先查题库，没有则提交一次获取 answerLabel，再用正确答案重新提交
        :param course_id: 课程 ID（resourceId UUID）
        :param answers_json: 题库数据
        :param course_prefix: 课程名称前缀（用于日志）
        :param source: 来源标识（WEIBAN/LYRA/PROTEUS/MOON）
        """
        try:
            res = self.api.list_question(course_id)
            data = res.get("data", {})
            exam_list = data.get("examQuestionList", [])
            if not exam_list:
                return
            self.log.info(f"{course_prefix} 发现 {len(exam_list)} 道课后习题")
            for i, question in enumerate(exam_list):
                title = clean_text(question.get("title", ""))
                question_id = question.get("id", "")
                option_list = question.get("optionList", [])
                if not option_list:
                    continue

                # 先从题库查找答案
                answer_ids = []
                if title in answers_json:
                    correct_contents = answers_json[title]
                    answer_ids = [opt["id"] for opt in option_list if clean_text(opt["content"]) in correct_contents]

                if answer_ids:
                    self.api.save_exam_question(course_id, question_id, json.dumps(answer_ids), source)
                    self.log.info(f"{course_prefix} 习题 {i+1}/{len(exam_list)} 题库命中，已作答")
                else:
                    # 题库没有，先提交第一个选项获取 answerLabel
                    wrong_answer = [option_list[0]["id"]]
                    res = self.api.save_exam_question(course_id, question_id, json.dumps(wrong_answer), source)
                    answer_label = res.get("data", {}).get("answerLabel", "")
                    if answer_label:
                        correct_letters = set()
                        for ch in answer_label.replace("-", ""):
                            if ch.isalpha():
                                correct_letters.add(ch)
                        if correct_letters:
                            letter_to_opt = {chr(65 + idx): opt for idx, opt in enumerate(option_list)}
                            answer_ids = [letter_to_opt[l]["id"] for l in correct_letters if l in letter_to_opt]
                            if answer_ids:
                                self.api.save_exam_question(course_id, question_id, json.dumps(answer_ids), source)
                                self.log.info(f"{course_prefix} 习题 {i+1}/{len(exam_list)} 通过 answerLabel 获取答案，已作答")
                                time.sleep(0.5)
                                continue
                    self.log.info(f"{course_prefix} 习题 {i+1}/{len(exam_list)} 已提交")
                time.sleep(0.5)
        except Exception as e:
            self.log.warning(f"{course_prefix} 处理课后习题失败：{e}")

    def record_answer(self, user_exam_plan_id: str, question_id: str, per_time: int, answers_ids: list, exam_plan_id: str) -> bool:
        """
        记录答题
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param per_time: 用时
        :param answers_ids: 答案 ID 列表
        :param exam_plan_id: 考试计划 ID
        :return:
        """
        res = self.api.exam_record_question(user_exam_plan_id, question_id, per_time, answers_ids, exam_plan_id)
        if res.get("code", -1) != "0":
            self.log.error(f"答题失败，请重新开启考试：{res}")
            return False
        self.log.info(f"保存答案成功")
        return True

    def sync_answers(self) -> None:
        """
        同步答案
        :return:
        """
        os.makedirs(answer_dir, exist_ok=True)
        if not os.path.exists(answer_path):
            self.log.info(f"题库不存在，正在下载...")
            with open(answer_path, "w", encoding="utf-8") as f:
                f.write(self.api.download_answer())
        try:
            with open(answer_path, encoding="utf-8") as f:
                answers_json = json.load(f)
        except Exception as e:
            self.log.error(f"读取题库失败，请重新下载题库：{e}")
            return

        user_project_ids = [p["userProjectId"] for p in self.api.list_my_project().get("data", [])]
        user_project_ids.extend([p["userProjectId"] for p in self.api.list_my_project(ended=1).get("data", [])])
        completion = self.api.list_completion()
        if completion.get("code", -1) != "0":
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [d["module"] for d in completion.get("data", []) if d["showable"] == 1]
        if "labProject" in showable_modules:
            self.log.info(f"加载实验室课程")
            lab_project = self.api.lab_index()
            if lab_project.get("code", -1) != "0":
                self.log.error(f"获取实验室课程失败：{lab_project}")
            user_project_ids.append(lab_project.get("data", {}).get("current", {}).get("userProjectId"))
        for user_project_id in user_project_ids:
            for plan in self.api.exam_list_plan(user_project_id).get("data", []):
                for history in self.api.exam_list_history(plan["examPlanId"], plan["examType"]).get("data", []):
                    questions = self.api.exam_review_paper(history["id"], history["isRetake"])["data"].get("questions", [])
                    for answer in questions:
                        title = answer["title"]
                        old_opts = {o["content"]: o["isCorrect"] for o in answers_json.get(title, {}).get("optionList", [])}
                        new_opts = old_opts | {o["content"]: o["isCorrect"] for o in answer.get("optionList", [])}
                        for content in new_opts.keys() - old_opts.keys():
                            self.log.info(f"发现题目：{title} 新选项：{content}")
                        answers_json[title] = {
                            "type": answer["type"],
                            "optionList": [{"content": content, "isCorrect": is_correct} for content, is_correct in new_opts.items()],
                        }

        with open(answer_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(answers_json, indent=2, ensure_ascii=False, sort_keys=True))
            f.write("\n")
