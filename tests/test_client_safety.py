from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import client as client_module
from client import WeBanClient, clean_text
from errors import (
    AccountBlockedError,
    APIResponseError,
    ResponseValidationError,
    WorkflowResult,
    WorkflowStatus,
)


class _NullLog:
    def __getattr__(self, name: str):
        del name
        return lambda *args, **kwargs: None


def _progress_response(
    finished: int,
    *,
    required: int = 1,
    optional: int = 0,
    push: int = 0,
) -> dict:
    return {
        "code": "0",
        "data": {
            "requiredNum": required,
            "requiredFinishedNum": finished,
            "optionalNum": optional,
            "optionalFinishedNum": 0,
            "pushNum": push,
            "pushFinishedNum": 0,
            "examNum": 0,
            "examFinishedNum": 0,
        },
    }


class _CourseAPI:
    def __init__(self, *, jupiter_success: bool = True) -> None:
        self.user = {"userId": "user", "userName": "张 三&同学"}
        self.jupiter_success = jupiter_success
        self.apinext_calls: list[dict] = []
        self.finish_calls: list[dict] = []
        self.list_question_calls = 0

    def study(self, course_id: str, project_id: str) -> dict:
        del course_id, project_id
        return {"code": "0"}

    def get_course_url(self, course_id: str, project_id: str) -> dict:
        del course_id, project_id
        return {
            "code": "0",
            "data": (
                "https://mcwk.mycourse.cn/course/C/C.html?"
                "userCourseId=user-course&weiban=weiban&csCapt=false"
            ),
        }

    def apinext(self, *args, **kwargs) -> dict:
        del args
        self.apinext_calls.append(dict(kwargs))
        return {
            "code": 200,
            "success": self.jupiter_success,
        }

    def list_question(self, course_id: str) -> dict:
        del course_id
        self.list_question_calls += 1
        return {
            "code": "0",
            "data": {
                "viewpointQuestionList": [],
                "examQuestionList": [],
            },
        }

    def save_question(self, *args) -> dict:
        del args
        return {"code": "0", "data": []}

    def save_exam_question(self, *args) -> dict:
        del args
        return {"code": "0", "data": {}}

    def finish_by_token(self, user_course_id: str, **kwargs) -> dict:
        self.finish_calls.append({"user_course_id": user_course_id, **kwargs})
        return {"code": "0"}


def _course_client(api: _CourseAPI, *, uses_apinext: bool) -> WeBanClient:
    client = object.__new__(WeBanClient)
    client.api = api
    client.log = _NullLog()
    client.study_base_time = 0
    client.study_random_upper = 0
    client.video_speed = 0
    client.jupiter_fallback = False
    client._captcha_handler = None
    client.parse_item_js = lambda *args, **kwargs: {
        "uses_apinext": uses_apinext,
        "nonstr_map": {1: "nonce"} if uses_apinext else {},
        "has_exam": False,
        "total_step": 1 if uses_apinext else 0,
        "video_duration": 0,
    }
    return client


@pytest.mark.parametrize(
    "response",
    [
        {"code": "0", "data": None},
        {"code": "-1"},
        {"code": "0", "data": [None, {"list": None}, {"list": [None, {}]}]},
    ],
    ids=["null-data", "business-failure", "malformed-entries"],
)
def test_tenant_lookup_returns_empty_on_malformed_list(response: dict) -> None:
    client = object.__new__(WeBanClient)
    client.log = _NullLog()
    client.tenant_name = "测试学校"
    client.api = SimpleNamespace(get_tenant_list_with_letter=lambda: response)

    assert client.get_tenant_code() == ""


def test_tenant_lookup_matches_trimmed_name() -> None:
    client = object.__new__(WeBanClient)
    client.log = _NullLog()
    client.tenant_name = "测试学校"
    client.api = SimpleNamespace(
        get_tenant_list_with_letter=lambda: {
            "code": "0",
            "data": [{"list": [{"name": " 测试学校 ", "code": "0000123"}]}],
        }
    )

    assert client.get_tenant_code() == "0000123"


@pytest.mark.parametrize(
    ("course", "expected"),
    [
        ({"finished": 1}, True),
        ({"finished": "1"}, True),
        ({"finished": 2}, False),
        ({"finished": None}, False),
        ({"finished": "unknown"}, False),
        ({"finished": float("inf")}, False),
        ({"finished": float("nan")}, False),
        ({}, False),
    ],
)
def test_course_finished_tolerates_unexpected_values(
    course: dict, expected: bool
) -> None:
    assert client_module._course_finished(course) is expected


def test_course_finished_handles_json_overflow_literal() -> None:
    # JSON 1e309 在 Python 中解析为 inf，int(inf) 抛 OverflowError
    course = json.loads('{"finished": 1e309}')
    assert client_module._course_finished(course) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (100, 100.0),
        ("60", 60.0),
        (0, 0.0),
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        (-1, None),
        (1e9, None),
        (True, None),
        (None, None),
        ("abc", None),
    ],
)
def test_finite_score_rejects_non_finite_and_out_of_range(
    value: object, expected: float | None
) -> None:
    assert client_module._finite_score(value) == expected


def test_brief_response_strips_body_and_control_characters() -> None:
    response = {
        "code": "-1",
        "msg": "bad\x00\x1bline\n" + "x" * 500,
        "data": {"token": "secret-token", "realName": "张三"},
    }

    brief = client_module._brief_response(response)

    assert "secret-token" not in brief
    assert "张三" not in brief
    assert "\x00" not in brief and "\x1b" not in brief and "\n" not in brief
    assert "data=<dict>" in brief
    assert len(brief) < 300
    assert client_module._brief_response(["not", "a", "dict"]) == "<list>"


def test_tenant_lookup_failure_log_does_not_echo_response_body() -> None:
    class RecordingLog(_NullLog):
        def __init__(self) -> None:
            self.errors: list[str] = []

        def error(self, message: str) -> None:
            self.errors.append(message)

    log = RecordingLog()
    client = object.__new__(WeBanClient)
    client.log = log
    client.tenant_name = "测试学校"
    client.api = SimpleNamespace(
        get_tenant_list_with_letter=lambda: {
            "code": "-1",
            "data": {"token": "leaked-token"},
        }
    )

    assert client.get_tenant_code() == ""
    assert log.errors
    assert all("leaked-token" not in line for line in log.errors)


def test_apinext_and_finish_share_one_unique_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _CourseAPI()
    client = _course_client(api, uses_apinext=True)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    assert client._study_one_course(
        {"resourceId": "course", "resourceName": "课程"},
        {"userProjectId": "project"},
        "分类",
        "项目",
        {},
        False,
    )

    trace_numbers = {call["unique_no"] for call in api.apinext_calls}
    assert len(trace_numbers) == 1
    assert api.finish_calls[0]["unique_no"] == trace_numbers.pop()


def test_ordinary_course_does_not_send_unique_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _CourseAPI()
    client = _course_client(api, uses_apinext=False)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    assert client._study_one_course(
        {"resourceId": "course", "resourceName": "课程"},
        {"userProjectId": "project"},
        "分类",
        "项目",
        {},
        False,
    )

    assert "unique_no" not in api.finish_calls[0]


def test_failed_jupiter_step_stops_before_questions_and_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _CourseAPI(jupiter_success=False)
    client = _course_client(api, uses_apinext=True)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    with pytest.raises(ResponseValidationError, match="apinext"):
        client._study_one_course(
            {"resourceId": "course", "resourceName": "课程"},
            {"userProjectId": "project"},
            "分类",
            "项目",
            {},
            False,
        )

    assert api.list_question_calls == 0
    assert api.finish_calls == []


def test_course_completion_polls_until_delayed_progress_is_visible() -> None:
    responses = [
        _progress_response(0),
        _progress_response(0),
        _progress_response(1),
    ]
    calls = 0
    delays: list[float] = []
    client = object.__new__(WeBanClient)
    client.log = _NullLog()

    def get_progress(*args, **kwargs) -> dict:
        nonlocal calls
        del args, kwargs
        response = responses[min(calls, len(responses) - 1)]
        calls += 1
        return response

    client.get_progress = get_progress
    client._sleep = lambda seconds: delays.append(seconds)
    client._study_one_course = lambda *args, **kwargs: True

    assert client._learn_course(
        {"resourceId": "course", "resourceName": "课程"},
        {"userProjectId": "project"},
        "分类",
        "项目",
        {},
        False,
    )
    assert calls == 3
    assert delays == [client_module.PROGRESS_POLL_DELAYS[0]]


def test_course_completion_with_no_progress_update_remains_incomplete() -> None:
    response = _progress_response(0)
    responses = [response] * (len(client_module.PROGRESS_POLL_DELAYS) + 2)
    calls = 0
    delays: list[float] = []
    client = object.__new__(WeBanClient)
    client.log = _NullLog()

    def get_progress(*args, **kwargs) -> dict:
        nonlocal calls
        del args, kwargs
        calls += 1
        return responses[min(calls - 1, len(responses) - 1)]

    client.get_progress = get_progress
    client._sleep = lambda seconds: delays.append(seconds)
    client._study_one_course = lambda *args, **kwargs: True

    assert not client._learn_course(
        {"resourceId": "course", "resourceName": "课程"},
        {"userProjectId": "project"},
        "分类",
        "项目",
        {},
        False,
    )
    assert calls == len(client_module.PROGRESS_POLL_DELAYS) + 2
    assert delays == list(client_module.PROGRESS_POLL_DELAYS)


def test_project_completion_polls_before_declaring_study_incomplete() -> None:
    responses = [_progress_response(0), _progress_response(1)]
    calls = 0
    delays: list[float] = []
    client = object.__new__(WeBanClient)
    client.log = _NullLog()

    def get_progress(*args, **kwargs) -> dict:
        nonlocal calls
        del args, kwargs
        response = responses[min(calls, len(responses) - 1)]
        calls += 1
        return response

    client.get_progress = get_progress
    client._sleep = lambda seconds: delays.append(seconds)

    assert client._check_project_course_done({"userProjectId": "project"}, "项目")
    assert calls == 2
    assert delays == [client_module.PROGRESS_POLL_DELAYS[0]]


def test_manual_captcha_cleanup_failure_does_not_skip_api_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoginAPI:
        def __init__(self) -> None:
            self.user = {"userId": ""}
            self.login_calls: list[tuple[str, int]] = []

        def get_timestamp(self, length: int, offset: int) -> int:
            assert (length, offset) == (13, 0)
            return 123

        def rand_letter_image(self, timestamp: int) -> bytes:
            assert timestamp == 123
            return b"captcha"

        def login(self, verify_code: str, verify_time: int) -> dict:
            self.login_calls.append((verify_code, verify_time))
            self.user["userId"] = "user"
            return {"code": "0"}

    api = LoginAPI()
    client = object.__new__(WeBanClient)
    client.api = api
    client.log = _NullLog()
    client.non_interactive = False
    client.captcha_debug_dir = tmp_path
    client._prompt = lambda message: "ABCD"

    monkeypatch.setattr(
        client_module.LoginCaptchaSolver,
        "recognize",
        lambda image, log: None,
    )
    monkeypatch.setattr(client_module.webbrowser, "open", lambda url: True)
    real_unlink = Path.unlink

    def fail_captcha_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == tmp_path / "verify_code.png":
            raise OSError("captcha file is still in use")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_captcha_unlink)

    assert client.login() == api.user
    assert api.login_calls == [("ABCD", 123)]


def test_parse_item_js_does_not_stop_before_later_nonstr_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <html>
      <script src="apicenext.js"></script>
      <script src="js/item.js"></script>
      <script src="build/js/COURSE.js"></script>
    </html>
    """

    def fetch(session, url: str, referer: str | None = None) -> str:
        del session, referer
        if url.endswith(".html"):
            return html
        if url.endswith("/js/item.js"):
            return "saveExamQuestion();"
        if url.endswith("/build/js/COURSE.js"):
            return 'const nonstrMap = new Map([[1, "n1"], [2, "n2"]]);'
        return ""

    monkeypatch.setattr(client_module, "_fetch_text", fetch)
    client = object.__new__(WeBanClient)
    client.api = SimpleNamespace(session=object())
    client.log = _NullLog()

    result = client.parse_item_js(
        "COURSE",
        "https://mcwk.mycourse.cn/course/COURSE/COURSE.html",
    )

    assert result["has_exam"] is True
    assert result["nonstr_map"] == {1: "n1", 2: "n2"}


def test_course_url_uses_standard_query_encoding() -> None:
    api = _CourseAPI()
    client = object.__new__(WeBanClient)
    client.api = api

    result = client._build_course_url(
        {"resourceId": "课程&id", "praiseNum": "1 2"},
        {"userProjectId": "项目&id"},
    )
    query = parse_qs(urlsplit(result).query)

    assert query["userName"][-1] == "张 三&同学"
    assert query["courseId"][-1] == "课程&id"
    assert query["userProjectId"][-1] == "项目&id"
    assert "张 三&同学" not in result


def _question(
    title: str,
    *,
    option_b: str = "否",
) -> dict:
    return {
        "id": "question",
        "title": title,
        "type": 1,
        "typeLabel": "单选题",
        "optionList": [
            {"id": "yes-id", "content": "是"},
            {"id": "no-id", "content": option_b},
        ],
    }


def _entry(correct: tuple[int, ...] = (0,), *, option_b: str = "否") -> dict:
    return {
        "type": 1,
        "optionList": [
            {"content": "是", "isCorrect": 1 if 0 in correct else 2},
            {"content": option_b, "isCorrect": 1 if 1 in correct else 2},
        ],
    }


def test_answer_matching_preserves_semantic_signs_and_rejects_stale_options() -> None:
    answers = {
        "温度>0吗？": _entry((0,)),
        "温度<0吗？": _entry((1,)),
    }

    assert clean_text("温度>0吗？") != clean_text("温度<0吗？")
    assert WeBanClient._answer_ids_for_question(
        answers,
        _question("温度>0吗？"),
    ) == ["yes-id"]
    assert (
        WeBanClient._answer_ids_for_question(
            answers,
            _question("温度>0吗？", option_b="未知"),
        )
        == []
    )


def test_fuzzy_match_must_be_unique_and_single_choice_cannot_union() -> None:
    ambiguous = {
        "以下正确？": _entry((0,)),
        "以下正确。": _entry((0,)),
    }
    assert (
        WeBanClient._answer_ids_for_question(
            ambiguous,
            _question("以下正确！"),
        )
        == []
    )
    assert (
        WeBanClient._answer_ids_for_question(
            {"单选题": _entry((0, 1))},
            _question("单选题"),
        )
        == []
    )


class _ExamAPI:
    def __init__(
        self,
        *,
        before: dict,
        paper: dict | None = None,
        odd_num: int = 2,
        finish_num: int = 0,
        history_prepare: dict | Exception | None = None,
        exam_score: object = 0,
        pass_score: object = 60,
    ) -> None:
        self.user = {"userId": "user", "realName": "用户"}
        self.before = before
        self.paper = paper
        self.odd_num = odd_num
        self.finish_num = finish_num
        self.exam_score = exam_score
        self.pass_score = pass_score
        # 已考过的计划会先用 preparePaper 读取满分；None 表示与正式响应一致
        self.history_prepare = history_prepare
        self.prepare_calls = 0
        self.start_calls = 0
        self.record_calls = 0
        self.submit_calls = 0

    def exam_list_plan(self, project_id: str) -> dict:
        del project_id
        return {
            "code": "0",
            "data": [
                {
                    "id": "user-plan",
                    "examPlanId": "plan",
                    "examPlanName": "考试",
                    "examOddNum": self.odd_num,
                    "examFinishNum": self.finish_num,
                    "examScore": self.exam_score,
                    "passScore": self.pass_score,
                }
            ],
        }

    def exam_before_paper(self, plan_id: str) -> dict:
        del plan_id
        return self.before

    def exam_prepare_paper(self, plan_id: str) -> dict:
        del plan_id
        self.prepare_calls += 1
        if self.finish_num > 0 and self.prepare_calls == 1:
            if isinstance(self.history_prepare, Exception):
                raise self.history_prepare
            if self.history_prepare is not None:
                return self.history_prepare
        return {
            "code": "0",
            "data": {
                "questionNum": 1,
                "paperScore": 100,
                "answerTime": 30,
                "realName": "用户",
                "userIDLabel": "学号",
            },
        }

    def exam_check(self, *args) -> dict:
        del args
        return {"code": "0"}

    def exam_start_paper(self, plan_id: str) -> dict:
        del plan_id
        self.start_calls += 1
        return self.paper or {"code": "0", "data": {"questionList": []}}

    def exam_record_question(self, *args) -> dict:
        del args
        self.record_calls += 1
        return {"code": "0"}

    def exam_submit_paper(self, plan_id: str) -> dict:
        del plan_id
        self.submit_calls += 1
        return {"code": "0", "data": {"score": 100}}


def _exam_client(api: _ExamAPI, answers: dict) -> WeBanClient:
    client = object.__new__(WeBanClient)
    client.api = api
    client.log = _NullLog()
    client.ai_config = None
    client._ai_key_warned = False
    client._eta_exam_avg = None
    client._captcha_handler = SimpleNamespace(
        handle_exam_captcha=lambda plan_id: {
            "randstr": f"rand-{plan_id}",
            "ticket": "ticket",
        }
    )
    client._load_answers_json = lambda warn_on_fail=False: answers
    return client


def _run_one_exam(
    client: WeBanClient,
    *,
    threshold: int = 90,
) -> WorkflowResult:
    return client.run_exam(
        exam_question_time="0,0",
        exam_submit_match_rate=threshold,
        only_project={
            "projectName": "项目",
            "userProjectId": "project",
            "completion": {"grey": 2, "active": 1},
        },
    )


def test_before_paper_failure_stops_current_exam_plan() -> None:
    api = _ExamAPI(before={"code": "-1"})
    result = _run_one_exam(_exam_client(api, {}))

    assert result.status is WorkflowStatus.INCOMPLETE
    assert api.prepare_calls == 0
    assert api.start_calls == 0


def test_empty_paper_is_never_submitted() -> None:
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
    )
    result = _run_one_exam(_exam_client(api, {}), threshold=0)

    assert result.status is WorkflowStatus.INCOMPLETE
    assert api.record_calls == 0
    assert api.submit_calls == 0


def test_last_attempt_requires_all_questions_to_map_to_legal_ids() -> None:
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
        paper={"code": "0", "data": {"questionList": [_question("未知题")]}},
        odd_num=1,
    )
    result = _run_one_exam(_exam_client(api, {}), threshold=0)

    assert result.status is WorkflowStatus.INCOMPLETE
    assert api.record_calls == 0
    assert api.submit_calls == 0


def test_valid_fully_mapped_paper_can_be_recorded_and_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
        paper={"code": "0", "data": {"questionList": [_question("已知题")]}},
    )
    client = _exam_client(api, {"已知题": _entry((0,))})
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    result = _run_one_exam(client)

    assert result.status is WorkflowStatus.SUCCESS
    assert api.record_calls == 1
    assert api.submit_calls == 1


@pytest.mark.parametrize(
    "history_prepare",
    [
        {"code": "0", "data": None},
        {"code": "-1"},
        {"code": "0", "data": {"paperScore": "not-a-number"}},
        {"code": "0", "data": {"paperScore": float("inf")}},
        {"code": "0", "data": {"paperScore": float("nan")}},
        APIResponseError(
            "请求失败",
            status_code=502,
            endpoint="preparePaper",
            summary="bad gateway",
        ),
    ],
    ids=[
        "null-data",
        "business-failure",
        "bad-score",
        "inf-score",
        "nan-score",
        "http-error",
    ],
)
def test_history_score_probe_failure_does_not_abort_exam(
    monkeypatch: pytest.MonkeyPatch, history_prepare: dict | Exception
) -> None:
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
        paper={"code": "0", "data": {"questionList": [_question("已知题")]}},
        finish_num=1,
        history_prepare=history_prepare,
    )
    client = _exam_client(api, {"已知题": _entry((0,))})
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    result = client.run_exam(
        exam_mode="force",
        exam_question_time="0,0",
        exam_submit_match_rate=90,
        only_project={
            "projectName": "项目",
            "userProjectId": "project",
            "completion": {"grey": 2, "active": 1},
        },
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert api.submit_calls == 1


def test_negative_infinite_paper_score_never_marks_perfect_mode_as_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 旧实现里 0 >= -inf 为真，perfect 模式会把这场考试当成"已满分"跳过
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
        paper={"code": "0", "data": {"questionList": [_question("已知题")]}},
        finish_num=1,
        history_prepare={"code": "0", "data": {"paperScore": float("-inf")}},
    )
    client = _exam_client(api, {"已知题": _entry((0,))})
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)

    result = client.run_exam(
        exam_mode="perfect",
        exam_question_time="0,0",
        exam_submit_match_rate=90,
        only_project={
            "projectName": "项目",
            "userProjectId": "project",
            "completion": {"grey": 2, "active": 1},
        },
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert api.submit_calls == 1


@pytest.mark.parametrize(
    ("exam_score", "pass_score"),
    [
        (float("-inf"), 60),
        (float("inf"), 60),
        (float("nan"), 60),
        (0, float("nan")),
        (0, float("-inf")),
        (0, 1e12),
    ],
    ids=[
        "neg-inf-score",
        "inf-score",
        "nan-score",
        "nan-pass",
        "neg-inf-pass",
        "huge-pass",
    ],
)
def test_non_finite_plan_scores_fail_closed_without_touching_paper(
    exam_score: object, pass_score: object
) -> None:
    api = _ExamAPI(
        before={"code": "0", "data": {"isExistedNotSubmit": False}},
        paper={"code": "0", "data": {"questionList": [_question("已知题")]}},
        finish_num=1,
        exam_score=exam_score,
        pass_score=pass_score,
    )
    result = _run_one_exam(_exam_client(api, {"已知题": _entry((0,))}))

    assert result.status is WorkflowStatus.INCOMPLETE
    assert api.prepare_calls == 0
    assert api.start_calls == 0
    assert api.submit_calls == 0


def test_review_merge_replaces_single_choice_truth_instead_of_union() -> None:
    answers = {"同一道题": _entry((0,))}
    reviewed = {
        "title": "同一道题",
        "type": 1,
        "optionList": _entry((1,))["optionList"],
    }

    assert WeBanClient._merge_reviewed_answer(answers, reviewed)
    correct = [
        option["content"]
        for option in answers["同一道题"]["optionList"]
        if option["isCorrect"] == 1
    ]
    assert correct == ["否"]


def test_history_response_supports_both_known_shapes_and_id_fields() -> None:
    history = {"examId": "exam"}
    assert WeBanClient._extract_history_list({"code": "0", "data": [history]}) == [
        history
    ]
    assert WeBanClient._extract_history_list(
        {"code": "0", "data": {"examHistoryList": [history]}}
    ) == [history]


def test_sync_skips_one_bad_plan_and_atomically_keeps_good_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "answer.json"
    root_path.write_text(
        json.dumps({"旧题": _entry((0,))}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "root_answer_path", str(root_path))
    monkeypatch.setattr(
        client_module,
        "answer_path",
        str(tmp_path / "answer" / "answer.json"),
    )
    monkeypatch.setattr(
        client_module,
        "bundle_answer_path",
        str(tmp_path / "bundle" / "answer.json"),
    )

    class SyncAPI:
        def list_my_project(self, ended: int = 2) -> dict:
            return (
                {
                    "code": "0",
                    "data": [{"userProjectId": "project"}],
                }
                if ended == 2
                else {"code": "0", "data": []}
            )

        def list_completion(self) -> dict:
            return {"code": "0", "data": []}

        def exam_list_plan(self, project_id: str) -> dict:
            assert project_id == "project"
            return {
                "code": "0",
                "data": [
                    {"examPlanId": "good", "examType": 1},
                    {"examPlanId": "bad", "examType": 1},
                ],
            }

        def exam_list_history(self, plan_id: str, exam_type: int) -> dict:
            assert exam_type == 1
            if plan_id == "bad":
                return {"code": "-1"}
            return {
                "code": "0",
                "data": {"examHistoryList": [{"examId": "exam-good"}]},
            }

        def exam_review_paper(self, exam_id: str, is_retake: int) -> dict:
            assert (exam_id, is_retake) == ("exam-good", 2)
            return {
                "code": "0",
                "data": {
                    "questions": [
                        {
                            "title": "新题",
                            "type": 1,
                            "optionList": _entry((1,))["optionList"],
                        }
                    ]
                },
            }

    client = object.__new__(WeBanClient)
    client.api = SyncAPI()
    client.log = _NullLog()

    result = client.sync_answers()
    stored = json.loads(root_path.read_text(encoding="utf-8"))

    assert result.status is WorkflowStatus.INCOMPLETE
    assert "旧题" in stored
    assert "新题" in stored


def test_sync_propagates_account_lock_without_further_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "answer.json"
    root_path.write_text(
        json.dumps({"旧题": _entry((0,))}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "root_answer_path", str(root_path))
    monkeypatch.setattr(
        client_module,
        "answer_path",
        str(tmp_path / "answer" / "answer.json"),
    )
    monkeypatch.setattr(
        client_module,
        "bundle_answer_path",
        str(tmp_path / "bundle" / "answer.json"),
    )

    class LockedAPI:
        def list_my_project(self, ended: int = 2) -> dict:
            del ended
            raise AccountBlockedError(detail_code="701")

    client = object.__new__(WeBanClient)
    client.api = LockedAPI()
    client.log = _NullLog()

    with pytest.raises(AccountBlockedError) as caught:
        client.sync_answers()
    assert caught.value.status is WorkflowStatus.LOCKED


def test_sync_downgrades_project_listing_network_error_to_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "answer.json"
    root_path.write_text(
        json.dumps({"旧题": _entry((0,))}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(client_module, "root_answer_path", str(root_path))
    monkeypatch.setattr(
        client_module,
        "answer_path",
        str(tmp_path / "answer" / "answer.json"),
    )
    monkeypatch.setattr(
        client_module,
        "bundle_answer_path",
        str(tmp_path / "bundle" / "answer.json"),
    )

    class FlakyAPI:
        def list_my_project(self, ended: int = 2) -> dict:
            if ended == 2:
                raise APIResponseError(
                    "请求失败",
                    status_code=503,
                    endpoint="listMyProject",
                    summary="unavailable",
                )
            return {"code": "0", "data": []}

        def list_completion(self) -> dict:
            raise OSError("connection reset")

    client = object.__new__(WeBanClient)
    client.api = FlakyAPI()
    client.log = _NullLog()

    result = client.sync_answers()

    assert result.status is WorkflowStatus.INCOMPLETE
    assert result.failed == 2
    assert "旧题" in json.loads(root_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("endpoint", ["completion", "lab"])
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"code": "0", "data": None},
        OSError("connection reset"),
        APIResponseError(
            "请求失败", status_code=503, endpoint="list", summary="unavailable"
        ),
    ],
    ids=["empty-object", "null-data", "network-error", "http-error"],
)
def test_sync_counts_module_failure_once_and_preserves_answers(
    tmp_path: Path, endpoint: str, response: dict | Exception
) -> None:
    class ModuleAPI:
        def __init__(self) -> None:
            self.lab_calls = 0

        def list_my_project(self, ended: int = 2) -> dict:
            return {"code": "0", "data": []}

        def list_completion(self) -> dict:
            if endpoint == "completion":
                if isinstance(response, Exception):
                    raise response
                return response
            return {
                "code": "0",
                "data": [{"module": "labProject", "showable": 1}],
            }

        def lab_index(self) -> dict:
            self.lab_calls += 1
            if isinstance(response, Exception):
                raise response
            return response

    answers = {"旧题": _entry((0,))}
    store = client_module.AnswerStore(
        tmp_path / "answer.json", validator=WeBanClient._is_valid_answers
    )
    store.write(answers)
    api = ModuleAPI()
    client = object.__new__(WeBanClient)
    client.api = api
    client.log = _NullLog()
    client.__dict__["_answer_store_instance"] = store

    result = client.sync_answers()

    assert result.status is WorkflowStatus.INCOMPLETE
    assert result.failed == 1
    assert api.lab_calls == (1 if endpoint == "lab" else 0)
    assert store.load() == WeBanClient._normalize_answers(answers)


def test_remote_answer_baseline_is_merged_inside_final_store_update() -> None:
    class Store:
        def __init__(self) -> None:
            self.write_calls = 0
            self.update_calls = 0
            self.result: dict | None = None

        def load(self) -> dict:
            raise client_module.AnswerStoreError("missing")

        def write(self, value: dict) -> None:
            del value
            self.write_calls += 1

        def update(self, mutator, *, default) -> dict:
            del default
            self.update_calls += 1
            # 模拟远程下载期间另一进程已经写入的新题目。
            result = mutator({"并发题": _entry((0,))})
            assert isinstance(result, dict)
            self.result = result
            return result

    class RemoteAPI:
        def download_answer(self) -> str:
            return json.dumps({"远程题": _entry((1,))}, ensure_ascii=False)

        def list_my_project(self, ended: int = 2) -> dict:
            del ended
            return {"code": "0", "data": []}

        def list_completion(self) -> dict:
            return {"code": "0", "data": []}

    store = Store()
    client = object.__new__(WeBanClient)
    client.api = RemoteAPI()
    client.log = _NullLog()
    client.__dict__["_answer_store_instance"] = store

    result = client.sync_answers()

    assert result.status is WorkflowStatus.SUCCESS
    assert store.write_calls == 0
    assert store.update_calls == 1
    assert store.result is not None
    assert {"远程题", "并发题"}.issubset(store.result)


@pytest.mark.parametrize("payload", ["[]", "null", '"text"'])
def test_remote_answer_rejects_non_object_before_normalizing(payload: str) -> None:
    class Store:
        def load(self) -> dict:
            raise client_module.AnswerStoreError("missing")

        def update(self, mutator, *, default) -> dict:
            del mutator, default
            raise AssertionError("invalid remote data must not be committed")

    class RemoteAPI:
        def download_answer(self) -> str:
            return payload

    client = object.__new__(WeBanClient)
    client.api = RemoteAPI()
    client.log = _NullLog()
    client.__dict__["_answer_store_instance"] = Store()

    result = client.sync_answers()

    assert result.status is WorkflowStatus.FAILED


def test_answer_validator_rejects_nonempty_object_without_usable_questions() -> None:
    assert not WeBanClient._is_valid_answers({"无效题目": {}})
    assert not WeBanClient._is_valid_answers(
        {"无效题目": {"optionList": [{"content": ""}]}}
    )
    assert WeBanClient._is_valid_answers({"有效题目": _entry((0,))})


def test_project_cycle_skips_exam_when_study_is_incomplete() -> None:
    client = object.__new__(WeBanClient)
    client.log = _NullLog()
    client._get_project_list = lambda: [
        {"projectName": "项目", "userProjectId": "project"}
    ]
    client.run_study = lambda *args, **kwargs: WorkflowResult.incomplete("学习未完成")
    exam_calls = 0

    def run_exam(*args, **kwargs) -> WorkflowResult:
        nonlocal exam_calls
        del args, kwargs
        exam_calls += 1
        return WorkflowResult.success()

    client.run_exam = run_exam
    result = client.run_project_cycle(
        study_time="0,0",
        study_mode="true",
        exam_mode="true",
        random_answer=True,
        exam_question_time="0,0",
        exam_submit_match_rate=90,
    )

    assert result.status is WorkflowStatus.INCOMPLETE
    assert exam_calls == 0


def test_client_close_releases_owned_resources_once() -> None:
    api_closes = 0
    handler_closes = 0

    def close_api() -> None:
        nonlocal api_closes
        api_closes += 1

    def close_handler() -> None:
        nonlocal handler_closes
        handler_closes += 1

    client = object.__new__(WeBanClient)
    client.api = SimpleNamespace(close=close_api)
    client._captcha_handler = SimpleNamespace(close=close_handler)
    client._closed = False

    client.close()
    client.close()

    assert api_closes == 1
    assert handler_closes == 1
