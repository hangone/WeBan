import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest
import requests

import api as api_module
from api import LoggingSession, WeBanAPI, handle_response

FIXED_TIMESTAMP = "1700000000.123"
FIXED_MILLISECOND_TIMESTAMP = "1700000000123"
LOGIN_CIPHERTEXT = (
    "tJEFOOI9vOrjpS88K_udmaFdzxwkuXGQHTSIfHIp19gDokig7gBWfr0FD0i0Z5FO"
    "3PJKNw0flUtJTito58oD7GMFVCmYpTW0Z2G5xgk6bMV-5BBunCJVF37JXXS3-AfT"
    "NKBGASU46RDjK8dIdzi2PA=="
)
JUPITER_CIPHERTEXT = (
    "U0NXcDRYcGZwQ3F0a2MwOW9IUzg3QmlUQlNYSDNtWDV0MVNQaHhNN09VSHpzVURv"
    "UWpJMmpjRFlRT1lHM0hUMWo4QkRZUUIxK3FzRHlRZlJqYXpoaTJiYmxWeU16ZFhZ"
    "WTVpd0RMZGlpbGZBV1hUdktBWTR3S0MxODcxdi9ES0x2RHlpUC9iaUoxOTg5MVZJ"
    "dFk5emVMeWRxK0JNb3RMTFZUQzlBbHRjckRqYkNhbUIyWHhDU2NaVFM5WkNVTnJl"
    "UENyQWpieW80blRqMUVHRGEwWVNkNUNYN2RNUWdCQy9vaHlwUzJaVG1iQkxjaDc1"
    "TkZEM2crekNGa0pVdmo3RUMzUjhJU1R2ZUJLVjNZR0lFQmtma2c9PQ=="
)


@dataclass(frozen=True)
class RecordedCall:
    method: str
    url: str
    kwargs: dict[str, Any]
    session_headers: dict[str, Any]


class RecordingSession:
    def __init__(
        self,
        *responses: requests.Response,
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.headers = dict(headers or {})
        self.responses = list(responses)
        self.calls: list[RecordedCall] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(
            RecordedCall(
                method=method,
                url=url,
                kwargs=copy.deepcopy(kwargs),
                session_headers=copy.deepcopy(self.headers),
            )
        )
        if not self.responses:
            raise AssertionError(f"没有为 {method} {url} 配置本地响应")
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)


def make_json_response(
    payload: dict[str, Any],
    status_code: int = 200,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload, ensure_ascii=False).encode()
    return response


def make_text_response(text: str, status_code: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/html"
    response._content = text.encode()
    return response


def make_api(
    response: requests.Response,
    *,
    tenant_code: str = "tenant-01",
    user: dict[str, str] | None = None,
) -> tuple[WeBanAPI, RecordingSession]:
    actual_user = user or {"userId": "user-01", "token": "token-01"}
    api = WeBanAPI(tenant_code=tenant_code, user=actual_user)
    session = RecordingSession(response, headers=dict(api.session.headers))
    api.session = session  # type: ignore[assignment]
    return api, session


def assert_default_headers(call: RecordedCall, token: str = "token-01") -> None:
    expected = {
        **LoggingSession.DEFAULT_HEADERS,
        "X-Token": token,
    }
    assert {key: call.session_headers[key] for key in expected} == expected


class CapturingLog:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)


def test_handle_response_returns_json_object() -> None:
    response = make_json_response({"code": "0", "data": {"value": 1}})

    assert handle_response(response) == {"code": "0", "data": {"value": 1}}


@pytest.mark.parametrize("body", ["", "<html>bad gateway</html>", '{"code":'])
def test_handle_response_rejects_non_json_200(body: str) -> None:
    log = CapturingLog()

    assert handle_response(make_text_response(body), log=log) == {}
    assert any("响应内容不是有效的 JSON" in message for message in log.errors)


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "Token 无效，请检查账号信息"),
        (403, "Token 无效，不允许同时登录，请重试"),
    ],
)
def test_handle_response_rejects_auth_errors(
    status_code: int,
    message: str,
) -> None:
    with pytest.raises(PermissionError, match=message):
        handle_response(make_text_response("denied", status_code))


def test_handle_response_turns_http_500_into_empty_result() -> None:
    log = CapturingLog()

    assert handle_response(make_text_response("server error", 500), log=log) == {}
    assert any("请求失败：500 server error" in message for message in log.errors)


def test_login_request_and_encrypted_form_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "code": "0",
        "data": {"token": "token-new", "userId": "user-new"},
    }
    api = WeBanAPI(
        tenant_code="tenant-01",
        account="student-01",
        password="secret",
    )
    session = RecordingSession(
        make_json_response(result),
        headers=dict(api.session.headers),
    )
    api.session = session  # type: ignore[assignment]
    monkeypatch.setattr(api, "get_timestamp", lambda *args: FIXED_TIMESTAMP)

    assert api.login("ABCD", 1700000000) == result

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == "https://weiban.mycourse.cn/pharos/login/login.do"
    assert call.kwargs == {
        "params": {"timestamp": FIXED_TIMESTAMP},
        "data": {"data": LOGIN_CIPHERTEXT},
        "timeout": (9.05, 15),
    }
    assert_default_headers(call, token="")
    assert api.user == result["data"]
    assert api.session.headers["X-Token"] == "token-new"
    assert api.password is None


def test_common_post_query_form_and_headers_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, session = make_api(make_json_response({"code": "0", "data": []}))
    monkeypatch.setattr(api, "get_timestamp", lambda *args: FIXED_TIMESTAMP)

    assert api.list_course("project-01", "category-01", choose_type=3) == {
        "code": "0",
        "data": [],
    }

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == ("https://weiban.mycourse.cn/pharos/usercourse/listCourse.do")
    assert call.kwargs == {
        "params": {"timestamp": FIXED_TIMESTAMP},
        "data": {
            "userProjectId": "project-01",
            "chooseType": 3,
            "categoryCode": "category-01",
            "tenantCode": "tenant-01",
            "userId": "user-01",
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_mercury_form_and_signature_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, session = make_api(make_json_response({"code": "0", "data": []}))
    monkeypatch.setattr(api, "get_timestamp", lambda *args: FIXED_TIMESTAMP)

    api.list_question("course-01")

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == "https://resource.mycourse.cn/mercuryprovider/router"
    assert call.kwargs == {
        "data": {
            "appKey": "00000001",
            "format": "json",
            "v": "1.0",
            "timestamp": FIXED_TIMESTAMP,
            "clientId": "pharos",
            "service": "mercury.microlecture.listQuestion",
            "id": "course-01",
            "sign": "0A0BD3C55C25782E26538F851DAE3246038AF447",
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_jupiter_json_and_encryption_contract() -> None:
    api, session = make_api(make_json_response({"code": 200, "success": True}))

    result = api.apinext(
        "user-course-01",
        "course-01",
        "project-01",
        step=7,
        finish=1,
        nonstr="nonce-01",
        unique_no="unique-01",
    )

    assert result == {"code": 200, "success": True}
    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == (
        "https://weiban.mycourse.cn/jupiterapi/api/statusercourse/v1/next"
    )
    assert call.kwargs == {
        "json": {"data": JUPITER_CIPHERTEXT},
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_exam_captcha_check_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, session = make_api(make_json_response({"code": "0"}))
    monkeypatch.setattr(api, "get_timestamp", lambda *args: FIXED_TIMESTAMP)

    api.exam_check("exam-plan-01", "rand-01", "ticket-01")

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == "https://weiban.mycourse.cn/pharos/exam/check.do"
    assert call.kwargs == {
        "params": {"timestamp": FIXED_TIMESTAMP},
        "data": {
            "userExamPlanId": "exam-plan-01",
            "randstr": "rand-01",
            "ticket": "ticket-01",
            "tenantCode": "tenant-01",
            "userId": "user-01",
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_course_captcha_check_contract() -> None:
    api, session = make_api(make_json_response({"code": "0", "data": "token"}))

    api.course_check(
        "user-course-01",
        "project-01",
        "course-01",
        "rand-01",
        "ticket-01",
    )

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == "https://weiban.mycourse.cn/pharos/usercourse/check.do"
    assert call.kwargs == {
        "data": {
            "userId": "user-01",
            "userCourseId": "user-course-01",
            "userProjectId": "project-01",
            "courseId": "course-01",
            "tenantCode": "tenant-01",
            "randstr": "rand-01",
            "ticket": "ticket-01",
        },
        "headers": {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://mcwk.mycourse.cn",
            "Referer": "https://mcwk.mycourse.cn/",
            "X-Token": None,
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_jsonp_finish_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = f"jQuery3410{10**15}_{FIXED_MILLISECOND_TIMESTAMP}"
    payload = f'{callback}({{"code":"0","detailCode":"0"}})'
    api, session = make_api(make_text_response(payload))
    monkeypatch.setattr(
        api,
        "get_timestamp",
        lambda *args: FIXED_MILLISECOND_TIMESTAMP,
    )
    monkeypatch.setattr(api_module, "randint", lambda lower, upper: lower)

    assert api.finish_by_token(
        "user-course-01",
        token="completion-token",
        unique_no="unique-01",
        referer="https://mcwk.mycourse.cn/course/demo/demo.html",
    ) == {"code": "0", "detailCode": "0"}

    call = session.calls[0]
    assert call.method == "GET"
    assert call.url == (
        "https://weiban.mycourse.cn/pharos/usercourse/v2/completion-token.do"
    )
    assert call.kwargs == {
        "params": {
            "userCourseId": "user-course-01",
            "tenantCode": "tenant-01",
            "uniqueNo": "unique-01",
            "callback": callback,
            "_": 1700000000124,
        },
        "headers": {
            "Accept": "*/*",
            "Referer": "https://mcwk.mycourse.cn/course/demo/demo.html",
            "X-Token": None,
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)


def test_exam_submit_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, session = make_api(make_json_response({"code": "0", "data": {"score": 100}}))
    monkeypatch.setattr(api, "get_timestamp", lambda *args: FIXED_TIMESTAMP)

    assert api.exam_submit_paper("exam-plan-01") == {
        "code": "0",
        "data": {"score": 100},
    }

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url == ("https://weiban.mycourse.cn/pharos/exam/submitPaper.do")
    assert call.kwargs == {
        "params": {"timestamp": FIXED_TIMESTAMP},
        "data": {
            "userExamPlanId": "exam-plan-01",
            "tenantCode": "tenant-01",
            "userId": "user-01",
        },
        "timeout": (9.05, 15),
    }
    assert_default_headers(call)
