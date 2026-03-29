from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict
import logging
from .base import BaseMixin

from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

if TYPE_CHECKING:
    import logging as _logging
    from playwright.sync_api import Page, BrowserContext, Browser, Playwright


@dataclass
class BrowserConfig:
    """浏览器启动与超时相关配置项。"""

    enabled: bool = False  # 是否启用浏览器模式
    headless: bool = False  # 是否无头（后台静默）运行
    channel: str = "chromium"  # 浏览器引擎：chromium / firefox / webkit
    slow_mo: int = 0  # 每步操作间隔（毫秒），调试时可适当调大
    timeout_ms: int = 30000  # 页面元素等待全局超时（毫秒）
    manual_login_timeout_sec: int = 300  # 人工扫码/输入验证码的最长等待时间（秒）


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 反自动化检测注入脚本（在每个新页面创建前注入）
# 目的：抹除 Playwright 留下的 webdriver 特征，降低被网站风控识别的概率
# ---------------------------------------------------------------------------
_STEALTH_JS = """
// 1. 隐藏 webdriver 属性：正常浏览器该属性为 undefined，自动化框架会暴露为 true
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. 模拟 Chrome 运行时对象，部分网站会检测 window.chrome 是否存在
window.navigator.chrome = { runtime: {} };

// 3. 模拟插件列表，正常浏览器有多个插件，无头浏览器默认为空数组
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

// 4. 模拟语言偏好，避免空语言列表被识别为自动化环境
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// 5. 修复 Notification 权限查询，防止权限 API 抛出异常暴露自动化特征
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


class BrowserMixin(BaseMixin):
    """封装 Playwright 浏览器的启动、注入与关闭逻辑。"""

    if TYPE_CHECKING:
        from typing import Union as _Union

        _page: "Page"
        _context: "BrowserContext"
        _browser: "Browser"
        _playwright: "Playwright"
        log: "_Union[_logging.Logger, _logging.LoggerAdapter]"
        base_url: str
        token: str
        user_id: str
        tenant_name: str
        account: str
        password: str
        continue_on_invalid_token: bool
        browser_config: "BrowserConfig"
        answers: Dict[str, Any]

    @staticmethod
    def _parse_browser_config(raw: Dict[str, Any]) -> BrowserConfig:
        """将原始 dict 配置解析为 BrowserConfig 数据类实例。"""
        return BrowserConfig(
            enabled=bool(raw.get("enabled", False)),
            headless=bool(raw.get("headless", False)),
            channel=str(raw.get("channel", "chromium")),
            slow_mo=int(raw.get("slow_mo", 0)),
            timeout_ms=int(raw.get("timeout_ms", 30000)),
            manual_login_timeout_sec=int(raw.get("manual_login_timeout_sec", 300)),
        )

    def _start(self) -> None:
        """启动 Playwright 浏览器实例并创建页面。
        若浏览器已启动但页面/上下文已被异常关闭，则先清理再重新启动。
        """
        # 如果 playwright 已启动，探测 page 是否仍然有效
        if self._playwright:
            try:
                if self._context and self._page:
                    _ = self._page.url  # 触发一次属性访问，若已关闭会抛出异常
                return
            except TargetClosedError:
                self.log.warning("检测到浏览器/上下文已被异常关闭，正在重新启动...")
                self._stop()
            except Exception:
                self._stop()

        # 启动 Playwright 并选择浏览器引擎
        if sync_playwright:
            self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self.browser_config.channel, None)
        if not launcher:
            raise RuntimeError("Browser channel not found")

        # 启动浏览器，传入反检测参数
        self._browser = launcher.launch(
            headless=self.browser_config.headless,
            slow_mo=self.browser_config.slow_mo,
            args=[
                "--mute-audio",  # 静音，避免视频声音干扰
                "--disable-blink-features=AutomationControlled",  # 禁用自动化标识
                "--disable-infobars",  # 隐藏"Chrome 正受到自动化软件控制"信息栏
            ],
        )

        # 创建浏览器上下文，保留原始默认视口行为，仅设置 UA
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4 Mobile/15E148 Safari/604.1"
            ),
        )

        # 在每个新页面创建前注入反检测脚本
        self._context.add_init_script(_STEALTH_JS)

        # 设置全局超时并打开新页面
        self._context.set_default_timeout(self.browser_config.timeout_ms)
        self._page = self._context.new_page()

    def _stop(self) -> None:
        """按顺序关闭页面上下文、浏览器和 Playwright 实例，忽略所有关闭异常。"""
        for obj in [self._context, self._browser, self._playwright]:
            if obj:
                try:
                    obj.close() if hasattr(obj, "close") else obj.stop()
                except Exception:
                    pass
        # 将所有引用重置为 None，便于 _start 判断状态
        self._playwright, self._browser, self._context, self._page = (  # type: ignore[assignment]
            None,
            None,
            None,
            None,
        )
