import requests
import json
import time
import os
import random
import webbrowser
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from base64 import urlsafe_b64encode, urlsafe_b64decode


# 请填写以下信息
tenantName = ""  # 学校全称
account = ""  # 账号（考生号）
password = ""  # 密码

# 下面不用动
TIMEOUT = 20  # 学习时间，太短完成失败，单位秒
REQUEST_DELAY = 1  # 请求间隔时间，单位秒
if tenantName == "":
    tenantName = input("[+] 请输入学校全称：")
if account == "":
    account = input("[+] 请输入账号（考生号）：")
if password == "":
    password = input("[+] 请输入密码（考生号）：")

key = urlsafe_b64decode("d2JzNTEyAAAAAAAAAAAAAA==")
cipher = AES.new(key, AES.MODE_ECB)
fail = []
session = requests.session()
session.headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
    "Referer": "https://weiban.mycourse.cn/",
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


def getTimestamp():
    return f"{int(time.time() * 1000)}.{random.randint(000, 999)}"


def getTenantListWithLetter(tenantName):
    url = "https://weiban.mycourse.cn/pharos/login/getTenantListWithLetter.do"
    params = {"timestamp": getTimestamp()}
    time.sleep(REQUEST_DELAY)
    response = session.post(url, params=params)
    for a in response.json()["data"]:
        for l in a["list"]:
            if tenantName in l["name"]:
                return l["code"]
    print(response.status_code, response.text)
    return False


def randLetterImage(verifyTime):
    url = "https://weiban.mycourse.cn/pharos/login/randLetterImage.do"
    params = {"time": verifyTime}
    time.sleep(REQUEST_DELAY)
    response = session.get(url, params=params)
    with open("captcha.png", "wb") as f:
        f.write(response.content)
    webbrowser.open(f"file://{os.path.abspath("captcha.png")}")
    return input("[+] 验证码已保存为 captcha.png，请打开查看并输入：")


def encrypt(data):
    return urlsafe_b64encode(
        cipher.encrypt(pad(data.encode(), AES.block_size))
    ).decode()


def login(account, password, tenantCode, verifyTime, verifyCode):
    url = "https://weiban.mycourse.cn/pharos/login/login.do"
    params = {"timestamp": getTimestamp()}
    data = {
        "keyNumber": account,
        "password": password,
        "tenantCode": tenantCode,
        "time": verifyTime,
        "verifyCode": verifyCode,
    }
    time.sleep(REQUEST_DELAY)
    response = session.post(
        url,
        params=params,
        data={"data": encrypt(json.dumps(data, separators=(",", ":")))},
    )
    if "token" in response.text:
        return response.json()["data"]
    print(response.status_code, response.text)
    return False


def listStudyTask(tenantCode, UserId, XToken):
    url = "https://weiban.mycourse.cn/pharos/index/listStudyTask.do"
    params = {"timestamp": getTimestamp()}
    data = {"tenantCode": tenantCode, "userId": UserId}
    session.headers["X-Token"] = XToken
    time.sleep(REQUEST_DELAY)
    response = session.post(url, params=params, data=data)
    session.headers.pop("X-Token")
    if "userProjectId" in response.text:
        return response.json()["data"]
    print(response.status_code, response.text)
    return False


def listCategory(tenantCode, userId, userProjectId, XToken):
    url = "https://weiban.mycourse.cn/pharos/usercourse/listCategory.do"
    params = {"timestamp": getTimestamp()}
    data = {
        "tenantCode": tenantCode,
        "userId": userId,
        "userProjectId": userProjectId,
        "chooseType": 3,
    }
    session.headers["X-Token"] = XToken
    time.sleep(REQUEST_DELAY)
    response = session.post(url, params=params, data=data)
    session.headers.pop("X-Token")
    if "categoryCode" in response.text:
        print("[+] 获取分类成功")
        return response.json()["data"]
    print(response.status_code, response.text)
    return False


def listCourse(tenantCode, userId, userProjectId, categoryCode, XToken):
    url = "https://weiban.mycourse.cn/pharos/usercourse/listCourse.do"
    params = {"timestamp": getTimestamp()}
    data = {
        "tenantCode": tenantCode,
        "userId": userId,
        "userProjectId": userProjectId,
        "chooseType": 3,
        "categoryCode": categoryCode,
    }
    session.headers["X-Token"] = XToken
    time.sleep(REQUEST_DELAY)
    response = session.post(url, params=params, data=data)
    session.headers.pop("X-Token")
    if "userCourseId" in response.text:
        return response.json()["data"]
    print("[-]", response.status_code, response.text)
    return False


def study(courseId, userProjectId, userId, tenantCode, XToken):
    url1 = "https://weiban.mycourse.cn/pharos/usercourse/study.do"
    url2 = "https://weiban.mycourse.cn/pharos/usercourse/getCourseUrl.do"
    params = {"timestamp": getTimestamp()}
    data = {
        "tenantCode": tenantCode,
        "userId": userId,
        "courseId": courseId,
        "userProjectId": userProjectId,
    }
    session.headers["X-Token"] = XToken
    time.sleep(REQUEST_DELAY)
    response1 = session.post(url1, params=params, data=data)
    time.sleep(REQUEST_DELAY)
    response2 = session.post(url2, params=params, data=data)
    session.headers.pop("X-Token")
    if (
        "code" in response1.text
        and response1.json()["code"] == "0"
        and "code" in response2.text
        and response2.json()["code"] == "0"
    ):
        return True
    print("[-]", response1.status_code, response1.text)
    print("[-]", response2.status_code, response2.text)
    return False


def getCaptcha(userCourseId, userProjectId, userId, tenantCode):
    url = "https://weiban.mycourse.cn/pharos/usercourse/getCaptcha.do"
    params = {
        "userCourseId": userCourseId,
        "userProjectId": userProjectId,
        "userId": userId,
        "tenantCode": tenantCode,
    }
    time.sleep(REQUEST_DELAY)
    response = session.get(url, params=params)
    if "captcha" in response.text:
        return response.json()["captcha"]["questionId"]
    print("[-]", response.status_code, response.text)
    return False


def randomXY():
    return random.randint(-5, 5)


def checkCaptcha(userCourseId, userProjectId, userId, tenantCode, questionId):
    url = "https://weiban.mycourse.cn/pharos/usercourse/checkCaptcha.do"
    params = {
        "userCourseId": userCourseId,
        "userProjectId": userProjectId,
        "userId": userId,
        "tenantCode": tenantCode,
        "questionId": questionId,
    }
    coordinateXYs = [
        {"x": 207 + randomXY(), "y": 436 + randomXY()},
        {"x": 67 + randomXY(), "y": 424 + randomXY()},
        {"x": 141 + randomXY(), "y": 427 + randomXY()},
    ]
    data = {"coordinateXYs": json.dumps(coordinateXYs, separators=(",", ":"))}
    time.sleep(REQUEST_DELAY)
    response = session.post(url, params=params, data=data)
    if "methodToken" in response.text:
        return response.json()["data"]["methodToken"]
    print("[-]", response.status_code, response.text)
    return False


def finish(methodToken, userCourseId, tenantCode):
    url1 = "https://weiban.mycourse.cn/pharos/usercourse/v2/" + userCourseId + ".do"
    url2 = "https://weiban.mycourse.cn/pharos/usercourse/v2/" + methodToken + ".do"
    params = {
        "callback": f"jQuery{random.randint(100000000000000, 999999999999999)}_{int(time.time()*1000)}",
        "userCourseId": userCourseId,
        "tenantCode": tenantCode,
        "_": int(time.time() * 1000),
    }
    time.sleep(REQUEST_DELAY)
    response = session.get(url1, params=params)
    if "ok" in response.text:
        return True
    print("[-] 使用 userCourseId 完成失败，将使用 methodToken 尝试")
    time.sleep(REQUEST_DELAY)
    response = session.get(url2, params=params)
    if "ok" in response.text:
        return True
    print("[-] 使用 methodToken 完成失败，请反馈")
    print(response.status_code, response.text)
    return False


def main():
    tenantCode = getTenantListWithLetter(tenantName)
    if not tenantCode:
        print("[-] 没找到你的学校代码，请检查学校全称是否正确")
        return
    print("[+] 获取学校代码成功", tenantCode)
    verifyTime = int(time.time() * 1000)
    verifyCode = randLetterImage(verifyTime)
    data = login(account, password, tenantCode, verifyTime, verifyCode)
    if not data:
        print("[-] 登录失败")
        return
    print("[+] 登录成功")
    userId = data["userId"]
    XToken = data["token"]
    userProjects = listStudyTask(tenantCode, userId, XToken)
    if not userProjects:
        print("[-] 获取项目失败")
    for i, userProject in enumerate(userProjects):
        userProjectId = userProject["userProjectId"]
        print(
            f"{'[+] 好了' if userProject['finished'] == '1' else '[-] 还没看'} {userProject['projectName']}"
        )
        if userProject["finished"] == "1":
            continue
        categories = listCategory(tenantCode, userId, userProjectId, XToken)
        if not categories:
            print("[-] 获取分类失败")
            return
        lenCategories = len(categories)
        for j, category in enumerate(categories):
            categoryCode = category["categoryCode"]
            categoryName = category["categoryName"]
            print(f"[+][{j}/{lenCategories}]", categoryName)
            courses = listCourse(
                tenantCode, userId, userProjectId, categoryCode, XToken
            )
            if not courses:
                print("[-] 获取课程失败")
                continue
            lenCourses = len(courses)
            for k, course in enumerate(courses):
                print(
                    f"[+][{j}/{lenCategories}][{k}/{lenCourses}]",
                    categoryName,
                    course["resourceName"],
                )
                userCourseId = course["userCourseId"]
                resourceId = course["resourceId"]
                print(
                    f"{'[+] 好了' if userProject['finished'] == '1' else '[-] 还没看'} {categoryName}{course['resourceName']}"
                )
                if course["finished"] == 1:
                    continue
                if not study(resourceId, userProjectId, userId, tenantCode, XToken):
                    print("[-] 预请求失败")
                    continue
                print(f"[+] 预请求成功，请等待 {TIMEOUT} 秒，不然不记入学习进度")
                time.sleep(TIMEOUT)
                questionId = getCaptcha(userCourseId, userProjectId, userId, tenantCode)
                if not questionId:
                    print("[-] 获取完成验证码失败")
                    continue
                print("[+] 获取完成验证码成功")
                methodToken = checkCaptcha(
                    userCourseId, userProjectId, userId, tenantCode, questionId
                )
                if not methodToken:
                    print("[-] 获取验证 Token 失败")
                    continue
                print("[+] 获取验证 Token 成功")
                if finish(methodToken, userCourseId, tenantCode):
                    print("[+] 完成课程", categoryName, course["resourceName"])
                else:
                    fail.append(course["resourceName"])
                    print("[-] 失败课程", categoryName, course["resourceName"])
    print("[+] 全部完成")
    if fail:
        print("[-] 失败课程，可过会重试", fail)


main()
input("按回车键退出")
