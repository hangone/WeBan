import os
import time
import webbrowser
from typing import Any, Dict, Optional, TYPE_CHECKING, Union
from urllib.parse import parse_qs, urlparse

from loguru import logger

from api import WeBanAPI

if TYPE_CHECKING:
    from ddddocr import DdddOcr


class WeBanClient:
    def __init__(self, account: str, password: str, tenant_name: str, study_time: int = 15):
        self.tenant_name = tenant_name
        self.ocr = self.get_ocr_instance()
        self.api = WeBanAPI(account, password)
        self.study_time = study_time
        self.fail = []

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

    def get_tenant_code(self, tenant_name: str) -> str | None:
        """
        获取学校代码
        :param tenant_name:
        :return: code
        """
        if not tenant_name:
            logger.error("学校全称不能为空")
            return None
        tenant_list = self.api.get_tenant_list_with_letter()
        if tenant_list.get("code", 1) == "0":
            logger.info(f"获取学校列表成功")
        for item in tenant_list.get("data", []):
            for entry in item.get("list", []):
                if entry.get("name", "") == tenant_name:
                    logger.success(f"找到学校代码: {entry.get("code", "")}")
                    self.api.set_tenant_code(entry.get("code", ""))
                    return entry.get("code", "")
        logger.error(f"没找到你的学校代码，请检查学校全称是否正确: {tenant_name}\n{tenant_list}")
        return None

    def login(self) -> Dict | None:
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
                webbrowser.open(f"file://{os.path.abspath("verify_code.png")}")
                verify_code = input("请查看 verify_code.png 输入验证码：")
            res = self.api.login(verify_code, int(verify_time))
            if self.api.user:
                break
            logger.error(f"登录出错: {res}")
        return self.api.user

    def run(self):
        logger.add("weban.log", encoding="utf-8", retention="10 days")
        logger.info("开始执行")

        if not self.get_tenant_code(self.tenant_name):
            return

        if not self.login():
            logger.error("登录失败")
            return
        logger.success("登录成功")

        study_task = self.api.list_study_task()
        if study_task.get("code") == "0":
            logger.info("获取任务列表成功")

        for task in study_task.get("data", []):
            project_prefix = task.get("projectName", "")
            logger.info(f"开始处理任务：{project_prefix}")
            categories1 = self.api.list_category(task.get("userProjectId"), 1)
            categories2 = self.api.list_category(task.get("userProjectId"), 2)
            categories3 = self.api.list_category(task.get("userProjectId"))
            categories = categories1.get("data", []) + categories2.get("data", []) + categories3.get("data", [])
            for category in categories:
                category_prefix = f"{project_prefix}/{category.get("categoryName")}"
                logger.info(f"开始处理分类 {category_prefix}")
                if category.get("finishedNum") >= category.get("totalNum"):
                    logger.success(f"{category_prefix} 已完成")
                    continue

                courses = self.api.list_course(task.get("userProjectId"), category.get("categoryCode"))
                for course in courses.get("data", []):
                    course_prefix = f"{category_prefix}/{course.get("resourceName")}"
                    logger.info(f"开始处理课程：{course_prefix}")
                    if course.get("finished") == 1:
                        logger.success(f"{course_prefix} 已完成")
                        continue

                    self.api.study(course.get("resourceId"), task.get("userProjectId"))
                    course_url = self.api.get_course_url(course.get("resourceId"), task.get("userProjectId")).get("data")
                    logger.info(f"等待 {self.study_time} 秒，模拟学习中...")
                    time.sleep(self.study_time)
                    # 检查是否需要验证码
                    query = parse_qs(urlparse(course_url).query)
                    cs_capt = query.get("csCapt", [None])[0]
                    token = None
                    if cs_capt == "true" and (res := self.api.invoke_captcha(course.get("userCourseId"), task.get("userProjectId"))).get("code") == "0":
                        token = res.get("data", {}).get("token")

                    if not self.api.finish_by_token(course.get("userCourseId"), token):
                        logger.error(f"完成课程失败：{course_prefix}")
                        self.fail.append(course.get("courseName"))
                        continue

                    logger.success(f"{course_prefix} 完成")

                    if (progress := self.api.show_progress(task.get("userProjectId"))).get("code") == "0":
                        progress = progress.get("data", {})
                        logger.info(f"{project_prefix} 总进度：{progress.get('requiredFinishedNum', 0)}/{progress.get('requiredNum', 0)}")

        if len(self.fail) > 0:
            logger.warning(f"以下课程学习失败：{self.fail}")
        else:
            logger.success("所有课程学习完成")
