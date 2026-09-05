"""运行时配置解析、路径归一化与启动前校验。

本模块只依赖标准库，确保应用可以在导入网络、浏览器和业务模块之前完成
全部配置校验。配置优先级统一为 CLI > 环境变量 > 账号 TOML > 全局 TOML
> 默认值。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import stat
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

STUDY_MODES = frozenset({"false", "true", "force"})
EXAM_MODES = frozenset({"false", "true", "perfect", "force"})

DEFAULT_SETTINGS: dict[str, Any] = {
    "study_mode": "true",
    "exam_mode": "true",
    "random_answer": True,
    "study_time": "20,10",
    "video_speed": 1.0,
    "exam_question_time": "3,3",
    "exam_submit_match_rate": 90,
    "browser_path": "",
    "cdp_host": "",
    "cdp_port": 0,
    "max_workers": 5,
    "debug": False,
    "jupiter_fallback": False,
}

DEFAULT_AI: dict[str, Any] = {
    "enable": False,
    "base_url": "",
    "api_key": "",
    "model": "",
    "timeout": 60,
    "max_retries": 2,
}

_ACCOUNT_FIELDS = (
    "tenant_name",
    "username",
    "password",
    "user_id",
    "token",
)
_SETTING_CLI_FIELDS = {
    "study_mode": "study_mode",
    "exam_mode": "exam_mode",
    "random_answer": "random_answer",
    "study_time": "study_time",
    "video_speed": "video_speed",
    "exam_question_time": "exam_question_time",
    "exam_submit_match_rate": "exam_submit_match_rate",
    "browser_path": "browser_path",
    "cdp_host": "cdp_host",
    "cdp_port": "cdp_port",
    "debug": "debug",
    "jupiter_fallback": "jupiter_fallback",
}


class ConfigError(ValueError):
    """配置不完整、格式错误或超出安全范围。"""


@dataclass(frozen=True)
class ResolvedPaths:
    """所有可写数据都锚定到同一个数据目录。"""

    program_dir: Path
    bundle_dir: Path
    data_dir: Path
    config_path: Path
    logs_dir: Path
    answer_dir: Path
    captcha_debug_dir: Path
    config_example_candidates: tuple[Path, ...]


@dataclass(frozen=True)
class InteractionPolicy:
    """显式交互策略，供入口及业务模块适配层共用。"""

    non_interactive: bool

    @property
    def allow_input(self) -> bool:
        return not self.non_interactive

    @property
    def allow_visible_browser(self) -> bool:
        return not self.non_interactive


@dataclass(frozen=True)
class AccountIdentity:
    """不含原始账号信息的日志身份和安全路径组件。"""

    log_key: str
    tenant_dir: str
    account_dir: str


@dataclass(frozen=True)
class AccountCredentials:
    tenant_name: str
    username: str
    password: str
    user_id: str
    token: str

    @property
    def uses_token(self) -> bool:
        return bool(self.user_id and self.token)

    @property
    def principal(self) -> str:
        return self.user_id if self.uses_token else self.username


@dataclass(frozen=True)
class AccountSettings:
    study_mode: str
    exam_mode: str
    random_answer: bool
    study_time: str
    video_speed: float
    exam_question_time: str
    exam_submit_match_rate: int
    browser_path: str | None
    cdp_host: str | None
    cdp_port: int | None
    debug: bool
    jupiter_fallback: bool


@dataclass(frozen=True)
class ResolvedAccount:
    credentials: AccountCredentials
    settings: AccountSettings
    identity: AccountIdentity


@dataclass(frozen=True)
class AISettings:
    enable: bool
    base_url: str
    api_key: str
    model: str
    timeout: int
    max_retries: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enable": self.enable,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }


@dataclass(frozen=True)
class RuntimeConfig:
    paths: ResolvedPaths
    interaction: InteractionPolicy
    accounts: tuple[ResolvedAccount, ...]
    ai: AISettings
    max_workers: int


def build_parser() -> argparse.ArgumentParser:
    """创建严格 CLI 解析器；未知参数由 argparse 以退出码 2 拒绝。"""

    parser = argparse.ArgumentParser(
        prog="WeBan",
        description="WeBan 学习自动化（多账号，可按项目交替学习+考试）",
        allow_abbrev=False,
    )
    parser.add_argument("--config", metavar="PATH", help="配置文件路径")
    parser.add_argument("--data-dir", metavar="PATH", help="统一数据目录")
    parser.add_argument(
        "--non-interactive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="禁用所有终端输入和可见浏览器回退",
    )
    parser.add_argument("--study-mode", choices=sorted(STUDY_MODES))
    parser.add_argument("--exam-mode", choices=sorted(EXAM_MODES))
    parser.add_argument("--random-answer", choices=["true", "false"])
    parser.add_argument("--cdp-host", metavar="HOST")
    parser.add_argument("--cdp-port", metavar="PORT")
    parser.add_argument("--max-workers", metavar="N")
    parser.add_argument("--tenant-name", dest="tenant_name", metavar="NAME")
    parser.add_argument("--username", metavar="USER")
    parser.add_argument("--password", metavar="PASS")
    parser.add_argument("--study-time", metavar="SEC")
    parser.add_argument("--video-speed", metavar="N")
    parser.add_argument("--exam-question-time", metavar="SEC")
    parser.add_argument("--exam-submit-match-rate", metavar="N")
    parser.add_argument("--browser-path", metavar="PATH")
    parser.add_argument("--jupiter-fallback", choices=["true", "false"])
    parser.add_argument("--user-id", metavar="ID")
    parser.add_argument("--token", metavar="TOKEN")
    parser.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="启用调试日志",
    )
    parser.add_argument("--ai-enable", choices=["true", "false"])
    parser.add_argument("--ai-base-url", metavar="URL")
    parser.add_argument("--ai-api-key", metavar="KEY")
    parser.add_argument("--ai-model", metavar="NAME")
    parser.add_argument("--ai-timeout", metavar="SEC")
    parser.add_argument("--ai-max-retries", metavar="N")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _absolute_path(value: object, *, relative_to: Path) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(value).strip()))
    if not text:
        raise ConfigError("路径不能为空")
    path = Path(text)
    if not path.is_absolute():
        path = relative_to / path
    return path.resolve(strict=False)


def resolve_paths(
    opts: argparse.Namespace,
    env: Mapping[str, str] | None = None,
    *,
    script_path: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    frozen: bool | None = None,
    executable_path: str | os.PathLike[str] | None = None,
    bundle_path: str | os.PathLike[str] | None = None,
) -> ResolvedPaths:
    """按 CLI > env > 默认值解析配置路径和统一数据根目录。"""

    source_env = os.environ if env is None else env
    current_dir = Path.cwd() if cwd is None else Path(cwd)
    current_dir = current_dir.resolve(strict=False)
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    source_script = Path(script_path or __file__).resolve(strict=False)
    source_executable = Path(executable_path or sys.executable).resolve(strict=False)
    program_dir = source_executable.parent if is_frozen else source_script.parent
    raw_bundle = bundle_path or getattr(sys, "_MEIPASS", None)
    resolved_bundle = (
        Path(raw_bundle).resolve(strict=False) if raw_bundle else program_dir
    )

    cli_data_dir = getattr(opts, "data_dir", None)
    env_data_dir = source_env.get("WB_DATA_DIR")
    data_value = cli_data_dir if cli_data_dir is not None else env_data_dir

    cli_config = getattr(opts, "config", None)
    env_config = source_env.get("WB_CONFIG")
    config_value = cli_config if cli_config is not None else env_config

    explicit_data_dir = (
        _absolute_path(data_value, relative_to=current_dir)
        if data_value not in (None, "")
        else None
    )
    explicit_config = (
        _absolute_path(config_value, relative_to=current_dir)
        if config_value not in (None, "")
        else None
    )

    if explicit_data_dir is not None:
        data_dir = explicit_data_dir
    elif explicit_config is not None:
        data_dir = explicit_config.parent
    else:
        data_dir = program_dir

    config_path = explicit_config or data_dir / "config.toml"
    candidates: list[Path] = []
    for candidate in (
        resolved_bundle / "config.example.toml",
        program_dir / "config.example.toml",
    ):
        if candidate not in candidates:
            candidates.append(candidate)

    logs_dir = data_dir / "logs"
    return ResolvedPaths(
        program_dir=program_dir,
        bundle_dir=resolved_bundle,
        data_dir=data_dir,
        config_path=config_path,
        logs_dir=logs_dir,
        answer_dir=data_dir / "answer",
        captcha_debug_dir=logs_dir / "captcha",
        config_example_candidates=tuple(candidates),
    )


def load_toml(path: Path) -> dict[str, Any]:
    """读取 TOML；不存在时返回空配置，损坏时给出可操作的配置错误。"""

    if not path.exists():
        return {}
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"配置文件编码错误：{path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 TOML 格式错误：{exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError("配置文件顶层必须是 TOML 表")
    return document


def _section(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是 TOML 表")
    return dict(value)


def _strict_bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{label} 必须是布尔值（true/false）")


def _strict_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} 必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ConfigError(f"{label} 必须是整数")
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{label} 不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{label} 不能大于 {maximum}")
    return parsed


def _strict_float(
    value: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} 必须是数字")
    if not isinstance(value, (str, int, float)):
        raise ConfigError(f"{label} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是数字") from exc
    if not math.isfinite(parsed):
        raise ConfigError(f"{label} 必须是有限数字")
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{label} 不能小于 {minimum:g}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{label} 不能大于 {maximum:g}")
    return parsed


def _enum(value: object, label: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} 必须是字符串")
    parsed = value.strip().lower()
    if parsed not in choices:
        allowed = "/".join(sorted(choices))
        raise ConfigError(f"{label} 只能是 {allowed}")
    return parsed


def _time_range(
    value: object,
    label: str,
    *,
    maximum_total: int,
) -> str:
    if isinstance(value, bool):
        raise ConfigError(f"{label} 必须是“基础秒数,随机上限”")
    if isinstance(value, int):
        parts: list[object] = [value]
    elif isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    else:
        raise ConfigError(f"{label} 必须是“基础秒数,随机上限”")
    if not 1 <= len(parts) <= 2 or any(part == "" for part in parts):
        raise ConfigError(f"{label} 必须是“基础秒数,随机上限”")
    base = _strict_int(parts[0], f"{label}基础秒数", minimum=0)
    random_upper = (
        _strict_int(parts[1], f"{label}随机上限", minimum=0) if len(parts) == 2 else 0
    )
    if base + random_upper > maximum_total:
        raise ConfigError(f"{label}最大等待时间不能超过 {maximum_total} 秒")
    return f"{base},{random_upper}"


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise ConfigError(f"{label} 必须是字符串")
    text = str(value).strip()
    return text or None


def _pick(
    opts: argparse.Namespace,
    env: Mapping[str, str],
    key: str,
    *,
    account: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
    default: object,
) -> object:
    cli_field = _SETTING_CLI_FIELDS.get(key, key)
    cli_value = getattr(opts, cli_field, None)
    if cli_value is not None:
        return cli_value
    env_key = f"WB_{key.upper()}"
    if env_key in env:
        return env[env_key]
    if account is not None and key in account and account[key] not in (None, ""):
        return account[key]
    if key in settings and settings[key] not in (None, ""):
        return settings[key]
    return default


def resolve_interaction_policy(
    opts: argparse.Namespace,
    settings: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    stdin_is_tty: bool | None = None,
) -> InteractionPolicy:
    source_env = os.environ if env is None else env
    cli_value = getattr(opts, "non_interactive", None)
    if cli_value is not None:
        return InteractionPolicy(non_interactive=bool(cli_value))
    if "WB_NON_INTERACTIVE" in source_env:
        return InteractionPolicy(
            non_interactive=_strict_bool(
                source_env["WB_NON_INTERACTIVE"], "WB_NON_INTERACTIVE"
            )
        )
    if "non_interactive" in settings:
        return InteractionPolicy(
            non_interactive=_strict_bool(
                settings["non_interactive"], "settings.non_interactive"
            )
        )
    environment = source_env.get("ENVIRONMENT", "").strip().lower()
    if environment in {"docker", "container"}:
        return InteractionPolicy(non_interactive=True)
    if stdin_is_tty is None:
        try:
            stdin_is_tty = bool(sys.stdin.isatty())
        except (AttributeError, ValueError):
            stdin_is_tty = False
    return InteractionPolicy(non_interactive=not stdin_is_tty)


def _resolve_account_settings(
    opts: argparse.Namespace,
    env: Mapping[str, str],
    raw_account: Mapping[str, Any],
    global_settings: Mapping[str, Any],
    paths: ResolvedPaths,
) -> AccountSettings:
    study_mode = _enum(
        _pick(
            opts,
            env,
            "study_mode",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["study_mode"],
        ),
        "study_mode",
        STUDY_MODES,
    )
    exam_mode = _enum(
        _pick(
            opts,
            env,
            "exam_mode",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["exam_mode"],
        ),
        "exam_mode",
        EXAM_MODES,
    )
    random_answer = _strict_bool(
        _pick(
            opts,
            env,
            "random_answer",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["random_answer"],
        ),
        "random_answer",
    )
    study_time = _time_range(
        _pick(
            opts,
            env,
            "study_time",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["study_time"],
        ),
        "study_time",
        maximum_total=86_400,
    )
    video_speed = _strict_float(
        _pick(
            opts,
            env,
            "video_speed",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["video_speed"],
        ),
        "video_speed",
        minimum=0,
        maximum=16,
    )
    exam_question_time = _time_range(
        _pick(
            opts,
            env,
            "exam_question_time",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["exam_question_time"],
        ),
        "exam_question_time",
        maximum_total=3_600,
    )
    match_rate = _strict_int(
        _pick(
            opts,
            env,
            "exam_submit_match_rate",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["exam_submit_match_rate"],
        ),
        "exam_submit_match_rate",
        minimum=0,
        maximum=100,
    )
    cdp_host = _optional_text(
        _pick(
            opts,
            env,
            "cdp_host",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["cdp_host"],
        ),
        "cdp_host",
    )
    cdp_port_value = _pick(
        opts,
        env,
        "cdp_port",
        account=raw_account,
        settings=global_settings,
        default=DEFAULT_SETTINGS["cdp_port"],
    )
    cdp_port_number = _strict_int(cdp_port_value, "cdp_port", minimum=0, maximum=65_535)
    cdp_port = cdp_port_number or None
    if bool(cdp_host) != bool(cdp_port):
        raise ConfigError("cdp_host 与 cdp_port 必须同时设置")
    if cdp_host and "://" in cdp_host:
        raise ConfigError("cdp_host 只填写主机名或 IP，不能包含 URL scheme")

    # CDP 是显式的远程浏览器选择，优先于 browser_path。只有未配置完整
    # CDP 时才解析并校验本地浏览器路径，避免无关的失效路径阻断 CDP 连接。
    browser_path: str | None = None
    if cdp_host is None:
        browser_value = _pick(
            opts,
            env,
            "browser_path",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["browser_path"],
        )
        browser_text = _optional_text(browser_value, "browser_path")
        if browser_text:
            resolved_browser = _absolute_path(
                browser_text, relative_to=paths.config_path.parent
            )
            if not resolved_browser.is_file():
                raise ConfigError(f"browser_path 指向的文件不存在：{resolved_browser}")
            browser_path = str(resolved_browser)

    debug = _strict_bool(
        _pick(
            opts,
            env,
            "debug",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["debug"],
        ),
        "debug",
    )
    jupiter_fallback = _strict_bool(
        _pick(
            opts,
            env,
            "jupiter_fallback",
            account=raw_account,
            settings=global_settings,
            default=DEFAULT_SETTINGS["jupiter_fallback"],
        ),
        "jupiter_fallback",
    )
    return AccountSettings(
        study_mode=study_mode,
        exam_mode=exam_mode,
        random_answer=random_answer,
        study_time=study_time,
        video_speed=video_speed,
        exam_question_time=exam_question_time,
        exam_submit_match_rate=match_rate,
        browser_path=browser_path,
        cdp_host=cdp_host,
        cdp_port=cdp_port,
        debug=debug,
        jupiter_fallback=jupiter_fallback,
    )


def _credential_text(value: object, label: str, *, strip: bool = True) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set, bool)):
        raise ConfigError(f"{label} 必须是字符串或数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{label} 不是有效值")
    text = str(value)
    return text.strip() if strip else text


def _normalize_credentials(
    raw: Mapping[str, Any], account_number: int
) -> AccountCredentials | None:
    values = {
        "tenant_name": _credential_text(
            raw.get("tenant_name"), f"第 {account_number} 个账号 tenant_name"
        ),
        "username": _credential_text(
            raw.get("username"), f"第 {account_number} 个账号 username"
        ),
        "password": _credential_text(
            raw.get("password"),
            f"第 {account_number} 个账号 password",
            strip=False,
        ),
        "user_id": _credential_text(
            raw.get("user_id"), f"第 {account_number} 个账号 user_id"
        ),
        "token": _credential_text(
            raw.get("token"), f"第 {account_number} 个账号 token"
        ),
    }
    if not any(values.values()):
        return None
    if not values["tenant_name"]:
        raise ConfigError(f"第 {account_number} 个账号缺少 tenant_name")
    has_password_login = bool(values["username"])
    has_any_token_field = bool(values["user_id"] or values["token"])
    has_token_login = bool(values["user_id"] and values["token"])
    if has_any_token_field and not has_token_login:
        raise ConfigError(f"第 {account_number} 个账号的 user_id 与 token 必须同时设置")
    if not has_password_login and not has_token_login:
        raise ConfigError(
            f"第 {account_number} 个账号需要 username，或 user_id + token"
        )
    if has_password_login and not values["password"]:
        values["password"] = values["username"]
    return AccountCredentials(**values)


def make_account_identity(
    credentials: AccountCredentials, account_index: int
) -> AccountIdentity:
    tenant_hash = hashlib.sha256(
        f"tenant\0{credentials.tenant_name}".encode()
    ).hexdigest()[:12]
    account_hash = hashlib.sha256(
        (
            f"{credentials.tenant_name}\0"
            f"{'token' if credentials.uses_token else 'password'}\0"
            f"{credentials.principal}"
        ).encode()
    ).hexdigest()[:16]
    return AccountIdentity(
        log_key=f"账号{account_index + 1:02d}-{account_hash}",
        tenant_dir=f"tenant-{tenant_hash}",
        account_dir=f"account-{account_hash}",
    )


def _credential_overrides(
    opts: argparse.Namespace, env: Mapping[str, str]
) -> tuple[dict[str, Any], set[str]]:
    values: dict[str, Any] = {}
    specified: set[str] = set()
    for field in _ACCOUNT_FIELDS:
        cli_value = getattr(opts, field, None)
        env_key = f"WB_{field.upper()}"
        if cli_value is not None:
            values[field] = cli_value
            specified.add(field)
        elif env_key in env:
            values[field] = env[env_key]
            specified.add(field)
    return values, specified


def _prepare_raw_accounts(
    opts: argparse.Namespace,
    env: Mapping[str, str],
    document: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_accounts_value = document.get("account", [])
    if raw_accounts_value is None:
        raw_accounts_value = []
    if not isinstance(raw_accounts_value, list) or not all(
        isinstance(item, dict) for item in raw_accounts_value
    ):
        raise ConfigError("[[account]] 必须是 TOML 数组表")
    raw_accounts = [dict(item) for item in raw_accounts_value]
    overrides, specified = _credential_overrides(opts, env)
    if not specified:
        return raw_accounts

    try:
        direct_probe = _normalize_credentials(overrides, 1)
    except ConfigError:
        direct_probe = None
    if direct_probe is not None:
        matching_index: int | None = None
        for index, raw in enumerate(raw_accounts):
            candidate = _normalize_credentials(raw, index + 1)
            if candidate is None:
                continue
            if (
                candidate.tenant_name == direct_probe.tenant_name
                and candidate.principal == direct_probe.principal
            ):
                matching_index = index
                break
        if matching_index is None:
            raw_accounts.insert(0, dict(overrides))
        else:
            matched = raw_accounts.pop(matching_index)
            matched.update(overrides)
            raw_accounts.insert(0, matched)
        return raw_accounts

    # 单独用环境变量覆盖密码/Token 等字段时，可无歧义地合并到唯一账号。
    non_blank_accounts = [
        raw
        for raw in raw_accounts
        if any(raw.get(field) not in (None, "") for field in _ACCOUNT_FIELDS)
    ]
    if len(non_blank_accounts) != 1:
        raise ConfigError("账号 CLI/环境变量不完整；多账号配置下必须提供完整登录身份")
    target = non_blank_accounts[0]
    target.update(overrides)
    return raw_accounts


def _resolve_ai(
    opts: argparse.Namespace,
    env: Mapping[str, str],
    raw_ai: Mapping[str, Any],
) -> AISettings:
    def pick(key: str) -> object:
        cli_value = getattr(opts, f"ai_{key}", None)
        if cli_value is not None:
            return cli_value
        env_key = f"WB_AI_{key.upper()}"
        if env_key in env:
            return env[env_key]
        if key in raw_ai:
            return raw_ai[key]
        return DEFAULT_AI[key]

    enable = _strict_bool(pick("enable"), "ai.enable")
    base_url = _optional_text(pick("base_url"), "ai.base_url") or ""
    api_key = _credential_text(pick("api_key"), "ai.api_key", strip=False)
    model = _optional_text(pick("model"), "ai.model") or ""
    timeout = _strict_int(pick("timeout"), "ai.timeout", minimum=1, maximum=600)
    max_retries = _strict_int(
        pick("max_retries"), "ai.max_retries", minimum=0, maximum=10
    )
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("ai.base_url 必须是有效的 http/https URL")
    if enable and (not base_url or not model):
        raise ConfigError("启用 AI 时必须设置 ai.base_url 和 ai.model")
    return AISettings(
        enable=enable,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_retries=max_retries,
    )


def build_runtime_config(
    opts: argparse.Namespace,
    document: Mapping[str, Any],
    paths: ResolvedPaths,
    env: Mapping[str, str] | None = None,
    *,
    stdin_is_tty: bool | None = None,
) -> RuntimeConfig:
    """合并并严格校验所有配置，不执行任何网络操作。"""

    source_env = os.environ if env is None else env
    global_settings = _section(document, "settings")
    raw_ai = _section(document, "ai")
    interaction = resolve_interaction_policy(
        opts,
        global_settings,
        source_env,
        stdin_is_tty=stdin_is_tty,
    )
    max_workers = _strict_int(
        _pick(
            opts,
            source_env,
            "max_workers",
            account=None,
            settings=global_settings,
            default=DEFAULT_SETTINGS["max_workers"],
        ),
        "max_workers",
        minimum=1,
        maximum=64,
    )
    ai = _resolve_ai(opts, source_env, raw_ai)
    raw_accounts = _prepare_raw_accounts(opts, source_env, document)

    resolved_accounts: list[ResolvedAccount] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_index, raw_account in enumerate(raw_accounts, start=1):
        credentials = _normalize_credentials(raw_account, raw_index)
        if credentials is None:
            continue
        dedupe_key = (
            credentials.tenant_name,
            "token" if credentials.uses_token else "password",
            credentials.principal,
        )
        if dedupe_key in seen:
            raise ConfigError(f"第 {raw_index} 个账号与前面的账号重复")
        seen.add(dedupe_key)
        settings = _resolve_account_settings(
            opts, source_env, raw_account, global_settings, paths
        )
        account_index = len(resolved_accounts)
        resolved_accounts.append(
            ResolvedAccount(
                credentials=credentials,
                settings=settings,
                identity=make_account_identity(credentials, account_index),
            )
        )

    return RuntimeConfig(
        paths=paths,
        interaction=interaction,
        accounts=tuple(resolved_accounts),
        ai=ai,
        max_workers=max_workers,
    )


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + replace，并尽量收紧凭据文件权限。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
        file = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        # fdopen 成功后描述符归文件对象所有；多线程下重复 close 可能误关
        # 其他线程刚复用的同号描述符（如日志文件）。
        descriptor = -1
        with file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temp_path.unlink(missing_ok=True)
        raise


def create_local_config_template(paths: ResolvedPaths) -> bool:
    """仅用本地模板创建配置，避免校验前发生远程下载。"""

    if paths.config_path.exists():
        return False
    for candidate in paths.config_example_candidates:
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        atomic_write_text(paths.config_path, content)
        return True
    atomic_write_text(
        paths.config_path,
        "# WeBan 配置文件\n[settings]\n\n"
        '[[account]]\ntenant_name = ""\nusername = ""\npassword = ""\n',
    )
    return True
