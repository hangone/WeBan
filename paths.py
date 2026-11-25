"""运行目录解析：程序目录、打包资源目录和可写数据目录。

数据目录优先级：
1. WB_DATA_DIR（--data-dir 或环境变量），config/logs/answer 全放这里；
2. 程序所在目录（源码运行，或 exe 所在目录可写）；
3. 用户数据目录（exe 放在 Program Files 等只读位置时自动回退）。

打包（PyInstaller）时程序目录是可执行文件所在目录，资源目录是
sys._MEIPASS 临时解压目录；源码运行时两者相同。
"""

import os
import sys
import tempfile

APP_NAME = "WeBan"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _program_dir() -> str:
    """源码运行时为项目目录，打包后为 exe 所在目录。"""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _program_dir()
BUNDLE_DIR = getattr(sys, "_MEIPASS", None) or BASE_DIR


def is_writable(path: str) -> bool:
    """目录可创建文件则视为可写；用临时文件探测，不留下残留。

    :param path: 待检测目录，不存在时先创建
    :return: 可创建临时文件返回 True
    """
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False


def user_data_dir() -> str:
    """当前平台的用户数据目录（不含创建）。"""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_NAME)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", APP_NAME
        )
    root = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(root, APP_NAME.lower())


_data_dir = os.environ.get("WB_DATA_DIR", "").strip()
DATA_DIR = os.path.abspath(_data_dir) if _data_dir else ""

if DATA_DIR:
    APP_DIR = DATA_DIR
    USED_FALLBACK = False
elif is_writable(BASE_DIR):
    APP_DIR = BASE_DIR
    USED_FALLBACK = False
else:
    APP_DIR = user_data_dir()
    USED_FALLBACK = True
    if not is_writable(APP_DIR):
        raise RuntimeError(
            f"程序目录与用户数据目录均不可写（{APP_DIR}），请用 --data-dir 指定可写目录"
        )

CONFIG_PATH = os.path.join(APP_DIR, "config.toml")
LOGS_DIR = os.path.join(APP_DIR, "logs")
ANSWER_DIR = os.path.join(APP_DIR, "answer")
ANSWER_PATH = os.path.join(ANSWER_DIR, "answer.json")
ROOT_ANSWER_PATH = os.path.join(APP_DIR, "answer.json")
BUNDLE_ANSWER_PATH = os.path.join(BUNDLE_DIR, "answer", "answer.json")

CONFIG_EXAMPLE_CANDIDATES = (
    os.path.join(BUNDLE_DIR, "config.example.toml"),
    os.path.join(BASE_DIR, "config.example.toml"),
)