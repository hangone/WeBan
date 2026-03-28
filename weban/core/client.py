from typing import Any, Dict, Optional
import logging
from .browser import BrowserMixin
from .auth import AuthMixin
from .study import StudyMixin
from .answer import AnswerMixin
from .exam import ExamMixin

logger = logging.getLogger(__name__)


class WeBanClient(BrowserMixin, AuthMixin, StudyMixin, AnswerMixin, ExamMixin):
    def __init__(
        self,
        tenant_name: str,
        account: Optional[str] = None,
        password: Optional[str] = None,
        user_id: Optional[str] = None,
        token: Optional[str] = None,
        continue_on_invalid_token: bool = True,
        user: Optional[Dict[str, str]] = None,
        browser: Optional[Dict[str, Any]] = None,
        log=None,
    ) -> None:
        self.log = log or logging.LoggerAdapter(logger, {"account": "系统"})
        self.tenant_name = (tenant_name or "").strip()
        self.account = (account or "").strip()
        self.password = (password or "").strip()
        self.user_id = (user_id or "").strip()
        self.token = (token or "").strip()
        self.continue_on_invalid_token = continue_on_invalid_token
        self.user = user or {}
        self.browser_config = self._parse_browser_config(browser or {})
        self.base_url = "https://weiban.mycourse.cn"
        self.answers = self._load_answers()

        # 浏览器相关对象在 _start() 时初始化，初始均为 None
        self._playwright: Optional[Any] = None  # type: ignore[assignment]
        self._browser: Optional[Any] = None  # type: ignore[assignment]
        self._context: Optional[Any] = None  # type: ignore[assignment]
        self._page: Optional[Any] = None  # type: ignore[assignment]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop()