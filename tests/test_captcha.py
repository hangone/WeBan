import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import captcha


class StubLog:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, message: object) -> None:
        self.messages.append(str(message))

    info = _record
    warning = _record
    error = _record
    success = _record


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeBrowser:
    def __init__(self, profile: Path | None = None) -> None:
        self.closed = False
        self._process = None
        self.config = SimpleNamespace(
            uses_custom_data_dir=profile is None,
            user_data_dir=profile,
        )

    async def aclose(self) -> None:
        self.closed = True


def make_handler(
    *,
    host: str = "127.0.0.1",
    port: int = 9222,
    non_interactive: bool = True,
) -> captcha.CaptchaHandler:
    return captcha.CaptchaHandler(
        tenant_code="tenant",
        user_id="user",
        token="token",
        log=StubLog(),
        cdp_host=host,
        cdp_port=port,
        non_interactive=non_interactive,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2),
        ("", 2),
        ("abc", 2),
        ("0", 2),
        ("-3", 2),
        (" 4 ", 4),
        ("999", 50),
    ],
)
def test_env_retry_count_falls_back_safely(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv("WB_CAPTCHA_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("WB_CAPTCHA_ROUNDS", raw)

    assert captcha._env_positive_int("WB_CAPTCHA_ROUNDS", 2) == expected


def test_check_browser_health_requests_real_cdp_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fake_get(url: str, *, timeout: float) -> FakeResponse:
        calls.append((url, timeout))
        return FakeResponse(
            {
                "Browser": "Chrome/128.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/id",
            }
        )

    monkeypatch.setattr(captcha.requests, "get", fake_get)

    result = captcha.check_browser_health(cdp_host="127.0.0.1", cdp_port=9222)

    assert result == "127.0.0.1:9222"
    assert calls == [("http://127.0.0.1:9222/json/version", captcha.CDP_HEALTH_TIMEOUT)]


def test_check_browser_health_prefers_cdp_over_missing_browser_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(url)
        return FakeResponse(
            {
                "Browser": "Chrome/128.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/id",
            }
        )

    monkeypatch.setattr(captcha.requests, "get", fake_get)

    result = captcha.check_browser_health(
        browser_path="missing-browser.exe",
        cdp_host="127.0.0.1",
        cdp_port=9222,
    )

    assert result == "127.0.0.1:9222"
    assert calls == ["http://127.0.0.1:9222/json/version"]


def test_check_browser_health_rejects_non_cdp_http_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        captcha.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"status": "ok"}),
    )

    with pytest.raises(RuntimeError, match="缺少 Browser"):
        captcha.check_browser_health(cdp_host="localhost", cdp_port=9222)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("127.0.0.1", None),
        (None, 9222),
    ],
)
def test_check_browser_health_rejects_partial_cdp_config(
    host: str | None,
    port: int | None,
) -> None:
    with pytest.raises(RuntimeError, match="必须同时提供"):
        captcha.check_browser_health(cdp_host=host, cdp_port=port)


def test_check_browser_health_rejects_missing_explicit_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detected = False

    def fake_detect() -> None:
        nonlocal detected
        detected = True

    monkeypatch.setattr(captcha, "detect_browser", fake_detect)

    with pytest.raises(RuntimeError, match="显式指定.*不存在"):
        captcha.check_browser_health(browser_path=str(tmp_path / "missing.exe"))

    assert detected is False


def test_handler_validates_its_final_account_browser_config() -> None:
    with pytest.raises(RuntimeError, match="必须同时提供"):
        captcha.CaptchaHandler(
            tenant_code="tenant",
            user_id="user",
            token="token",
            log=StubLog(),
            cdp_host="127.0.0.1",
        )


def test_handler_defers_network_health_check_until_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str | None, int | None]] = []

    def fake_check(
        path: str | None,
        host: str | None,
        port: int | None,
    ) -> str:
        calls.append((path, host, port))
        return "127.0.0.1:9222"

    monkeypatch.setattr(captcha, "check_browser_health", fake_check)
    handler = make_handler()

    assert calls == []
    asyncio.run(handler._ensure_browser_ready())
    asyncio.run(handler._ensure_browser_ready())

    assert calls == [(None, "127.0.0.1", 9222)]


def test_handler_lazily_discovers_default_cdp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_local_browser = False

    def fail_local_check(*args: object, **kwargs: object) -> str:
        nonlocal checked_local_browser
        del args, kwargs
        checked_local_browser = True
        raise AssertionError("不应继续探测本地浏览器")

    monkeypatch.setattr(
        captcha,
        "_detect_default_cdp_endpoint",
        lambda: ("127.0.0.1", 9223),
    )
    monkeypatch.setattr(captcha, "check_browser_health", fail_local_check)
    handler = captcha.CaptchaHandler(
        tenant_code="tenant",
        user_id="user",
        token="token",
        log=StubLog(),
        non_interactive=True,
    )

    asyncio.run(handler._ensure_browser_ready())

    assert (handler.cdp_host, handler.cdp_port) == ("127.0.0.1", 9223)
    assert handler._browser_ready is True
    assert checked_local_browser is False


def test_course_url_origin_excludes_path_query_and_fragment() -> None:
    assert (
        captcha._origin_from_url("https://mcwk.mycourse.cn/course/view?id=1#/chapter")
        == "https://mcwk.mycourse.cn"
    )


def test_shared_cdp_endpoint_serializes_captcha_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    max_active = 0

    async def fake_flow(
        self: captcha.CaptchaHandler,
        user_exam_plan_id: str,
    ) -> dict[str, str]:
        nonlocal active, max_active
        del self
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return {"randstr": user_exam_plan_id, "ticket": "ticket"}

    monkeypatch.setattr(
        captcha.CaptchaHandler,
        "_handle_exam_captcha_flow",
        fake_flow,
    )
    first = make_handler(port=19321)
    second = make_handler(port=19321)
    first._browser_ready = True
    second._browser_ready = True

    async def run_both() -> list[dict[str, str]]:
        return list(
            await asyncio.gather(
                first.handle_exam_captcha_async("first"),
                second.handle_exam_captcha_async("second"),
            )
        )

    results = asyncio.run(run_both())

    assert max_active == 1
    assert {result["randstr"] for result in results} == {"first", "second"}


def test_exam_flow_has_hard_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never_finishes(
        self: captcha.CaptchaHandler,
        user_exam_plan_id: str,
    ) -> dict[str, str]:
        del self, user_exam_plan_id
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(captcha, "EXAM_FLOW_TIMEOUT", 0.02)
    monkeypatch.setattr(
        captcha.CaptchaHandler,
        "_handle_exam_captcha_flow",
        never_finishes,
    )
    handler = make_handler(port=19322)
    handler._browser_ready = True

    with pytest.raises(RuntimeError, match="无感验证码处理超时"):
        asyncio.run(handler.handle_exam_captcha_async("plan"))


def test_cleanup_only_closes_explicitly_owned_instance() -> None:
    from nodriver.core import util as nd_util

    owned = FakeBrowser()
    foreign = FakeBrowser()
    registry = nd_util.get_registered_instances()
    registry_any: Any = registry
    registry_any.update({owned, foreign})
    try:
        asyncio.run(captcha.kill_stray_browsers([owned]))

        assert owned.closed is True
        assert owned not in registry
        assert foreign.closed is False
        assert foreign in registry
    finally:
        registry.discard(owned)
        registry.discard(foreign)


def test_local_health_probe_preserves_preexisting_registered_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nodriver.core import util as nd_util

    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"stub")
    executable.chmod(0o755)
    owned = FakeBrowser()
    foreign = FakeBrowser()
    registry = nd_util.get_registered_instances()
    registry_any: Any = registry
    registry_any.add(foreign)

    async def get_probe_page(url: str) -> None:
        assert url.startswith("data:text/html")

    owned.get = get_probe_page  # type: ignore[attr-defined]

    async def fake_start(**kwargs: object) -> FakeBrowser:
        del kwargs
        registry_any.add(owned)
        return owned

    monkeypatch.setattr(captcha.nodriver, "start", fake_start)
    try:
        result = captcha.check_browser_health(browser_path=str(executable))

        assert result == str(executable.resolve())
        assert owned.closed is True
        assert owned not in registry
        assert foreign.closed is False
        assert foreign in registry
    finally:
        registry.discard(owned)
        registry.discard(foreign)


def test_cleanup_removes_only_owned_temporary_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "state").write_text("temporary", encoding="utf-8")
    browser = FakeBrowser(profile)

    asyncio.run(captcha.kill_stray_browsers([browser]))

    assert browser.closed is True
    assert not profile.exists()


@pytest.mark.parametrize(
    ("entry_url", "initial_origin"),
    [
        ("https://example.test/course/item?id=3#section", "https://example.test"),
        ("https://example.test:443/course", "https://example.test:443"),
        ("https://EXAMPLE.test/course", "https://EXAMPLE.test"),
        ("http://example.test/course", "http://example.test"),
    ],
    ids=["unchanged", "default-port", "hostname-case", "https-redirect"],
)
def test_build_page_uses_origin_and_restores_local_storage(
    monkeypatch: pytest.MonkeyPatch,
    entry_url: str,
    initial_origin: str,
) -> None:
    class FakeTab:
        def __init__(self) -> None:
            self.navigated_to: list[str] = []
            self.scripts: list[str] = []
            self.closed = False

        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            self.scripts.append(expression)
            if "localStorage.length" in expression:
                return {
                    "origin": "https://example.test",
                    "items": {
                        "user": "previous-user",
                        "theme": "dark",
                    },
                }
            if return_by_value and "typeof TencentCaptcha" in expression:
                return True
            if return_by_value and "window.location.origin" in expression:
                return "https://example.test"
            return None

        async def get(self, url: str) -> None:
            self.navigated_to.append(url)

        async def close(self) -> None:
            self.closed = True

    class PageBrowser(FakeBrowser):
        def __init__(self, tab: FakeTab) -> None:
            super().__init__()
            self.tab = tab
            self.opened: list[tuple[str, bool]] = []

        async def get(self, url: str, *, new_tab: bool = False) -> FakeTab:
            self.opened.append((url, new_tab))
            return self.tab

    handler = make_handler(port=19323)
    tab = FakeTab()
    browser = PageBrowser(tab)

    async def no_health_check() -> None:
        return None

    async def create_browser(headless: bool = False) -> PageBrowser:
        assert headless is True
        return browser

    monkeypatch.setattr(handler, "_ensure_browser_ready", no_health_check)
    monkeypatch.setattr(handler, "_create_browser", create_browser)

    async def scenario() -> None:
        built_browser, built_tab = await handler._build_page(
            entry_url,
            headless=True,
        )
        assert (built_browser, built_tab) == (browser, tab)
        assert handler._browser_states[id(browser)]["origin"] == "https://example.test"
        browser_any: Any = browser
        await handler._quit_browser(browser_any, "test")

    asyncio.run(scenario())

    assert browser.opened == [(f"{initial_origin}/", True)]
    assert tab.navigated_to == [entry_url]
    assert any("previous-user" in script for script in tab.scripts)
    assert any("localStorage.clear()" in script for script in tab.scripts)
    assert tab.closed is True
    assert browser.closed is True


def test_cleanup_skips_local_storage_restore_when_origin_navigation_fails() -> None:
    class FailingNavigationTab:
        def __init__(self) -> None:
            self.scripts: list[str] = []
            self.navigated_to: list[str] = []
            self.closed = False

        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            self.scripts.append(expression)
            if return_by_value and "window.location.origin" in expression:
                return "https://other.test"
            return None

        async def get(self, url: str) -> None:
            self.navigated_to.append(url)
            raise RuntimeError("navigation failed")

        async def close(self) -> None:
            self.closed = True

    handler = make_handler(port=19328)
    browser = FakeBrowser()
    tab = FailingNavigationTab()
    handler._browser_states[id(browser)] = {
        "tab": tab,
        "storage": {"user": "previous-user"},
        "close_tab": True,
        "origin": "https://example.test",
    }

    browser_any: Any = browser
    asyncio.run(handler._quit_browser(browser_any, "navigation-failure"))

    assert tab.navigated_to == ["https://example.test/"]
    assert not any("localStorage.clear()" in script for script in tab.scripts)
    assert tab.closed is True
    assert browser.closed is True


def test_cleanup_skips_local_storage_restore_after_origin_redirect() -> None:
    class RedirectingTab:
        def __init__(self) -> None:
            self.current_origin = "https://other.test"
            self.origin_checks = 0
            self.scripts: list[str] = []
            self.navigated_to: list[str] = []
            self.closed = False

        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            self.scripts.append(expression)
            if return_by_value and "window.location.origin" in expression:
                self.origin_checks += 1
                return self.current_origin
            return None

        async def get(self, url: str) -> None:
            self.navigated_to.append(url)
            self.current_origin = "https://redirected.test"

        async def close(self) -> None:
            self.closed = True

    handler = make_handler(port=19329)
    browser = FakeBrowser()
    tab = RedirectingTab()
    handler._browser_states[id(browser)] = {
        "tab": tab,
        "storage": {"user": "previous-user"},
        "close_tab": True,
        "origin": "https://example.test",
    }

    browser_any: Any = browser
    asyncio.run(handler._quit_browser(browser_any, "origin-redirect"))

    assert tab.navigated_to == ["https://example.test/"]
    assert tab.origin_checks == 2
    assert not any("localStorage.clear()" in script for script in tab.scripts)
    assert tab.closed is True
    assert browser.closed is True


def test_empty_local_storage_snapshot_is_valid() -> None:
    class EmptyStorageTab:
        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            del expression, return_by_value
            return {"origin": "https://example.test", "items": {}}

    handler = make_handler(port=19325)

    assert asyncio.run(handler._snapshot_local_storage(EmptyStorageTab())) == (
        "https://example.test",
        {},
    )


def test_local_storage_snapshot_requires_its_origin() -> None:
    class MissingOriginTab:
        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            return {"items": {"user": "previous-user"}}

    handler = make_handler(port=19371)
    with pytest.raises(RuntimeError, match="localStorage 快照"):
        asyncio.run(handler._snapshot_local_storage(MissingOriginTab()))


def test_sdk_loading_has_hard_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverReadyTab:
        async def evaluate(
            self,
            expression: str,
            *,
            return_by_value: bool = False,
        ) -> object:
            del expression
            return False if return_by_value else None

    monkeypatch.setattr(captcha, "SDK_LOAD_TIMEOUT", 0.02)

    with pytest.raises(RuntimeError, match="SDK 加载超时"):
        asyncio.run(make_handler(port=19326)._ensure_captcha_sdk(NeverReadyTab()))


def test_cdp_script_call_has_hard_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowTab:
        async def evaluate(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            await asyncio.Event().wait()

    monkeypatch.setattr(captcha, "CDP_CALL_TIMEOUT", 0.02)

    with pytest.raises(RuntimeError, match="CDP 脚本执行超时"):
        asyncio.run(make_handler(port=19327)._eval_json(SlowTab(), "1"))


def test_browser_close_has_hard_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nodriver.core import util as nd_util

    class HangingBrowser(FakeBrowser):
        async def aclose(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(captcha, "CLOSE_TIMEOUT", 0.03)
    browser = HangingBrowser()
    browser_any: Any = browser
    registry = nd_util.get_registered_instances()
    registry_any: Any = registry
    registry_any.add(browser)
    try:
        asyncio.run(
            asyncio.wait_for(
                make_handler(port=19327)._quit_browser(browser_any, "timeout-test"),
                timeout=0.2,
            )
        )
        assert browser not in registry
    finally:
        registry.discard(browser)


def test_auto_solver_offloads_download_and_opencv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = make_handler(port=19324)
    calls: list[str] = []
    image = np.zeros((10, 10, 3), dtype=np.uint8)

    async def fake_to_thread(func: Any, *args: object, **kwargs: object) -> Any:
        calls.append(func.__name__)
        return func(*args, **kwargs)

    async def ready_state(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        return {
            "bgUrl": "https://captcha.test/main.png",
            "ansUrl": "https://captcha.test/prompt.png",
            "bgRect": {"x": 0, "y": 0, "w": 10, "h": 10},
        }

    def fake_fetch_image(url: str) -> np.ndarray:
        del url
        return image.copy()

    def fake_detect_points(
        prompt: np.ndarray,
        main: np.ndarray,
    ) -> tuple[list[None], list[dict]]:
        del prompt, main
        return [None, None, None], []

    monkeypatch.setattr(captcha.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(captcha, "fetch_image", fake_fetch_image)
    monkeypatch.setattr(captcha, "detect_points", fake_detect_points)
    monkeypatch.setattr(handler, "_wait_until", ready_state)

    result = asyncio.run(handler._auto_solve_once(object(), 1, False))

    assert result is None
    assert calls.count("fake_fetch_image") == 2
    assert "fake_detect_points" in calls


def test_atomic_debug_write_cleans_temporary_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(captcha.cv2, "imwrite", lambda *args, **kwargs: False)

    with pytest.raises(OSError, match="无法写入"):
        captcha._write_png_atomic(
            tmp_path / "debug.png",
            np.zeros((2, 2, 3), dtype=np.uint8),
        )

    assert list(tmp_path.iterdir()) == []


def test_missing_login_model_has_clear_safe_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = StubLog()
    monkeypatch.setattr(captcha, "__file__", str(tmp_path / "captcha.py"))
    monkeypatch.setattr(captcha.LoginCaptchaSolver, "_initialized", False)
    monkeypatch.setattr(captcha.LoginCaptchaSolver, "_ocr", None)
    monkeypatch.setattr(captcha.sys, "frozen", False, raising=False)

    assert captcha.LoginCaptchaSolver.get_ocr(log) is None
    assert captcha.LoginCaptchaSolver.recognize(b"invalid", log) is None
    message = "\n".join(log.messages)
    assert "模型文件不存在" in message
    assert "交互模式可人工输入" in message
    assert "无交互模式将安全失败" in message
