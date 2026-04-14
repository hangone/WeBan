import json
import time

from typing import TYPE_CHECKING, Any, Dict
import logging

# OCR 函数统一在 captcha 模块中维护
from .captcha import (
    _get_ocr,
    _ocr_captcha_with_retry,
)

from .base import BaseMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Login error classification (mirrors Login.vue onSubmit + util-ajax.js)
# ---------------------------------------------------------------------------

# Toast messages that indicate a permanent / non-retryable failure.
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
_RETRYABLE_ERROR_PATTERNS = (
    "验证码",
    "密码",
    "账号或密码",
    "用户名或密码",
)


def _classify_toast(msg: str) -> str:
    for pat in _FATAL_ERROR_PATTERNS:
        if pat in msg:
            return "fatal"
    for pat in _RETRYABLE_ERROR_PATTERNS:
        if pat in msg:
            return "retryable"
    if msg.strip():
        if "锁定" in msg or "停用" in msg or "禁用" in msg:
            return "fatal"
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
_SEL_POPUP_CONFIRM = (
    ".van-dialog__confirm, .mint-msgbox-confirm, "
    "button:has-text('确定'), button:has-text('确认')"
)


class AuthMixin(BaseMixin):
    if TYPE_CHECKING:
        from typing import Union as _Union
        from playwright.sync_api import Page, BrowserContext, Browser, Playwright
        from .browser import BrowserConfig
        import logging as _logging

        _page: Page | None
        _context: BrowserContext | None
        _browser: Browser | None
        _playwright: Playwright | None
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        browser_config: BrowserConfig

        def _start(self) -> None: ...

    def _storage_has_auth(self) -> bool:
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
        if not self._page:
            return False
        try:
            loc = self._page.locator(_SEL_LOGIN_FORM_INPUTS)
            if loc.count() == 0:
                return False
            for i in range(min(loc.count(), 8)):
                try:
                    it = loc.nth(i)
                    if it.is_visible():
                        # 检查尺寸，防止幽灵元素
                        bb = it.bounding_box()
                        if bb and bb["width"] > 5 and bb["height"] > 5:
                            return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _has_post_login_markers(self) -> bool:
        if not self._page:
            return False
        try:
            return self._page.locator(_SEL_POST_LOGIN_MARKERS).count() > 0
        except Exception:
            return False

    def _is_logged_in(self) -> bool:
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

            if ("#/login" in url or "/login" in url.split("#")[0]) and has_login_form:
                return False
            if has_post_login_markers:
                return True
            if has_auth and not has_login_form:
                return True
            if "#/login" not in url and "/login" not in url.split("#")[0] and has_auth:
                return True
            return False
        except Exception:
            return False

    def _handle_auth_response(self, response: Any) -> None:
        """拦截登录 API 响应，直接提取 Token 和用户信息。"""
        try:
            url = response.url
            if "login.do" in url and response.status == 200:
                res = response.json()
                if isinstance(res, dict) and str(res.get("code")) == "0":
                    data = res.get("data") or {}
                    token = data.get("token")
                    user = data.get("user") or {}
                    if token:
                        self.token = token
                        self._auth_captured = True
                        self.log.info(
                            f"[网络拦截] 发现有效登录 Token：{user.get('userName') or '用户'}"
                        )
                        if user.get("userId"):
                            self.user_id = str(user.get("userId"))
                        tenant_name_val = user.get("tenantName")
                        if tenant_name_val:
                            self.tenant_name = str(tenant_name_val)
        except Exception:
            pass

    def _handle_login_popups(self) -> bool:
        if not self._page:
            return False
        try:
            confirm_btn = self._page.locator(_SEL_POPUP_CONFIRM).first
            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                msg_text = "提示"
                try:
                    for sel in [
                        ".mint-msgbox-message",
                        ".van-dialog__message",
                        ".el-message__content",
                    ]:
                        msg_el = self._page.locator(sel).first
                        if msg_el.count() > 0 and msg_el.is_visible():
                            msg_text = msg_el.inner_text().strip()
                            break
                except Exception:
                    pass
                self.log.info(f"[登录辅助] 处理弹窗: {msg_text[:60]}...")
                confirm_btn.click(force=True)
                time.sleep(1)
                return True
        except Exception:
            pass
        return False

    def _navigate_and_check_login(self) -> bool:
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
        try:
            self._start()
        except Exception as e:
            self.log.error(f"浏览器启动失败: {e}")
            return {"ok": False, "msg": f"浏览器启动失败: {e}"}

        if not self._page:
            raise RuntimeError("Page is not initialized")

        # 检查页面是否已关闭
        def is_page_valid() -> bool:
            try:
                return self._page is not None and not self._page.is_closed()
            except Exception:
                return False

        def _extract_user_result() -> Dict[str, Any]:
            result: Dict[str, Any] = {"ok": True}
            try:
                if not self._page or self._page.is_closed():
                    return result
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

        if self.user_id and self.token:
            self.log.info("检测到配置了 userId 和 token，尝试直接使用...")
            if not is_page_valid():
                self.log.warning("页面已关闭，尝试重新启动浏览器...")
                self._start()
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
            if self._is_logged_in():
                self.log.info("使用配置的 Token 登录成功")
                return _extract_user_result()

        if not is_page_valid():
            self.log.warning("页面已关闭，尝试重新启动浏览器...")
            self._start()

        self._page.goto(f"{self.base_url}/#/", wait_until="domcontentloaded")
        time.sleep(1)
        self.log.info("进入登录流程")
        self._auth_captured = False

        self._page.on("response", self._handle_auth_response)
        try:
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
                    try:
                        self._page.locator(_SEL_MODAL_OVERLAY).wait_for(
                            state="hidden", timeout=3000
                        )
                    except Exception:
                        pass
                except Exception as e:
                    self.log.warning(f"自动选择学校失败: {e}")

                for _ in range(2):
                    if self._handle_login_popups():
                        time.sleep(0.5)
                    else:
                        break

            # --- 填写账号密码 ---
            if self.account and self.password:
                try:
                    acc_input = self._page.locator(_SEL_INPUT_ACCOUNT).first
                    acc_input.wait_for(state="visible", timeout=5000)
                    acc_input.fill(self.account)
                    pwd_input = self._page.locator(_SEL_INPUT_PASSWORD).first
                    pwd_input.wait_for(state="visible", timeout=5000)
                    pwd_input.fill(self.password)

                    capt_img = self._page.locator(_SEL_CAPTCHA_IMG).first
                    try:
                        capt_img.wait_for(state="visible", timeout=5000)
                    except Exception:
                        pass

                    if capt_img.is_visible():
                        ocr = _get_ocr()
                        code = _ocr_captcha_with_retry(capt_img, ocr, self.log)
                        if code:
                            self.log.debug(f"[文字验证码] 识别结果: {code}")
                            capt_input = self._page.locator(_SEL_CAPTCHA_INPUT).first
                            if capt_input.is_visible():
                                capt_input.fill(code)

                    submit_loc = self._page.locator(_SEL_LOGIN_SUBMIT_BTN)
                    if submit_loc.count() > 0:
                        submit_loc.first.click(force=True)
                    else:
                        self._page.keyboard.press("Enter")
                    time.sleep(2)
                except Exception as e:
                    self.log.warning(f"自动填写表单失败: {e}")

            # --- 轮询等待 ---
            deadline = time.time() + self.browser_config.manual_login_timeout_sec
            _last_reported: set = set()
            _auth_detected_at: float | None = None
            _last_was_tencent: bool = False

            while time.time() < deadline:
                if not is_page_valid():
                    self.log.warning("页面已关闭，退出登录流程")
                    return {"ok": False, "msg": "页面已关闭"}

                if self._is_logged_in() or getattr(self, "_auth_captured", False):
                    self.log.info("登录成功")
                    return _extract_user_result()

                try:
                    has_auth = self._storage_has_auth()
                    has_form = self._has_login_form()
                    if has_auth and not has_form:
                        if _auth_detected_at is None:
                            _auth_detected_at = time.time()
                            self.log.info("发现登录态，等待跳转...")
                        elif time.time() - _auth_detected_at >= 2:
                            if self._navigate_and_check_login():
                                return _extract_user_result()
                    else:
                        _auth_detected_at = None
                except Exception:
                    pass

                # 注：登录页通常只有文字验证码，不需要检查点选验证码
                # 腾讯点选验证码主要出现在课程完成页，不在登录页
                # 因此这里注释掉点选验证码检查，避免误判
                pass

                self._handle_login_popups()

                try:
                    raw_msgs = self._page.locator(_SEL_TOAST_MESSAGE).all_inner_texts()
                    for msg in [m.strip() for m in raw_msgs if m.strip()]:
                        kind = _classify_toast(msg)
                        if kind == "ignore":
                            continue
                        if msg not in _last_reported:
                            _last_reported.add(msg)
                            if kind == "fatal":
                                self.log.error(f"登录失败：{msg}")
                                return {"ok": False, "msg": msg}
                            self.log.warning(f"提示：{msg}")

                        if "验证码" in msg and any(
                            k in msg for k in ("错", "误", "效", "不正确")
                        ):
                            capt_img = self._page.locator(_SEL_CAPTCHA_IMG).first
                            if capt_img.is_visible():
                                ocr = _get_ocr()
                                code = _ocr_captcha_with_retry(capt_img, ocr, self.log)
                                if code:
                                    self.log.info(f"[验证码重试] 识别结果: {code}")
                                    capt_input = self._page.locator(
                                        _SEL_CAPTCHA_INPUT
                                    ).first
                                    if capt_input.is_visible():
                                        capt_input.fill(code)
                                        submit_loc = self._page.locator(
                                            _SEL_LOGIN_SUBMIT_BTN
                                        )
                                        if submit_loc.count() > 0:
                                            submit_loc.first.click(force=True)
                                        else:
                                            self._page.keyboard.press("Enter")
                                        time.sleep(2)
                                        _last_reported.discard(msg)
                except Exception:
                    pass
                time.sleep(1.0)
        finally:
            try:
                if self._page:
                    self._page.remove_listener("response", self._handle_auth_response)
            except Exception:
                pass

        if self._is_logged_in():
            return _extract_user_result()
        self.log.error("登录超时")
        return {"ok": False}
