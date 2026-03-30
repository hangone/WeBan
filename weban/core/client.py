from typing import Any, Dict
import logging
import threading
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
        account: str,
        password: str,
        user_id: str,
        token: str,
        user: Dict[str, str],
        browser: Dict[str, Any],
        log: Any = None,
    ) -> None:
        self.log = log or logging.LoggerAdapter(logger, {"account": "系统"})
        self.tenant_name = (tenant_name or "").strip()
        self.account = (account or "").strip()
        self.password = (password or "").strip()
        self.user_id = (user_id or "").strip()
        self.token = (token or "").strip()
        self.user = user or {}
        self.browser_config = self._parse_browser_config(browser or {})
        self.base_url = "https://weiban.mycourse.cn"
        self.answers = self._load_answers()

        # 浏览器相关对象在 _start() 时初始化，由 BrowserMixin 统一管理
        # 共享资源锁
        self._answers_lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop()
