import json
import sys

from loguru import logger

from client import WeBanClient

if __name__ == "__main__":
    try:
        with open("config.json", encoding="utf-8") as f:
            configs = json.load(f)
    except FileNotFoundError:
        logger.error("config.json 文件不存在，自动创建")
        with open("config.json", "w", encoding="utf-8") as f:
            data = [{"tenant_name": "学校名称", "account": "用户名 1", "password": "密码 1", "study": True, "exam": False, "exam_use_time": 2000}]
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
        exam = config.get("exam", False)
        exam_use_time = config.get("exam_use_time", 2000)

        if not all([account, password, tenant_name]):
            logger.error(f"config.json 文件中缺少必要的配置信息 (tenant_name, account, password): {config}")
            continue

        try:
            client = WeBanClient(account, password, tenant_name)
            client.run()
        except Exception as e:
            logger.error(f"[-][{account}] 运行失败: {e}")
            continue
