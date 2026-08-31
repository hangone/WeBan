from __future__ import annotations

import getpass
import hashlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

import requests
from loguru import logger as base_logger

from runtime_config import (
    AccountCredentials,
    ConfigError,
    InteractionPolicy,
    ResolvedAccount,
    RuntimeConfig,
    atomic_write_text,
    build_runtime_config,
    create_local_config_template,
    load_toml,
    parse_args,
    resolve_interaction_policy,
    resolve_paths,
)

GITHUB_REPO = "hangone/WeBan"
UPDATE_CHECK_TIMEOUT = 3
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_PARTIAL_FAILURE = 3
EXIT_INTERRUPTED = 130

_SYNC_LOCK = threading.Lock()


def _resolve_version() -> str:
    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "pyproject.toml")
    candidates.append(Path(__file__).resolve().parent / "pyproject.toml")
    for path in candidates:
        try:
            with path.open("rb") as file:
                version = tomllib.load(file).get("project", {}).get("version")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if version:
            return str(version)
    try:
        return distribution_version("weban")
    except PackageNotFoundError:
        return "unknown"


VERSION = f"v{_resolve_version()}"


class LogRedactor:
    """在日志分发前统一脱敏，覆盖终端和所有文件 sink。"""

    _KEYS = (
        r"password|passwd|pwd|token|x-token|authorization|cookie|ticket|"
        r"user_?id|username|login_?name|student_?id|account|real_?name|"
        r"tenant_?name|tenant_?code|mobile|phone|email|id_?card"
    )
    _QUOTED_PAIR = re.compile(
        rf"(?i)(?P<prefix>[\"']?(?:{_KEYS})[\"']?\s*[:=]\s*)"
        r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
    )
    _UNQUOTED_PAIR = re.compile(
        rf"(?i)(?P<prefix>\b(?:{_KEYS})\b\s*[:=]\s*)"
        r"(?P<value>[^,\s&;}\]]+)"
    )
    _AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: object) -> None:
        with self._lock:
            for value in values:
                if value is None:
                    continue
                text = str(value)
                # 极短值只能依靠带键名的规则脱敏，避免把普通数字/字母全替换。
                if len(text) >= 4:
                    self._values.add(text)

    def register_account(self, account: ResolvedAccount) -> None:
        credentials = account.credentials
        self.register(
            credentials.tenant_name,
            credentials.username,
            credentials.password,
            credentials.user_id,
            credentials.token,
        )

    def redact(self, text: object) -> str:
        result = str(text)
        result = self._AUTH.sub(lambda match: f"{match.group(1)} <redacted>", result)
        result = self._QUOTED_PAIR.sub(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"<redacted>{match.group('quote')}"
            ),
            result,
        )
        result = self._UNQUOTED_PAIR.sub(
            lambda match: f"{match.group('prefix')}<redacted>",
            result,
        )
        with self._lock:
            sensitive_values = sorted(self._values, key=len, reverse=True)
        for value in sensitive_values:
            result = result.replace(value, "<redacted>")
        return result

    def __call__(self, record: Any) -> None:
        record["message"] = self.redact(record["message"])


def render_console_record(record: Mapping[str, Any]) -> str:
    """格式化终端记录，不修改共享 record。"""

    message = str(record.get("message", ""))
    level = record.get("level")
    level_name = getattr(level, "name", str(level or "INFO"))
    if level_name == "DEBUG":
        message = message.replace("\n", "\\n").replace("\r", "\\r")
        if len(message) > 2_000:
            message = f"{message[:2_000]}…"
    timestamp = record.get("time")
    format_time = getattr(timestamp, "strftime", None)
    if callable(format_time):
        time_text = format_time("%Y-%m-%d %H:%M:%S")
    else:
        time_text = time.strftime("%Y-%m-%d %H:%M:%S")
    extra = record.get("extra")
    account = extra.get("account", "系统") if isinstance(extra, Mapping) else "系统"
    return f"{time_text}|{level_name:<7}|{account}|{message}\n"


def _setup_logging(
    runtime: RuntimeConfig, redactor: LogRedactor, *, stream: TextIO = sys.stdout
) -> tuple[Any, str]:
    runtime.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    run_start_ts = time.strftime("%Y%m%d-%H%M%S")
    base_logger.remove()
    base_logger.configure(patcher=redactor)

    def terminal_sink(message: Any) -> None:
        stream.write(render_console_record(message.record))
        stream.flush()

    base_logger.add(terminal_sink, format="{message}", colorize=False)
    system_format = "{time:YYYY-MM-DD HH:mm:ss}|{level:<7}|{extra[account]}|{message}"
    base_logger.add(
        runtime.paths.logs_dir / f"weban-{run_start_ts}.log",
        encoding="utf-8",
        format=system_format,
        rotation="10 MB",
        retention="7 days",
        filter=lambda record: record["extra"].get("account") == "系统",
    )
    return base_logger.bind(account="系统"), run_start_ts


def _make_account_filter(log_key: str) -> Callable[[Any], bool]:
    return lambda record: record["extra"].get("account") == log_key


class StopRequested(InterruptedError):
    """停止事件打断业务模块中的同步等待。"""


class InterruptibleTime:
    """保留 time 模块接口，仅把 sleep 替换成可中断等待。"""

    def __init__(self, stop_event: threading.Event):
        self._stop_event = stop_event

    def sleep(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        if self._stop_event.wait(delay):
            raise StopRequested("运行已被中断")

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@dataclass(frozen=True)
class RuntimeDependencies:
    client_class: type[Any]
    check_browser_health: Callable[[str | None, str | None, int | None], str]


def _set_module_attr(module: ModuleType, name: str, value: object) -> None:
    setattr(module, name, value)


def _load_business_modules() -> tuple[ModuleType, ModuleType]:
    # 局部静态 import 同时保证启动校验前不加载浏览器依赖，并让 PyInstaller
    # 能发现模块；测试可替换此加载边界而不导入真实浏览器。
    import captcha
    import client

    return captcha, client


def _apply_runtime_adapters(
    runtime: RuntimeConfig, stop_event: threading.Event
) -> RuntimeDependencies:
    """导入业务模块后注入统一路径、交互策略和可中断等待。

    当前 client/captcha 尚无这些构造参数，因此入口使用兼容适配；若后续模块
    增加正式参数，_create_client() 会自动优先传入。
    """

    os.environ["WB_DATA_DIR"] = str(runtime.paths.data_dir)
    os.environ["WB_NON_INTERACTIVE"] = (
        "1" if runtime.interaction.non_interactive else "0"
    )
    captcha_module, client_module = _load_business_modules()

    policy_fn = lambda: runtime.interaction.non_interactive
    _set_module_attr(captcha_module, "is_non_interactive", policy_fn)
    _set_module_attr(client_module, "is_non_interactive", policy_fn)

    interruptible_time = InterruptibleTime(stop_event)
    _set_module_attr(captcha_module, "time", interruptible_time)
    _set_module_attr(client_module, "time", interruptible_time)

    # client.py 目前在导入时计算这些全局路径；显式覆盖可同时修复
    # --data-dir 晚于 import 生效和自定义 --config 路径不统一的问题。
    _set_module_attr(client_module, "base_path", str(runtime.paths.data_dir))
    _set_module_attr(client_module, "answer_dir", str(runtime.paths.answer_dir))
    _set_module_attr(
        client_module,
        "answer_path",
        str(runtime.paths.answer_dir / "answer.json"),
    )
    _set_module_attr(
        client_module,
        "root_answer_path",
        str(runtime.paths.data_dir / "answer.json"),
    )

    original_handler = getattr(client_module, "_weban_original_captcha_handler", None)
    if original_handler is None:
        original_handler = client_module.__dict__["CaptchaHandler"]
        _set_module_attr(
            client_module, "_weban_original_captcha_handler", original_handler
        )

    def configured_captcha_handler(*args: Any, **kwargs: Any) -> Any:
        tenant_code = kwargs.get("tenant_code")
        user_id = kwargs.get("user_id")
        if tenant_code is None and args:
            tenant_code = args[0]
        if user_id is None and len(args) > 1:
            user_id = args[1]
        digest = hashlib.sha256(
            f"{tenant_code or ''}\0{user_id or ''}".encode()
        ).hexdigest()[:16]
        kwargs.setdefault(
            "debug_dir",
            runtime.paths.captcha_debug_dir / f"account-{digest}",
        )
        kwargs.setdefault("non_interactive", runtime.interaction.non_interactive)
        kwargs.setdefault("stop_event", stop_event)
        return original_handler(*args, **kwargs)

    _set_module_attr(client_module, "CaptchaHandler", configured_captcha_handler)
    return RuntimeDependencies(
        client_class=client_module.__dict__["WeBanClient"],
        check_browser_health=captcha_module.__dict__["check_browser_health"],
    )


class AccountRunStatus(str, Enum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AccountRunResult:
    account_index: int
    log_key: str
    status: AccountRunStatus
    detail: str = ""


@dataclass(frozen=True)
class RunSummary:
    results: tuple[AccountRunResult, ...]

    @property
    def success_count(self) -> int:
        return sum(result.status is AccountRunStatus.SUCCESS for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status is AccountRunStatus.FAILED for result in self.results)

    @property
    def incomplete_count(self) -> int:
        return sum(
            result.status is AccountRunStatus.INCOMPLETE for result in self.results
        )

    @property
    def cancelled_count(self) -> int:
        return sum(
            result.status is AccountRunStatus.CANCELLED for result in self.results
        )

    @property
    def exit_code(self) -> int:
        if not self.results:
            return EXIT_FAILURE
        if self.success_count == len(self.results):
            return EXIT_SUCCESS
        if self.success_count == 0:
            return EXIT_FAILURE
        return EXIT_PARTIAL_FAILURE


def _raise_if_stopped(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise StopRequested("运行已被中断")


def _workflow_status(result: Any) -> str:
    """读取业务层结构化结果，同时兼容旧客户端返回 None。"""

    if result is None:
        return "success"
    status = getattr(result, "status", None)
    value = getattr(status, "value", status)
    if isinstance(value, str):
        return value.lower()
    ok = getattr(result, "ok", None)
    if ok is True:
        return "success"
    if ok is False:
        return "incomplete"
    return "success"


def _workflow_message(result: Any, fallback: str) -> str:
    message = getattr(result, "message", "")
    return str(message).strip() or fallback


@contextmanager
def _interruptible_lock(lock: Any, stop_event: threading.Event) -> Iterator[None]:
    acquired = False
    while not acquired:
        _raise_if_stopped(stop_event)
        acquired = lock.acquire(timeout=0.2)
    try:
        yield
    finally:
        lock.release()


def _client_kwargs(
    account: ResolvedAccount,
    runtime: RuntimeConfig,
    stop_event: threading.Event,
    log: Any,
    client_class: type[Any],
) -> dict[str, Any]:
    settings = account.settings
    kwargs: dict[str, Any] = {
        "log": log,
        "browser_path": settings.browser_path,
        "cdp_host": settings.cdp_host,
        "cdp_port": settings.cdp_port,
        "debug": settings.debug,
        "ai_config": runtime.ai.as_dict(),
        "video_speed": settings.video_speed,
        "jupiter_fallback": settings.jupiter_fallback,
    }
    try:
        parameters = inspect.signature(client_class).parameters
    except (TypeError, ValueError):
        parameters = {}
    optional_integration = {
        "interaction_policy": runtime.interaction,
        "non_interactive": runtime.interaction.non_interactive,
        "stop_event": stop_event,
        "data_dir": runtime.paths.data_dir,
        "captcha_debug_dir": (
            runtime.paths.captcha_debug_dir
            / account.identity.tenant_dir
            / account.identity.account_dir
        ),
    }
    for name, value in optional_integration.items():
        if name in parameters:
            kwargs[name] = value
    return kwargs


def _create_client(
    account: ResolvedAccount,
    runtime: RuntimeConfig,
    stop_event: threading.Event,
    log: Any,
    client_class: type[Any],
) -> Any:
    credentials = account.credentials
    kwargs = _client_kwargs(account, runtime, stop_event, log, client_class)
    if credentials.uses_token:
        return client_class(
            credentials.tenant_name,
            user={"userId": credentials.user_id, "token": credentials.token},
            **kwargs,
        )
    return client_class(
        credentials.tenant_name,
        credentials.username,
        credentials.password,
        **kwargs,
    )


def run_account(
    account: ResolvedAccount,
    runtime: RuntimeConfig,
    account_index: int,
    stop_event: threading.Event,
    dependencies: RuntimeDependencies,
    logger: Any,
    run_start_ts: str,
) -> AccountRunResult:
    """运行一个账号并返回结构化结果，不把异常转换成进程级成功。"""

    identity = account.identity
    account_log_dir = (
        runtime.paths.logs_dir / identity.tenant_dir / identity.account_dir
    )
    try:
        account_log_dir.mkdir(parents=True, exist_ok=True)
        handler_id = base_logger.add(
            account_log_dir / f"weban-{run_start_ts}.log",
            encoding="utf-8",
            level="DEBUG",
            format=("{time:YYYY-MM-DD HH:mm:ss}|{level:<7}|{extra[account]}|{message}"),
            rotation="10 MB",
            retention="7 days",
            filter=_make_account_filter(identity.log_key),
        )
    except OSError as exc:
        logger.error(f"{identity.log_key} 无法创建独立日志：{type(exc).__name__}")
        return AccountRunResult(
            account_index,
            identity.log_key,
            AccountRunStatus.FAILED,
            "log_setup_failed",
        )

    log = base_logger.bind(account=identity.log_key)
    client: Any = None
    incomplete_reasons: list[str] = []
    try:
        _raise_if_stopped(stop_event)
        settings = account.settings
        random_answer = settings.random_answer
        if runtime.interaction.non_interactive and not random_answer:
            log.warning("无交互模式下强制启用随机作答")
            random_answer = True

        if account.credentials.uses_token:
            log.info("使用 Token 凭据登录")
        else:
            log.info("使用密码凭据登录")
        client = _create_client(
            account, runtime, stop_event, log, dependencies.client_class
        )
        _raise_if_stopped(stop_event)
        if not client.login():
            log.error("登录失败")
            return AccountRunResult(
                account_index,
                identity.log_key,
                AccountRunStatus.FAILED,
                "login_failed",
            )

        log.info("登录成功，模拟打开首页")
        client.simulate_home_page()
        _raise_if_stopped(stop_event)
        log.info("开始同步答案")
        with _interruptible_lock(_SYNC_LOCK, stop_event):
            _raise_if_stopped(stop_event)
            initial_sync = client.sync_answers()
        initial_sync_status = _workflow_status(initial_sync)
        if initial_sync_status in {"failed", "incomplete"}:
            reason = _workflow_message(initial_sync, "初始题库同步未完整完成")
            log.warning(reason)
            incomplete_reasons.append(reason)
        elif initial_sync_status == "locked":
            log.error("初始题库同步报告账号已锁定")
            return AccountRunResult(
                account_index,
                identity.log_key,
                AccountRunStatus.FAILED,
                "account_locked",
            )

        client.exam_mode = settings.exam_mode
        _raise_if_stopped(stop_event)
        workflow = client.run_project_cycle(
            study_time=settings.study_time,
            study_mode=settings.study_mode,
            exam_mode=settings.exam_mode,
            random_answer=random_answer,
            exam_question_time=settings.exam_question_time,
            exam_submit_match_rate=settings.exam_submit_match_rate,
        )
        workflow_status = _workflow_status(workflow)
        if workflow_status in {"failed", "locked"}:
            reason = _workflow_message(workflow, "学习或考试流程失败")
            log.error(reason)
            return AccountRunResult(
                account_index,
                identity.log_key,
                AccountRunStatus.FAILED,
                f"workflow_{workflow_status}",
            )
        if workflow_status == "incomplete":
            reason = _workflow_message(workflow, "学习或考试未完整完成")
            log.warning(reason)
            incomplete_reasons.append(reason)

        _raise_if_stopped(stop_event)
        log.info("最终同步答案")
        with _interruptible_lock(_SYNC_LOCK, stop_event):
            _raise_if_stopped(stop_event)
            final_sync = client.sync_answers()
        final_sync_status = _workflow_status(final_sync)
        if final_sync_status in {"failed", "incomplete"}:
            reason = _workflow_message(final_sync, "最终题库同步未完整完成")
            log.warning(reason)
            incomplete_reasons.append(reason)
        elif final_sync_status == "locked":
            log.error("最终题库同步报告账号已锁定")
            return AccountRunResult(
                account_index,
                identity.log_key,
                AccountRunStatus.FAILED,
                "account_locked",
            )

        if incomplete_reasons:
            log.warning("执行结束，但存在未完整确认的阶段")
            return AccountRunResult(
                account_index,
                identity.log_key,
                AccountRunStatus.INCOMPLETE,
                "; ".join(dict.fromkeys(incomplete_reasons)),
            )
        log.success("执行完成")
        return AccountRunResult(
            account_index, identity.log_key, AccountRunStatus.SUCCESS
        )
    except (StopRequested, InterruptedError):
        log.warning("任务已中断")
        return AccountRunResult(
            account_index,
            identity.log_key,
            AccountRunStatus.CANCELLED,
            "interrupted",
        )
    except PermissionError as exc:
        log.error(f"权限错误：{exc}")
        return AccountRunResult(
            account_index,
            identity.log_key,
            AccountRunStatus.FAILED,
            "permission_error",
        )
    except (RuntimeError, ValueError) as exc:
        log.error(f"运行失败（{type(exc).__name__}）：{exc}")
        return AccountRunResult(
            account_index,
            identity.log_key,
            AccountRunStatus.FAILED,
            type(exc).__name__,
        )
    except Exception as exc:  # noqa: BLE001 - 账号边界必须转为结构化失败
        log.error(f"未预期错误（{type(exc).__name__}）：{exc}")
        return AccountRunResult(
            account_index,
            identity.log_key,
            AccountRunStatus.FAILED,
            type(exc).__name__,
        )
    finally:
        close_client = getattr(client, "close", None)
        if close_client is not None:
            try:
                close_client()
            except Exception as exc:  # noqa: BLE001 - 清理失败不能覆盖原始结果
                log.error(f"客户端资源清理失败（{type(exc).__name__}）")
        base_logger.remove(handler_id)


def _probe_login(
    account: ResolvedAccount,
    runtime: RuntimeConfig,
    stop_event: threading.Event,
    dependencies: RuntimeDependencies,
) -> bool:
    log = base_logger.bind(account=account.identity.log_key)
    log.info("正在验证交互输入的账号")
    client: Any = None
    try:
        client = _create_client(
            account, runtime, stop_event, log, dependencies.client_class
        )
        return bool(client.login())
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        log.error(f"账号验证失败（{type(exc).__name__}）：{exc}")
        return False
    finally:
        close_client = getattr(client, "close", None)
        if close_client is not None:
            try:
                close_client()
            except Exception as exc:  # noqa: BLE001 - 验证结果优先
                log.error(f"验证客户端清理失败（{type(exc).__name__}）")


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or ""))


def _run_update_check(logger: Any) -> None:
    try:
        response = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"WeBan/{VERSION}",
            },
            timeout=UPDATE_CHECK_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning(f"检查更新失败（网络异常）：{exc}，已跳过")
        return
    if response.status_code != 200:
        logger.warning(f"检查更新失败：HTTP {response.status_code}，已跳过")
        return
    try:
        data = response.json()
    except ValueError:
        logger.warning("检查更新失败：响应解析异常，已跳过")
        return
    if not isinstance(data, dict):
        logger.warning("检查更新失败：响应格式异常，已跳过")
        return
    latest = str(data.get("tag_name") or "")
    if _parse_version(latest) > _parse_version(VERSION):
        latest_url = data.get("html_url") or (
            f"https://github.com/{GITHUB_REPO}/releases/latest"
        )
        logger.warning(f"发现新版本 {latest}，下载地址：{latest_url}")
    else:
        logger.info(f"已是最新版本（{VERSION}）")


def _check_update_async(logger: Any) -> None:
    threading.Thread(
        target=_run_update_check,
        args=(logger,),
        daemon=True,
        name="update-check",
    ).start()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _first_local_template(runtime: RuntimeConfig) -> str | None:
    for candidate in runtime.paths.config_example_candidates:
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def save_interactive_account(
    runtime: RuntimeConfig, credentials: AccountCredentials, logger: Any
) -> None:
    """原子保存交互凭据，并尽可能将配置权限限制为当前用户读写。"""

    config_path = runtime.paths.config_path
    if config_path.exists():
        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"无法读取配置文件：{config_path}") from exc
    else:
        content = _first_local_template(runtime) or "# WeBan 配置文件\n[settings]\n"
    if content and not content.endswith("\n"):
        content += "\n"
    content += (
        "\n[[account]]\n"
        f"tenant_name = {_toml_string(credentials.tenant_name)}\n"
        f"username = {_toml_string(credentials.username)}\n"
        f"password = {_toml_string(credentials.password)}\n"
        f"user_id = {_toml_string(credentials.user_id)}\n"
        f"token = {_toml_string(credentials.token)}\n"
    )
    atomic_write_text(config_path, content, mode=0o600)
    logger.success(f"账号已安全保存到 {config_path}")


def prompt_account_interactive(
    policy: InteractionPolicy,
    *,
    input_fn: Callable[[str], str] | None = None,
    password_fn: Callable[[str], str] | None = None,
) -> dict[str, str] | None:
    """交互读取账号；密码使用 getpass，不在终端回显。"""

    if not policy.allow_input:
        raise RuntimeError("无交互模式禁止读取终端输入")
    read_input = input if input_fn is None else input_fn
    read_password = getpass.getpass if password_fn is None else password_fn
    print("\n请输入账号信息：")
    try:
        tenant_name = read_input("  学校全称：").strip()
        username = read_input("  用户名（学号）：").strip()
        password = read_password("  密码（默认同用户名）：")
    except EOFError:
        return None
    if not tenant_name or not username:
        return None
    return {
        "tenant_name": tenant_name,
        "username": username,
        "password": password or username,
    }


def open_editor(
    path: Path,
    policy: InteractionPolicy,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> None:
    if not policy.allow_input:
        raise RuntimeError("无交互模式禁止打开编辑器")
    if sys.platform == "win32":
        command = ["notepad", str(path)]
    elif sys.platform == "darwin":
        command = ["open", "-t", str(path)]
    else:
        command = ["xdg-open", str(path)]
    try:
        subprocess.Popen(command)
    except OSError as exc:
        raise RuntimeError("无法打开配置编辑器") from exc
    read_input = input if input_fn is None else input_fn
    read_input("编辑完成后按回车键继续...")


def _discover_cdp() -> tuple[str, int] | None:
    for host, port in (
        ("127.0.0.1", 9222),
        ("127.0.0.1", 9223),
        ("host.docker.internal", 9222),
        ("host.docker.internal", 9223),
    ):
        try:
            with socket.create_connection((host, port), timeout=1):
                response = requests.get(f"http://{host}:{port}/json/version", timeout=3)
            data = response.json()
            if response.ok and isinstance(data, dict) and data.get("Browser"):
                return host, port
        except (OSError, requests.RequestException, ValueError):
            continue
    return None


def _prepare_browsers(
    runtime: RuntimeConfig,
    dependencies: RuntimeDependencies,
    logger: Any,
) -> RuntimeConfig:
    needed_accounts = [
        account
        for account in runtime.accounts
        if account.settings.study_mode != "false"
        or account.settings.exam_mode != "false"
    ]
    if not needed_accounts:
        return runtime

    discovered = None
    if any(
        not account.settings.browser_path
        and not account.settings.cdp_host
        and not account.settings.cdp_port
        for account in needed_accounts
    ):
        discovered = _discover_cdp()
        if discovered:
            logger.info("自动探测到本地 CDP 浏览器")

    updated_accounts: list[ResolvedAccount] = []
    checked: set[tuple[str | None, str | None, int | None]] = set()
    for account in runtime.accounts:
        settings = account.settings
        if (
            discovered
            and not settings.browser_path
            and not settings.cdp_host
            and not settings.cdp_port
        ):
            settings = replace(settings, cdp_host=discovered[0], cdp_port=discovered[1])
        if settings.study_mode != "false" or settings.exam_mode != "false":
            key = (settings.browser_path, settings.cdp_host, settings.cdp_port)
            if key not in checked:
                dependencies.check_browser_health(*key)
                checked.add(key)
        updated_accounts.append(replace(account, settings=settings))
    logger.info("浏览器检测通过")
    return replace(runtime, accounts=tuple(updated_accounts))


def _execute_accounts(
    runtime: RuntimeConfig,
    stop_event: threading.Event,
    dependencies: RuntimeDependencies,
    logger: Any,
    run_start_ts: str,
    *,
    use_multithread: bool,
) -> RunSummary:
    if not use_multithread or len(runtime.accounts) <= 1:
        logger.info("使用单线程模式，逐个执行")
        results: list[AccountRunResult] = []
        for index, account in enumerate(runtime.accounts):
            _raise_if_stopped(stop_event)
            results.append(
                run_account(
                    account,
                    runtime,
                    index,
                    stop_event,
                    dependencies,
                    logger,
                    run_start_ts,
                )
            )
        return RunSummary(tuple(results))

    max_workers = min(len(runtime.accounts), runtime.max_workers)
    logger.info(f"使用多线程模式，最大并发数：{max_workers}")
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures: dict[Future[AccountRunResult], int] = {}
    results = []
    try:
        for index, account in enumerate(runtime.accounts):
            future = executor.submit(
                run_account,
                account,
                runtime,
                index,
                stop_event,
                dependencies,
                logger,
                run_start_ts,
            )
            futures[future] = index
        for future in as_completed(futures):
            index = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - future 边界兜底
                logger.error(
                    f"账号任务 {index + 1} 异常（{type(exc).__name__}）：{exc}"
                )
                results.append(
                    AccountRunResult(
                        index,
                        f"账号{index + 1:02d}",
                        AccountRunStatus.FAILED,
                        type(exc).__name__,
                    )
                )
    except KeyboardInterrupt:
        stop_event.set()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    results.sort(key=lambda result: result.account_index)
    return RunSummary(tuple(results))


def _register_runtime_secrets(redactor: LogRedactor, runtime: RuntimeConfig) -> None:
    redactor.register(runtime.ai.api_key)
    for account in runtime.accounts:
        redactor.register_account(account)


def _build_runtime(
    argv: list[str] | None,
    env: Mapping[str, str],
) -> tuple[Any, dict[str, Any], RuntimeConfig, bool]:
    opts = parse_args(argv)
    paths = resolve_paths(opts, env, script_path=__file__)
    document = load_toml(paths.config_path)
    raw_settings = document.get("settings", {})
    if raw_settings is None:
        raw_settings = {}
    if not isinstance(raw_settings, dict):
        raise ConfigError("[settings] 必须是 TOML 表")
    policy = resolve_interaction_policy(opts, raw_settings, env)
    created_template = False
    if not paths.config_path.exists() and policy.non_interactive:
        created_template = create_local_config_template(paths)
        document = load_toml(paths.config_path)
    runtime = build_runtime_config(opts, document, paths, env)
    return opts, document, runtime, created_template


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    source_env = dict(os.environ if env is None else env)
    stop_event = threading.Event()
    logger: Any = None
    try:
        opts, document, runtime, created_template = _build_runtime(argv, source_env)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED

    redactor = LogRedactor()
    _register_runtime_secrets(redactor, runtime)
    try:
        logger, run_start_ts = _setup_logging(runtime, redactor)
    except OSError as exc:
        print(f"日志初始化失败：{exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED

    try:
        logger.info(f"程序启动，当前版本：{VERSION}")
        if created_template:
            logger.warning(
                f"已创建本地配置模板，请填写账号后重试：{runtime.paths.config_path}"
            )
        dependencies: RuntimeDependencies | None = None

        if not runtime.accounts:
            if runtime.interaction.non_interactive:
                logger.error(
                    f"没有有效账号；请填写 {runtime.paths.config_path}，"
                    "或设置 WB_TENANT_NAME/WB_USERNAME/WB_PASSWORD"
                )
                return EXIT_CONFIG_ERROR
            logger.warning("未找到有效账号，请按提示输入")
            while not runtime.accounts:
                raw_account = prompt_account_interactive(runtime.interaction)
                if raw_account is None:
                    logger.error("账号输入不完整或已中断")
                    return EXIT_CONFIG_ERROR
                candidate_document = dict(document)
                candidate_document["account"] = [raw_account]
                candidate_runtime = build_runtime_config(
                    opts,
                    candidate_document,
                    runtime.paths,
                    source_env,
                    stdin_is_tty=True,
                )
                _register_runtime_secrets(redactor, candidate_runtime)
                if dependencies is None:
                    dependencies = _apply_runtime_adapters(
                        candidate_runtime, stop_event
                    )
                if not _probe_login(
                    candidate_runtime.accounts[0],
                    candidate_runtime,
                    stop_event,
                    dependencies,
                ):
                    logger.error("登录验证失败，请重新输入")
                    continue
                save_interactive_account(
                    candidate_runtime,
                    candidate_runtime.accounts[0].credentials,
                    logger,
                )
                document = load_toml(runtime.paths.config_path)
                runtime = build_runtime_config(
                    opts,
                    document,
                    runtime.paths,
                    source_env,
                    stdin_is_tty=True,
                )
                _register_runtime_secrets(redactor, runtime)

        if len(runtime.accounts) == 1 and runtime.interaction.allow_input:
            choice = input("当前已配置 1 个账号，是否更换账号？(y/N)：").strip().lower()
            if choice == "y":
                open_editor(runtime.paths.config_path, runtime.interaction)
                document = load_toml(runtime.paths.config_path)
                runtime = build_runtime_config(
                    opts,
                    document,
                    runtime.paths,
                    source_env,
                    stdin_is_tty=True,
                )
                if not runtime.accounts:
                    logger.error("编辑后的配置中没有有效账号")
                    return EXIT_CONFIG_ERROR
                _register_runtime_secrets(redactor, runtime)

        logger.info(f"共加载到 {len(runtime.accounts)} 个账号")
        if dependencies is None:
            dependencies = _apply_runtime_adapters(runtime, stop_event)

        # 所有配置、账号和路径均已校验后才允许外网更新检查。
        _check_update_async(logger)
        logger.info("程序更新地址：https://github.com/hangone/WeBan")

        if len(runtime.accounts) > 1 and runtime.interaction.allow_input:
            choice = (
                input(f"检测到 {len(runtime.accounts)} 个账号，是否同时运行？(Y/n)：")
                .strip()
                .lower()
            )
            use_multithread = choice != "n"
        else:
            use_multithread = len(runtime.accounts) > 1

        summary = _execute_accounts(
            runtime,
            stop_event,
            dependencies,
            logger,
            run_start_ts,
            use_multithread=use_multithread,
        )
        logger.info(
            "所有账号执行完成："
            f"成功 {summary.success_count}，未完整 {summary.incomplete_count}，"
            f"失败 {summary.failed_count}，"
            f"中断 {summary.cancelled_count}"
        )
        exit_code = summary.exit_code
        if runtime.interaction.allow_input:
            input("按回车键退出")
        return exit_code
    except KeyboardInterrupt:
        stop_event.set()
        if logger is not None:
            logger.warning("用户终止，正在停止账号任务")
        return EXIT_INTERRUPTED
    except ConfigError as exc:
        logger.error(f"配置错误：{exc}")
        return EXIT_CONFIG_ERROR
    except (OSError, RuntimeError) as exc:
        logger.error(f"启动失败（{type(exc).__name__}）：{exc}")
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - 进程入口必须返回非零退出码
        logger.error(f"未预期启动错误（{type(exc).__name__}）：{exc}")
        return EXIT_FAILURE
    finally:
        # Loguru 文件 sink 在 Windows 上持有独占句柄；显式移除可保证
        # 嵌入调用、测试及临时数据目录在 main() 返回后立即可清理。
        base_logger.remove()


if __name__ == "__main__":
    raise SystemExit(main())
