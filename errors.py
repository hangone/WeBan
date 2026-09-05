from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WorkflowStatus(str, Enum):
    """账号内工作流的可观察终态。"""

    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    LOCKED = "locked"


@dataclass(frozen=True)
class WorkflowResult:
    """学习、考试或同步阶段的结构化结果。"""

    status: WorkflowStatus
    message: str = ""
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    details: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is WorkflowStatus.SUCCESS

    @classmethod
    def success(
        cls, message: str = "", *, completed: int = 0, skipped: int = 0
    ) -> WorkflowResult:
        return cls(
            WorkflowStatus.SUCCESS,
            message,
            completed=completed,
            skipped=skipped,
        )

    @classmethod
    def incomplete(
        cls,
        message: str,
        *,
        completed: int = 0,
        failed: int = 1,
        skipped: int = 0,
        details: tuple[str, ...] = (),
    ) -> WorkflowResult:
        return cls(
            WorkflowStatus.INCOMPLETE,
            message,
            completed=completed,
            failed=failed,
            skipped=skipped,
            details=details,
        )

    @classmethod
    def failed_result(
        cls, message: str, *, details: tuple[str, ...] = ()
    ) -> WorkflowResult:
        return cls(
            WorkflowStatus.FAILED,
            message,
            failed=1,
            details=details,
        )

    def combine(self, other: WorkflowResult, message: str = "") -> WorkflowResult:
        """合并连续阶段，保留其中最严重的状态。"""

        priority = {
            WorkflowStatus.SUCCESS: 0,
            WorkflowStatus.INCOMPLETE: 1,
            WorkflowStatus.FAILED: 2,
            WorkflowStatus.LOCKED: 3,
        }
        status = (
            self.status
            if priority[self.status] >= priority[other.status]
            else other.status
        )
        deciding_result = self if status is self.status else other
        return WorkflowResult(
            status=status,
            message=message or deciding_result.message,
            completed=self.completed + other.completed,
            failed=self.failed + other.failed,
            skipped=self.skipped + other.skipped,
            details=self.details + other.details,
        )


class WeBanError(RuntimeError):
    """可预期的业务或协议错误基类。"""


class ResponseValidationError(WeBanError):
    """响应结构不完整，继续执行可能产生副作用。"""


class APIResponseError(WeBanError):
    """HTTP 或响应编码错误，不包含凭据和完整响应正文。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        endpoint: str,
        summary: str,
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.summary = summary
        status = f"HTTP {status_code}" if status_code is not None else "HTTP 未知"
        super().__init__(f"{message}（{status}，端点 {endpoint}，摘要 {summary}）")


class TokenInvalidError(PermissionError):
    """Token 失效或账号在别处登录。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str = "",
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        super().__init__(message)


class AccountBlockedError(TokenInvalidError):
    """平台返回行为异常或锁号，必须立刻终止当前账号。"""

    def __init__(
        self,
        message: str = "系统检测到行为异常或账号已锁定",
        *,
        detail_code: str = "",
        status_code: int | None = None,
        endpoint: str = "",
    ) -> None:
        self.detail_code = detail_code
        self.status = WorkflowStatus.LOCKED
        self.result = WorkflowResult(
            WorkflowStatus.LOCKED,
            message,
            failed=1,
        )
        super().__init__(
            message,
            status_code=status_code,
            endpoint=endpoint,
        )
