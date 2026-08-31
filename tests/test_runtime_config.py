from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from runtime_config import (
    ConfigError,
    ResolvedPaths,
    atomic_write_text,
    build_runtime_config,
    parse_args,
    resolve_interaction_policy,
    resolve_paths,
)


def _document(
    *,
    settings: dict[str, object] | None = None,
    account: dict[str, object] | None = None,
    ai: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "settings": settings or {},
        "account": [
            account
            or {
                "tenant_name": "测试学校",
                "username": "student-001",
                "password": "secret-password",
            }
        ],
        "ai": ai or {},
    }


def _paths(
    tmp_path: Path,
    opts: argparse.Namespace | None = None,
    env: dict[str, str] | None = None,
) -> ResolvedPaths:
    parsed = parse_args([]) if opts is None else opts
    return resolve_paths(
        parsed,
        env or {},
        script_path=tmp_path / "app" / "main.py",
        cwd=tmp_path,
        frozen=False,
    )


def test_cli_paths_override_environment_and_share_one_data_root(
    tmp_path: Path,
) -> None:
    opts = parse_args(
        [
            "--config",
            "cli/config.toml",
            "--data-dir",
            "cli-data",
        ]
    )
    paths = _paths(
        tmp_path,
        opts,
        {
            "WB_CONFIG": "env/config.toml",
            "WB_DATA_DIR": "env-data",
        },
    )

    assert paths.config_path == (tmp_path / "cli" / "config.toml").resolve()
    assert paths.data_dir == (tmp_path / "cli-data").resolve()
    assert paths.logs_dir == paths.data_dir / "logs"
    assert paths.answer_dir == paths.data_dir / "answer"
    assert paths.captcha_debug_dir == paths.logs_dir / "captcha"


def test_wb_config_is_honored_and_anchors_default_data_root(
    tmp_path: Path,
) -> None:
    paths = _paths(
        tmp_path,
        env={"WB_CONFIG": "profile/custom.toml"},
    )

    assert paths.config_path == (tmp_path / "profile" / "custom.toml").resolve()
    assert paths.data_dir == paths.config_path.parent


def test_cli_then_env_then_account_then_global_precedence(
    tmp_path: Path,
) -> None:
    opts = parse_args(
        [
            "--study-mode",
            "force",
            "--video-speed",
            "2",
        ]
    )
    document = _document(
        settings={
            "study_mode": "true",
            "exam_mode": "true",
            "video_speed": 1,
            "debug": False,
        },
        account={
            "tenant_name": "测试学校",
            "username": "student-001",
            "study_mode": "false",
            "exam_mode": "perfect",
            "video_speed": 3,
        },
    )
    runtime = build_runtime_config(
        opts,
        document,
        _paths(tmp_path, opts),
        {
            "WB_STUDY_MODE": "true",
            "WB_EXAM_MODE": "force",
            "WB_DEBUG": "yes",
        },
        stdin_is_tty=True,
    )
    settings = runtime.accounts[0].settings

    assert settings.study_mode == "force"
    assert settings.exam_mode == "force"
    assert settings.video_speed == 2
    assert settings.debug is True


def test_account_toml_overrides_global_when_cli_and_env_are_absent(
    tmp_path: Path,
) -> None:
    opts = parse_args([])
    runtime = build_runtime_config(
        opts,
        _document(
            settings={"study_mode": "true"},
            account={
                "tenant_name": "测试学校",
                "username": "student-001",
                "study_mode": "false",
            },
        ),
        _paths(tmp_path, opts),
        {},
        stdin_is_tty=True,
    )

    assert runtime.accounts[0].settings.study_mode == "false"


def test_ai_cli_and_environment_overrides_are_typed(
    tmp_path: Path,
) -> None:
    opts = parse_args(["--ai-enable", "true", "--ai-timeout", "25"])
    runtime = build_runtime_config(
        opts,
        _document(
            ai={
                "enable": False,
                "base_url": "https://toml.invalid/v1",
                "model": "toml-model",
                "timeout": 60,
                "max_retries": 2,
            }
        ),
        _paths(tmp_path, opts),
        {
            "WB_AI_BASE_URL": "https://example.test/v1",
            "WB_AI_MODEL": "env-model",
            "WB_AI_MAX_RETRIES": "4",
        },
        stdin_is_tty=True,
    )

    assert runtime.ai.enable is True
    assert runtime.ai.base_url == "https://example.test/v1"
    assert runtime.ai.model == "env-model"
    assert runtime.ai.timeout == 25
    assert runtime.ai.max_retries == 4


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("study_mode", "unsafe"),
        ("exam_mode", "loop"),
        ("random_answer", "perhaps"),
        ("study_time", "-1,0"),
        ("study_time", "86400,1"),
        ("video_speed", -0.1),
        ("video_speed", "nan"),
        ("exam_question_time", "1,3600"),
        ("exam_submit_match_rate", 101),
        ("max_workers", 0),
        ("max_workers", 65),
        ("cdp_port", 65_536),
    ],
)
def test_invalid_runtime_values_fail_before_network(
    tmp_path: Path, key: str, value: object
) -> None:
    opts = parse_args([])
    settings = {key: value}

    with pytest.raises(ConfigError):
        build_runtime_config(
            opts,
            _document(settings=settings),
            _paths(tmp_path, opts),
            {},
            stdin_is_tty=True,
        )


def test_half_configured_cdp_is_rejected(tmp_path: Path) -> None:
    opts = parse_args([])

    with pytest.raises(ConfigError, match="必须同时设置"):
        build_runtime_config(
            opts,
            _document(settings={"cdp_host": "127.0.0.1"}),
            _paths(tmp_path, opts),
            {},
            stdin_is_tty=True,
        )


def test_nonexistent_explicit_browser_is_rejected(tmp_path: Path) -> None:
    opts = parse_args([])

    with pytest.raises(ConfigError, match="文件不存在"):
        build_runtime_config(
            opts,
            _document(settings={"browser_path": "missing-browser.exe"}),
            _paths(tmp_path, opts),
            {},
            stdin_is_tty=True,
        )


def test_account_scalars_are_normalized_to_strings(tmp_path: Path) -> None:
    opts = parse_args([])
    runtime = build_runtime_config(
        opts,
        _document(
            account={
                "tenant_name": 1001,
                "username": 20_240_001,
                "password": "",
            }
        ),
        _paths(tmp_path, opts),
        {},
        stdin_is_tty=True,
    )
    credentials = runtime.accounts[0].credentials

    assert credentials.tenant_name == "1001"
    assert credentials.username == "20240001"
    assert credentials.password == "20240001"


def test_password_only_environment_override_applies_to_single_toml_account(
    tmp_path: Path,
) -> None:
    opts = parse_args([])
    runtime = build_runtime_config(
        opts,
        _document(),
        _paths(tmp_path, opts),
        {"WB_PASSWORD": "environment-secret"},
        stdin_is_tty=True,
    )

    assert runtime.accounts[0].credentials.password == "environment-secret"


def test_cli_noninteractive_choice_overrides_environment(tmp_path: Path) -> None:
    del tmp_path
    opts = parse_args(["--no-non-interactive"])

    policy = resolve_interaction_policy(
        opts,
        {},
        {"WB_NON_INTERACTIVE": "true"},
        stdin_is_tty=False,
    )

    assert policy.non_interactive is False


def test_unknown_cli_argument_is_an_error() -> None:
    with pytest.raises(SystemExit) as caught:
        parse_args(["--unknown-option"])

    assert caught.value.code == 2


def test_account_log_identity_contains_only_hash_components(
    tmp_path: Path,
) -> None:
    opts = parse_args([])
    raw_username = "../CON/secret-user"
    runtime = build_runtime_config(
        opts,
        _document(
            account={
                "tenant_name": "../../租户",
                "username": raw_username,
            }
        ),
        _paths(tmp_path, opts),
        {},
        stdin_is_tty=True,
    )
    identity = runtime.accounts[0].identity
    log_path = (
        runtime.paths.logs_dir / identity.tenant_dir / identity.account_dir
    ).resolve()

    log_path.relative_to(runtime.paths.logs_dir.resolve())
    assert raw_username not in str(log_path)
    assert identity.tenant_dir.startswith("tenant-")
    assert identity.account_dir.startswith("account-")
    assert "/" not in identity.account_dir
    assert "\\" not in identity.account_dir


def test_atomic_write_replaces_content_and_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "private" / "config.toml"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")

    assert target.read_text(encoding="utf-8") == "second"
    assert not list(target.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
