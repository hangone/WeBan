"""
腾讯验证码处理模块
- 无感验证码: 考试前自动处理，无用户交互
- 图片点选验证码: 课程完成时，通过浏览器让用户手动处理
"""

import json
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


class CaptchaHandler:
    """通过浏览器处理腾讯验证码"""

    def __init__(self, tenant_code: str, user_id: str, token: str, log,
                 browser_path: Optional[str] = None) -> None:
        """初始化验证码处理器。

        :param tenant_code: 租户编码
        :param user_id: 用户 ID
        :param token: 认证令牌
        :param log: 日志记录器（需支持 info/warning/success 方法）
        :param browser_path: 浏览器可执行文件路径，留空则自动查找
        """
        self._auth = {
            "userId": user_id,
            "token": token,
            "tenantCode": tenant_code,
        }
        self.log = log
        self.browser_path = browser_path

    # ── 浏览器 / 页面构建 ──────────────────────────────

    def _create_browser(self, headless: bool = False) -> Chromium:
        """创建 Chromium 实例。

        :param headless: True 时以无头模式运行（无需用户交互）
        :return: 已配置的 Chromium 对象

        auto_port() 避免端口冲突；窗口尺寸 428x818 模拟移动端以匹配腾讯验证码的移动版 UI。
        """
        co = ChromiumOptions().auto_port().mute(True).set_argument('--window-size', '428,818')
        if headless:
            co.headless(True)
        if self.browser_path:
            co.set_browser_path(self.browser_path)
        return Chromium(co)

    def _inject_auth(self, tab) -> None:
        """向页面注入 localStorage 认证信息。

        :param tab: ChromiumTab
        json.dumps 对含特殊字符的 token 做安全编码，
        避免 JS 代码注入（例如 token 中出现引号或反斜杠时）。
        """
        tab.run_js(f"""\
            const user = {json.dumps(self._auth)};
            localStorage.setItem('user', JSON.stringify(user));
        """)

    def _ensure_captcha_sdk(self, tab) -> None:
        """确保页面已加载腾讯验证码 SDK。

        :param tab: ChromiumTab
        """
        tab.run_js(f"""\
            if (typeof TencentCaptcha === 'undefined') {{
                const script = document.createElement('script');
                script.src = '{TCAPTCHA_SDK_URL}';
                script.async = false;
                document.head.appendChild(script);
            }}
        """)

    def _build_page(self, entry_url: str, headless: bool = False):
        """启动浏览器，注入认证信息，加载 SDK。

        :param entry_url: 入口页面 URL（必须在腾讯验证码的域名白名单内）
        :param headless: 是否以无头模式运行
        :return: (browser, tab) 元组

        页面加载两次：第一次建立域名（localStorage 按域名隔离）；
        注入认证后重新加载，使页面能读取到 localStorage 中的登录态。
        """
        self.log.info(f"正在打开验证码入口页面: {entry_url}")
        browser = self._create_browser(headless)
        tab = browser.latest_tab
        tab.get(entry_url, timeout=600)               # 第一次：建立域名
        self._inject_auth(tab)
        tab.get(entry_url, timeout=30)                # 第二次：读取注入的认证
        tab.wait(3)
        self._ensure_captcha_sdk(tab)
        tab.wait(2)
        return browser, tab

    # ── 验证码执行 ──────────────────────────────────────

    def _run_captcha(self, tab, app_id: str) -> Dict[str, str]:
        """调用腾讯验证码 SDK，等待用户完成或关闭。

        :param tab: 浏览器标签页对象
        :param app_id: 腾讯验证码 appId
        :return: {"randstr": str, "ticket": str}
                     — 验证通过时返回的随机串和票据

        回调中 ret 值的含义：0=验证通过，2=用户主动关闭，其他=验证失败。
        """
        result_json = tab.run_js(f"""\
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
        """, as_expr=True, timeout=120)
        return json.loads(result_json)

    # ── 公开方法 ────────────────────────────────────────

    def handle_exam_captcha(self, user_exam_plan_id: str) -> Dict[str, str]:
        """处理考试前的无感验证码。

        无感模式：验证码在后台自动完成，无需用户交互，因此使用 headless=True。

        :param user_exam_plan_id: 考试计划 ID（预留，目前未使用）
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
        """
        self.log.info("正在处理无感验证码")
        browser, tab = self._build_page(EXAM_ENTRY_URL, headless=True)
        try:
            result = self._run_captcha(tab, EXAM_CAPTCHA_APP_ID)
            self.log.success("已获取无感验证码")
            return result
        finally:
            browser.quit()

    def handle_course_captcha(self, course_url: Optional[str] = None) -> Dict[str, str]:
        """处理课程完成时的图片点选验证码。

        需要用户手动点击图片，因此以 headless=False（默认）打开可见浏览器窗口。
        图片点选无法自动化：腾讯会随机要求"点击图中所有包含 X 的位置"。

        :param course_url: 课程入口 URL，留空则使用默认的 mcwk.mycourse.cn
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
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
