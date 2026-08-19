## v3.10.0 更新日志

**低配服务器兼容**（本次重点）
- 兼容无 AVX2 的廉价云服务器（QEMU 虚拟 CPU，如 1H1G 小机）：numpy 降至 <2，opencv 锁定 4.10，全平台可直接运行，不再启动即崩

**使用体验**
- 首次运行改为**交互式输入学校、学号、密码**，登录成功才自动保存配置；输错会提示重试且不会写坏配置文件
- 一条命令直接运行：`--tenant-name/--username/--password`（或 `WB_TENANT_NAME/WB_USERNAME/WB_PASSWORD` 环境变量），无需配置文件
- README 重写为 0 基础快速开始：按系统下载二进制（附镜像加速）→ 运行 → 填账号三步上手

**无交互/容器运行**
- 无交互模式下所有输入用默认值、不等待手动输入，适合 Docker/cron/后台
- `WB_DATA_DIR` 数据目录（config/logs/answer 全部持久化，适合 Docker 挂载）

**其他**
- 依赖与锁文件全面对齐（requirements.txt / uv.lock 同步当前版本）
- 多账号并发、AI 搜题、验证码自动识别、课程学习与考试等原有功能保持不变

> 完整参数对照表见 README「参数总览」。