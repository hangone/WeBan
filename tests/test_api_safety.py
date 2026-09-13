from __future__ import annotations

import json
from collections.abc import Callable

import pytest
import requests

import api as api_module
from api import LoggingSession, WeBanAPI, handle_response
from errors import AccountBlockedError, APIResponseError


def _response(
    payload: dict | str,
    status: int = 200,
    content_type: str = "application/json",
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response.headers["Content-Type"] = content_type
    if isinstance(payload, dict):
        response._content = json.dumps(payload, ensure_ascii=False).encode()
    else:
        response._content = payload.encode()
    return response


class _ListLog:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str) -> None:
        self.messages.append(str(message))

    def error(self, message: str) -> None:
        self.messages.append(str(message))


def _transport(
    responses: list[requests.Response],
    calls: list[tuple[str, str]],
) -> Callable[..., requests.Response]:
    def request(method: str, url: str, **kwargs: object) -> requests.Response:
        del kwargs
        calls.append((method, url))
        return responses.pop(0)

    return request


def test_side_effect_request_is_sent_once_but_query_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = WeBanAPI(
        tenant_code="tenant",
        user={"userId": "user", "token": "token"},
    )
    calls: list[tuple[str, str]] = []
    responses = [_response("server error", 500, "text/plain")]
    monkeypatch.setattr(
        api.session._session,
        "request",
        _transport(responses, calls),
    )
    monkeypatch.setattr(api_module.time, "sleep", lambda _: None)

    with pytest.raises(APIResponseError):
        api.exam_submit_paper("plan")
    assert len(calls) == 1

    calls.clear()
    responses.extend(
        [
            _response("busy", 500, "text/plain"),
            _response({"code": "0", "data": []}),
        ]
    )
    assert api.exam_list_plan("project") == {"code": "0", "data": []}
    assert len(calls) == 2


def test_jsonp_finish_get_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = WeBanAPI(
        tenant_code="tenant",
        user={"userId": "user", "token": "token"},
    )
    calls: list[tuple[str, str]] = []
    responses = [_response("busy", 503, "text/plain")]
    monkeypatch.setattr(
        api.session._session,
        "request",
        _transport(responses, calls),
    )

    with pytest.raises(APIResponseError):
        api.finish_by_token("course")
    assert len(calls) == 1


def test_structured_error_redacts_endpoint_and_body() -> None:
    response = _response(
        {
            "token": "token-secret",
            "userId": "user-secret",
            "message": "failed",
        },
        status=500,
    )

    with pytest.raises(APIResponseError) as caught:
        handle_response(
            response,
            endpoint="https://example.test/api?ticket=ticket-secret",
            strict=True,
        )

    rendered = str(caught.value)
    assert "token-secret" not in rendered
    assert "user-secret" not in rendered
    assert "ticket-secret" not in rendered
    assert caught.value.status_code == 500


def test_debug_logging_redacts_request_and_login_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _ListLog()
    session = LoggingSession(log=log, debug=True)
    response = _response(
        {
            "token": "response-token-secret",
            "userId": "response-user-secret",
        }
    )
    monkeypatch.setattr(
        session._session,
        "request",
        lambda *args, **kwargs: response,
    )

    session.post(
        "https://weiban.mycourse.cn/test?ticket=url-ticket-secret",
        data={
            "ticket": "body-ticket-secret",
            "userId": "body-user-secret",
        },
    )

    rendered = "\n".join(log.messages)
    for secret in (
        "response-token-secret",
        "response-user-secret",
        "url-ticket-secret",
        "body-ticket-secret",
        "body-user-secret",
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "-1", "detailCode": "10018", "msg": "行为存在异常"},
        {"code": "701", "msg": "Account locked"},
    ],
)
def test_lock_codes_raise_dedicated_exception(payload: dict) -> None:
    with pytest.raises(AccountBlockedError):
        handle_response(
            _response(payload),
            endpoint="https://weiban.mycourse.cn/test",
            strict=True,
        )


def test_session_and_api_close_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = WeBanAPI(
        password="secret",
        user={"userId": "user", "token": "token"},
    )
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    monkeypatch.setattr(api.session._session, "close", close)
    api.close()
    api.close()

    assert close_calls == 1
    assert api.password is None
    assert "token" not in api.user
    assert "X-Token" not in api.session.headers
    with pytest.raises(RuntimeError, match="已关闭"):
        api.session.get("https://example.test")
