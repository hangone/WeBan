## v3.11.2 更新日志

- 新增油猴脚本 `weban.user.js`：浏览器内完成学习与考试，题库三链路查询，设置面板，52 门课程实测的通用状态机。
- 新增 `server.py`（原 `captcha_server.py` 升级）：题库查询 `/answer/query`、`/answer/match` 与点选验证码 `/captcha/point`。
- 浏览器启动：调试端口就绪后连接失败重试一次，清理 nodriver 半成品实例；CDP 模式无 Chrome 时不再在连接前抛 `FileNotFoundError`。
- 单门课程完课失败不再中止整个账号，继续后续课程。
- `study_time` 支持 `"基础,随机上限"`（默认 `"20,10"`），旧写法 `30` 仍可用。
- 题库新增 27 条并同步考试选项；urllib3 升至 2.8.0，补齐 pysocks。
