import pytest

from client import _check_code_ok


@pytest.mark.parametrize("code", [0, "0", 1, "1", 200, "200"])
def test_main_api_accepts_verified_numeric_and_string_codes(
    code: int | str,
) -> None:
    assert _check_code_ok({"code": code})


@pytest.mark.parametrize("code", [-1, "-1", 2, "2", "", "invalid"])
def test_business_error_codes_are_not_success(code: int | str) -> None:
    assert not _check_code_ok({"code": code})


@pytest.mark.parametrize("code", [0, "0", 1, "1"])
def test_jsonp_finish_accepts_only_verified_codes(code: int | str) -> None:
    assert _check_code_ok({"code": code}, allow_200=False)


@pytest.mark.parametrize("code", [200, "200"])
def test_jsonp_finish_rejects_code_200(code: int | str) -> None:
    assert not _check_code_ok({"code": code}, allow_200=False)


def test_missing_code_is_not_misclassified_as_success() -> None:
    assert not _check_code_ok({"data": {"value": 1}})


@pytest.mark.parametrize("allow_200", [True, False])
def test_explicit_null_code_keeps_official_compatibility(
    allow_200: bool,
) -> None:
    assert _check_code_ok({"code": None}, allow_200=allow_200)


def test_empty_response_is_not_success() -> None:
    assert not _check_code_ok({})
