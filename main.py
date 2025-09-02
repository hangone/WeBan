import json
import os
import sys
import threading
import traceback
from concurrent.futures import as_completed, ThreadPoolExecutor

# 下次记得写类型注解
# from typing import Dict, Any

from loguru import logger

from client import WeBanClient

VERSION = "v3.5.17"

if getattr(sys, 'frozen', False):
    base_path: str = os.path.dirname(sys.executable)
else:
    base_path: str = os.path.dirname(os.path.abspath(__file__))
log_path: str = os.path.join(base_path, "weban.log")
config_path: str = os.path.join(base_path, "config.json")

# 日志
logger.remove()
logger = logger.bind(account="系统")
log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green>|<level>{level:<7}</level>|<blue>{extra[account]}</blue>|<cyan>{message}</cyan>"
logger.add(sink=sys.stdout, colorize=True, format=log_format)
logger.add(log_path, encoding="utf-8", format=log_format, retention="1 days")

# 同步锁，防止同时读写题库
sync_lock = threading.Lock()


def run_account(config, account_index):
    """运行单个账号的任务"""
    tenant_name = config.get("tenant_name", "").strip()
    account = config.get("account", "").strip()
    password = config.get("password", "").strip()
    user = config.get("user", {})
    study = config.get("study", True)
    study_time = config.get("study_time", 15)
    exam = config.get("exam", True)
    exam_use_time = config.get("exam_use_time", 250)

    if user.get("tenantName"):
        tenant_name = user["tenantName"]

    try:
        log = logger.bind(account=account or user.get("userId"))
        log.info(f"开始执行")

        if all([tenant_name, user.get("userId"), user.get("token")]):
            log.info(f"使用 Token 登录")
            client = WeBanClient(tenant_name, user=user, log=log)
        elif all([tenant_name, account, password]):
            log.info(f"使用密码登录")
            client = WeBanClient(tenant_name, account, password, log=log)
        else:
            log.error(f"缺少必要的配置信息, (tenant_name, account, password) or (tenant_name, userId, token)")
            return False

        if not client.login():
            log.error(f"登录失败")
            return False

        log.info(f"登录成功，开始同步答案")
        with sync_lock:
            client.sync_answers()

        if study:
            log.info(f"开始学习 (每个任务时长: {study_time}秒)")
            client.run_study(study_time)

        if exam:
            log.info(f"开始考试 (总时长: {exam_use_time}秒)")
            client.run_exam(exam_use_time)

        log.info(f"最终同步答案")
        with sync_lock:
            client.sync_answers()

        log.success(f"执行完成")
        return True

    except PermissionError as e:
        logger.error(f"权限错误: {e}")
        return False

    except RuntimeError as e:
        logger.error(f"运行时错误: {e}")
        return False
    
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        return False

    except Exception as e:
        logger.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)
        return False


def create_initial_config() -> list[dict]:
    """创建初始配置文件"""
    logger.error("config.json 文件不存在，请填写信息")
    tenant_name = input("请填写学校名称: ").strip()
    client = WeBanClient(tenant_name=tenant_name, log=logger)
    tenant_config: Dict[str, Any] = client.api.get_tenant_config()
    if tenant_config.get("code", -1) != "0":
        logger.error(f"未找到学校 {tenant_name} 的配置，请检查学校名称是否正确")
        exit(1)
    prompt = tenant_config["data"]
    logger.info(prompt.get("popPrompt", ""))
    account = input(f"账号请输入{prompt.get('userNamePrompt', '')}：").strip()
    password = input(f"密码请输入{prompt.get('passwordPrompt', '')}：").strip()

    configs = [{"tenant_name": tenant_name, "account": account, "password": password, "study": True, "user": {"userId": "", "token": ""}, "study_time": 15, "exam": True, "exam_use_time": 250}]

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(configs, indent=2, ensure_ascii=False))

    return configs


if __name__ == "__main__":
    try:
        logger.info(f"程序启动，当前版本：{VERSION}")
        logger.info(f"程序更新地址：https://github.com/hangone/WeBan")

        # 加载配置文件
        try:
            configs = json.load(open(config_path, encoding="utf-8"))
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
    except KeyboardInterrupt:
        print("用户终止")
    except Exception as e:
        logger.error(f"运行失败: {e}")
        traceback.print_exc(file=sys.stderr)

    try:
        input("按回车键退出")
    except:
        pass
