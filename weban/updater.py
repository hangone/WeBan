import threading
import urllib.request
import urllib.error
import json
import logging

GITHUB_API_URL = "https://api.github.com/repos/hangone/WeBan/releases/latest"
TIMEOUT = 10


def _parse_version(version_str: str) -> tuple[int, ...]:
    """将版本字符串（如 v4.0.0）解析为可比较的元组。"""
    v = version_str.lstrip("v").strip()
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _check_update(current_version: str, logger: logging.Logger) -> None:
    """实际执行更新检查的函数，在后台线程中运行。"""
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={"User-Agent": "WeBan-UpdateChecker"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest_tag: str = data.get("tag_name", "")
        if not latest_tag:
            logger.debug("更新检查：未能从响应中获取版本号")
            return

        current_tuple = _parse_version(current_version)
        latest_tuple = _parse_version(latest_tag)

        if latest_tuple > current_tuple:
            html_url: str = data.get(
                "html_url", "https://github.com/hangone/WeBan/releases"
            )
            logger.warning(
                f"发现新版本 {latest_tag}（当前 {current_version}），"
                f"请前往更新：{html_url}"
            )
        else:
            logger.debug(f"更新检查完成，当前已是最新版本（{current_version}）")

    except urllib.error.URLError as e:
        logger.debug(f"更新检查失败（网络错误）：{e}")
    except TimeoutError:
        logger.debug("更新检查超时，已跳过")
    except Exception as e:
        logger.debug(f"更新检查遇到未知错误，已跳过：{e}")


def check_update_async(current_version: str, logger: logging.Logger) -> None:
    """在后台线程中异步检查更新，不阻塞主程序。"""
    t = threading.Thread(
        target=_check_update,
        args=(current_version, logger),
        daemon=True,
        name="update-checker",
    )
    t.start()
