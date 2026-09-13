import pytest
import requests


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何未被本地替身接管的 HTTP 请求都应立即失败。"""

    def fail_request(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("测试禁止访问真实网络")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_request)
