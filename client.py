import json
import os
import time
import webbrowser
from random import randint
from typing import Any, Dict, Optional, TYPE_CHECKING, Union
from urllib.parse import parse_qs, urlparse

from loguru import logger

from api import WeBanAPI

if TYPE_CHECKING:
    from ddddocr import DdddOcr


class WeBanClient:

    def __init__(self, account: str, password: str, tenant_name: str) -> None:
        self.tenant_code = None
        self.tenant_name = tenant_name
        self.ocr = self.get_ocr_instance()
        self.api = WeBanAPI(account, password)
        self.study_time = 15
        self.fail = []

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
        :param _cache:
        :return:
        """
        if not _cache["ocr"]:
            try:
                import ddddocr

                _cache["ocr"] = ddddocr.DdddOcr(show_ad=False)
            except ImportError:
                ddddocr = None
                logger.warning("ddddocr 库未安装，验证码识别功能将不可用，请运行 'pip install ddddocr' 进行安装以启用自动识别。")

        return _cache["ocr"]

    def get_tenant_code(self) -> str | None:
        """
        获取学校代码
        :return: code
        """
        if not self.tenant_name:
            logger.error("学校全称不能为空")
            return None
        tenant_list = self.api.get_tenant_list_with_letter()
        if tenant_list.get("code", 1) == "0":
            logger.info(f"获取学校列表成功")
        tenant_names = []
        for item in tenant_list.get("data", []):
            for entry in item.get("list", []):
                tenant_names.append(entry.get("name", ""))
                if entry.get("name", "") == self.tenant_name:
                    logger.success(f"找到学校代码: {entry['code']}")
                    self.api.set_tenant_code(entry["code"])
                    return entry["code"]
        logger.error(f"没找到你的学校代码，请检查学校全称是否正确: {self.tenant_name}\n{tenant_names}")
        return None

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
                logger.info(f"{project_prefix} 进度：必修课：{required_finished_num}/{required_num}，推送课：{push_finished_num}/{push_num}，自选课：{optional_finished_num}/{optional_num}，考试：{exam_finished_num}/{exam_num}，预计剩余时间：{eta} 秒")
        return progress

    def login(self) -> Dict | None:
        if not self.get_tenant_code():
            return None
        retry_limit = 3
        for i in range(retry_limit + 2):
            if i > 0:
                logger.info(f"登录失败，正在重试 {i}/{retry_limit+2} 次")
            verify_time = self.api.get_timestamp(13, 0)
            verify_image = self.api.rand_letter_image(verify_time)
            if i < retry_limit and self.ocr:
                verify_code = self.ocr.classification(verify_image)
                logger.info(f"自动验证码识别结果: {verify_code}")
                if len(verify_code) != 4:
                    logger.info("验证码识别失败，正在重试")
                    continue
            else:
                open("verify_code.png", "wb").write(verify_image)
                webbrowser.open(f"file://{os.path.abspath('verify_code.png')}")
                verify_code = input("请查看 verify_code.png 输入验证码：")
            res = self.api.login(verify_code, int(verify_time))
            if self.api.user:
                break
            logger.error(f"登录出错: {res}")
            break
        return self.api.user

    def run_study(self, study_time: int | None) -> None:
        if study_time:
            self.study_time = study_time
        study_task = self.api.list_study_task()
        if study_task.get("code", -1) != "0":
            logger.error(f"获取任务列表失败：{study_task}")
            return
        logger.info("获取任务列表成功")

        study_task = study_task.get("data", {})
        for task in study_task.get("studyTaskList", []):
            project_prefix = task["projectName"]
            logger.info(f"开始处理任务：{project_prefix}")

            # 获取学习进度
            self.get_progress(task["userProjectId"], project_prefix)

            # 聚合类别 1：推送课，2：自选课，3：必修课
            for choose_type in [(3, "必修课", "requiredNum", "requiredFinishedNum"), (1, "推送课", "pushNum", "pushFinishedNum"), (2, "自选课", "optionalNum", "optionalFinishedNum")]:
                categories = self.api.list_category(task["userProjectId"], choose_type[0])
                if categories.get("code") != "0":
                    logger.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    continue
                for category in categories.get("data", []):
                    category_prefix = f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    logger.info(f"开始处理 {category_prefix}")
                    if category["finishedNum"] >= category["totalNum"]:
                        logger.success(f"{category_prefix} 已完成")
                        continue

                    # 获取学习进度
                    progress = self.get_progress(task["userProjectId"], project_prefix, False)
                    if progress[choose_type[3]] >= progress[choose_type[2]]:
                        logger.info(f"{category_prefix} 已达到要求，跳过")
                        break

                    courses = self.api.list_course(task["userProjectId"], category["categoryCode"], choose_type[0])
                    for course in courses.get("data", []):
                        course_prefix = f"{category_prefix}/{course['resourceName']}"
                        # 获取学习进度
                        progress = self.get_progress(task["userProjectId"], category_prefix)
                        if progress[choose_type[3]] >= progress[choose_type[2]]:
                            logger.info(f"{category_prefix} 已达到要求，跳过")
                            break

                        logger.info(f"开始处理课程：{course_prefix}")
                        if course["finished"] == 1:
                            logger.success(f"{course_prefix} 已完成")
                            continue

                        self.api.study(course["resourceId"], task["userProjectId"])
                        course_url = self.api.get_course_url(course["resourceId"], task["userProjectId"])["data"] + "&weiban=weiban"
                        logger.info(f"等待 {self.study_time} 秒，模拟学习中...")
                        time.sleep(self.study_time)

                        if "userCourseId" not in course:
                            logger.success(f"{course_prefix} 完成")
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
                                logger.info(f"课程需要验证码，正在获取...")
                                res = self.api.invoke_captcha(course["userCourseId"], task["userProjectId"])
                                if res.get("code", -1) != "0":
                                    logger.error(f"获取验证码失败：{res}")
                                token = res.get("data", {}).get("methodToken", None)

                            res = self.api.finish_by_token(course["userCourseId"], token)

                        logger.success(f"{course_prefix} 完成 {res}")

        if len(self.fail) > 0:
            logger.warning(f"以下课程学习失败：{self.fail}")
        else:
            logger.success("所有课程学习完成")

    def run_exam(self, use_time: int = 600):
        # 加载题库
        answers_json = {}

        with open("answer/answer.json", encoding="utf-8") as f:
            for title, options in json.load(f).items():
                correct_answers = [answer["content"] for answer in options.get("optionList", []) if answer["isCorrect"] == 1]
                if correct_answers:
                    answers_json[title] = correct_answers

        # 获取项目
        projects = self.api.list_my_project()
        if projects.get("code", -1) != "0":
            logger.error(f"获取考试列表失败：{projects}")
            return

        projects = projects.get("data", [])
        for project in projects:
            if project["finished"] == 1:
                logger.success(f"考试项目 {project['projectName']} 已完成")
                continue

            logger.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]

            # 获取考试计划
            exam_plans = self.api.exam_list_plan(user_project_id)
            if exam_plans.get("code", -1) != "0":
                logger.error(f"获取考试计划失败：{exam_plans}")
                return
            exam_plans = exam_plans["data"]
            for plan in exam_plans:
                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]
                # 是否存在完成的考试记录
                before_paper = self.api.exam_before_paper(plan["id"])
                if before_paper.get("code", -1) != "0":
                    logger.error(f"获取考试记录失败：{before_paper}")
                before_paper = before_paper.get("data", {})
                if before_paper.get("isExistedNotSubmit"):
                    logger.warning(f"存在未提交的考试数据，继续将清除未提交数据（Y/n）:")
                    if input().lower() == "n":
                        logger.error("用户取消")
                        return

                # 预请求
                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if prepare_paper.get("code", -1) != "0":
                    logger.error(f"获取考试信息失败：{prepare_paper}")
                    continue
                prepare_paper = prepare_paper["data"]
                question_num = prepare_paper["questionNum"]
                logger.info(f"考试信息：用户：{prepare_paper['realName']}，ID：{prepare_paper['userIDLabel']}，题目数：{question_num}，试卷总分：{prepare_paper['paperScore']}，限时 {prepare_paper['answerTime']} 分钟")
                per_time = use_time // prepare_paper["answerTime"]

                # 检查验证码
                is_verified = False
                retry_limit = 3
                for i in range(retry_limit + 2):
                    if i > 0:
                        logger.info(f"识别失败，正在重试 {i}/{retry_limit+2} 次")
                    verify_time = self.api.get_timestamp(13, 0)
                    verify_image = self.api.rand_letter_image(verify_time)
                    if i < retry_limit and self.ocr:
                        verify_code = self.ocr.classification(verify_image)
                        logger.info(f"自动验证码识别结果: {verify_code}")
                        if len(verify_code) != 4:
                            logger.info("验证码识别失败，正在重试")
                            continue
                    else:
                        open("verify_code.png", "wb").write(verify_image)
                        webbrowser.open(f"file://{os.path.abspath('verify_code.png')}")
                        verify_code = input("请查看 verify_code.png 输入验证码：")
                    res = self.api.exam_check_verify_code(user_exam_plan_id, verify_code, int(verify_time))
                    if res.get("code") == "0":
                        logger.success("验证码正确")
                        is_verified = True
                        break
                    logger.error(f"验证码错误：{res}")
                if not is_verified:
                    logger.error("验证码错误，请重新考试")
                    continue
                logger.info("验证码正确，开始考试")

                # 获取考试题目
                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if exam_paper.get("code", -1) != "0":
                    logger.error(f"获取考试题目失败：{exam_paper}")
                    continue
                exam_paper = exam_paper.get("data", {})
                question_list = exam_paper.get("questionList", [])
                have_answer = []  # 有答案的题目
                no_answer = []  # 无答案的题目
                failed_questions = []  # 答题失败的题目

                for i, question in enumerate(question_list):
                    if question["title"] in answers_json:
                        have_answer.append(question)
                        continue
                    no_answer.append(question)

                logger.info(f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，无答案的题目数：{len(no_answer)}")
                correct_rate = len(have_answer) / question_num
                if correct_rate < 0.9:
                    logger.warning(f"题库正确率 {correct_rate} 少于 90%，是否继续考试？（y/N）")
                    if input().lower() != "y":
                        logger.error("用户取消")
                        continue

                for i, question in enumerate(no_answer):
                    logger.info(f"[{i}/{len(no_answer)}]题目不在题库中，请手动选择答案")
                    logger.info(f"题目类型：{question['typeLabel']}，题目标题：{question['title']}")
                    for j, option in enumerate(question["optionList"]):
                        logger.info(f"{j + 1}. {option['content']}")
                    answer = input("请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）：")
                    answers_ids = []
                    for ans in answer.strip().split(","):
                        ans = ans.strip()
                        if ans.isdigit() and 1 <= int(ans) <= len(question["optionList"]):
                            logger.info(f"选择答案：{ans}，内容：{question['optionList'][int(ans) - 1]['content']}")
                            answers_ids.append(question["optionList"][int(ans) - 1]["id"])
                            continue
                        logger.error(f"无效的答案序号：{ans}，跳过")
                    logger.info(f"正在提交当前答案")
                    if not self.record_answer(user_exam_plan_id, question["id"], 7, answers_ids, exam_plan_id):
                        failed_questions.append(question)
                        continue

                logger.info(f"手动答题结束，开始答题库中的题目，共 {len(have_answer)} 道题目")
                for i, question in enumerate(have_answer):
                    logger.info(f"[{i}/{len(have_answer)}]题目在题库中，开始答题")
                    logger.info(f"题目类型：{question['typeLabel']}，题目标题：{question['title']}")
                    for j, option in enumerate(question["optionList"]):
                        logger.info(f"{j + 1}. {option['content']}")
                    answers = answers_json[question["title"]]
                    logger.info(f"题库答案：{', '.join(answers)}")
                    answers_ids = []
                    for option in question["optionList"]:
                        if option["content"] in answers:
                            answers_ids.append(option["id"])
                    if not self.record_answer(user_exam_plan_id, question["id"], per_time, answers_ids, exam_plan_id):
                        failed_questions.append(question)
                        continue

                logger.info("完成考试，正在提交答案...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if submit_res.get("code", -1) != "0":
                    logger.error(f"提交答案失败，请重启考试：{submit_res}")
                    continue
                logger.success(f"答案提交成功，考试完成，成绩：{submit_res['data']['score']} 分")

    def record_answer(self, user_exam_plan_id: str, question_id: str, per_time: int, answers_ids: list, exam_plan_id: str, skip_wait: bool = True) -> bool:
        """
        记录答题
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param per_time: 用时
        :param answers_ids: 答案 ID 列表
        :param exam_plan_id: 考试计划 ID
        :param skip_wait: 是否跳过等待
        :return:
        """
        this_time = per_time + randint(-1, 1)
        if not skip_wait:
            logger.info(f"等待 {this_time-2} 秒，模拟答题中...")
            time.sleep(this_time - 2)
        res = self.api.exam_record_question(user_exam_plan_id, question_id, this_time, answers_ids, exam_plan_id)
        logger.info(f"答题结果：{res}")
        if res.get("code", -1) != "0":
            logger.error(f"答题失败，请重新开启考试")
            return False
        return True

    def sync_answers(self) -> None:
        """
        同步答案
        :return:
        """
        os.makedirs("answer", exist_ok=True)
        if not os.path.exists("answer/answer.json"):
            logger.info("题库不存在，正在下载...")
            with open("answer/answer.json", "w", encoding="utf-8") as f:
                f.write(self.api.download_answer())
        answers_json = json.load(open("answer/answer.json", encoding="utf-8"))
        for project in self.api.list_my_project().get("data", []):
            for plan in self.api.exam_list_plan(project["userProjectId"]).get("data", []):
                for history in self.api.exam_list_history(plan["examPlanId"], plan["examType"]).get("data", []):
                    questions = self.api.exam_review_paper(history["id"], history["isRetake"])["data"].get("questions", [])
                    for answer in questions:
                        title = answer["title"]
                        old_opts = {o["content"]: o["isCorrect"] for o in answers_json.get(title, {}).get("optionList", [])}
                        new_opts = old_opts | {o["content"]: o["isCorrect"] for o in answer.get("optionList", [])}
                        for content in new_opts.keys() - old_opts.keys():
                            logger.info(f"发现题目：{title} 新选项：{content}")
                        answers_json[title] = {
                            "type": answer["type"],
                            "optionList": [{"content": content, "isCorrect": is_correct} for content, is_correct in new_opts.items()],
                        }

        with open("answer/answer.json", "w", encoding="utf-8") as f:
            f.write(json.dumps(answers_json, indent=2, ensure_ascii=False, sort_keys=True))
