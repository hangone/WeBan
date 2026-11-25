"""终端交互：输入提示与日志输出不互相覆盖。

日志写入控制台前先确认光标在行首，不在行首就先补一个换行。Windows
用 GetConsoleScreenBufferInfo 读取光标列，能覆盖其他进程在提示行后
追加输出的情况；其他平台用本进程最后一次写入是否以换行结尾判断。
输入提示显示期间日志先缓冲，回答后按原顺序补写。
"""

import sys
import threading

_write_lock = threading.RLock()
_prompt_lock = threading.Lock()
_active_prompt = False
_pending: list[str] = []
_ends_with_newline = True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Coord(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _ScreenBufferInfo(ctypes.Structure):
        _fields_ = [
            ("dwSize", _Coord),
            ("dwCursorPosition", _Coord),
            ("wAttributes", wintypes.WORD),
            ("srWindow", wintypes.SMALL_RECT),
            ("dwMaximumWindowSize", _Coord),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetStdHandle.restype = wintypes.HANDLE
    _kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ScreenBufferInfo),
    ]

    def _console_at_line_start() -> bool | None:
        """控制台光标是否在行首；stdout 不是控制台时返回 None。"""
        info = _ScreenBufferInfo()
        handle = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not _kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
        return info.dwCursorPosition.X == 0

else:

    def _console_at_line_start() -> bool | None:
        return None


def _write_now(text: str) -> None:
    """调用方需持有 _write_lock；必要时先补换行，再写并刷新。"""
    global _ends_with_newline
    at_start = _console_at_line_start()
    if at_start is False or (at_start is None and not _ends_with_newline):
        text = "\n" + text
    sys.stdout.write(text)
    sys.stdout.flush()
    _ends_with_newline = text.endswith("\n")


class Stream:
    """loguru 控制台 sink：提示期间缓冲消息，写入前检查光标是否在行首。"""

    def __init__(self, stream=None):
        self._stream = stream if stream is not None else sys.stdout

    def write(self, message) -> None:
        global _ends_with_newline
        text = str(message)
        with _write_lock:
            if _active_prompt:
                _pending.append(text)
                return
            at_start = _console_at_line_start()
            if at_start is False or (at_start is None and not _ends_with_newline):
                text = "\n" + text
            self._stream.write(text)
            self._stream.flush()
            _ends_with_newline = text.endswith("\n")

    def flush(self) -> None:
        with _write_lock:
            self._stream.flush()


def ask(prompt: str = "") -> str:
    """显示提示并读取一行，语义同内置 input()（不含结尾换行）。

    提示显示期间其他线程的日志先缓冲，回答后补写。非交互 stdin 不会
    回显回车，读取后补一个换行。

    :param prompt: 提示文本，可直接带末尾冒号和空格
    :return: 读取到的行，去掉结尾的 CR/LF
    :raises EOFError: stdin 已关闭
    """
    global _active_prompt, _ends_with_newline
    with _prompt_lock:
        with _write_lock:
            _active_prompt = True
            if prompt:
                _ends_with_newline = False
        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            line = sys.stdin.readline()
            if line == "":
                raise EOFError
        finally:
            with _write_lock:
                _active_prompt = False
                try:
                    tty = sys.stdin.isatty()
                except (AttributeError, ValueError):
                    tty = False
                if not tty:
                    # 非交互 stdin 不回显回车
                    if prompt:
                        sys.stdout.write("\n")
                    _ends_with_newline = True
                elif prompt:
                    # TTY 回车由终端回显换行，Windows 用光标检测判断
                    _ends_with_newline = True
                for text in _pending:
                    _write_now(text)
                _pending.clear()
                sys.stdout.flush()
        return line.rstrip("\r\n")