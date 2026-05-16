import hashlib
import json
import time
from base64 import b64encode, urlsafe_b64decode, urlsafe_b64encode
from random import randint
from typing import Any, Dict
from uuid import uuid4

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from requests.adapters import HTTPAdapter, Retry


def create_retry_session(baseurl) -> requests.Session:
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
        "Referer": f"{baseurl}/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Dnt": "1",
        "Sec-Gpc": "1",
        "Sec-Fetch-Dest": "script",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site",
        "Te": "trailers",
    }
    return session


def handle_response(response: requests.Response) -> Dict[str, Any]:
    """
    处理接口响应
    :param response: 接口响应
    :return: 处理后的结果
    """
    if response.status_code != 200:
        if response.status_code == 403:
            raise PermissionError("Token 无效，不允许同时登录，请重试")
        if response.status_code == 401:
            raise PermissionError("Token 无效，请检查账号信息")
        print(f"请求失败：{response.status_code} {response.text}")
        return {}
    try:
        response_data = response.json()
    except json.JSONDecodeError:
        print(f"响应内容不是有效的 JSON：{response.text}")
        return {}
    return response_data


class WeBanAPI:

    def __init__(self, tenant_code: str | None = None, account: str | None = None, password: str | None = None, user: Dict[str, str] | None = None, timeout: int | tuple = (9.05, 15), session: requests.Session | None = None):
        self.account = account
        self.password = password
        self.tenant_code = tenant_code
        self.baseurl = "https://weiban.mycourse.cn"
        self.timeout = timeout  # 连接超时和读取超时
        self.session = session or create_retry_session(self.baseurl)
        self.user = user or {"userId": "", "token": ""}
        self.session.headers["X-Token"] = self.user["token"]

    @staticmethod
    def get_timestamp(int_len: int = 10, frac_len: int = 3) -> str:
        """
        获取当前时间戳，单位为毫秒，保留三位小数
        :param int_len: 整数部分长度
        :param frac_len: 小数部分长度
        :return:
        1234567890.123
        """
        t = str(time.time_ns())
        return f"{t[:int_len]}.{t[int_len:int_len+frac_len]}" if frac_len else t[:int_len]

    @staticmethod
    def encrypt(data) -> str:
        """
        AES加密
        :param data: json 字符串
        :return: base64 编码的加密字符串
        """
        key = urlsafe_b64decode("d2JzNTEyAAAAAAAAAAAAAA==")  # wbs512
        return urlsafe_b64encode(AES.new(key, AES.MODE_ECB).encrypt(pad(data.encode(), AES.block_size))).decode()

    def set_tenant_code(self, tenant_code: str):
        """
        设置学校代码
        :param tenant_code: 学校代码
        :return:
        """
        self.tenant_code = tenant_code

    def get_tenant_list_with_letter(self) -> Dict[str, Any]:
        """
        获取学校代码和名称列表

        :return:
        {
          "code": "0",
          "data": [
            {
              "index": "a",
              "list": [
                { "code": "0000010", "name": "安全教育" }
              ]
            }
          ],
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/login/getTenantListWithLetter.do"
        params = {"timestamp": self.get_timestamp()}
        response = self.session.post(url, params=params, timeout=self.timeout)
        return handle_response(response)

    def get_tenant_config(self, tenant_code: str | None = None) -> Dict[str, Any]:
        """
        获取学校配置

        :return:
        {
          "code": "0",
          "data": {
            "code": "0000010",
            "name": "安全教育",
            "userNamePrompt": "请输入学号",
            "passwordPrompt": "请输入学号",
            "forgetPasswordUserNamePrompt": "",
            "displayPop": 2,
            "popPrompt": "",
            "loginType": "1",
            "forgetPassword": 2,
            "customerTitle": "安全微伴",
            "customerLoginTips": "安全教育"
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/login/getTenantConfig.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": tenant_code or self.tenant_code}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def get_simple_config(self, tenant_code: str | None = None) -> Dict[str, Any]:
        """
        获取简单配置
        :param tenant_code: 学校代码
        :return:
        """
        url = f"{self.baseurl}/pharos/tenantconfig/getSimpleConfig.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": tenant_code or self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def get_help(self, tenant_code: str | None = None) -> Dict[str, Any]:
        """
        获取帮助文件
        :return:
        {
            "code": "0",
            "data": {
                "helpFileUrl": ""
            },
            "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/login/getHelp.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": tenant_code or self.tenant_code}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def rand_letter_image(self, verify_time: str | None) -> bytes:
        """
        获取验证码图片
        :return:
        images bytes
        """
        url = f"{self.baseurl}/pharos/login/randLetterImage.do"
        params = {"time": verify_time or self.get_timestamp(frac_len=0)}
        response = self.session.get(url, params=params, timeout=self.timeout)
        return response.content

    def login(self, verify_code: str, verify_time: int | None) -> Dict[str, Any]:
        """
        登录
        :param verify_code: 验证码
        :param verify_time: 验证码时间戳
        :return:
        {
          "code": "0",
          "data": {
            "token": "${uuid}",
            "userId": "${uuid}",
            "userName": "",
            "realName": "",
            "userNameLabel": "学号",
            "uniqueValue": "",
            "isBind": "1",
            "tenantCode": "0000010",
            "batchCode": "",
            "gender": 1,
            "openid": "",
            "switchGoods": 1,
            "switchDanger": 1,
            "switchNetCase": 1,
            "preBanner": "https://h.mycourse.cn/pharosfile/resources/images/projectbanner/pre.png",
            "normalBanner": "https://h.mycourse.cn/pharosfile/resources/images/projectbanner/normal.png",
            "specialBanner": "https://h.mycourse.cn/pharosfile/resources/images/projectbanner/special.png",
            "militaryBanner": "https://h.mycourse.cn/pharosfile/resources/images/projectbanner/military.png",
            "isLoginFromWechat": 2,
            "tenantName": "安全教育",
            "tenantType": 1,
            "loginSide": 1,
            "popForcedCompleted": 2,
            "showGender": 2,
            "showOrg": 2,
            "orgLabel": "院系",
            "nickName": "",
            "imageUrl": "https://resource.mycourse.cn/mercury/resources/mercury/wb/images/portrait.jpg",
            "defensePower": 60,
            "knowledgePower": 60,
            "safetyIndex": 99
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/login/login.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "keyNumber": self.account,
            "password": self.password,
            "tenantCode": self.tenant_code,
            "time": verify_time or int(self.get_timestamp(frac_len=0)),
            "verifyCode": verify_code,
        }
        encrypt_data = self.encrypt(json.dumps(data, separators=(",", ":")))
        response = self.session.post(url, params=params, data={"data": encrypt_data}, timeout=self.timeout)
        if response.json().get("data", {}).get("token", None):
            self.user = response.json()["data"]
            self.session.headers["X-Token"] = self.user["token"]
            self.password = None
        return handle_response(response)

    def list_completion(self) -> Dict[str, Any]:
        """
        获取模块
        :return:
        {
          "code": "0",
          "data": [
            {
              "module": "labProject",
              "showable": 2
            },
            {
              "module": "fireTrainingProject",
              "showable": 2
            },
            {
              "module": "trainingActivity",
              "showable": 2
            },
            {
              "module": "virtualTrainingPlace",
              "showable": 2
            },
            {
              "module": "notice",
              "showable": 0,
              "completion": {
                "marked": 2,
                "finished": 2,
                "grey": 1,
                "active": 2,
                "message": "无通知"
              }
            },
            {
              "module": "forcePassword",
              "showable": 2
            }
          ],
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/index/listCompletion.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def lab_index(self) -> Dict[str, Any]:
        """
        获取实验室模块信息
        :return:
        {
          "code": "0",
          "data": {
            "current": {
              "projectName": "2025级硕士生实验室安全教育",
              "projectImageUrl": "https://weibanstatic.mycourse.cn/pharos/resource/10000024/image/project/20250707/ae93f8d9-80b6-4047-97a6-aa344794d2ee.jpg",
              "endTime": "2025-10-12",
              "progressPet": 2,
              "userProjectId": "${uuid}",
              "projectCategory": 9,
              "projectAttribute": 3,
              "existedCertificate": 2
            },
            "projects": [{
              "projectName": "2025级硕士生实验室安全教育",
              "projectImageUrl": "https://weibanstatic.mycourse.cn/pharos/resource/10000024/image/project/20250707/ae93f8d9-80b6-4047-97a6-aa344794d2ee.jpg",
              "endTime": "2025-10-12",
              "progressPet": 2,
              "userProjectId": "${uuid}",
              "projectCategory": 9,
              "projectAttribute": 3,
              "existedCertificate": 2
            }],
            "ebookState": 1,
            "labCardState": 2,
            "ebookIsMust": 2
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/lab/index.do"
        params = {"timestamp": self.get_timestamp(10, 1)}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def list_study_task(self) -> Dict[str, Any]:
        """
        获取学习任务列表
        :return:
        {
          "code": "0",
          "data": {
            "studyTaskList": [
              {
                "projectId": "${uuid}",
                "projectName": "2025年春季安全教育",
                "projectImageUrl": "",
                "endTime": "2025-05-31",
                "finished": 2,
                "progressPet": 5,
                "exceedPet": 46,
                "assessment": "完成进度达到100%视为完成",
                "userProjectId": "${uuid}",
                "projectMode": 1,
                "projectCategory": 9,
                "projectAttribute": 1,
                "studyState": 5,
                "studyStateLabel": "未完成",
                "certificateAcquired": 2,
                "completion": {
                  "marked": 1,
                  "finished": 2,
                  "grey": 2,
                  "active": 1,
                  "message": ""
                }
              }
            ],
            "indexShowType": 2
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/index/listStudyTask.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def list_my_project(self, ended: int = 2) -> Dict[str, Any]:
        """
        获取我的项目列表，和 list_study_task 几乎相同
        :param ended: 1:进行中 2:已结束
        :return:
        {
          "code": "0",
          "data": [
            {
              "projectId": "${uuid}",
              "projectName": "2025年春季安全教育",
              "projectImageUrl": "",
              "endTime": "2025-05-31",
              "finished": 2,
              "progressPet": 5,
              "exceedPet": 46,
              "assessment": "完成进度达到100%视为完成",
              "userProjectId": "${uuid}",
              "projectMode": 1,
              "projectCategory": 9,
              "projectAttribute": 1,
              "studyState": 5,
              "studyStateLabel": "未完成",
              "certificateAcquired": 2
            }
          ],
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/index/listMyProject.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"], "ended": ended}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def show_progress(self, user_project_id: str) -> Dict[str, Any]:
        """
        获取学习任务进度
        :param user_project_id: 用户项目ID
        :return:
        {
          "code": "0",
          "data": {
            "name": "2025年春季安全教育",
            "pushNum": 0,
            "pushFinishedNum": 0,
            "optionalNum": 0,
            "optionalFinishedNum": 0,
            "requiredNum": 100,
            "requiredFinishedNum": 6,
            "examNum": 1,
            "examFinishedNum": 0,
            "examAssessmentNum": 1,
            "endTime": "2025-05-31 00:00:00",
            "ended": 2,
            "lastDays": 31,
            "progressPet": 5,
            "finished": 2,
            "imageUrl": "",
            "studyRank": 0,
            "assessment": "完成进度达到100%视为完成",
            "assessmentRemark": "(完成课程占进度条的80%，考试通过占进度条的20%)",
            "existedExam": 1,
            "existedOptionalCourse": 2
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/project/showProgress.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def list_valve(self) -> Dict[str, Any]:
        """获取项目页功能开关，浏览器进入项目页时会调用。"""
        url = f"{self.baseurl}/pharos/index/listValve.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def get_next_task(self, user_project_id: str) -> Dict[str, Any]:
        """获取项目下一步状态，浏览器进入项目页时会调用。"""
        url = f"{self.baseurl}/pharos/project/getNextTask.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def get_project_simple(self, user_project_id: str) -> Dict[str, Any]:
        """获取项目基础模式信息，浏览器进入项目页时会调用。"""
        url = f"{self.baseurl}/pharos/project/getSimple.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def list_category(self, user_project_id: str, choose_type: int = 3) -> Dict[str, Any]:
        """
        获取课程分类列表
        :param user_project_id: 用户项目ID
        :param choose_type: PushCourse(1,"推送课"),OptionalCourse(2,"自选课"),RequiredCourse(3,"必修课")
        :return:
        {
          "code": "0",
          "data": [
            {
              "categoryCode": "101001001",
              "categoryName": "国家安全各个方面",
              "categoryRemark": "国家安全是国家的基本利益，是一个国家处于没有危险的客观状态。本系列从保密、反间谍、反邪教、国情教育等方面介绍了国家安全知识。",
              "totalNum": 11,
              "finishedNum": 6,
              "categoryImageUrl": "https://jxstatic.mycourse.cn/image/category/20210929/8557a267-c38d-4eb3-81c6-d4d55637c068.jpg"
            }
          ]
        }
        """
        url = f"{self.baseurl}/pharos/usercourse/listCategory.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
            "chooseType": choose_type,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def list_course(self, user_project_id: str, category_code: str, choose_type: int = 3) -> Dict[str, Any]:
        """
        获取课程列表
        :param user_project_id: 用户项目ID
        :param category_code: 课程分类代码
        :param choose_type: PushCourse(1,"推送课"),OptionalCourse(2,"自选课"),RequiredCourse(3,"必修课")
        :return:
        {
          "code": "0",
          "data": [
            {
              "userCourseId": "${uuid}",
              "resourceId": "${uuid}",
              "resourceName": "扫黑除恶应知应会知识(上）",
              "finished": 2,
              "isPraise": 2,
              "isShare": 2,
              "praiseNum": 36844,
              "shareNum": 0,
              "shared": 2,
              "source": 1,
              "imageUrl": "https://jxstatic.mycourse.cn/image/microlecture/20200101/33e72c5e-f4a8-4e06-b253-6c907da76963.png",
              "categoryName": "国家安全各个方面"
            }
          ]
        }
        """
        url = f"{self.baseurl}/pharos/usercourse/listCourse.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
            "chooseType": choose_type,
            "categoryCode": category_code,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def init_index(self, user_project_id: str) -> Dict[str, Any]:
        """
        初始化课程索引（开始学习前调用，模拟浏览器行为）
        :param user_project_id: 用户项目 ID
        :return:
        {"code":"0","detailCode":"0"}
        """
        url = f"{self.baseurl}/pharos/usercourse/initIndex.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def study(self, course_id: str, user_project_id: str) -> Dict[str, Any]:
        """
        开始学习课程
        :param course_id: 课程ID
        :param user_project_id: 用户项目ID
        :return:
        {
            "code":"0",
            "detailCode":"0"
        }
        """
        url = f"{self.baseurl}/pharos/usercourse/study.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "courseId": course_id,
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def get_course_url(self, course_id: str, user_project_id: str) -> Dict[str, Any]:
        """
        获取课程链接
        :param course_id: 课程ID
        :param user_project_id: 用户项目ID
        :return:
        {
            "code":"0",
            "data":"https://mcwk.mycourse.cn/course/A11072/A11072.html?userCourseId=&tenantCode=&type=1&csComm=true&csCapt=true",
            "detailCode":"0"
        }
        """
        url = f"{self.baseurl}/pharos/usercourse/getCourseUrl.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "courseId": course_id,
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def invoke_captcha(self, user_course_id: str, user_project_id: str) -> Dict[str, Any]:
        """
        通过验证码获取完成 token
        :param user_course_id: 用户课程ID
        :param user_project_id: 用户项目ID
        :return:
        {"code":"0",data:{"methodToken",""}}
        """
        fetch_url = f"{self.baseurl}/pharos/usercourse/getCaptcha.do"
        check_url = f"{self.baseurl}/pharos/usercourse/checkCaptcha.do"
        params = {
            "userCourseId": user_course_id,
            "userProjectId": user_project_id,
            "userId": self.user["userId"],
            "tenantCode": self.tenant_code,
        }
        response = self.session.get(fetch_url, params=params, timeout=self.timeout)  # {"captcha":{"num":3,"questionId":"${uuid}","imageUrl":"${url}"}}
        params["questionId"] = handle_response(response).get("captcha", {}).get("questionId", "")
        coordinates = [{"x": x + randint(-5, 5), "y": y + randint(-5, 5)} for x, y in [(207, 436), (67, 424), (141, 427)]]
        data = {"coordinateXYs": json.dumps(coordinates, separators=(",", ":"))}
        time.sleep(3)
        response = self.session.post(check_url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def finish_by_token(self, user_course_id: str, token: str | None = None, course_type: str | None = "weiban", unique_no: str | None = None, referer: str | None = None) -> Dict[str, Any]:
        """
        通过 userCourseId 或验证码 token 完成课程
        :param user_course_id: 用户课程 ID
        :param token: 验证码 token（如有）
        :param course_type: 课程类型 weiban, open, moon
        :param unique_no: UUID，来自 apinext 接口
        :param referer: HTTP Referer 头，模拟浏览器行为
        :return:
        {"msg": "ok", "code": "0", "detailCode": "0"}
        """
        url = f"{self.baseurl}/pharos/usercourse/v2/{token or user_course_id}.do"
        data = {
            "userCourseId": user_course_id,
            "tenantCode": self.tenant_code,
        }
        if unique_no:
            data["uniqueNo"] = unique_no

        if course_type == "open":
            url = f"https://open.mycourse.cn/proteus/usercourse/finish.do"
        elif course_type == "moon":
            url = f"https://moon.mycourse.cn/moonapi/api/study/activity/microCourse/v1/finishedCourse"

        retry_delays = (3, 6, 10, 15, 20)
        for attempt in range(len(retry_delays) + 1):
            if course_type == "weiban":
                # weiban 走 jQuery JSONP（GET + callback 参数）
                ts = int(self.get_timestamp(13, 0))
                callback = f"jQuery3410{randint(10**15, 10**16 - 1)}_{ts}"
                params = {**data, "callback": callback, "_": ts + 1}
                response = self.session.get(url, params=params, timeout=self.timeout)
            else:
                response = self.session.post(url, data=data, timeout=self.timeout)
            try:
                result = response.json()
            except json.JSONDecodeError:
                text = response.text
                start = text.find("(")
                end = text.rfind(")")
                if start != -1 and end != -1:
                    text = text[start + 1:end]
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": response.text}
            # 10018 = 服务端还未完成 apinext 进度落库，等待后重试完课接口。
            if result.get("detailCode") == "10018" and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            return result

    def finish_lyra(self, user_activity_id: str) -> Dict[str, Any]:
        """
        完成安全实训
        :param user_activity_id: 用户活动 ID
        :return:
        {"msg":"ok","code":"0","detailCode":"0"}
        """
        url = f"https://lyra.mycourse.cn/lyraapi/study/course/finish.api"
        data = {"userActivityId": user_activity_id}
        response = self.session.post(url, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_list_plan(self, user_project_id: str) -> Dict[str, Any]:
        """
        获取考试计划列表
        :param user_project_id: 用户课程 ID
        :return:
        {
          "code": "0",
          "data": [
            {
              "id": "${uuid}",
              "examPlanId": "${uuid}",
              "examPlanName": "结课考试",
              "answerNum": 3,
              "answerTime": 60,
              "passScore": 80,
              "isRetake": 2,
              "examType": 2,
              "isAssessment": 1,
              "startTime": "2025-03-01 00:00:00",
              "endTime": "2025-04-31 23:59:59",
              "examFinishNum": 1,
              "examOddNum": 2,
              "examScore": 100,
              "examTimeState": 2,
              "displayState": 1,
              "prompt": ""
            }
          ],
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/listPlan.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userProjectId": user_project_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_before_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """
        获取是否有未提交的答案
        :param user_exam_plan_id: 用户考试计划 ID
        :return:
        {
          "code": "0",
          "data": {
            "isExistedNotSubmit": false
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/beforePaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_prepare_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """
        准备考试
        :param user_exam_plan_id: 用户考试计划 ID
        :return:
        {
          "code": "0",
          "data": {
            "realName": "张三",
            "userIDLabel": "学号：",
            "questionNum": 50,
            "paperScore": 100,
            "answerTime": 60
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/preparePaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_check(self, user_exam_plan_id: str, randstr: str, ticket: str) -> Dict[str, Any]:
        """
        无感验证码校验（考试前）—— appId: 190330343
        调用 exam/check.do 验证，成功后才可以 exam_start_paper
        :param user_exam_plan_id: 用户考试计划 ID
        :param randstr: 腾讯验证码 randstr
        :param ticket: 腾讯验证码 ticket
        :return:
        {"code":"0","detailCode":"0"}
        """
        url = f"{self.baseurl}/pharos/exam/check.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
            "randstr": randstr,
            "ticket": ticket,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def course_check(self, user_course_id: str, user_project_id: str, course_id: str, randstr: str, ticket: str) -> Dict[str, Any]:
        """
        验证码校验（课程完成时）—— appId: 195119536
        调用 pharos/usercourse/check.do 获取完课 token
        :param user_course_id: 用户课程 ID
        :param user_project_id: 用户项目 ID
        :param course_id: 课程 ID
        :param randstr: 腾讯验证码 randstr
        :param ticket: 腾讯验证码 ticket
        :return:
        {"code":"0","data":"${token}","detailCode":"0"}
        """
        url = f"{self.baseurl}/pharos/usercourse/check.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userCourseId": user_course_id,
            "userProjectId": user_project_id,
            "courseId": course_id,
            "randstr": randstr,
            "ticket": ticket,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_check_verify_code(self, user_exam_plan_id: str, verfy_code: str, verify_time: int | None) -> Dict[str, Any]:
        """
        检查考试验证码
        :param user_exam_plan_id: 用户考试计划 ID
        :param verfy_code: 验证码
        :param verify_time: 验证码 13 位时间戳
        :return:
        {
          "code": "0",
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/checkVerifyCode.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "time": verify_time or int(self.get_timestamp(frac_len=0)),
            "userExamPlanId": user_exam_plan_id,
            "verifyCode": verfy_code,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_start_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """
        开始考试
        :param user_exam_plan_id: 用户考试计划 ID
        :return:
        {
          "code": "0",
          "data": {
            "answerTime": 60,
            "questionList": [
              {
                "id": "${uuid}",
                "title": "小玲有辆最高时速40公里每小时的电动自行车，按照这个时速上路，如果遇到事故，极有可能被认定为（     ）追责。",
                "type": 1,
                "typeLabel": "单选题",
                "score": 2,
                "sequence": 0,
                "isRight": 0,
                "optionList": [
                  {
                    "id": "${uuid}",
                    "questionId": "${uuid}",
                    "content": "机动车",
                    "sequence": 1,
                    "selected": 2,
                    "attachmentList": []
                  },
                  {
                    "id": "${uuid}",
                    "questionId": "${uuid}",
                    "content": "非机动车",
                    "sequence": 2,
                    "selected": 2,
                    "attachmentList": []
                  },
                  {
                    "id": "${uuid}",
                    "questionId": "${uuid}",
                    "content": "行人",
                    "sequence": 3,
                    "selected": 2,
                    "attachmentList": []
                  },
                  {
                    "id": "${uuid}",
                    "questionId": "${uuid}",
                    "content": "残疾人用车",
                    "sequence": 4,
                    "selected": 2,
                    "attachmentList": []
                  }
                ],
                "attachmentList": []
              }
            ]
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/startPaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_record_question(self, user_exam_plan_id: str, question_id: str, use_time: int, answer_ids: list | None, exam_plan_id: str) -> Dict[str, Any]:
        """
        记录考试答案
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param use_time: 本题用时，单位为秒
        :param answer_ids: 答案 ID, 列表形式
        :param exam_plan_id: 考试计划 ID
        :return:
        {
          "code": "0",
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/recordQuestion.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
            "questionId": question_id,
            "useTime": use_time,
            "examPlanId": exam_plan_id,
        }
        if answer_ids:
            data["answerIds"] = ",".join(answer_ids)
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_submit_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """
        提交考试
        :param user_exam_plan_id: 用户考试计划 ID
        :return:
        {
          "code": "0",
          "data": {
            "score": 100,
            "redpacketInfo": {
              "redpacketName": "",
              "redpacketComment": "",
              "redpacketMoney": 0.0,
              "isSendRedpacket": 2
            },
            "ebookInfo": { "displayBook": 2 }
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/submitPaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_fresh_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """
        重置考试题目
        :param user_exam_plan_id: 用户考试计划 ID
        :return:
        {
          "code": "0",
          "data": {
            "answerTime": 56,
            "questionList": [
              {
                "id": "536a43c6-c6f4-4fbf-97f5-a64e53cb813c",
                "title": "昏厥的病人不能随意搬动，但可适当挪动头部来保持病人呼吸通畅。",
                "type": 1,
                "typeLabel": "单选题",
                "score": 2,
                "sequence": 0,
                "isRight": 0,
                "optionList": [
                  {
                    "id": "afab5aea-2cab-46c2-b3cc-6009c6b6de55",
                    "questionId": "536a43c6-c6f4-4fbf-97f5-a64e53cb813c",
                    "content": "对。",
                    "sequence": 1,
                    "selected": 1,
                    "attachmentList": []
                  },
                  {
                    "id": "2913e6b8-51a8-4716-a2d9-9d019ef900da",
                    "questionId": "536a43c6-c6f4-4fbf-97f5-a64e53cb813c",
                    "content": "错。",
                    "sequence": 2,
                    "selected": 2,
                    "attachmentList": []
                  }
                ],
                "attachmentList": []
              }
            ]
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/freshPaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamPlanId": user_exam_plan_id,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_review_paper(self, user_exam_id: str, is_retake: int = 2) -> Dict[str, Any]:
        """
        查看考试结果
        :param user_exam_id: 用户考试 ID
        :param is_retake: 是否重考，1：是，2：否
        :return:
        {
          "code": "0",
          "data": {
            "submitTime": "2025-05-19 01:59:37",
            "score": 100,
            "useTime": 526,
            "questions": [
              {
                "title": "题目",
                "type": 1,
                "typeLabel": "单选题",
                "score": 2,
                "sequence": 0,
                "analysis": "",
                "isRight": 1,
                "optionList": [
                  {
                    "content": "正确。",
                    "sequence": 1,
                    "selected": 1,
                    "isCorrect": 1,
                    "attachmentList": []
                  },
                  {
                    "content": "错误。",
                    "sequence": 2,
                    "selected": 2,
                    "isCorrect": 2,
                    "attachmentList": []
                  }
                ],
                "attachmentList": []
              }
            ]
          },
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/reviewPaper.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "userExamId": user_exam_id,
            "isRetake": is_retake,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def exam_list_history(self, exam_plan_id: str, exam_type: int) -> Dict[str, Any]:
        """
        获取考试历史记录
        :param exam_plan_id: 考试计划 ID
        :param exam_type: 考试类型
        :return:
        {
          "code": "0",
          "data": [
            {
              "id": "${uuid}",
              "examPlanId": "${uuid}",
              "examPlanName": "结课考试",
              "answerNum": 5,
              "answerTime": 60,
              "passScore": 80,
              "isRetake": 2,
              "examType": 2,
              "isAssessment": 1,
              "startTime": "2025-02-21 00:00:00",
              "endTime": "2025-02-26 23:59:59",
              "examFinishNum": 1,
              "examOddNum": 4,
              "examScore": 86,
              "examTimeState": 3,
              "displayState": 1,
              "prompt": ""
            }
          ],
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/exam/listHistory.do"
        params = {"timestamp": self.get_timestamp()}
        data = {
            "tenantCode": self.tenant_code,
            "userId": self.user["userId"],
            "examPlanId": exam_plan_id,
            "examType": exam_type,
        }
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def download_answer(self) -> str:
        """
        下载最新题库
        :return:
        {
          "(       )是麻醉诱导常用的药物之一。": {
            "optionList": [
              {
                "content": ""依托咪酯"",
                "isCorrect": 1
              }
            ],
            "type": 1
          }
        }
        """
        return self.session.get(f"https://ghfast.top/https://github.com/hangone/WeBan/raw/refs/heads/main/answer/answer.json", timeout=self.timeout).text

    def apinext(self, user_course_id: str, course_id: str, user_project_id: str, step: int = 0, finish: int = 2, nonstr: str = "", unique_no: str | None = None) -> Dict[str, Any]:
        """
        学习进度追踪接口（apinext）
        部分课程需要调用此接口记录翻页和完成状态
        :param user_course_id: 用户课程 ID
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :param step: 步骤编号
        :param finish: 1=完成, 2=翻页中
        :param nonstr: 从 item.js 的 nonstrMap 中提取的字符串
        :param unique_no: UUID，同一课程应保持一致
        :return:
        {
          "code": 200,
          "message": "操作成功",
          "status": 200,
          "data": true,
          "v": "Resp",
          "success": true
        }
        """
        data = {
            "userCourseId": user_course_id,
            "uniqueNo": unique_no or str(uuid4()),
            "userId": self.user["userId"],
            "courseId": course_id,
            "userProjectId": user_project_id,
            "finished": finish,
            "step": step,
            "nonstr": nonstr,
            "tenantCode": self.tenant_code,
        }
        key = b"KkGv9d8E5jYb2xHwL3ZqRpXoNt6MmSge"
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded = pad(json.dumps(data, separators=(",", ":")).encode(), AES.block_size)
        # JS: Base64(Utf8.parse(Base64(ciphertext))) = 双重 Base64 编码
        encrypted_b64 = b64encode(b64encode(cipher.encrypt(padded))).decode()
        url = f"{self.baseurl}/jupiterapi/api/statusercourse/v1/next"
        response = self.session.post(url, json={"data": encrypted_b64}, timeout=self.timeout)
        return handle_response(response)

    def _mercury_request(self, params: dict) -> Dict[str, Any]:
        """mercuryprovider 通用请求：合并标准参数、签名、发送"""
        standard = {
            "appKey": "00000001",
            "format": "json",
            "v": "1.0",
            "timestamp": self.get_timestamp(),
            "clientId": "pharos",
        }
        merged = {**standard, **params}
        secret_key = "75uet0kwvnc90xo"
        sign_str = secret_key
        for k in sorted(merged.keys()):
            sign_str += k + str(merged[k])
        sign_str += secret_key
        merged["sign"] = hashlib.sha1(sign_str.encode()).hexdigest().upper()
        response = self.session.post("https://resource.mycourse.cn/mercuryprovider/router", data=merged, timeout=self.timeout)
        return handle_response(response)

    def list_question(self, course_id: str) -> Dict[str, Any]:
        """
        获取课后习题列表（course_id 为 resourceId UUID）
        :return:
        {
          "code": "0",
          "data": {
            "viewpointQuestionList": [
              {
                "id": "${uuid}",
                "type": 2,
                "score": 0,
                "title": "你认为哪种方式更能丰富自己的小金库呢？",
                "sequence": 1,
                "optionList": [
                  {"id": "${uuid}", "content": "日息5%保本高息理财", "sequence": 1, "isCorrect": 2},
                  {"id": "${uuid}", "content": "国企内部理财", "sequence": 2, "isCorrect": 2},
                  {"id": "${uuid}", "content": "影视投资分红", "sequence": 3, "isCorrect": 2},
                  {"id": "${uuid}", "content": "都不要", "sequence": 4, "isCorrect": 2}
                ]
              }
            ],
            "examQuestionList": [
              {
                "id": "${uuid}",
                "type": 1,
                "score": 0,
                "title": "任何形式的刷单都是违法行为，这种说法正确吗？",
                "sequence": 0,
                "optionList": [
                  {"id": "${uuid}", "content": "正确", "sequence": 1, "isCorrect": 2},
                  {"id": "${uuid}", "content": "错误", "sequence": 2, "isCorrect": 2}
                ]
              }
            ]
          },
          "detailCode": "0"
        }
        """
        return self._mercury_request({"service": "mercury.microlecture.listQuestion", "id": course_id})

    def save_question(self, course_id: str, question_id: str, answers: str, source: str = "WEIBAN") -> Dict[str, Any]:
        """
        提交课中观点题答案 (mercury.microlecture.saveQuestion)
        :return:
        {
          "code": "0",
          "data": {"isRight": 1, "analysis": "", "answerLabel": "-A"},
          "detailCode": "0"
        }
        """
        return self._mercury_request({
            "service": "mercury.microlecture.saveQuestion",
            "courseId": course_id,
            "questionId": question_id,
            "answers": answers,
            "userId": self.user["userId"],
            "tenantCode": self.tenant_code,
            "source": source,
        })

    def save_exam_question(self, course_id: str, question_id: str, answers: str, source: str = "WEIBAN") -> Dict[str, Any]:
        """
        提交课后习题答案
        :return:
        {
          "code": "0",
          "data": {
            "isRight": 1,
            "analysis": "<p>无论是刷单返利、刷信誉、刷流水...全部属于违法行为...</p>",
            "answerLabel": "-A"
          },
          "detailCode": "0"
        }
        """
        return self._mercury_request({
            "service": "mercury.microlecture.saveExamQuestion",
            "courseId": course_id,
            "questionId": question_id,
            "answers": answers,
            "userId": self.user["userId"],
            "tenantCode": self.tenant_code,
            "source": source,
        })
