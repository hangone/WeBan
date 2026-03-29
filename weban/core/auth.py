import json
import time

from typing import TYPE_CHECKING, Any, Dict
import logging

# OCR 函数统一在 captcha 模块中维护
from .captcha import (
    handle_click_captcha,
    has_captcha,
    _get_ocr,
    _ocr_captcha_with_retry,
)

from .base import BaseMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Login error classification (mirrors Login.vue onSubmit + util-ajax.js)
# ---------------------------------------------------------------------------

# Toast messages that indicate a permanent / non-retryable failure.
# When any of these substrings appear we stop waiting and return early.
_FATAL_ERROR_PATTERNS = (
    "账号不存在",
    "用户不存在",
    "账号已被禁用",
    "账号已锁定",
    "账号已停用",
    "已被冻结",
    "不存在该用户",
    "学校不存在",
    "该学校",  # e.g. "该学校暂未开放"
    "信息无效",  # detailCode=4: "信息无效，请联系在线课服"
    "无效账号",
    "未找到用户",
    "未注册",
)

# Toast messages that are retryable (captcha / password wrong → user may fix).
# We log them as warnings but keep polling.
_RETRYABLE_ERROR_PATTERNS = (
    "验证码",
    "密码",
    "账号或密码",
    "用户名或密码",
)


def _classify_toast(msg: str) -> str:
    """Classify a login toast message.

    Returns:
        'fatal'     – stop immediately, login cannot succeed
        'retryable' – log warning and keep waiting
        'ignore'    – unrelated toast, skip
    """
    for pat in _FATAL_ERROR_PATTERNS:
        if pat in msg:
            return "fatal"
    for pat in _RETRYABLE_ERROR_PATTERNS:
        if pat in msg:
            return "retryable"
    # Any other non-empty server message is worth showing but we keep waiting
    if msg.strip():
        return "retryable"
    return "ignore"


# ---------------------------------------------------------------------------
# DOM 元素选择器常量定义
# ---------------------------------------------------------------------------
_SEL_LOGIN_FORM_INPUTS = (
    "input[placeholder*='选择学校'], "
    "input[placeholder*='搜索关键词'], "
    "input[placeholder*='账号'], "
    "input[placeholder*='学号'], "
    "input[type='password'], "
    "a.loginp-submit, button[type='submit'], button:has-text('登录')"
)
_SEL_POST_LOGIN_MARKERS = (
    ".task-block, .van-tab, .van-collapse-item, .img-texts-item, "
    ".fchl-item, .broadcast-modal, .img-text-block, #agree"
)
_SEL_INPUT_TENANT = "input[placeholder*='选择学校']"
_SEL_INPUT_TENANT_SEARCH = "input[placeholder*='搜索关键词']"
_SEL_MODAL_OVERLAY = ".v-modal, .van-overlay"
_SEL_INPUT_ACCOUNT = "input[type='text']:not([readonly]), input[placeholder*='账号'], input[placeholder*='学号']"
_SEL_INPUT_PASSWORD = "input[type='password']"
_SEL_CAPTCHA_IMG = "img.loginp-label-verify, img[src*='randLetterImage']"
_SEL_CAPTCHA_INPUT = "input[maxlength='6'][autocomplete='off'], input[maxlength='6']"
_SEL_LOGIN_SUBMIT_BTN = (
    "a.loginp-submit, button[type='submit'], button:has-text('登录'):not([disabled])"
)
_SEL_TOAST_MESSAGE = (
    ".van-toast__text, .van-toast, "
    ".mint-toast, .mint-toast-text, "
    ".van-dialog__message, .el-message__content"
)


class AuthMixin(BaseMixin):
    if TYPE_CHECKING:
        from typing import Union as _Union
        from playwright.sync_api import Page, BrowserContext, Browser, Playwright
        from .browser import BrowserConfig
        import logging as _logging

        _page: Page
        _context: BrowserContext
        _browser: Browser
        _playwright: Playwright
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        continue_on_invalid_token: bool
        browser_config: BrowserConfig

        def _start(self) -> None: ...

    def _storage_has_auth(self) -> bool:
        """检查 localStorage 中是否已写入登录态。"""
        if not self._page:
            return False
        try:
            token = self._page.evaluate("localStorage.getItem('token')")
            user_data = self._page.evaluate("localStorage.getItem('user')")
            return bool(token and str(token).strip()) and bool(
                user_data and str(user_data).strip()
            )
        except Exception:
            return False

    def _has_login_form(self) -> bool:
        """检查当前页面是否仍停留在登录表单。"""
        if not self._page:
            return False
        try:
            loc = self._page.locator(_SEL_LOGIN_FORM_INPUTS)
            if loc.count() == 0:
                return False
            for i in range(min(loc.count(), 8)):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _has_post_login_markers(self) -> bool:
        """检查登录后首页/任务页的关键元素是否已出现。"""
        if not self._page:
            return False
        try:
            return self._page.locator(_SEL_POST_LOGIN_MARKERS).count() > 0
        except Exception:
            return False

    def _is_logged_in(self) -> bool:
        """检查当前页面是否已登录。综合 URL、localStorage 和页面结构判断。"""
        if not self._page:
            return False
        try:
            url = self._page.url.lower()
            if not url or url == "about:blank":
                return False
            if "weiban.mycourse.cn" not in url:
                return False
            if "#/403" in url:
                return False

            has_auth = self._storage_has_auth()
            has_login_form = self._has_login_form()
            has_post_login_markers = self._has_post_login_markers()

            # 登录页且表单仍可见，认为尚未登录成功
            if ("#/login" in url or "/login" in url.split("#")[0]) and has_login_form:
                return False

            # 出现登录后页面特征，直接视为成功
            if has_post_login_markers:
                return True

            # 某些单页应用场景下 URL 还没切走，但 localStorage 已写入且表单消失
            if has_auth and not has_login_form:
                return True

            # 不在登录页、且 localStorage 已有登录态，也视为成功
            if "#/login" not in url and "/login" not in url.split("#")[0] and has_auth:
                return True

            return False
        except Exception:
            return False

    def _navigate_and_check_login(self) -> bool:
        """导航到任务页后检查是否已登录。"""
        if not self._page:
            return False
        try:
            self._page.goto(
                f"{self.base_url}/#/learning-task-list",
                wait_until="domcontentloaded",
            )
            time.sleep(2)
            return self._is_logged_in()
        except Exception:
            return False

    def login(self) -> Dict[str, Any]:
        self._start()

        def _extract_user_result() -> Dict[str, Any]:
            result: Dict[str, Any] = {"ok": True}
            try:
                user_data = self._page.evaluate("localStorage.getItem('user')")
                if user_data:
                    user_obj = json.loads(user_data)
                    result["tenant_name"] = user_obj.get("tenantName", "")
                    result["username"] = (
                        user_obj.get("uniqueValue") or user_obj.get("userName") or ""
                    )
            except Exception:
                pass
            return result

        # --- Token 注入模式 ---
        injected = False
        if self.user_id and self.token:
            self.log.info("检测到配置了 userId 和 token，尝试直接使用...")
            self._page.goto(f"{self.base_url}/#/", wait_until="domcontentloaded")
            self._page.evaluate(f"""
                localStorage.setItem('token', '{self.token}');
                let user = localStorage.getItem('user');
                let userObj = user ? JSON.parse(user) : {{}};
                userObj.id = '{self.user_id}';
                userObj.userId = '{self.user_id}';
                localStorage.setItem('user', JSON.stringify(userObj));
            """)
            self._page.reload(wait_until="domcontentloaded")
            time.sleep(2)
            injected = True
            if self._is_logged_in():
                self.log.info("使用配置的 Token 登录成功")
                return _extract_user_result()
            else:
                self.log.warning("提供的 Token 无效或已过期")
                if not self.continue_on_invalid_token:
                    self.log.error("已配置 token 无效时不继续使用账号密码登录")
                    return {"ok": False}
                self.log.info("尝试回退到正常登录流程...")

        if not injected:
            # 直接导航到首页，让它跳转到登录页
            self._page.goto(f"{self.base_url}/#/", wait_until="domcontentloaded")
            time.sleep(1)

        self.log.info("进入登录流程")

        # --- 选择学校 ---
        if self.tenant_name:
            try:
                tenant_input = self._page.locator(_SEL_INPUT_TENANT)
                tenant_input.wait_for(state="visible", timeout=5000)
                tenant_input.click()
                time.sleep(0.5)
                self._page.locator(_SEL_INPUT_TENANT_SEARCH).fill(self.tenant_name)
                time.sleep(0.5)
                self._page.locator(
                    f".van-cell__title span:text-is('{self.tenant_name}')"
                ).first.click()
                time.sleep(0.5)
                # 等待学校选择的遮罩层或弹窗消失，避免阻挡后续点击
                try:
                    self._page.locator(_SEL_MODAL_OVERLAY).wait_for(
                        state="hidden", timeout=3000
                    )
                except Exception:
                    pass
            except Exception as e:
                self.log.warning(f"自动选择学校失败，等待手动选择: {e}")

        # --- 填写账号密码 ---
        if self.account and self.password:
            try:
                acc_input = self._page.locator(_SEL_INPUT_ACCOUNT).first
                acc_input.wait_for(state="visible", timeout=5000)
                acc_input.fill(self.account)

                pwd_input = self._page.locator(_SEL_INPUT_PASSWORD).first
                pwd_input.wait_for(state="visible", timeout=5000)
                pwd_input.fill(self.password)

                # 图片验证码
                capt_img = self._page.locator(_SEL_CAPTCHA_IMG).first
                try:
                    capt_img.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass

                if capt_img.is_visible():
                    try:
                        ocr = _get_ocr()
                        code = _ocr_captcha_with_retry(capt_img, ocr, self.log)
                        if code is None:
                            self.log.warning(
                                "[文字验证码] 识别失败，跳过自动填写，等待手动输入"
                            )
                        else:
                            self.log.debug(f"[文字验证码] ddddocr 识别结果: {code}")
                            capt_input = self._page.locator(_SEL_CAPTCHA_INPUT).first
                            capt_input.wait_for(state="visible", timeout=2000)
                            capt_input.fill(code)
                    except Exception as e:
                        self.log.error(f"[文字验证码] 处理失败: {e}")

                # 点击登录按钮（a.loginp-submit 是 Login.vue 中唯一的提交按钮）
                try:
                    submit_loc = self._page.locator(_SEL_LOGIN_SUBMIT_BTN)
                    if submit_loc.count() > 0:
                        submit_loc.first.click(force=True)
                    else:
                        self._page.keyboard.press("Enter")
                except Exception:
                    self._page.keyboard.press("Enter")
                time.sleep(2)
            except Exception as e:
                self.log.warning(f"自动填写账号密码失败，等待手动输入: {e}")

        # --- 轮询等待登录成功 ---
        deadline = time.time() + self.browser_config.manual_login_timeout_sec
        _last_reported: set = set()  # 已上报过的 toast，避免重复刷屏
        _auth_detected_at: float | None = None

        while time.time() < deadline:
            # 优先直接判断是否已经登录成功
            if self._is_logged_in():
                self.log.info("登录成功")
                return _extract_user_result()

            # 某些手动登录场景下会先写入 localStorage，再异步跳转页面
            try:
                has_auth = self._storage_has_auth()
                has_login_form = self._has_login_form()

                if has_auth and not has_login_form:
                    if _auth_detected_at is None:
                        _auth_detected_at = time.time()
                        self.log.info("检测到登录态已写入，等待页面完成跳转...")
                    elif time.time() - _auth_detected_at >= 2:
                        if self._navigate_and_check_login():
                            self.log.info("登录成功")
                            return _extract_user_result()
                else:
                    _auth_detected_at = None
            except Exception:
                pass

            # ---- 点选验证码 ----
            try:
                if has_captcha(self._page):
                    self.log.info("[点选验证码] 检测到点选验证码，尝试自动识别...")
                    handle_click_captcha(self._page, self.log)
            except Exception:
                break

            # ---- 检测页面 Toast / Dialog 错误提示 ----
            try:
                raw_msgs = self._page.locator(_SEL_TOAST_MESSAGE).all_inner_texts()
                msgs = [m.strip() for m in raw_msgs if m.strip()]

                for msg in msgs:
                    kind = _classify_toast(msg)

                    if kind == "ignore":
                        continue

                    # 只对新出现的消息上报
                    if msg not in _last_reported:
                        _last_reported.add(msg)

                        if kind == "fatal":
                            self.log.error(f"登录失败：{msg}")
                            return {"ok": False, "msg": msg}
                        else:
                            # retryable：记录警告，判断是否为验证码错误并自动重试
                            self.log.warning(f"登录提示：{msg}")

                    # 验证码识别错误 → 自动刷新重试
                    if "验证码" in msg and any(
                        k in msg for k in ("错", "误", "效", "不正确")
                    ):
                        capt_img = self._page.locator(_SEL_CAPTCHA_IMG).first
                        if capt_img.is_visible():
                            ocr = _get_ocr()
                            code = _ocr_captcha_with_retry(capt_img, ocr, self.log)
                            if code is None:
                                self.log.warning(
                                    "[文字验证码] 重新识别失败，继续等待手动处理"
                                )
                            else:
                                self.log.info(f"[文字验证码] 重新识别结果: {code}")
                                capt_input = self._page.locator(
                                    _SEL_CAPTCHA_INPUT
                                ).first
                                if capt_input.is_visible():
                                    capt_input.fill(code)
                                    try:
                                        submit_loc = self._page.locator(
                                            _SEL_LOGIN_SUBMIT_BTN
                                        )
                                        if submit_loc.count() > 0:
                                            submit_loc.first.click(force=True)
                                        else:
                                            self._page.keyboard.press("Enter")
                                    except Exception:
                                        self._page.keyboard.press("Enter")
                                    time.sleep(2)
                                    _last_reported.discard(msg)  # 允许下次重新上报

            except Exception:
                pass

            if self._is_logged_in():
                self.log.info("登录成功")
                return _extract_user_result()
            time.sleep(1.0)

        self.log.error("登录超时，请检查账号、密码或网络是否正常")
        return {"ok": False}
