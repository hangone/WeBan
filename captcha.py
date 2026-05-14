"""
腾讯验证码处理模块
- 无感验证码 (appId: 190330343): 考试前自动处理，无用户交互（entry: weiban.mycourse.cn）
- 滑块验证码 (appId: 195119536): 课程完成时，通过 Playwright 打开浏览器让用户手动处理（entry: mcwk.mycourse.cn/course/）
"""

import json
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright

# 腾讯验证码 SDK 地址
TCAPTCHA_SDK_URL = "https://turing.captcha.qcloud.com/TJCaptcha.js"

# 验证码 appId
EXAM_CAPTCHA_APP_ID = "190330343"    # 无感验证码（考试）
COURSE_CAPTCHA_APP_ID = "195119536"  # 滑块验证码（课程完成）

# 默认入口页面
EXAM_ENTRY_URL = "https://weiban.mycourse.cn/#/course"
COURSE_ENTRY_URL = "https://mcwk.mycourse.cn/"


class CaptchaHandler:
    """通过 Playwright 处理腾讯验证码"""

    def __init__(self, tenant_code: str, user_id: str, token: str, log) -> None:
        self.tenant_code = tenant_code
        self.user_id = user_id
        self.token = token
        self.log = log

    def _build_page(self, entry_url: str) -> Any:
        """
        启动 Playwright 浏览器，注入 localStorage 认证信息，导航到目标页面
        :param entry_url: 触发验证码的入口页面 URL
        :return: (playwright, browser, page)
        """
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=False)

        context = browser.new_context()
        page = context.new_page()

        # 先导航到站点以建立域名上下文，然后注入 localStorage 认证
        self.log.info(f"正在打开验证码入口页面: {entry_url}")
        page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)

        # 注入 localStorage user 字段（网站使用 localStorage 认证）
        page.evaluate(f"""
            () => {{
                const user = {{
                    userId: '{self.user_id}',
                    token: '{self.token}',
                    tenantCode: '{self.tenant_code}',
                }};
                localStorage.setItem('user', JSON.stringify(user));
            }}
        """)

        # 刷新页面使 localStorage 生效
        page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # 确保 TCaptcha SDK 已加载
        page.evaluate(f"""
            () => {{
                if (typeof TencentCaptcha === 'undefined') {{
                    const script = document.createElement('script');
                    script.src = '{TCAPTCHA_SDK_URL}';
                    script.async = false;
                    document.head.appendChild(script);
                }}
            }}
        """)
        page.wait_for_timeout(2000)

        return p, browser, page

    def _run_captcha(self, page: Any, app_id: str) -> Dict[str, str]:
        """
        在页面上运行腾讯验证码，返回 randstr 和 ticket
        :param page: Playwright page 对象
        :param app_id: 腾讯验证码 appId
        :return: {"randstr": "...", "ticket": "..."}
        """
        result_json = page.evaluate(f"""
            () => {{
                return new Promise((resolve, reject) => {{
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
            }}
        """)

        return json.loads(result_json)

    def handle_exam_captcha(self, user_exam_plan_id: str) -> Dict[str, str]:
        """
        处理考试前的无感验证码 (appId: 190330343)
        无感模式下验证码会自动完成，无需用户交互
        入口页面: weiban.mycourse.cn/#/course
        :param user_exam_plan_id: 用户考试计划 ID
        :return: {"randstr": "...", "ticket": "..."}
        """
        self.log.info("正在通过 Playwright 处理无感验证码 (appId: 190330343)...")
        p, browser, page = self._build_page(EXAM_ENTRY_URL)

        try:
            result = self._run_captcha(page, EXAM_CAPTCHA_APP_ID)
            self.log.success("无感验证码自动通过")
            return result
        except Exception as e:
            self.log.error(f"无感验证码处理失败: {e}")
            raise
        finally:
            browser.close()
            p.stop()

    def handle_course_captcha(
        self,
        course_url: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        处理课程完成时的滑块验证码 (appId: 195119536)
        会弹出浏览器窗口，需要用户手动完成滑块验证
        入口页面: mcwk.mycourse.cn/course/{course_code}/{course_code}.html
        :param course_url: 课程页面 URL（如 https://mcwk.mycourse.cn/course/DA0309018/DA0309018.html）
        :return: {"randstr": "...", "ticket": "..."}
        """
        self.log.info("=" * 50)
        self.log.warning("需要手动完成验证码！正在打开浏览器...")
        self.log.info("请在浏览器窗口中完成滑块验证，完成后程序将自动继续")
        self.log.info("=" * 50)

        entry_url = course_url or COURSE_ENTRY_URL
        p, browser, page = self._build_page(entry_url)

        try:
            result = self._run_captcha(page, COURSE_CAPTCHA_APP_ID)
            self.log.success("验证码手动验证完成")
            return result
        except Exception as e:
            self.log.error(f"验证码处理失败: {e}")
            raise
        finally:
            browser.close()
            p.stop()
