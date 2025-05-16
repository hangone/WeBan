import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from random import choice, randint
from typing import Dict

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
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Te": "trailers",
    }
    return session


class WeBanAPI:

    def __init__(self, account: str, password: str, tenant_code: str | None = None, baseurl: str = "https://weiban.mycourse.cn", timeout: int | tuple = (9.05, 15), session: requests.Session | None = None):
        self.account = account
        self.password = password
        self.tenant_code = tenant_code
        self.baseurl = baseurl
        self.timeout = timeout  # 连接超时和读取超时
        self.session = session or create_retry_session(baseurl)
        self.user = None

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

    def get_tenant_list_with_letter(self) -> Dict:
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
        return response.json()

    def get_tenant_config(self, tenant_code: str) -> Dict:
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
        data = {"tenantCode": tenant_code}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return response.json()

    def get_help(self, tenant_code: str) -> Dict:
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
        data = {"tenantCode": tenant_code}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return response.json()

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

    def login(self, verify_code: str, verify_time: int | None) -> Dict:
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
        if response.json().get("data", {}).get("token"):
            self.user = response.json().get("data")
            self.session.headers["X-Token"] = self.user.get("token")
            self.password = None
        return response.json()

    def list_study_task(self) -> Dict:
        """
        获取学习任务列表
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
          "detailCode": "0"
        }
        """
        url = f"{self.baseurl}/pharos/index/listStudyTask.do"
        params = {"timestamp": self.get_timestamp()}
        data = {"tenantCode": self.tenant_code, "userId": self.user["userId"]}
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return response.json()

    def show_progress(self, user_project_id: str) -> Dict:
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
        return response.json()

    def list_category(self, user_project_id: str, choose_type: int = 3) -> Dict:
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
        return response.json()

    def list_course(self, user_project_id: str, category_code: str, choose_type: int = 3) -> Dict:
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
        return response.json()

    def study(self, course_id: str, user_project_id: str) -> Dict:
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
        return response.json()

    def get_course_url(self, course_id: str, user_project_id: str) -> Dict:
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
        return response.json()

    def invoke_captcha(self, user_course_id: str, user_project_id: str) -> Dict:
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
        params["questionId"] = response.json().get("captcha", {}).get("questionId", "")
        coordinates = [{"x": x + randint(-5, 5), "y": y + randint(-5, 5)} for x, y in [(207, 436), (67, 424), (141, 427)]]
        data = {"coordinateXYs": json.dumps(coordinates, separators=(",", ":"))}
        response = self.session.post(check_url, params=params, data=data, timeout=self.timeout)
        return response.json()

    def finish_by_token(self, user_course_id: str, token: str | None = None) -> str:
        """
        通过 userCourseId 或验证码 token 完成课程
        :param user_course_id: 用户课程 ID
        :param token: 用户课程 ID 或验证码 token
        :return:
        jQuery341002461326005930642_1747119073594({"msg":"ok","code":"0","detailCode":"0"})
        """
        url = f"{self.baseurl}/pharos/usercourse/v2/{token or user_course_id}.do"
        params = {
            "callback": f"jQuery3210{''.join(choice('123456789') for _ in range(15))}_{int(self.get_timestamp(13,0))}",
            "userCourseId": user_course_id,
            "tenantCode": self.tenant_code,
            "_": int(self.get_timestamp(13, 0)),
        }
        response = self.session.get(url, params=params, timeout=self.timeout)
        return response.text
