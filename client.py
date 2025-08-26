import json
import os
import time
import webbrowser
from random import randint
from typing import Any, Dict, Optional, TYPE_CHECKING, Union
from urllib.parse import parse_qs, urlparse

from loguru import logger
import re

from api import WeBanAPI

if TYPE_CHECKING:
    from ddddocr import DdddOcr


current_dir = os.path.dirname(os.path.abspath(__file__))
answer_dir = os.path.join(current_dir, "answer")
answer_path = os.path.join(answer_dir, "answer.json")

def clean_text(text):
    # 只保留字母、数字和汉字，自动去除所有符号和空格
    return re.sub(r"[^\w\u4e00-\u9fa5]", "", text)


class WeBanClient:

    def __init__(self, tenant_name: str, account: str | None = None, password: str | None = None, user: Dict[str, str] | None = None, log=logger) -> None:
        self.log = log
        self.tenant_name = tenant_name.strip()
        self.study_time = 15
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

    @staticmethod
    def get_ocr_instance(_cache: Dict[str, Any] = {"ocr": None}) -> Optional[Union["DdddOcr", None]]:
        """
        检查是否安装 ddddocr 库，多次调用返回同一个 DdddOcr 实例
        """
        if not _cache["ocr"]:
            try:
                import ddddocr

                _cache["ocr"] = ddddocr.DdddOcr(show_ad=False)
            except ImportError:
                ddddocr = None
                # print("ddddocr 库未安装，验证码识别功能将不可用，请运行 'pip install ddddocr' 进行安装以启用自动识别。")

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
                verify_code = self.ocr.classification(verify_image)
                self.log.info(f"自动验证码识别结果: {verify_code}")
                if len(verify_code) != 4:
                    self.log.warning(f"验证码识别失败，正在重试")
                    continue
            else:
                open("verify_code.png", "wb").write(verify_image)
                webbrowser.open(f"file://{os.path.abspath('verify_code.png')}")
                verify_code = input(f"请查看 verify_code.png 输入验证码：")
            res = self.api.login(verify_code, int(verify_time))
            if res.get("detailCode") == "67":
                self.log.warning(f"验证码识别失败，正在重试")
                continue
            if self.api.user.get("userId"):
                return self.api.user
            self.log.error(f"登录出错，请检查 config.json 内账号密码，或删除文件后重试: {res}")
            break
        return None

    def run_study(self, study_time: int | None) -> None:
        if study_time:
            self.study_time = study_time
        study_task = self.api.list_study_task()
        if study_task.get("code", -1) != "0":
            self.log.error(f"获取任务列表失败：{study_task}")
            return
        self.log.info(f"获取任务列表成功")

        study_task = study_task.get("data", {})
        for task in study_task.get("studyTaskList", []):
            project_prefix = task["projectName"]
            self.log.info(f"开始处理任务：{project_prefix}")
            need_capt = []

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
                    if category["finishedNum"] >= category["totalNum"]:
                        self.log.success(f"{category_prefix} 已完成")
                        continue

                    # 获取学习进度
                    progress = self.get_progress(task["userProjectId"], project_prefix, False)
                    if progress[choose_type[3]] >= progress[choose_type[2]]:
                        self.log.info(f"{category_prefix} 已达到要求，跳过")
                        break

                    courses = self.api.list_course(task["userProjectId"], category["categoryCode"], choose_type[0])
                    for course in courses.get("data", []):
                        course_prefix = f"{category_prefix}/{course['resourceName']}"
                        # 获取学习进度
                        progress = self.get_progress(task["userProjectId"], category_prefix)
                        if progress[choose_type[3]] >= progress[choose_type[2]]:
                            self.log.info(f"{category_prefix} 已达到要求，跳过")
                            break

                        self.log.info(f"开始处理课程：{course_prefix}")
                        if course["finished"] == 1:
                            self.log.success(f"{course_prefix} 已完成")
                            continue

                        self.api.study(course["resourceId"], task["userProjectId"])
                        course_url = self.api.get_course_url(course["resourceId"], task["userProjectId"])["data"] + "&weiban=weiban"
                        self.log.info(f"等待 {self.study_time} 秒，模拟学习中...")
                        time.sleep(self.study_time)

                        if "userCourseId" not in course:
                            self.log.success(f"{course_prefix} 完成")
                            continue

                        query = parse_qs(urlparse(course_url).query)
                        if query.get("lyra", [None])[0] == "lyra":  # 安全实训
                            res = self.api.finish_lyra(query.get("userActivityId", [None])[0])
                        elif query.get("weiban", [None])[0] != "weiban":
                            res = self.api.finish_by_token(course["userCourseId"], course_type="open")
                        elif query.get("source", [None])[0] == "moon":
                            res = self.api.finish_by_token(course["userCourseId"], course_type="moon")
                        else:
                            # 检查是否需要验证码
                            token = None
                            if query.get("csCapt", [None])[0] == "true":
                                self.log.warning(f"课程需要验证码，暂时无法处理...")
                                need_capt.append(course_prefix)
                                continue
                                res = self.api.invoke_captcha(course["userCourseId"], task["userProjectId"])
                                if res.get("code", -1) != "0":
                                    self.log.error(f"获取验证码失败：{res}")
                                token = res.get("data", {}).get("methodToken", None)

                            res = self.api.finish_by_token(course["userCourseId"], token)
                            if "ok" not in res:
                                self.log.error(f"{course_prefix} 完成失败：{res}")

                        self.log.success(f"{course_prefix} 完成")
                        
            if need_capt:
                self.log.warning(f"以下课程需要验证码，请手动完成：")
                for c in need_capt:
                    self.log.warning(f" - {c}")

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
                    self.log.success(f"考试项目 {project['projectName']}/{plan['examPlanName']} 最高成绩 {plan['examScore']} 分。已考试次数 {plan['examFinishNum']} 次，还剩 {plan['examOddNum']} 次。需要重考吗(y/N)？")
                    if input().strip().lower() != "y":
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

                # 获取考试题目
                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if exam_paper.get("code", -1) != "0":
                    self.log.error(f"获取考试题目失败：{exam_paper}")
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
                        answer = input("请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）：").replace(" ", "").replace("，", ",")
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
        for project in self.api.list_my_project().get("data", []):
            for plan in self.api.exam_list_plan(project["userProjectId"]).get("data", []):
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
