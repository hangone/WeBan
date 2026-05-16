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
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
        "Referer": f"{baseurl}/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Dnt": "1", "Sec-Gpc": "1",
        "Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "same-site",
        "Te": "trailers",
    }
    return session


def handle_response(response: requests.Response) -> Dict[str, Any]:
    """处理接口响应"""
    if response.status_code != 200:
        if response.status_code == 403:
            raise PermissionError("Token 无效，不允许同时登录，请重试")
        if response.status_code == 401:
            raise PermissionError("Token 无效，请检查账号信息")
        print(f"请求失败：{response.status_code} {response.text}")
        return {}
    try:
        return response.json()
    except json.JSONDecodeError:
        print(f"响应内容不是有效的 JSON：{response.text}")
        return {}


class WeBanAPI:

    # 题库下载地址（GitHub raw + ghfast.top 加速代理）
    ANSWER_URL = "https://ghfast.top/https://github.com/hangone/WeBan/raw/refs/heads/main/answer/answer.json"

    def __init__(self, tenant_code: str | None = None, account: str | None = None,
                 password: str | None = None, user: Dict[str, str] | None = None,
                 timeout: int | tuple = (9.05, 15), session: requests.Session | None = None):
        self.account = account
        self.password = password
        self.tenant_code = tenant_code
        self.baseurl = "https://weiban.mycourse.cn"
        self.timeout = timeout
        self.session = session or create_retry_session(self.baseurl)
        self.user = user or {"userId": "", "token": ""}
        self.session.headers["X-Token"] = self.user["token"]

    @staticmethod
    def get_timestamp(int_len: int = 10, frac_len: int = 3) -> str:
        """
        获取当前时间戳，单位为毫秒，默认保留三位小数
        :param int_len: 整数部分长度
        :param frac_len: 小数部分长度
        :return:
        1234567890.123
        """
        t = str(time.time_ns())
        return f"{t[:int_len]}.{t[int_len:int_len + frac_len]}" if frac_len else t[:int_len]

    @staticmethod
    def encrypt(data) -> str:
        """
        用固定密钥 wbs512+ECB 模式加密登录请求体
        :param data: json 字符串
        :return: base64 编码的加密字符串
        """
        key = urlsafe_b64decode("d2JzNTEyAAAAAAAAAAAAAA==")  # wbs512
        return urlsafe_b64encode(
            AES.new(key, AES.MODE_ECB).encrypt(pad(data.encode(), AES.block_size))
        ).decode()

    # ========================================================================
    # 核心请求辅助方法
    # ========================================================================

    def _post(self, endpoint: str, data: dict | None = None,
              timestamp_args: tuple | None = None) -> Dict[str, Any]:
        """
        通用 POST 请求，封装所有端点共用的模板代码。
        自动拼接 baseurl、timestamp 置入 query、注入 tenantCode 到 body。
        如果 userId 已设置（登录后），自动注入 userId。
        :param endpoint: 接口路径（如 "/pharos/exam/startPaper.do"）
        :param data: POST body（dict），会自动补 tenantCode/userId
        :param timestamp_args: 传递给 get_timestamp 的参数，用以控制 ts 小数位数长度（如 (10,1)）
        :return: 接口返回的 JSON dict
        """
        url = f"{self.baseurl}{endpoint}"
        if timestamp_args:
            ts = self.get_timestamp(*timestamp_args)
        else:
            ts = self.get_timestamp()
        params = {"timestamp": ts}
        if data is None:
            data = {}
        data.setdefault("tenantCode", self.tenant_code)
        if self.user.get("userId"):
            data.setdefault("userId", self.user["userId"])
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def _mercury_request(self, params: dict) -> Dict[str, Any]:
        """
        mercuryprovider 通用请求。会将 appKey/format/v/timestamp/clientId 标准参数与传入 params 合并，
        按 key 字母序拼接成 sign_str，用固定密钥 75uet0kwvnc90xo 做包装式 SHA1 签名。
        :param params: service/id 等业务参数 dict
        :return: mercuryprovider 返回的 JSON
        """
        standard = {
            "appKey": "00000001",
            "format": "json",
            "v": "1.0",
            "timestamp": self.get_timestamp(),
            "clientId": "pharos",
        }
        merged = {**standard, **params}
        secret_key = "75uet0kwvnc90xo"
        # 签名算法：secret + 按 key 升序拼接 keyvalue + secret，再做 SHA1 转大写
        sign_str = secret_key
        for k in sorted(merged.keys()):
            sign_str += k + str(merged[k])
        sign_str += secret_key
        merged["sign"] = hashlib.sha1(sign_str.encode()).hexdigest().upper()
        response = self.session.post(
            "https://resource.mycourse.cn/mercuryprovider/router",
            data=merged, timeout=self.timeout)
        return handle_response(response)

    # ========================================================================
    # 登录相关
    # ========================================================================

    def set_tenant_code(self, tenant_code: str):
        """设置学校代码
        :param tenant_code: 学校代码
        :return: 学校代码
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
        response = self.session.post(url, params={"timestamp": self.get_timestamp()},
                                     timeout=self.timeout)
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
        :return: 简单配置 dict
        """
        return self._post("/pharos/tenantconfig/getSimpleConfig.do",
                          {"tenantCode": tenant_code or self.tenant_code})

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
        return self._post("/pharos/login/getHelp.do",
                          {"tenantCode": tenant_code or self.tenant_code})

    def rand_letter_image(self, verify_time: str | None) -> bytes:
        """获取验证码图片
        :param verify_time: 验证码时间戳
        :return: 验证码图片字节
        """
        url = f"{self.baseurl}/pharos/login/randLetterImage.do"
        params = {"time": verify_time or self.get_timestamp(frac_len=0)}
        response = self.session.get(url, params=params, timeout=self.timeout)
        return response.content

    def login(self, verify_code: str, verify_time: int | None) -> Dict[str, Any]:
        """
        登录，请求体经 AES-ECB 加密后发送。成功后自动将 token、userId 存入 self.user 并更新 X-Token 头。
        :param verify_code: 用户输入的验证码
        :param verify_time: 验证码获取时间戳（秒级整数）
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
        payload = {
            "keyNumber": self.account,
            "password": self.password,
            "tenantCode": self.tenant_code,
            "time": verify_time or int(self.get_timestamp(frac_len=0)),
            "verifyCode": verify_code,
        }
        encrypted = self.encrypt(json.dumps(payload, separators=(",", ":")))
        response = self.session.post(
            f"{self.baseurl}/pharos/login/login.do",
            params={"timestamp": self.get_timestamp()},
            data={"data": encrypted}, timeout=self.timeout)
        result = handle_response(response)
        if result.get("data", {}).get("token"):
            self.user = result["data"]
            self.session.headers["X-Token"] = self.user["token"]
            self.password = None
        return result

    # ========================================================================
    # 首页 / 项目
    # ========================================================================

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
        return self._post("/pharos/index/listCompletion.do")

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
        # 该端点要求 timestamp 带 1 位小数（如 1234567890.1）
        return self._post("/pharos/lab/index.do", timestamp_args=(10, 1))

    def list_study_task(self) -> Dict[str, Any]:
        """获取学习任务列表
        :return: 学习任务列表 dict
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
        return self._post("/pharos/index/listStudyTask.do")

    def list_my_project(self, ended: int = 2) -> Dict[str, Any]:
        """
        获取我的项目列表。
        :param ended: 1=已结束, 2=未结束/进行中（默认 2）
        :return: 项目列表 dict，data 字段为项目数组
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
        return self._post("/pharos/index/listMyProject.do", {"ended": ended})

    def show_progress(self, user_project_id: str) -> Dict[str, Any]:
        """获取学习任务进度
        :param user_project_id: 用户项目 ID
        :return: 进度 dict
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
        return self._post("/pharos/project/showProgress.do",
                          {"userProjectId": user_project_id})

    def list_valve(self) -> Dict[str, Any]:
        """获取项目页功能开关
        :return: 功能开关 dict
        """
        return self._post("/pharos/index/listValve.do")

    def get_next_task(self, user_project_id: str) -> Dict[str, Any]:
        """获取项目下一步状态
        :param user_project_id: 用户项目 ID
        :return: 下一步状态 dict
        """
        return self._post("/pharos/project/getNextTask.do",
                          {"userProjectId": user_project_id})

    def get_project_simple(self, user_project_id: str) -> Dict[str, Any]:
        """获取项目基础模式信息
        :param user_project_id: 用户项目 ID
        :return: 项目基础信息 dict
        """
        return self._post("/pharos/project/getSimple.do",
                          {"userProjectId": user_project_id})

    # ========================================================================
    # 课程
    # ========================================================================

    def list_category(self, user_project_id: str, choose_type: int = 3) -> Dict[str, Any]:
        """获取课程分类列表
        :param user_project_id: 用户项目 ID
        :param choose_type: 课程类型（1=推送课, 2=自选课, 3=必修课）
        :return: 课程分类列表 dict
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
        return self._post("/pharos/usercourse/listCategory.do",
                          {"userProjectId": user_project_id, "chooseType": choose_type})

    def list_course(self, user_project_id: str, category_code: str,
                    choose_type: int = 3) -> Dict[str, Any]:
        """获取课程列表
        :param user_project_id: 用户项目 ID
        :param category_code: 分类代码
        :param choose_type: 课程类型（1=推送课, 2=自选课, 3=必修课）
        :return: 课程列表 dict
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
        return self._post("/pharos/usercourse/listCourse.do",
                          {"userProjectId": user_project_id, "chooseType": choose_type,
                           "categoryCode": category_code})

    def init_index(self, user_project_id: str) -> Dict[str, Any]:
        """初始化课程索引（开始学习前调用，模拟浏览器行为）
        :param user_project_id: 用户项目 ID
        :return: 初始化结果 dict
        {"code":"0","detailCode":"0"}
        """
        return self._post("/pharos/usercourse/initIndex.do",
                          {"userProjectId": user_project_id})

    def study(self, course_id: str, user_project_id: str) -> Dict[str, Any]:
        """开始学习课程
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :return: 学习结果 dict
        {
            "code":"0",
            "detailCode":"0"
        }
        """
        return self._post("/pharos/usercourse/study.do",
                          {"courseId": course_id, "userProjectId": user_project_id})

    def get_course_url(self, course_id: str, user_project_id: str) -> Dict[str, Any]:
        """获取课程链接
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :return: 课程链接 dict
        {
            "code":"0",
            "data":"https://mcwk.mycourse.cn/course/A11072/A11072.html?userCourseId=&tenantCode=&type=1&csComm=true&csCapt=true",
            "detailCode":"0"
        }
        """
        return self._post("/pharos/usercourse/getCourseUrl.do",
                          {"courseId": course_id, "userProjectId": user_project_id})

    def invoke_captcha(self, user_course_id: str, user_project_id: str) -> Dict[str, Any]:
        """
        通过验证码获取完成 token。
        不走 OCR —— 验证码是"找出正确汉字"，但后端仅校验坐标，
        所以直接用固定坐标 +-5px 随机抖动暴力猜测，实测可过。
        :param user_course_id: 用户课程 ID
        :param user_project_id: 用户项目 ID
        :return: 包含完成 token 的 dict
        {"code":"0",data:{"methodToken",""}}
        """
        fetch_url = f"{self.baseurl}/pharos/usercourse/getCaptcha.do"
        check_url = f"{self.baseurl}/pharos/usercourse/checkCaptcha.do"
        params = {
            "userCourseId": user_course_id, "userProjectId": user_project_id,
            "userId": self.user["userId"], "tenantCode": self.tenant_code,
        }
        response = self.session.get(fetch_url, params=params, timeout=self.timeout)
        params["questionId"] = handle_response(response).get("captcha", {}).get("questionId", "")
        # 三组固定基准坐标 + 随机 ±5px 抖动，服务端容差校验
        coords = [{"x": x + randint(-5, 5), "y": y + randint(-5, 5)}
                  for x, y in [(207, 436), (67, 424), (141, 427)]]
        data = {"coordinateXYs": json.dumps(coords, separators=(",", ":"))}
        time.sleep(3)
        response = self.session.post(check_url, params=params, data=data, timeout=self.timeout)
        return handle_response(response)

    def finish_by_token(self, user_course_id: str, token: str | None = None,
                        course_type: str | None = "weiban",
                        unique_no: str | None = None,
                        referer: str | None = None) -> Dict[str, Any]:
        """
        通过 userCourseId 或验证码 token 完成课程。
        :param user_course_id: 用户课程 ID
        :param token: 验证码获取的 token，weiban 模式会替代 URL 中的 id
        :param course_type: 决定接口——"weiban"（JSONP GET，需绕过跨域）、"open"（POST）、"moon"（POST）
        :param unique_no: 微课专用唯一标识
        :param referer: 课程来源
        :return: 完成结果 dict
        JSONP 模式是因为 weiban 服务端返回 callback 包裹而非 JSON，前端通过 <script> 标签跨域拉取。
        detailCode=10018 表示服务端进度尚未落库，采用指数退避重试（3/6/10/15/20 秒，最多 6 次）。
        """
        data = {"userCourseId": user_course_id, "tenantCode": self.tenant_code}
        if unique_no:
            data["uniqueNo"] = unique_no

        if course_type == "open":
            url = "https://open.mycourse.cn/proteus/usercourse/finish.do"
        elif course_type == "moon":
            url = "https://moon.mycourse.cn/moonapi/api/study/activity/microCourse/v1/finishedCourse"
        else:
            url = f"{self.baseurl}/pharos/usercourse/v2/{token or user_course_id}.do"

        is_jsonp = course_type not in ("open", "moon")
        for attempt in range(6):
            if is_jsonp:
                ts = int(self.get_timestamp(13, 0))
                cb = f"jQuery3410{randint(10 ** 15, 10 ** 16 - 1)}_{ts}"
                response = self.session.get(url,
                                            params={**data, "callback": cb, "_": ts + 1},
                                            timeout=self.timeout)
            else:
                response = self.session.post(url, data=data, timeout=self.timeout)
            try:
                result = response.json()
            except json.JSONDecodeError:
                text = response.text
                s, e = text.find("("), text.rfind(")")
                if s != -1 and e != -1:
                    text = text[s + 1:e]
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": response.text}
            if result.get("detailCode") == "10018" and attempt < 5:
                time.sleep((3, 6, 10, 15, 20)[attempt])
                continue
            return result
        return {}  # unreachable, satisfies type checker

    def finish_lyra(self, user_activity_id: str) -> Dict[str, Any]:
        """完成安全实训（Lyra 独立微服务，不走 _post 统一入口）
        :param user_activity_id: 用户活动 ID
        :return: 完成结果 dict
        {"msg":"ok","code":"0","detailCode":"0"}
        """
        response = self.session.post(
            "https://lyra.mycourse.cn/lyraapi/study/course/finish.api",
            data={"userActivityId": user_activity_id}, timeout=self.timeout)
        return handle_response(response)

    # ========================================================================
    # 考试
    # ========================================================================

    def exam_list_plan(self, user_project_id: str) -> Dict[str, Any]:
        """获取考试计划列表
        :param user_project_id: 用户项目 ID
        :return: 考试计划列表 dict
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
        return self._post("/pharos/exam/listPlan.do",
                          {"userProjectId": user_project_id})

    def exam_before_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """获取是否有未提交的答案
        :param user_exam_plan_id: 用户考试计划 ID
        :return: 未提交答案信息 dict
        {
          "code": "0",
          "data": {
            "isExistedNotSubmit": false
          },
          "detailCode": "0"
        }
        """
        return self._post("/pharos/exam/beforePaper.do",
                          {"userExamPlanId": user_exam_plan_id})

    def exam_prepare_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """准备考试
        :param user_exam_plan_id: 用户考试计划 ID
        :return: 考试准备结果 dict
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
        return self._post("/pharos/exam/preparePaper.do",
                          {"userExamPlanId": user_exam_plan_id})

    def exam_check(self, user_exam_plan_id: str, randstr: str,
                   ticket: str) -> Dict[str, Any]:
        """无感验证码校验（考试前），appId: 190330343
        :param user_exam_plan_id: 用户考试计划 ID
        :param randstr: 验证码随机串
        :param ticket: 验证码票据
        :return: 校验结果 dict
        {"code":"0","detailCode":"0"}
        """
        return self._post("/pharos/exam/check.do",
                          {"userExamPlanId": user_exam_plan_id,
                           "randstr": randstr, "ticket": ticket})

    def course_check(self, user_course_id: str, user_project_id: str,
                     course_id: str, randstr: str, ticket: str) -> Dict[str, Any]:
        """验证码校验（课程完成时），appId: 195119536
        :param user_course_id: 用户课程 ID
        :param user_project_id: 用户项目 ID
        :param course_id: 课程 ID
        :param randstr: 验证码随机串
        :param ticket: 验证码票据
        :return: 校验结果 dict
        {"code":"0","data":"${token}","detailCode":"0"}

        """
        return self._post("/pharos/usercourse/check.do",
                          {"userCourseId": user_course_id,
                           "userProjectId": user_project_id,
                           "courseId": course_id,
                           "randstr": randstr, "ticket": ticket})

    def exam_check_verify_code(self, user_exam_plan_id: str, verfy_code: str,
                               verify_time: int | None) -> Dict[str, Any]:
        """检查考试验证码
        :param user_exam_plan_id: 用户考试计划 ID
        :param verfy_code: 验证码
        :param verify_time: 验证码时间戳
        :return: 校验结果 dict
        {
          "code": "0",
          "detailCode": "0"
        }
        """
        return self._post("/pharos/exam/checkVerifyCode.do",
                          {"userExamPlanId": user_exam_plan_id,
                           "time": verify_time or int(self.get_timestamp(frac_len=0)),
                           "verifyCode": verfy_code})

    def exam_start_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """开始考试，返回试卷题目列表（data 字段含 questionList 数组，每题有 questionId/answerIds 等字段）
        :param user_exam_plan_id: 用户考试计划 ID
        :return: 试卷题目列表 dict
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
        return self._post("/pharos/exam/startPaper.do",
                          {"userExamPlanId": user_exam_plan_id})

    def exam_record_question(self, user_exam_plan_id: str, question_id: str,
                             use_time: int, answer_ids: list | None,
                             exam_plan_id: str) -> Dict[str, Any]:
        """记录考试答案
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param use_time: 答题用时（秒）
        :param answer_ids: 答案 ID 列表
        :param exam_plan_id: 考试计划 ID
        :return: 记录结果 dict
        {
          "code": "0",
          "detailCode": "0"
        }
        """
        data = {
            "userExamPlanId": user_exam_plan_id, "questionId": question_id,
            "useTime": use_time, "examPlanId": exam_plan_id,
        }
        if answer_ids:
            data["answerIds"] = ",".join(answer_ids)
        return self._post("/pharos/exam/recordQuestion.do", data)

    def exam_submit_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """提交考试
        :param user_exam_plan_id: 用户考试计划 ID
        :return: 提交结果 dict
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
        return self._post("/pharos/exam/submitPaper.do",
                          {"userExamPlanId": user_exam_plan_id})

    def exam_fresh_paper(self, user_exam_plan_id: str) -> Dict[str, Any]:
        """重置考试题目
        :param user_exam_plan_id: 用户考试计划 ID
        :return: 刷新结果 dict
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
        return self._post("/pharos/exam/freshPaper.do",
                          {"userExamPlanId": user_exam_plan_id})

    def exam_review_paper(self, user_exam_id: str, is_retake: int = 2) -> Dict[str, Any]:
        """查看考试结果
        :param user_exam_id: 用户考试 ID
        :param is_retake: 1=补考, 2=正常考试
        :return: 考试结果 dict
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
        return self._post("/pharos/exam/reviewPaper.do",
                          {"userExamId": user_exam_id, "isRetake": is_retake})

    def exam_list_history(self, exam_plan_id: str, exam_type: int) -> Dict[str, Any]:
        """获取考试历史记录
        :param exam_plan_id: 考试计划 ID
        :param exam_type: 考试类型
        :return: 考试历史列表 dict
        {
          "code": "0",
          "data": {
            "examHistoryList": [
              {
                "examId": "${uuid}",
                "examName": "2025级硕士生实验室安全教育",
                "examType": 1,
                "examTime": "2025-10-12",
                "examScore": 100,
                "examStatus": 2
              }
            ]
          },
          "detailCode": "0"
        }
        """
        return self._post("/pharos/exam/listHistory.do",
                          {"examPlanId": exam_plan_id, "examType": exam_type})

    # ========================================================================
    # 题库与进度
    # ========================================================================

    def download_answer(self) -> str:
        """下载最新题库
        :return: 题库 JSON 字符串
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
        return self.session.get(self.ANSWER_URL, timeout=self.timeout).text

    def apinext(self, user_course_id: str, course_id: str, user_project_id: str,
                step: int = 0, finish: int = 2, nonstr: str = "",
                unique_no: str | None = None) -> Dict[str, Any]:
        """
        学习进度追踪接口，部分课程需要此接口记录翻页和完成状态。
        :param user_course_id: 用户课程 ID
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :param step: 当前页码（翻一页调一次）
        :param finish: 1=完成, 2=翻页中
        :param nonstr: 扩展字段
        :param unique_no: 微课专用唯一标识
        :return: 进度记录结果 dict
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
        # 双重 Base64：仿 JS 前端 CryptoJS.enc.Base64.stringify(CryptoJS.enc.Utf8.parse(Base64(ciphertext)))
        # 后端先做 atob 再 AES-CBC 解密，因此需要两次编码
        encrypted_b64 = b64encode(b64encode(cipher.encrypt(padded))).decode()
        response = self.session.post(
            f"{self.baseurl}/jupiterapi/api/statusercourse/v1/next",
            json={"data": encrypted_b64}, timeout=self.timeout)
        return handle_response(response)

    def list_question(self, course_id: str) -> Dict[str, Any]:
        """获取课后习题列表（course_id 为 resourceId UUID）
        :param course_id: 课程 ID（resourceId UUID）
        :return: 习题列表 dict
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
        return self._mercury_request(
            {"service": "mercury.microlecture.listQuestion", "id": course_id})

    def save_question(self, course_id: str, question_id: str, answers: str,
                      source: str = "WEIBAN") -> Dict[str, Any]:
        """提交课中观点题答案
        :param course_id: 课程 ID
        :param question_id: 题目 ID
        :param answers: 答案内容
        :param source: 来源（默认 WEIBAN）
        :return: 提交结果 dict
        {
          "code": "0",
          "data": {"isRight": 1, "analysis": "", "answerLabel": "-A"},
          "detailCode": "0"
        }
        """
        return self._mercury_request({
            "service": "mercury.microlecture.saveQuestion",
            "courseId": course_id, "questionId": question_id, "answers": answers,
            "userId": self.user["userId"], "tenantCode": self.tenant_code,
            "source": source,
        })

    def save_exam_question(self, course_id: str, question_id: str, answers: str,
                           source: str = "WEIBAN") -> Dict[str, Any]:
        """提交课后习题答案
        :param course_id: 课程 ID
        :param question_id: 题目 ID
        :param answers: 答案内容
        :param source: 来源（默认 WEIBAN）
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
            "courseId": course_id, "questionId": question_id, "answers": answers,
            "userId": self.user["userId"], "tenantCode": self.tenant_code,
            "source": source,
        })
