"""
腾讯验证码处理模块
- 无感验证码 (appId: 190330343): 考试前自动处理，无用户交互（entry: weiban.mycourse.cn）
- 图片点选验证码 (appId: 195119536): 课程完成时，通过浏览器让用户手动处理（entry: mcwk.mycourse.cn/course/）
"""

import json
import os
import platform
from pathlib import Path
from shutil import which
from typing import Dict, Optional

from DrissionPage import Chromium, ChromiumOptions

# 腾讯验证码 SDK 地址
TCAPTCHA_SDK_URL = "https://turing.captcha.qcloud.com/TJCaptcha.js"

# 验证码 appId
EXAM_CAPTCHA_APP_ID = "190330343"    # 无感验证码（考试）
COURSE_CAPTCHA_APP_ID = "195119536"  # 图片点选验证码（课程完成）

# 默认入口页面
EXAM_ENTRY_URL = "https://weiban.mycourse.cn/#/course"
COURSE_ENTRY_URL = "https://mcwk.mycourse.cn/"

# 各平台浏览器候选路径（按优先级排列）
_BROWSER_CANDIDATES: Dict[str, list[str]] = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ],
    "windows": [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        os.path.expandvars("%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
        "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome-beta",
        "/usr/bin/google-chrome-unstable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/google-chrome",
        "/snap/bin/chromium",
    ],
}


def find_browser_path() -> Optional[str]:
    """
    自动查找系统中可用的 Chrome/Chromium 浏览器路径
    DrissionPage 内置检测覆盖不全（如 macOS 上只查 Google Chrome，不查 Dev/Canary/Chromium），
    这里补上各平台常见浏览器的路径检测。
    :return: 浏览器可执行文件路径，找不到返回 None
    """
    system = platform.system().lower()

    # 1) 尝试 shutil.which（比 DrissionPage 覆盖更全的 CLI 名称）
    cli_names = ["chrome", "chromium", "google-chrome", "google-chrome-stable",
                 "google-chrome-unstable", "google-chrome-beta", "chromium-browser",
                 "microsoft-edge", "brave-browser", "brave"]
    if system == "darwin":
        cli_names.extend(["google-chrome-dev", "google-chrome-canary"])
    for name in cli_names:
        path = which(name)
        if path:
            return path

    # 2) 按平台检查固定路径
    candidates = _BROWSER_CANDIDATES.get(system, [])
    for path in candidates:
        if path and Path(path).is_file():
            return path

    return None


class CaptchaHandler:
    """通过浏览器处理腾讯验证码"""

    def __init__(self, tenant_code: str, user_id: str, token: str, log,
                 browser_path: Optional[str] = None) -> None:
        self.tenant_code = tenant_code
        self.user_id = user_id
        self.token = token
        self.log = log
        self.browser_path = browser_path

    def _build_page(self, entry_url: str, headless: bool = False):
        """
        启动浏览器，注入 localStorage 认证信息，导航到目标页面
        auto_port() 确保每个浏览器实例使用独立端口和临时用户目录，线程安全
        :param entry_url: 触发验证码的入口页面 URL
        :param headless: 是否使用无头模式
        :return: (Chromium, ChromiumTab) 元组
        """
        co = ChromiumOptions().auto_port()

        if headless:
            co.headless(True)

        # 确定浏览器路径：优先用显式配置，其次自动检测
        path = self.browser_path or find_browser_path()
        if path:
            co.set_browser_path(path)

        browser = Chromium(co)
        tab = browser.latest_tab

        # 先导航到站点以建立域名上下文，然后注入 localStorage 认证
        self.log.info(f"正在打开验证码入口页面: {entry_url}")
        tab.get(entry_url, timeout=600)

        # 注入 localStorage user 字段（网站使用 localStorage 认证）
        tab.run_js(f"""
            const user = {{
                userId: '{self.user_id}',
                token: '{self.token}',
                tenantCode: '{self.tenant_code}',
            }};
            localStorage.setItem('user', JSON.stringify(user));
        """)

        # 刷新页面使 localStorage 生效
        tab.get(entry_url, timeout=30)
        tab.wait(3)

        # 确保 TCaptcha SDK 已加载
        tab.run_js(f"""
            if (typeof TencentCaptcha === 'undefined') {{
                const script = document.createElement('script');
                script.src = '{TCAPTCHA_SDK_URL}';
                script.async = false;
                document.head.appendChild(script);
            }}
        """)
        tab.wait(2)

        return browser, tab

    def _run_captcha(self, tab, app_id: str) -> Dict[str, str]:
        """
        在页面上运行腾讯验证码，返回 randstr 和 ticket
        :param tab: ChromiumTab 对象
        :param app_id: 腾讯验证码 appId
        :return: {"randstr": "...", "ticket": "..."}
        """
        result_json = tab.run_js(f"""
            (async () => {{
                return await new Promise((resolve, reject) => {{
                    const captcha = new TencentCaptcha('{app_id}', (res) => {{
                        if (res.ret === 0) {{
                            resolve(JSON.stringify({{randstr: res.randstr, ticket: res.ticket}}));
                        }} else if (res.ret === 2) {{
                            reject(new Error('用户主动关闭了验证码'));
                        }} else {{
                            reject(new Error('验证码验证失败: ret=' + res.ret));
                        }}
                    }}, {{
                        userLanguage: 'zh-cn',
                        loading: false,
                    }});
                    captcha.show();
                }});
            }})()
        """, as_expr=True, timeout=30)

        return json.loads(result_json)

    def handle_exam_captcha(self, user_exam_plan_id: str) -> Dict[str, str]:
        """
        处理考试前的无感验证码 (appId: 190330343)
        无感模式下验证码会自动完成，无需用户交互
        入口页面: weiban.mycourse.cn/#/course
        :param user_exam_plan_id: 用户考试计划 ID
        :return: {"randstr": "...", "ticket": "..."}
        """
        self.log.info("正在处理无感验证码 (appId: 190330343)...")
        browser, tab = self._build_page(EXAM_ENTRY_URL, headless=True)

        try:
            result = self._run_captcha(tab, EXAM_CAPTCHA_APP_ID)
            self.log.success("已获取无感验证码")
            return result
        finally:
            browser.quit()

    def handle_course_captcha(
        self,
        course_url: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        处理课程完成时的图片点选验证码 (appId: 195119536)
        会弹出浏览器窗口，需要用户手动完成图片点选验证
        入口页面: mcwk.mycourse.cn/course/{course_code}/{course_code}.html
        :param course_url: 课程页面 URL（如 https://mcwk.mycourse.cn/course/DA0309018/DA0309018.html）
        :return: {"randstr": "...", "ticket": "..."}
        """
        self.log.info("=" * 50)
        self.log.warning("需要手动完成验证码！正在打开浏览器...")
        self.log.info("请在浏览器窗口中完成图片点选验证，完成后程序将自动继续")
        self.log.info("=" * 50)

        entry_url = course_url or COURSE_ENTRY_URL
        browser, tab = self._build_page(entry_url)

        try:
            result = self._run_captcha(tab, COURSE_CAPTCHA_APP_ID)
            self.log.success("验证码手动验证完成")
            return result
        finally:
            browser.quit()
