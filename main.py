import argparse
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from loguru import logger

from captcha import check_browser_health, is_non_interactive
from client import WeBanClient, read_first_existing

# ── 命令行参数与环境变量（优先级：CLI > 环境变量 > 配置文件）────────

def _env_bool(name: str, default: bool = False) -> bool:
    """读取 WB_ 前缀布尔环境变量：1/true/yes 为真，其余为默认"""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_args() -> tuple[argparse.Namespace, list[str]]:
    """解析命令行参数（--config/--data-dir/--non-interactive 等）。

    返回值 (opts, unknown)：opts 为解析结果；unknown 为未识别参数，
    保留不改动（历史调用方式兼容）。
    """
    parser = argparse.ArgumentParser(
        prog="WeBan",
        description="WeBan 学习自动化（多账号，可按项目交替学习+考试）",
        # 禁用前缀缩写（allow_abbrev）：--tenant 不再被当作 --tenant-name
        # 的缩写，参数必须写全名，与配置文件键名/环境变量名严格对应
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="配置文件路径（默认: 程序目录/config.toml）",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help=(
            "数据目录：config.toml、logs、answer 都放在此目录下 "
            "（适合 Docker 挂载持久化，如 docker run -v ./data:/app/data "
            "-e WB_DATA_DIR=/app/data）"
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="无交互模式：所有输入使用默认值，不打开编辑器，末尾不等待回车",
    )
    parser.add_argument(
        "--study-mode",
        choices=["false", "true", "force"],
        help="学习模式（覆盖配置文件）",
    )
    parser.add_argument(
        "--exam-mode",
        choices=["false", "true", "perfect", "force"],
        help="考试模式（覆盖配置文件）",
    )
    parser.add_argument(
        "--random-answer",
        choices=["true", "false"],
        help="题库外题目是否随机作答（覆盖配置文件）",
    )
    parser.add_argument(
        "--cdp-host",
        metavar="HOST",
        help="CDP 浏览器地址（覆盖配置文件）",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        metavar="PORT",
        help="CDP 浏览器端口（覆盖配置文件）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        metavar="N",
        help="多账号最大并发数（覆盖配置文件）",
    )
    parser.add_argument(
        "--tenant-name",
        dest="tenant_name",
        metavar="NAME",
        help="单账号学校全称（与配置文件 tenant_name 对应；配合 --username 免配置文件，"
        "可配合环境变量 WB_TENANT_NAME）",
    )
    parser.add_argument(
        "--username",
        metavar="USER",
        help="单账号用户名（配合 --tenant-name，免配置文件，可配合环境变量 WB_USERNAME）",
    )
    parser.add_argument(
        "--password",
        metavar="PASS",
        help="单账号密码（默认同用户名，可配合环境变量 WB_PASSWORD）",
    )
    parser.add_argument(
        "--study-time",
        metavar="SEC",
        help='每门课学习时长 "基础,随机上限"（秒），如 "20,5"（覆盖配置文件）',
    )
    parser.add_argument(
        "--video-speed",
        type=float,
        metavar="N",
        help="视频课程倍速：0=不按视频时长等待，1=按原时长，2=半速（覆盖配置文件）",
    )
    parser.add_argument(
        "--exam-question-time",
        metavar="SEC",
        help='每道考试题答题等待时长 "基础,随机上限"（秒），如 "3,3"（覆盖配置文件）',
    )
    parser.add_argument(
        "--exam-submit-match-rate",
        type=int,
        metavar="N",
        help="允许交卷的最低题库匹配率（百分比，覆盖配置文件）",
    )
    parser.add_argument(
        "--browser-path",
        metavar="PATH",
        help="浏览器可执行文件路径（覆盖配置文件）",
    )
    parser.add_argument(
        "--jupiter-fallback",
        choices=["true", "false"],
        help="对未加载 apicenext.js 的课程是否补发 jupiter 翻页轨迹（覆盖配置文件）",
    )
    parser.add_argument(
        "--user-id",
        metavar="ID",
        help="单账号用户 ID（配合 --tenant-name --token 用 Token 登录，可配合环境变量 WB_USER_ID）",
    )
    parser.add_argument(
        "--token",
        metavar="TOKEN",
        help="单账号登录 Token（配合 --tenant-name --user-id，可配合环境变量 WB_TOKEN）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试日志（覆盖配置文件）",
    )
    # AI 搜题（覆盖配置文件 [ai] 段）
    parser.add_argument(
        "--ai-enable",
        choices=["true", "false"],
        help="是否启用 AI 搜题（覆盖配置文件 [ai].enable）",
    )
    parser.add_argument(
        "--ai-base-url",
        metavar="URL",
        help="AI 服务 API 基础路径（覆盖配置文件 [ai].base_url）",
    )
    parser.add_argument(
        "--ai-api-key",
        metavar="KEY",
        help="AI 服务 API Key（覆盖配置文件 [ai].api_key）",
    )
    parser.add_argument(
        "--ai-model",
        metavar="NAME",
        help="AI 模型名称（覆盖配置文件 [ai].model）",
    )
    parser.add_argument(
        "--ai-timeout",
        type=int,
        metavar="SEC",
        help="AI 请求超时秒数（覆盖配置文件 [ai].timeout）",
    )
    parser.add_argument(
        "--ai-max-retries",
        type=int,
        metavar="N",
        help="AI 请求失败最大重试次数（覆盖配置文件 [ai].max_retries）",
    )
    opts, unknown = parser.parse_known_args()
    return opts, unknown


def merge_ai_config(opts: argparse.Namespace, ai: dict) -> dict:
    """CLI/环境变量覆盖 AI 配置（[ai] 段）：CLI > 环境变量 > 配置文件。

    未通过 CLI/env 指定的字段保留配置文件原值。
    """
    merged = dict(ai)
    cli_map = {
        "enable": opts.ai_enable,
        "base_url": opts.ai_base_url,
        "api_key": opts.ai_api_key,
        "model": opts.ai_model,
        "timeout": opts.ai_timeout,
        "max_retries": opts.ai_max_retries,
    }
    for key, cli_val in cli_map.items():
        env_val = os.environ.get(f"WB_AI_{key.upper()}")
        if cli_val is not None:
            merged[key] = (
                str(cli_val).strip().lower() in ("1", "true", "yes")
                if key == "enable"
                else cli_val
            )
        elif env_val is not None:
            merged[key] = (
                env_val.strip().lower() in ("1", "true", "yes")
                if key == "enable"
                else env_val
            )
    return merged


# 命令行 > 环境变量 > 自动检测（配置文件在 load_config 后再合并）
_OPTS, _ = _parse_args()
if _OPTS.data_dir:
    _data_dir = _OPTS.data_dir
elif os.environ.get("WB_DATA_DIR"):
    _data_dir = os.environ["WB_DATA_DIR"]
else:
    _data_dir = None


def _detect_non_interactive() -> bool:
    """判定是否无交互运行。

    优先级：--non-interactive 显式指定 > 环境/自动检测。
    环境/自动检测复用 captcha.is_non_interactive()：
    - ENVIRONMENT=docker（或 container）：Dockerfile 默认设置，容器环境
    - stdin 不是 TTY：Docker 无 -it、cron、管道、后台运行、SSH 无 TTY
      会话等都无法接收用户输入，自动进入无交互模式。不用某个专用
      "交互开关"环境变量，因为无交互的环境远不止 docker，且 docker
      -it 时其实可以交互。
    """
    if _OPTS.non_interactive:
        return True
    return is_non_interactive()


NON_INTERACTIVE = _detect_non_interactive()


def _resolve_version() -> str:
    """版本号单一来源：pyproject.toml（打包后从冻结资源读取），importlib.metadata 作回退"""
    candidates = []
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(os.path.join(bundle, "pyproject.toml"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyproject.toml"))

    for path in candidates:
        try:
            with open(path, "rb") as f:
                version = tomllib.load(f).get("project", {}).get("version")
            if version:
                return version
        except (OSError, tomllib.TOMLDecodeError):
            continue

    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("weban")
    except ImportError:  # PackageNotFoundError 是其子类，统一回退 unknown
        return "unknown"


VERSION = f"v{_resolve_version()}"

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(os.path.abspath(sys.executable))
    bundle_path = sys._MEIPASS  # type: ignore[attr-defined]
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    bundle_path = base_path

if _data_dir:
    # Docker/数据目录模式：config/logs/answer 全部放数据目录（可挂载持久化）
    config_path = os.path.join(_data_dir, "config.toml")
    logs_dir = os.path.join(_data_dir, "logs")
else:
    config_path = os.path.join(base_path, "config.toml")
    logs_dir = os.path.join(base_path, "logs")
# 模板可能位于: 打包资源目录(_MEIPASS, onefile 解压) / exe 旁 / 源码目录
# frozen 时 base_path 是 exe 目录而模板在 bundle 里，必须 bundle 优先
config_example_candidates = [
    os.path.join(bundle_path, "config.example.toml"),
    os.path.join(base_path, "config.example.toml"),
]

# 本次进程启动时间戳，用于日志文件名区分每次运行（如 20260810-132642）
run_start_ts = time.strftime("%Y%m%d-%H%M%S")


def _log_format_message(record) -> str:
    """终端 sink 的格式化函数。

    DEBUG 级别的请求/响应详情（含 HTML 页面）带大量换行，转义成单行
    并限制字数，避免刷屏；其余级别（INFO/SUCCESS/WARNING/ERROR 等）
    保持消息原样，正常换行不受影响。日志文件 sink 不经过此函数。
    """
    if record["level"].name == "DEBUG":
        msg = record["message"].replace("\n", "\\n").replace("\r", "\\r")
        if len(msg) > 2000:
            msg = msg[:2000]
        record["message"] = msg
    # 注意：format 为函数时 loguru 不会自动补结尾换行（字符串格式才会），
    # 必须显式加 \n，否则终端所有日志挤在一行
    return log_format + "\n"  # loguru 会基于修改后的 record 替换占位符

# 远程模板下载地址（jsDelivr CDN 稳定；gh-proxy 免费公共代理会限流 403，
# github 官方 raw 域名在国内不稳定，均不用）
CONFIG_EXAMPLE_URL = (
    "https://cdn.jsdelivr.net/gh/hangone/WeBan@main/config.example.toml"
)

# ── 日志 ──
logger.remove()
logger = logger.bind(account="系统")
log_format = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green>|"
    "<level>{level:<7}</level>|"
    "<blue>{extra[account]}</blue>|"
    "<cyan>{message}</cyan>"
)
# 终端输出转义为单行并截断超长消息（DEBUG 模式请求/响应详情可能刷屏）；
# 日志文件使用完整格式，不转义、不截断
logger.add(
    sink=sys.stdout,
    colorize=True,
    format=_log_format_message,
)

os.makedirs(logs_dir, exist_ok=True)
logger.add(
    os.path.join(logs_dir, f"weban-{run_start_ts}.log"),
    encoding="utf-8",
    format=log_format,
    retention="7 days",
)

# 同步锁，防止同时读写题库
sync_lock = threading.Lock()


# ── 更新检查 ──────────────────────────────────────────────

GITHUB_REPO = "hangone/WeBan"
# 网络异常时的请求超时（秒）：检查失败也不能让用户久等
UPDATE_CHECK_TIMEOUT = 3


def _parse_version(text: str) -> tuple[int, ...]:
    """版本号解析为可比较的整数元组（忽略非数字段，如 v3.9.6 → (3,9,6)）"""
    return tuple(int(part) for part in re.findall(r"\d+", text or ""))


def _run_update_check() -> None:
    """执行一次 GitHub 最新 Release 检查并输出结果（同步实现）。

    有新版 → WARNING 提示下载地址；无新版 → DEBUG；
    网络/HTTP/解析失败 → WARNING 说明原因并跳过（不重试、不阻塞）。
    """
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"WeBan/{VERSION}",
            },
            timeout=UPDATE_CHECK_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning(f"检查更新失败（网络异常）：{e}，已跳过")
        return
    if resp.status_code != 200:
        logger.warning(f"检查更新失败：GitHub API 返回 {resp.status_code}，已跳过")
        return
    try:
        data = resp.json()
        if not isinstance(data, dict):
            logger.warning("检查更新失败：响应格式异常，已跳过")
            return
    except ValueError:
        logger.warning("检查更新失败：响应解析异常，已跳过")
        return
    latest = data.get("tag_name") or ""
    if _parse_version(latest) > _parse_version(VERSION):
        latest_url = data.get("html_url") or (
            f"https://github.com/{GITHUB_REPO}/releases/latest"
        )
        logger.warning(
            f"发现新版本 {latest}（当前 {VERSION}），请前往 {latest_url} 下载更新"
        )
    else:
        logger.info(f"已是最新版本（{VERSION}）")


def _check_update_async() -> None:
    """异步检查更新：后台线程执行，不阻塞主流程；失败只提示不等待"""
    threading.Thread(target=_run_update_check, daemon=True, name="update-check").start()


# ── 工具函数 ──────────────────────────────────────────────


def open_editor(path: str):
    """打开系统编辑器编辑指定文件"""
    logger.info(f"配置文件路径: {path}")
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except FileNotFoundError:
        logger.warning("无法打开编辑器，请手动编辑上述文件")
    try:
        print("编辑完成后按回车键继续...", flush=True)
        input()
    except EOFError:  # stdin 关闭（如管道执行）时直接结束
        pass


def is_account_valid(account: dict) -> bool:
    """检查账号是否有效：tenant_name 非空 AND (username 非空 OR (user_id 非空 AND token 非空))"""
    tenant_name = account.get("tenant_name", "").strip()
    username = account.get("username", "").strip()
    user_id = account.get("user_id", "").strip()
    token = account.get("token", "").strip()
    return bool(tenant_name) and (bool(username) or (bool(user_id) and bool(token)))


# ── 配置加载 ──────────────────────────────────────────────


def load_config() -> dict:
    """加载 config.toml，不存在则下载远程模板；无交互模式不打开编辑器"""
    if not os.path.exists(config_path):
        logger.info("config.toml 不存在，正在下载远程模板...")
        downloaded = False
        try:
            resp = requests.get(CONFIG_EXAMPLE_URL, timeout=30)
            resp.raise_for_status()
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
            logger.success(f"远程模板已下载到 {config_path}")
            downloaded = True
        except OSError as e:
            logger.warning(f"下载远程模板失败 ({CONFIG_EXAMPLE_URL}): {e}")

        if not downloaded:
            local_template = read_first_existing(config_example_candidates)
            if local_template is not None:
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(local_template)
                logger.success(f"已从本地模板创建 {config_path}")
                downloaded = True

        if os.path.exists(config_path):
            if NON_INTERACTIVE:
                logger.warning(
                    "已创建空配置模板，请在挂载的数据目录中填写账号信息后重试"
                )
            else:
                logger.info("正在打开配置文件，请填写账号信息后保存...")
                open_editor(config_path)
            # 重新加载
            with open(config_path, "rb") as f:
                return tomllib.load(f)
        else:
            logger.error("无法创建配置文件")
            sys.exit(1)

    with open(config_path, "rb") as f:
        return tomllib.load(f)


# ── 账号级日志过滤器 ─────────────────────────────────────────


def _make_account_filter(account_name: str):
    """返回一个 loguru filter，只放行 extra[account] == account_name 的日志记录"""

    def filter_fn(record) -> bool:
        return record["extra"].get("account") == account_name

    return filter_fn


# ── 单个账号执行 ────────────────────────────────────────────


def run_account(
    account_config: dict, global_settings: dict, ai_config: dict, account_index: int
) -> bool:
    """运行单个账号的任务

    :param account_config: [[account]] 的字典
    :param global_settings: [settings] 的字典
    :param ai_config: [ai] 的字典
    :param account_index: 账号序号
    :return: 成功返回 True，失败返回 False
    """

    def get_setting(key, default=None):
        """账号级优先，回退到全局设置"""
        val = account_config.get(key)
        if val is not None and val != "":
            return val
        return global_settings.get(key, default)

    def cli_or_env(key: str, cli_val, default=None):
        """命令行参数 > 环境变量 > 默认值"""
        if cli_val is not None:
            return cli_val
        env_val = os.environ.get(f"WB_{key.upper()}")
        if env_val is not None:
            return env_val
        return default

    # 必填字段（password 默认为 username）
    tenant_name = account_config.get("tenant_name", "").strip()
    username = account_config.get("username", "").strip()
    password = account_config.get("password", "") or username
    user_id = account_config.get("user_id", "")
    token_val = account_config.get("token", "")

    # 账号标识（用于日志文件夹名）
    account_name = username or user_id or f"account_{account_index}"

    # 合并设置（账号级优先，回退到全局；CLI/环境变量最高优先）
    study_mode = cli_or_env(
        "study_mode", _OPTS.study_mode, get_setting("study_mode", "true")
    )
    exam_mode = cli_or_env(
        "exam_mode", _OPTS.exam_mode, get_setting("exam_mode", "true")
    )
    random_answer_raw = cli_or_env(
        "random_answer", _OPTS.random_answer, get_setting("random_answer", True)
    )
    if isinstance(random_answer_raw, str):
        random_answer = random_answer_raw.strip().lower() in ("1", "true", "yes")
    else:
        random_answer = bool(random_answer_raw)
    # 无交互模式下不允许手动输入答案，强制随机作答
    if NON_INTERACTIVE and not random_answer:
        log = logger.bind(account=account_name)
        log.warning("无交互模式下强制启用随机作答（random_answer=true）")
        random_answer = True
    study_time = cli_or_env(
        "study_time", _OPTS.study_time, get_setting("study_time", "20,10")
    )
    video_speed = float(
        cli_or_env(
            "video_speed",
            _OPTS.video_speed,
            get_setting("video_speed", 1),
        )
    )
    exam_question_time = cli_or_env(
        "exam_question_time", _OPTS.exam_question_time, get_setting("exam_question_time", "3,3")
    )
    exam_submit_match_rate = int(
        cli_or_env(
            "exam_submit_match_rate",
            _OPTS.exam_submit_match_rate,
            get_setting("exam_submit_match_rate", 90),
        )
    )
    browser_path = (
        _OPTS.browser_path
        or os.environ.get("WB_BROWSER_PATH", "").strip()
        or get_setting("browser_path", "")
        or None
    )
    cdp_host = cli_or_env(
        "cdp_host", _OPTS.cdp_host, get_setting("cdp_host", "") or None
    )
    cdp_port_raw = cli_or_env(
        "cdp_port", _OPTS.cdp_port, get_setting("cdp_port", 0) or None
    )
    cdp_port = int(cdp_port_raw) if cdp_port_raw else None
    debug_raw = cli_or_env("debug", _OPTS.debug, get_setting("debug", False))
    if isinstance(debug_raw, str):
        debug = debug_raw.strip().lower() in ("1", "true", "yes")
    else:
        debug = bool(debug_raw)
    jupiter_fallback_raw = cli_or_env(
        "jupiter_fallback", _OPTS.jupiter_fallback, get_setting("jupiter_fallback", False)
    )
    jupiter_fallback = str(jupiter_fallback_raw).lower() in (
        "1",
        "true",
        "yes",
    )

    # 为该账号创建专属日志文件夹
    account_log_dir = os.path.join(logs_dir, account_name)
    os.makedirs(account_log_dir, exist_ok=True)
    account_log_path = os.path.join(
        account_log_dir, f"weban-{run_start_ts}.log"
    )

    # 添加只属于该账号的日志 sink（debug 请求/响应详情走 DEBUG 级别，需放行）
    account_filter = _make_account_filter(account_name)
    handler_id = logger.add(
        account_log_path,
        encoding="utf-8",
        level="DEBUG",
        format=log_format,
        filter=account_filter,
    )

    log = logger.bind(account=account_name)

    try:
        # ── 构建客户端 ──
        if token_val and user_id:
            # Token 登录（优先）
            user = {"userId": user_id, "token": token_val}
            log.info("使用 Token 登录")
            client = WeBanClient(
                tenant_name,
                user=user,
                log=log,
                browser_path=browser_path,
                cdp_host=cdp_host,
                cdp_port=cdp_port,
                debug=debug,
                ai_config=ai_config,
                video_speed=video_speed,
                jupiter_fallback=jupiter_fallback,
            )
        elif tenant_name and username:
            # 密码登录 — password 默认为 username
            log.info("使用密码登录")
            client = WeBanClient(
                tenant_name,
                username,
                password,
                log=log,
                browser_path=browser_path,
                cdp_host=cdp_host,
                cdp_port=cdp_port,
                debug=debug,
                ai_config=ai_config,
                video_speed=video_speed,
                jupiter_fallback=jupiter_fallback,
            )
        else:
            log.error(
                "缺少必要的配置信息: 需要填写 tenant_name 和 username，"
                "或 tenant_name + user_id + token"
            )
            return False

        if not client.login():
            log.error("登录失败")
            return False

        log.info("登录成功，模拟打开首页")
        client.simulate_home_page()

        log.info("登录成功，开始同步答案")
        with sync_lock:
            client.sync_answers()

        # ── 学习 + 考试（按项目交替：完成一个项目的课程和考试后切换下一个） ──
        client.exam_mode = exam_mode  # 进度预估需要知道考试是否计入
        client.run_project_cycle(
            study_time=study_time,
            study_mode=study_mode,
            exam_mode=exam_mode,
            random_answer=random_answer,
            exam_question_time=exam_question_time,
            exam_submit_match_rate=exam_submit_match_rate,
        )

        # ── 最终同步 ──
        log.info("最终同步答案")
        with sync_lock:
            client.sync_answers()

        log.success("执行完成")
        return True

    except PermissionError as e:
        log.error(f"权限错误: {e}")
        return False
    except RuntimeError as e:
        log.error(f"运行时错误: {e}")
        return False
    except ValueError as e:
        log.error(f"参数错误: {e}")
        return False
    except Exception as e:  # noqa: BLE001 -- 入口兜底，任何未预期异常都记录并返回失败
        log.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        logger.remove(handler_id)


# ── 入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        logger.info(f"程序启动，当前版本：{VERSION}")
        _check_update_async()  # 异步检查更新，失败/无新版不阻塞主流程
        logger.info("程序更新地址：https://github.com/hangone/WeBan")

        # 加载配置文件
        def load_all_config():
            config = load_config()
            return (
                config.get("settings", {}),
                config.get("ai", {}),
                config.get("account", []),
            )

        global_settings, ai_config, accounts = load_all_config()

        # 命令行/环境变量直传单账号（免配置文件，如 Docker 单用户）：
        # --tenant-name + --username [--password] 或 WB_TENANT_NAME + WB_USERNAME [WB_PASSWORD]
        # 或 --tenant-name + --user-id + --token（Token 登录）
        cli_tenant = (
            _OPTS.tenant_name
            or os.environ.get("WB_TENANT_NAME", "").strip()
        )
        cli_username = _OPTS.username or os.environ.get("WB_USERNAME", "").strip()
        cli_user_id = _OPTS.user_id or os.environ.get("WB_USER_ID", "").strip()
        cli_token = _OPTS.token or os.environ.get("WB_TOKEN", "").strip()
        if cli_tenant and cli_username:
            cli_password = (
                _OPTS.password or os.environ.get("WB_PASSWORD", "") or cli_username
            )
            cli_account = {
                "tenant_name": cli_tenant,
                "username": cli_username,
                "password": cli_password,
            }
        elif cli_tenant and cli_user_id and cli_token:
            cli_account = {
                "tenant_name": cli_tenant,
                "user_id": cli_user_id,
                "token": cli_token,
            }
        else:
            cli_account = None
        if cli_account is not None:
            # CLI/env 账号优先于配置文件同用户名账号（避免重复），置于最前
            accounts = [
                a
                for a in accounts
                if a.get("username") != cli_account.get("username")
                or a.get("tenant_name") != cli_tenant
            ]
            accounts.insert(0, cli_account)
            logger.info(
                f"使用命令行/环境变量指定账号：{cli_tenant}/"
                f"{cli_account.get('username') or cli_account.get('user_id')}"
            )

        # 过滤有效账号
        valid_accounts = [a for a in accounts if is_account_valid(a)]

        if not valid_accounts:
            if NON_INTERACTIVE:
                logger.error(
                    f"没有找到有效的账号配置，请检查 {config_path}"
                )
                sys.exit(1)
            logger.warning("没有找到有效的账号配置，正在打开配置文件...")
            open_editor(config_path)
            global_settings, ai_config, accounts = load_all_config()
            ai_config = merge_ai_config(_OPTS, ai_config)
            valid_accounts = [a for a in accounts if is_account_valid(a)]
            if not valid_accounts:
                logger.error("仍然没有有效的账号配置，请检查 config.toml")
                sys.exit(1)

        # 单账号时提示是否更换（无交互模式跳过，直接使用该账号）
        if len(valid_accounts) == 1 and not NON_INTERACTIVE:
            acct = valid_accounts[0]
            acct_name = (
                acct.get("username")
                or acct.get("user_id")
                or acct.get("tenant_name", "")
            )
            choice = (
                input(f"当前账号：{acct_name}，是否更换账号？(y/N，默认N): ")
                .strip()
                .lower()
            )
            if choice == "y":
                open_editor(config_path)
                global_settings, ai_config, accounts = load_all_config()
                ai_config = merge_ai_config(_OPTS, ai_config)
                valid_accounts = [a for a in accounts if is_account_valid(a)]
                if not valid_accounts:
                    logger.error("没有有效的账号配置")
                    sys.exit(1)

        accounts = valid_accounts
        logger.info(f"共加载到 {len(accounts)} 个账号")

        # 检测浏览器是否可用（优先级：CLI > 环境变量 > 配置文件 → 自动检测）
        browser_path = (
            _OPTS.browser_path
            or os.environ.get("WB_BROWSER_PATH", "").strip()
            or global_settings.get("browser_path", "")
            or None
        )
        cdp_host = (
            _OPTS.cdp_host
            or os.environ.get("WB_CDP_HOST", "").strip()
            or global_settings.get("cdp_host", "")
            or None
        )
        cdp_port_raw = (
            _OPTS.cdp_port
            if _OPTS.cdp_port is not None
            else os.environ.get("WB_CDP_PORT", "").strip()
            or global_settings.get("cdp_port", 0)
        )
        cdp_port = int(cdp_port_raw) or None

        # 用户未配置时，自动探测可用的 CDP 端口
        if not browser_path and not cdp_host and not cdp_port:
            import socket
            for host, port in [
                ("127.0.0.1", 9222), ("127.0.0.1", 9223),
                ("host.docker.internal", 9222), ("host.docker.internal", 9223),
            ]:
                try:
                    with socket.create_connection((host, port), timeout=1):
                        # 端口可达，进一步验证是否为 CDP 服务
                        resp = requests.get(f"http://{host}:{port}/json/version", timeout=3)
                        if resp.ok and "Browser" in resp.json():
                            cdp_host, cdp_port = host, port
                            logger.info(f"自动探测到 CDP 浏览器 {host}:{port}")
                            break
                except (OSError, requests.RequestException, ValueError):
                    continue

        try:
            resolved = check_browser_health(browser_path, cdp_host, cdp_port)
            logger.info(f"浏览器检测通过: {resolved}")
        except RuntimeError as e:
            logger.error(f"浏览器检测失败: {e}")
            sys.exit(1)

        # 将探测结果写回 global_settings，供 run_account 读取
        if cdp_host:
            global_settings["cdp_host"] = cdp_host
        if cdp_port:
            global_settings["cdp_port"] = cdp_port

        # 是否多线程
        max_workers_raw = _OPTS.max_workers
        if max_workers_raw is None:
            env_mw = os.environ.get("WB_MAX_WORKERS")
            if env_mw is not None:
                max_workers_raw = int(env_mw)
        if max_workers_raw is None:
            max_workers_raw = int(global_settings.get("max_workers", 5))
        max_workers = min(len(accounts), max_workers_raw)

        if len(accounts) > 1 and not NON_INTERACTIVE:
            choice = (
                input(f"检测到 {len(accounts)} 个账号，是否同时运行？(Y/n，默认Y): ")
                .strip()
                .lower()
            )
            use_multithread = choice != "n"
        else:
            # 无交互模式：多账号默认并发执行
            use_multithread = len(accounts) > 1

        if use_multithread and len(accounts) > 1:
            logger.info(f"使用多线程模式，最大并发数: {max_workers}")
            success_count = 0
            failed_count = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_account = {
                    executor.submit(run_account, cfg, global_settings, ai_config, i): (
                        cfg,
                        i,
                    )
                    for i, cfg in enumerate(accounts)
                }

                for future in as_completed(future_to_account):
                    cfg, idx = future_to_account[future]
                    try:
                        if future.result():
                            success_count += 1
                        else:
                            failed_count += 1
                    except Exception as e:  # noqa: BLE001 -- 线程结果可能抛任意异常
                        logger.error(f"[账号 {idx + 1}] 线程执行异常: {e}")
                        failed_count += 1

            logger.info(
                f"所有账号执行完成！成功: {success_count}，失败: {failed_count}"
            )
        else:
            logger.info("使用单线程模式，逐个执行")
            success_count = 0
            failed_count = 0

            for i, cfg in enumerate(accounts):
                if run_account(cfg, global_settings, ai_config, i):
                    success_count += 1
                else:
                    failed_count += 1

            logger.info(
                f"所有账号执行完成！成功: {success_count}，失败: {failed_count}"
            )

    except KeyboardInterrupt:
        print("用户终止")
    except Exception as e:  # noqa: BLE001 -- 入口兜底
        logger.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)

    if not NON_INTERACTIVE:
        try:
            input("按回车键退出")
        except EOFError:
            pass
