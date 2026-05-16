import os
import subprocess
import sys
import threading
import tomllib
import traceback
from concurrent.futures import as_completed, ThreadPoolExecutor

import requests
from loguru import logger

from client import WeBanClient

VERSION = "v3.6.0"

if getattr(sys, "frozen", False):
    # pyfuze: sys.executable 指向解压后的 Python，需用 argv[0] 定位原始可执行文件
    base_path = os.path.dirname(os.path.abspath(sys.argv[0]))
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

config_path = os.path.join(base_path, "config.toml")
config_example_path = os.path.join(base_path, "config.example.toml")
logs_dir = os.path.join(base_path, "logs")

# 远程模板下载地址
CONFIG_EXAMPLE_URL = (
    "https://github.com/hangone/WeBan/raw/refs/heads/main/"
    "config.example.toml"
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
logger.add(sink=sys.stdout, colorize=True, format=log_format)

os.makedirs(logs_dir, exist_ok=True)
logger.add(
    os.path.join(logs_dir, "weban.log"),
    encoding="utf-8", format=log_format,
    retention="7 days",
)

# 同步锁，防止同时读写题库
sync_lock = threading.Lock()


# ── 工具函数 ──────────────────────────────────────────────

def open_editor(path: str):
    """打开系统编辑器编辑指定文件"""
    editor = os.environ.get("EDITOR")
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad", path])
        elif sys.platform == "darwin":
            if editor:
                subprocess.Popen([editor, path])
            else:
                subprocess.Popen(["open", "-t", path])
        else:
            if editor:
                subprocess.Popen([editor, path])
            else:
                subprocess.Popen(["xdg-open", path])
    except FileNotFoundError:
        logger.warning(f"无法打开编辑器，请手动编辑文件: {path}")
        return
    try:
        input("编辑完成后按回车键继续...")
    except Exception:
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
    """加载 config.toml，不存在则下载远程模板并打开编辑器"""
    if not os.path.exists(config_path):
        logger.info("config.toml 不存在，正在下载远程模板...")
        downloaded = False
        try:
            resp = requests.get(CONFIG_EXAMPLE_URL, timeout=30)
            resp.raise_for_status()
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
            logger.success(f"远程模板已下载到 {config_path}")
            downloaded = True
        except Exception as e:
            logger.error(f"下载远程模板失败: {e}")

        if not downloaded and os.path.exists(config_example_path):
            import shutil
            shutil.copy(config_example_path, config_path)
            logger.success(f"已从本地模板创建 {config_path}")

        if os.path.exists(config_path):
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
    def filter_fn(record: dict) -> bool:
        return record["extra"].get("account") == account_name
    return filter_fn


# ── 单个账号执行 ────────────────────────────────────────────

def run_account(account_config: dict, global_settings: dict, account_index: int) -> bool:
    """运行单个账号的任务

    :param account_config: [[account]] 的字典
    :param global_settings: [settings] 的字典
    :param account_index: 账号序号
    :return: 成功返回 True，失败返回 False
    """

    def get_setting(key, default=None):
        """账号级优先，回退到全局设置"""
        val = account_config.get(key)
        if val is not None and val != "":
            return val
        return global_settings.get(key, default)

    # 必填字段（password 默认为 username）
    tenant_name = account_config.get("tenant_name", "").strip()
    username = account_config.get("username", "").strip()
    password = account_config.get("password", "") or username
    user_id = account_config.get("user_id", "")
    token_val = account_config.get("token", "")

    # 账号标识（用于日志文件夹名）
    account_name = username or user_id or f"account_{account_index}"

    # 合并设置（账号级优先，回退到全局）
    study_mode = get_setting("study_mode", "true")
    exam_mode = get_setting("exam_mode", "true")
    random_answer = get_setting("random_answer", True)
    study_time = int(get_setting("study_time", 20))
    exam_question_time = get_setting("exam_question_time", "3,3")
    exam_submit_match_rate = int(get_setting("exam_submit_match_rate", 90))
    browser_path = get_setting("browser_path", "") or None
    debug = get_setting("debug", False)

    # 为该账号创建专属日志文件夹
    account_log_dir = os.path.join(logs_dir, account_name)
    os.makedirs(account_log_dir, exist_ok=True)
    account_log_path = os.path.join(account_log_dir, "weban.log")

    # 添加只属于该账号的日志 sink
    account_filter = _make_account_filter(account_name)
    handler_id = logger.add(
        account_log_path, encoding="utf-8", format=log_format,
        retention="7 days", filter=account_filter,
    )

    log = logger.bind(account=account_name)

    try:
        # ── 构建客户端 ──
        if token_val and user_id:
            # Token 登录（优先）
            user = {"userId": user_id, "token": token_val}
            log.info("使用 Token 登录")
            client = WeBanClient(tenant_name, user=user, log=log, browser_path=browser_path, debug=debug)
        elif tenant_name and username:
            # 密码登录 — password 默认为 username
            log.info("使用密码登录")
            client = WeBanClient(
                tenant_name, username, password, log=log, browser_path=browser_path, debug=debug,
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

        log.info("登录成功，开始同步答案")
        with sync_lock:
            client.sync_answers()

        # ── 学习 ──
        study = study_mode != "false"

        if study:
            mode_desc = {"true": "正常", "force": "强制重新学习"}.get(study_mode, study_mode)
            log.info(f"开始学习 (模式: {mode_desc}, 每个任务时长: {study_time}秒)")
            client.run_study(study_time, study_mode)
        else:
            log.info("学习模式已关闭，跳过所有学习任务")

        # ── 考试 ──
        exam = exam_mode != "false"
        if exam:
            mode_desc = {
                "true": "正常",
                "perfect": "追求满分",
                "force": "强制重考",
            }.get(exam_mode, exam_mode)
            log.info(f"开始考试 (模式: {mode_desc})")
            client.run_exam(
                exam_mode=exam_mode,
                random_answer=random_answer,
                exam_question_time=exam_question_time,
                exam_submit_match_rate=exam_submit_match_rate,
            )
        else:
            log.info("考试模式已关闭，跳过所有考试任务")

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
    except Exception as e:
        log.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        logger.remove(handler_id)


# ── 入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        logger.info(f"程序启动，当前版本：{VERSION}")
        logger.info("程序更新地址：https://github.com/hangone/WeBan")

        # 加载配置文件
        config = load_config()
        global_settings = config.get("settings", {})
        accounts = config.get("account", [])

        # 过滤有效账号
        valid_accounts = [a for a in accounts if is_account_valid(a)]

        if not valid_accounts:
            logger.warning("没有找到有效的账号配置，正在打开配置文件...")
            open_editor(config_path)
            # 重新加载并检查
            config = load_config()
            global_settings = config.get("settings", {})
            accounts = config.get("account", [])
            valid_accounts = [a for a in accounts if is_account_valid(a)]
            if not valid_accounts:
                logger.error("仍然没有有效的账号配置，请检查 config.toml")
                sys.exit(1)

        # 单账号时提示是否更换
        if len(valid_accounts) == 1:
            acct = valid_accounts[0]
            acct_name = acct.get("username") or acct.get("user_id") or acct.get("tenant_name", "")
            choice = input(f"当前账号：{acct_name}，是否更换账号？(y/N，默认N): ").strip().lower()
            if choice == "y":
                open_editor(config_path)
                config = load_config()
                global_settings = config.get("settings", {})
                accounts = config.get("account", [])
                valid_accounts = [a for a in accounts if is_account_valid(a)]
                if not valid_accounts:
                    logger.error("没有有效的账号配置")
                    sys.exit(1)

        accounts = valid_accounts
        logger.info(f"共加载到 {len(accounts)} 个账号")

        # 是否多线程
        max_workers = min(len(accounts), int(global_settings.get("max_workers", 5)))

        if len(accounts) > 1:
            choice = input(
                f"检测到 {len(accounts)} 个账号，是否同时运行？(Y/n，默认Y): "
            ).strip().lower()
            use_multithread = choice != "n"
        else:
            use_multithread = False

        if use_multithread and len(accounts) > 1:
            logger.info(f"使用多线程模式，最大并发数: {max_workers}")
            success_count = 0
            failed_count = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_account = {
                    executor.submit(run_account, cfg, global_settings, i): (cfg, i)
                    for i, cfg in enumerate(accounts)
                }

                for future in as_completed(future_to_account):
                    cfg, idx = future_to_account[future]
                    try:
                        if future.result():
                            success_count += 1
                        else:
                            failed_count += 1
                    except Exception as e:
                        logger.error(f"[账号 {idx + 1}] 线程执行异常: {e}")
                        failed_count += 1

            logger.info(f"所有账号执行完成！成功: {success_count}，失败: {failed_count}")
        else:
            logger.info("使用单线程模式，逐个执行")
            success_count = 0
            failed_count = 0

            for i, cfg in enumerate(accounts):
                if run_account(cfg, global_settings, i):
                    success_count += 1
                else:
                    failed_count += 1

            logger.info(f"所有账号执行完成！成功: {success_count}，失败: {failed_count}")

    except KeyboardInterrupt:
        print("用户终止")
    except Exception as e:
        logger.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)

    try:
        input("按回车键退出")
    except Exception:
        pass
