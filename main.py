import json
import sys

from loguru import logger

from client import WeBanClient

if __name__ == "__main__":
    logger.add("weban.log", encoding="utf-8", retention="1 days")
    logger.info("开始执行")

    try:
        with open("config.json", encoding="utf-8") as f:
            configs = json.load(f)
    except FileNotFoundError:
        logger.error("config.json 文件不存在，自动创建")
        with open("config.json", "w", encoding="utf-8") as f:
            data = [{"tenant_name": "学校名称", "account": "用户名", "password": "密码", "study": True, "study_time": 15, "exam": False, "exam_use_time": 600}]
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error("config.json 文件格式错误，请检查")
        sys.exit(1)

    logger.info(f"共加载到 {len(configs)} 个账号")
    for config in configs:
        tenant_name = config.get("tenant_name")
        account = config.get("account")
        password = config.get("password")
        study = config.get("study", True)
        study_time = config.get("study_time", 15)
        exam = config.get("exam", False)
        exam_use_time = config.get("exam_use_time", 600)

        if not all([account, password, tenant_name]):
            logger.error(f"config.json 文件中缺少必要的配置信息 (tenant_name, account, password): {config}")
            continue

        try:
            client = WeBanClient(account, password, tenant_name)
            if not client.login():
                logger.error(f"[{account}] 登录失败")
                continue

            logger.info(f"[{account}] 同步答案")
            client.sync_answers()

            if study:
                logger.info(f"[{account}] 开始学习")
                client.run_study(study_time)

            if exam:
                logger.info(f"[{account}] 开始考试")
                client.run_exam(exam_use_time)

            logger.info(f"[{account}] 同步答案")
            client.sync_answers()

        except Exception as e:
            logger.error(f"[{account}] 运行失败: {e}")
            continue
