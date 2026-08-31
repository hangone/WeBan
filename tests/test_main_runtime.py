from __future__ import annotations

import copy
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest
from loguru import logger as base_logger

import main
from runtime_config import (
    InteractionPolicy,
    build_runtime_config,
    load_toml,
    parse_args,
    resolve_paths,
)


def _runtime(tmp_path: Path, *, non_interactive: bool = True):
    args = ["--data-dir", str(tmp_path)]
    args.append("--non-interactive" if non_interactive else "--no-non-interactive")
    opts = parse_args(args)
    paths = resolve_paths(
        opts,
        {},
        script_path=tmp_path / "app" / "main.py",
        cwd=tmp_path,
        frozen=False,
    )
    return build_runtime_config(
        opts,
        {
            "account": [
                {
                    "tenant_name": "测试学校",
                    "username": "student-001",
                    "password": "password-secret",
                }
            ]
        },
        paths,
        {},
        stdin_is_tty=not non_interactive,
    )


def test_log_redactor_covers_credentials_and_registered_identifiers() -> None:
    redactor = main.LogRedactor()
    redactor.register("student-001", "password-secret", "token-secret")
    raw = (
        'username="student-001" password=password-secret '
        '"token":"token-secret" Cookie: session-cookie '
        "Authorization: Bearer abc.def"
    )

    redacted = redactor.redact(raw)

    assert "student-001" not in redacted
    assert "password-secret" not in redacted
    assert "token-secret" not in redacted
    assert "session-cookie" not in redacted
    assert "abc.def" not in redacted
    assert "<redacted>" in redacted


def test_console_rendering_does_not_mutate_shared_record() -> None:
    record = {
        "message": "line one\nline two",
        "level": SimpleNamespace(name="DEBUG"),
        "time": SimpleNamespace(strftime=lambda _: "2026-08-30 12:00:00"),
        "extra": {"account": "账号01-deadbeef"},
    }
    before = copy.deepcopy(record["message"])

    rendered = main.render_console_record(record)

    assert record["message"] == before
    assert "line one\\nline two" in rendered


def test_noninteractive_prompt_never_calls_input() -> None:
    called = False

    def forbidden(_: str) -> str:
        nonlocal called
        called = True
        raise AssertionError("不应读取输入")

    with pytest.raises(RuntimeError, match="禁止"):
        main.prompt_account_interactive(
            InteractionPolicy(non_interactive=True),
            input_fn=forbidden,
            password_fn=forbidden,
        )

    assert called is False


def test_password_prompt_uses_hidden_input_callback() -> None:
    answers = iter(["测试学校", "student-001"])
    password_prompts: list[str] = []

    account = main.prompt_account_interactive(
        InteractionPolicy(non_interactive=False),
        input_fn=lambda _: next(answers),
        password_fn=lambda prompt: password_prompts.append(prompt) or "secret",
    )

    assert account == {
        "tenant_name": "测试学校",
        "username": "student-001",
        "password": "secret",
    }
    assert password_prompts == ["  密码（默认同用户名）："]


def test_ctrl_c_during_credential_prompt_propagates_for_exit_130() -> None:
    with pytest.raises(KeyboardInterrupt):
        main.prompt_account_interactive(
            InteractionPolicy(non_interactive=False),
            input_fn=lambda _: (_ for _ in ()).throw(KeyboardInterrupt),
        )


def test_interruptible_sleep_stops_immediately() -> None:
    stop_event = threading.Event()
    clock = main.InterruptibleTime(stop_event)
    stop_event.set()

    with pytest.raises(main.StopRequested):
        clock.sleep(3_600)


def test_waiting_for_sync_lock_is_interruptible() -> None:
    lock = threading.Lock()
    lock.acquire()
    stop_event = threading.Event()
    timer = threading.Timer(0.01, stop_event.set)
    timer.start()
    try:
        with (
            pytest.raises(main.StopRequested),
            main._interruptible_lock(lock, stop_event),
        ):
            raise AssertionError("不应取得锁")
    finally:
        lock.release()
        timer.cancel()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([main.AccountRunStatus.SUCCESS], main.EXIT_SUCCESS),
        ([main.AccountRunStatus.FAILED], main.EXIT_FAILURE),
        ([main.AccountRunStatus.INCOMPLETE], main.EXIT_FAILURE),
        (
            [main.AccountRunStatus.SUCCESS, main.AccountRunStatus.FAILED],
            main.EXIT_PARTIAL_FAILURE,
        ),
        (
            [main.AccountRunStatus.SUCCESS, main.AccountRunStatus.INCOMPLETE],
            main.EXIT_PARTIAL_FAILURE,
        ),
        ([main.AccountRunStatus.CANCELLED], main.EXIT_FAILURE),
    ],
)
def test_structured_summary_maps_to_exit_codes(
    statuses: list[main.AccountRunStatus], expected: int
) -> None:
    summary = main.RunSummary(
        tuple(
            main.AccountRunResult(index, f"account-{index}", status)
            for index, status in enumerate(statuses)
        )
    )

    assert summary.exit_code == expected


def test_run_account_returns_structured_success(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    class FakeClient:
        instances: ClassVar[list[FakeClient]] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.exam_mode = ""
            self.sync_count = 0
            self.closed = False
            self.cycle_args: dict[str, Any] = {}
            self.__class__.instances.append(self)

        def login(self) -> bool:
            return True

        def simulate_home_page(self) -> None:
            return None

        def sync_answers(self) -> None:
            self.sync_count += 1

        def run_project_cycle(self, **kwargs: Any) -> SimpleNamespace:
            self.cycle_args = kwargs
            return SimpleNamespace(
                status=SimpleNamespace(value="success"),
                message="",
                ok=True,
            )

        def close(self) -> None:
            self.closed = True

    result = main.run_account(
        runtime.accounts[0],
        runtime,
        0,
        threading.Event(),
        main.RuntimeDependencies(FakeClient, lambda *_: "ok"),
        base_logger.bind(account="系统"),
        "20260830-120000",
    )

    assert result.status is main.AccountRunStatus.SUCCESS
    assert FakeClient.instances[0].sync_count == 2
    assert FakeClient.instances[0].cycle_args["study_mode"] == "true"
    assert FakeClient.instances[0].closed is True


def test_run_account_reports_incomplete_workflow_and_closes_client(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    class IncompleteClient:
        instance: ClassVar[IncompleteClient | None] = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            self.exam_mode = ""
            self.closed = False
            self.__class__.instance = self

        def login(self) -> bool:
            return True

        def simulate_home_page(self) -> None:
            return None

        def sync_answers(self) -> SimpleNamespace:
            return SimpleNamespace(
                status=SimpleNamespace(value="success"),
                message="",
                ok=True,
            )

        def run_project_cycle(self, **kwargs: Any) -> SimpleNamespace:
            del kwargs
            return SimpleNamespace(
                status=SimpleNamespace(value="incomplete"),
                message="部分课程未完成",
                ok=False,
            )

        def close(self) -> None:
            self.closed = True

    result = main.run_account(
        runtime.accounts[0],
        runtime,
        0,
        threading.Event(),
        main.RuntimeDependencies(IncompleteClient, lambda *_: "ok"),
        base_logger.bind(account="系统"),
        "20260830-120000",
    )

    assert result.status is main.AccountRunStatus.INCOMPLETE
    assert "部分课程未完成" in result.detail
    assert IncompleteClient.instance is not None
    assert IncompleteClient.instance.closed is True


def test_runtime_adapter_injects_paths_policy_and_safe_captcha_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    captcha_module = ModuleType("captcha")
    client_module = ModuleType("client")

    class FakeCaptchaHandler:
        def __init__(self, **kwargs: Any) -> None:
            self.debug_dir = kwargs["debug_dir"]

    class FakeClient:
        pass

    captcha_module.is_non_interactive = lambda: False  # type: ignore[attr-defined]
    captcha_module.check_browser_health = lambda *_: "ok"  # type: ignore[attr-defined]
    client_module.is_non_interactive = lambda: False  # type: ignore[attr-defined]
    client_module.CaptchaHandler = FakeCaptchaHandler  # type: ignore[attr-defined]
    client_module.WeBanClient = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setattr(
        main,
        "_load_business_modules",
        lambda: (captcha_module, client_module),
    )
    monkeypatch.setenv("WB_DATA_DIR", "previous")
    monkeypatch.setenv("WB_NON_INTERACTIVE", "0")

    dependencies = main._apply_runtime_adapters(runtime, threading.Event())
    handler = client_module.CaptchaHandler(  # type: ignore[attr-defined]
        tenant_code="../../tenant",
        user_id="../CON",
    )

    assert dependencies.client_class is FakeClient
    assert client_module.is_non_interactive() is True  # type: ignore[attr-defined]
    assert captcha_module.is_non_interactive() is True  # type: ignore[attr-defined]
    assert Path(client_module.answer_dir) == runtime.paths.answer_dir  # type: ignore[attr-defined]
    assert Path(handler.debug_dir).is_relative_to(runtime.paths.captcha_debug_dir)
    assert ".." not in Path(handler.debug_dir).name


def test_interactive_account_save_is_atomic_and_parseable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, non_interactive=False)

    class CapturingLogger:
        def success(self, _: str) -> None:
            return None

    main.save_interactive_account(
        runtime,
        runtime.accounts[0].credentials,
        CapturingLogger(),
    )
    document = load_toml(runtime.paths.config_path)

    assert document["account"][0]["username"] == "student-001"
    assert document["account"][0]["password"] == "password-secret"
    assert not list(runtime.paths.config_path.parent.glob("*.tmp"))


def test_noninteractive_missing_config_never_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "builtins.input",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应读取输入")),
    )

    exit_code = main.main(
        ["--non-interactive", "--data-dir", str(tmp_path)],
        env={},
    )

    assert exit_code == main.EXIT_CONFIG_ERROR
    assert (tmp_path / "config.toml").exists()
    log_files = list((tmp_path / "logs").glob("*.log"))
    assert log_files
    for log_file in log_files:
        log_file.unlink()
