from __future__ import annotations

import os
import sys
import threading
from typing import Any

import importlib


_INITIAL_CONFIG = """# WeBan 配置文件
# 首次运行时如果不存在 config.toml，程序会按此模板自动生成。

[settings]
# 学习模式：
# false = 不学习
# true  = 正常学习（跳过已完成）
# force = 强制学习（已完成也学习，全部完成后循环）
study_mode = "true"

# 考试模式：
# false = 不考试
# true  = 及格后不再考试
# force = 及格后也重新考试
exam_mode = "true"

# 遇到未知题目是否随机作答：
# true  = 单选选第一个 / 多选全选
# false = 在终端等待手动输入答案
random_answer = false

# 浏览器是否后台无头运行
browser_headless = false

# 每个视频学习停留时长（秒）
study_time = 20

# 每道题最少答题时长（秒）
exam_question_time = 5

# 每道题随机附加等待上限（秒）
exam_question_time_offset = 3

# 允许提交试卷的最低题库匹配率（百分比）
exam_submit_match_rate = 90

# 多账号并发最大线程数
max_workers = 5

# 页面加载 / 元素等待超时时间（毫秒）
browser_timeout_ms = 30000

# 手动登录等待超时时间（秒）
manual_login_timeout_sec = 300

# 任务结束后是否自动关闭浏览器
close_browser_on_finish = true

# token 失效时是否自动回退账号密码登录
continue_on_invalid_token = true

# 是否启用调试日志
debug = false

[[account]]
tenant_name = ""
username = ""
password = ""
# userId = ""
# token = ""
# continue_on_invalid_token = true
"""


class AppConfig:
    """负责加载、初始化和持久化 `config.toml`。"""

    def __init__(self, base_path: str, logger: Any) -> None:
        self.base_path = base_path
        self.config_path = os.path.join(base_path, "config.toml")
        self.logger = logger
        self.settings: dict[str, Any] = {}
        self.accounts: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _create_initial_config(self):
        """创建默认配置文件并返回解析后的 TOML 文档。"""
        self.logger.info("config.toml 文件不存在，正在自动生成默认配置...")
        with open(self.config_path, "w", encoding="utf-8") as file:
            file.write(_INITIAL_CONFIG)
        tomlkit = importlib.import_module("tomlkit")
        return tomlkit.loads(_INITIAL_CONFIG)

    def _load_document(self):
        """读取现有配置文件，若不存在则自动初始化。"""
        try:
            tomlkit = importlib.import_module("tomlkit")
            if not os.path.exists(self.config_path):
                return self._create_initial_config()
            with open(self.config_path, "r", encoding="utf-8") as file:
                return tomlkit.load(file)
        except Exception as exc:
            self.logger.error(f"config.toml 文件读取错误，请检查格式是否正确: {exc}")
            sys.exit(1)

    def load(self) -> None:
        """加载配置文件内容到内存。"""
        document = self._load_document()
        self.settings = dict(document.get("settings", {}))
        self.accounts = list(document.get("account", []))

        if not self.accounts:
            self.logger.error("没有找到有效的账号配置 [[account]]，请检查 config.toml")
            sys.exit(1)

    def update_account_state(
        self,
        account_index: int,
        login_info: dict[str, Any] | None = None,
    ) -> None:
        """在登录成功后回填账号信息到配置文件。"""
        if login_info is None:
            return

        with self._lock:
            try:
                tomlkit = importlib.import_module("tomlkit")
                with open(self.config_path, "r", encoding="utf-8") as file:
                    document = tomlkit.load(file)

                accounts = document.get("account", [])
                if 0 <= account_index < len(accounts):
                    account = accounts[account_index]

                    tenant_name = login_info.get("tenant_name")
                    username = login_info.get("username")

                    if tenant_name:
                        account["tenant_name"] = tenant_name
                    if username:
                        account["username"] = username

                with open(self.config_path, "w", encoding="utf-8") as file:
                    tomlkit.dump(document, file)
            except Exception as exc:
                self.logger.error(f"保存账号登录信息到 config.toml 失败: {exc}")