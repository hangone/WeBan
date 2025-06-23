import json
import sys
import threading
import traceback
from concurrent.futures import as_completed, ThreadPoolExecutor

from loguru import logger

from client import WeBanClient

# 日志
logger.remove()
logger = logger.bind(account="系统")
log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>|<level>{level:<7}</level>|<blue>{extra[account]}</blue>|<cyan>{message}</cyan>"
logger.add(sink=sys.stdout, colorize=True, format=log_format)
logger.add("weban.log", encoding="utf-8", format=log_format, retention="1 days")

# 同步锁，防止同时读写题库
sync_lock = threading.Lock()


def run_account(config, account_index):
    """运行单个账号的任务"""
    tenant_name = config.get("tenant_name")
    account = config.get("account")
    password = config.get("password")
    study = config.get("study", True)
    study_time = config.get("study_time", 15)
    exam = config.get("exam", False)
    exam_use_time = config.get("exam_use_time", 600)

    if not all([account, password, tenant_name]):
        logger.error(f"[账号{account_index+1}] config.json 文件中缺少必要的配置信息 (tenant_name, account, password): {config}")
        return False

    try:
        log = logger.bind(account=account)
        log.info(f"开始执行")

        client = WeBanClient(account, password, tenant_name, log)
        if not client.login():
            log.error(f"登录失败")
            return False

        log.info(f"登录成功，开始同步答案")
        with sync_lock:
            client.sync_answers()

        if study:
            log.info(f"开始学习 (时长: {study_time}分钟)")
            client.run_study(study_time)

        if exam:
            log.info(f"开始考试 (时长: {exam_use_time}秒)")
            client.run_exam(exam_use_time)

        log.info(f"最终同步答案")
        with sync_lock:
            client.sync_answers()

        log.success(f"执行完成")
        return True

    except Exception as e:
        logger.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)
        return False


def create_initial_config():
    """创建初始配置文件"""
    logger.error("config.json 文件不存在，请填写信息")
    tenant_name = input("请填写学校名称: ").strip()
    client = WeBanClient("", "", tenant_name)
    tenant_code = client.get_tenant_code()
    if not tenant_code:
        exit(1)
    prompt = client.api.get_tenant_config(tenant_code).get("data", {})
    logger.info(prompt.get("popPrompt", ""))
    account = input(f"账号{prompt.get('userNamePrompt', '') or '请填写用户名'}：").strip()
    password = input(f"密码{prompt.get('passwordPrompt', '') or '请填写密码'}：").strip()

    configs = [{"tenant_name": tenant_name, "account": account, "password": password, "study": True, "study_time": 15, "exam": True, "exam_use_time": 600}]

    with open("config.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(configs, indent=2, ensure_ascii=False))

    return configs


if __name__ == "__main__":
    logger.info("程序启动")

    # 加载配置文件
    try:
        configs = json.load(open("config.json", encoding="utf-8"))
    except FileNotFoundError:
        configs = create_initial_config()
    except json.JSONDecodeError:
        logger.error("config.json 文件格式错误，请检查")
        exit(1)

    if not configs:
        logger.error("没有找到有效的账号配置")
        exit(1)

    logger.info(f"共加载到 {len(configs)} 个账号")

    # 询问是否使用多线程
    use_multithread = True
    if len(configs) > 1:
        choice = input(f"检测到 {len(configs)} 个账号，是否同时运行？(Y/n，默认Y): ").strip().lower()
        use_multithread = choice != "n"

    if use_multithread and len(configs) > 1:
        # 多线程执行
        max_workers = min(len(configs), 5)  # 限制最大线程数为5
        logger.info(f"使用多线程模式，最大并发数: {max_workers}")

        success_count = 0
        failed_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            future_to_account = {executor.submit(run_account, config, i): (config, i) for i, config in enumerate(configs)}

            # 等待所有任务完成
            for future in as_completed(future_to_account):
                config, account_index = future_to_account[future]
                try:
                    success = future.result()
                    if success:
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.error(f"[账号 {account_index+1}] 线程执行异常: {e}")
                    failed_count += 1

        logger.info(f"所有账号执行完成！成功: {success_count}，失败: {failed_count}")

    else:
        # 单线程执行（原有逻辑）
        logger.info("使用单线程模式，逐个执行")
        success_count = 0
        failed_count = 0

        for i, config in enumerate(configs):
            success = run_account(config, i)
            if success:
                success_count += 1
            else:
                failed_count += 1

        logger.info(f"所有账号执行完成！成功: {success_count}，失败: {failed_count}")

    input("按回车键退出")
