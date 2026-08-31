# _WeBan_ 安全微课 安全微伴 大学安全教育

## 介绍

如果本项目帮到了你，可以在右上角点亮 Star，谢谢你！

实现了课程学习和根据题库自动考试，支持多用户多线程运行，自动验证码识别等。

运行前后会自动合并题库。为避免意外耗尽考试次数，每次运行对每个考试计划最多新开一张试卷；需要再次尝试时，请确认剩余次数后重新运行。可将 `answer/answer.json` 文件提交 PR 一起完善题库。

## 功能特性

- **课程学习**：自动遍历项目 → 分类 → 课程，模拟翻页、答题、等待学习时长后完课；按项目交替完成课程与考试
- **自动考试**：基于题库自动答题，支持单选/多选，未匹配题目可随机作答或手动输入
- **验证码识别**：课程点选验证码自动识别（OpenCV，2 轮 × 3 次）；可选 ONNX 模型用于登录字符验证码，缺失时明确降级
- **多账号并发**：支持配置多个账号，可多线程同时执行
- **题库同步**：考试前后自动从服务器同步题库，支持多用户共享
- **单轮考试**：`perfect` / `force` 只影响本轮是否参加考试，不会在一次运行中连续重考
- **进度监控**：完课后自动检查进度是否更新，未更新则警告提示
- **调试模式**：开启 `debug` 可查看额外请求/响应信息；日志可能含个人数据，分享前必须复查脱敏
- **无交互运行**：Docker / cron / 后台环境自动无交互，数据目录持久化
- **低配兼容**：numpy 1.26 + OpenCV 4.10 锁定，兼容无 AVX2 的 QEMU 虚拟 CPU（便宜 1H1G 云服务器可跑）

## 使用

> **零基础三步上手**：① 下载二进制文件 → ② 双击/命令行运行 → ③ 输入学校、学号、密码。不需要安装 Python，不需要写代码。

### ⭐ 快速开始（推荐：下载即用）

**第 1 步：下载你的系统对应的文件**

点这里打开最新版下载页 → [**Releases**](https://github.com/hangone/WeBan/releases/latest)，按自己的电脑系统下载（不确定系统就按下面的表选）：

| 你的电脑                             | 点击下载（GitHub）                                                                                       | 下载太慢用镜像                                                                                               |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Windows（绝大多数电脑）              | [WeBan-windows-x64.exe](https://github.com/hangone/WeBan/releases/latest/download/WeBan-windows-x64.exe) | [镜像](https://gh-proxy.com/https://github.com/hangone/WeBan/releases/latest/download/WeBan-windows-x64.exe) |
| Mac 苹果电脑（Intel 芯片）           | [WeBan-macos-x64](https://github.com/hangone/WeBan/releases/latest/download/WeBan-macos-x64)             | [镜像](https://gh-proxy.com/https://github.com/hangone/WeBan/releases/latest/download/WeBan-macos-x64)       |
| Mac 苹果电脑（M1/M2/M3/M4 芯片）     | [WeBan-macos-arm64](https://github.com/hangone/WeBan/releases/latest/download/WeBan-macos-arm64)         | [镜像](https://gh-proxy.com/https://github.com/hangone/WeBan/releases/latest/download/WeBan-macos-arm64)     |
| Linux（Ubuntu/Debian/CentOS，64 位） | [WeBan-linux-x64](https://github.com/hangone/WeBan/releases/latest/download/WeBan-linux-x64)             | [镜像](https://gh-proxy.com/https://github.com/hangone/WeBan/releases/latest/download/WeBan-linux-x64)       |
| Linux（树莓派/ARM 服务器）           | [WeBan-linux-arm64](https://github.com/hangone/WeBan/releases/latest/download/WeBan-linux-arm64)         | [镜像](https://gh-proxy.com/https://github.com/hangone/WeBan/releases/latest/download/WeBan-linux-arm64)     |

> **Mac 怎么判断芯片**：屏幕左上角 → 关于本机，看"芯片"一栏写的是 Apple M 系列（arm64）还是 Intel（x64）。Windows 不确定就选 x64（2010 年后几乎都是）。

**第 2 步：运行**

- **Windows**：双击 `WeBan-windows-x64.exe`（第一次运行如被 SmartScreen 拦截，点"更多信息" → "仍要运行"；杀毒软件误报请添加信任）
- **Mac**：在文件目录打开终端运行：
  ```bash
  chmod +x WeBan-macos-*
  xattr -cr WeBan-macos-*
  ./WeBan-macos-arm64   # Intel 芯片换成 WeBan-macos-x64
  # 然后可能会卡一会，是苹果在验证签名，等就行
  ```
- **Linux**：
  ```bash
  chmod +x WeBan-linux-*
  ./WeBan-linux-x64     # ARM 服务器换成 WeBan-linux-arm64
  ```

**第 3 步：填账号，开始**

第一次运行（或还没有配置文件时），程序会**直接让你输入学校、用户名、密码**（不用编辑任何文件）：

```
请输入账号信息：
  学校全称（如：XX大学）: XX大学
  用户名（学号/考生号之类的）: <你的账号>
  密码（默认同用户名）: <你的密码>
```

输入后程序会**自动验证账号**：登录成功就会把账号自动保存到配置文件 `config.toml`，然后开始学习和考试；**如果学校全称或用户名密码错了，会提示你重新输入，不会写坏配置文件**。之后每次运行都会接着上次的进度继续。

> 配置文件 `config.toml` 在程序旁边（Windows 是 exe 所在文件夹，Mac/Linux 是运行命令的目录），下次运行前也可以手动改它。用 `--data-dir` 可以指定固定位置（见下方参数表）。
>
> `config.toml` 中的密码、Token 和 AI API Key 均为明文敏感信息。请限制文件权限，不要提交到版本库、上传网盘或随日志分享。

**不想交互输入？一条命令直接跑**（学校/学号/密码写在命令里，无需配置文件）：

```bash
# Windows (PowerShell)
$env:WB_TENANT_NAME="你的学校全称"; $env:WB_USERNAME="你的学号"; $env:WB_PASSWORD="你的密码"; .\WeBan-windows-x64.exe

# mac / Linux
WB_TENANT_NAME="你的学校全称" WB_USERNAME=你的学号 WB_PASSWORD=你的密码 ./WeBan-macos-arm64
```

> 命令行参数可能进入 Shell 历史，环境变量也可能被同机进程或运维平台读取。短期任务可用环境变量，长期使用请保护好配置文件和运行环境。
>
> 全部参数对照表见下方"参数总览"；一个文件管理多个账号时可用配置文件方式。

### 参数总览

**每个配置项都有命令行参数和环境变量两种方式，三类名称一一对应**：配置文件键名（`snake_case`）= 命令行参数（`--kebab-case`）= 环境变量（`WB_SNAKE_CASE`），例如 `study_time` ↔ `--study-time` ↔ `WB_STUDY_TIME`。优先级均为 **命令行 > 环境变量 > 配置文件**：

| 配置文件键               | 参数                         | 环境变量                    | 说明                                                                               |
| ------------------------ | ---------------------------- | --------------------------- | ---------------------------------------------------------------------------------- |
| —                        | `--config PATH`              | `WB_CONFIG`                 | 配置文件路径（默认: 程序目录/config.toml）                                         |
| —                        | `--data-dir PATH`            | `WB_DATA_DIR`               | 数据目录（config/logs/answer 都在此，适合挂载）                                    |
| —                        | `--non-interactive`          | —                           | 无交互模式（环境变量用 `ENVIRONMENT=docker`/`container` 或 stdin 非 TTY 自动判定） |
| `study_mode`             | `--study-mode`               | `WB_STUDY_MODE`             | 学习模式；`force` 仅在本轮重学一次，不无限循环                                    |
| `exam_mode`              | `--exam-mode`                | `WB_EXAM_MODE`              | 考试模式；`perfect`/`force` 每计划每轮最多新开一张试卷                             |
| `random_answer`          | `--random-answer`            | `WB_RANDOM_ANSWER`          | 题库外题目是否随机作答（`true`/`false`）                                           |
| `study_time`             | `--study-time SEC`           | `WB_STUDY_TIME`             | 每门课学习时长 `"基础,随机上限"`（秒），如 `"20,5"`                                |
| `video_speed`            | `--video-speed N`            | `WB_VIDEO_SPEED`            | 等待时间倍速：`0`=忽略视频时长、`1`=原时长、`2`=等待一半时长（2 倍速）              |
| `exam_question_time`     | `--exam-question-time SEC`   | `WB_EXAM_QUESTION_TIME`     | 每道考试题答题等待时长 `"基础,随机上限"`（秒）                                     |
| `exam_submit_match_rate` | `--exam-submit-match-rate N` | `WB_EXAM_SUBMIT_MATCH_RATE` | 允许交卷的最低题库匹配率（百分比）                                                 |
| `browser_path`           | `--browser-path PATH`        | `WB_BROWSER_PATH`           | 浏览器可执行文件路径                                                               |
| `cdp_host`               | `--cdp-host HOST`            | `WB_CDP_HOST`               | CDP 浏览器地址                                                                     |
| `cdp_port`               | `--cdp-port PORT`            | `WB_CDP_PORT`               | CDP 浏览器端口                                                                     |
| `jupiter_fallback`       | `--jupiter-fallback`         | `WB_JUPITER_FALLBACK`       | 对未加载 apicenext.js 的课程是否补发 jupiter 翻页轨迹                              |
| `max_workers`            | `--max-workers N`            | `WB_MAX_WORKERS`            | 多账号最大并发数                                                                   |
| `debug`                  | `--debug`                    | `WB_DEBUG`                  | 启用额外调试日志（可能含个人数据）                                                 |
| `tenant_name`            | `--tenant-name NAME`         | `WB_TENANT_NAME`            | 单账号学校全称（免配置文件）                                                       |
| `username`               | `--username USER`            | `WB_USERNAME`               | 单账号用户名                                                                       |
| `password`               | `--password PASS`            | `WB_PASSWORD`               | 单账号密码（默认同用户名）                                                         |
| `user_id`                | `--user-id ID`               | `WB_USER_ID`                | 单账号用户 ID（Token 登录）                                                        |
| `token`                  | `--token TOKEN`              | `WB_TOKEN`                  | 单账号登录 Token（配合 `--tenant-name --user-id`）                                 |
| `[ai].enable`            | `--ai-enable`                | `WB_AI_ENABLE`              | 是否启用 AI 搜题（默认关闭；启用会向第三方发送题干和选项）                         |
| `[ai].base_url`          | `--ai-base-url URL`          | `WB_AI_BASE_URL`            | AI 服务 API 基础路径                                                               |
| `[ai].api_key`           | `--ai-api-key KEY`           | `WB_AI_API_KEY`             | AI 服务 API Key                                                                    |
| `[ai].model`             | `--ai-model NAME`            | `WB_AI_MODEL`               | AI 模型名称                                                                        |
| `[ai].timeout`           | `--ai-timeout SEC`           | `WB_AI_TIMEOUT`             | AI 请求超时秒数                                                                    |
| `[ai].max_retries`       | `--ai-max-retries N`         | `WB_AI_MAX_RETRIES`         | AI 请求失败最大重试次数                                                            |

无交互自动判定：`ENVIRONMENT=docker`（或 container）、stdin 非 TTY（cron/后台/管道）、或显式 `--non-interactive`。

模式均按**单轮**执行：`study_mode=force` 会在本轮重新学习已完成课程一次；`exam_mode=perfect` 会以满分为目标、`force` 会忽略已及格状态，但两者都不会在同一次运行中自动连续开卷。需要再次考试时必须重新运行，以便人工确认剩余机会。

`video_speed` 只用于计算等待时间，不会操控网页播放器。大于 `0` 时，视频课程等待目标为 `max(study_time, 视频时长 / video_speed)`；设为 `0` 时忽略视频时长，只遵守 `study_time`。

#### 数据与隐私

- 启用 AI 搜题后，题干和选项会发送到 `base_url` 指向的服务商。使用前请确认数据允许外传，并接受对方的隐私与留存政策。
- `password`、`token`、`api_key` 都属于凭据。不要写入镜像、公开仓库或截图；配置文件应仅允许运行账号读取。
- 程序会尽量脱敏常见凭据，但 `debug` 日志和验证码调试文件仍可能含账号、课程、响应正文等个人信息。分享前二次打码，用完及时删除。
- CDP 端点等同于浏览器完全控制权。仅使用无个人会话的专用浏览器配置，并通过回环地址或防火墙限制访问，切勿暴露到公网。

**完全不写 config.toml 也能运行**（单账号 + 全部设置走 CLI/env）：

```bash
# 环境变量
WB_TENANT_NAME="你的学校全称" WB_USERNAME=你的学号 WB_PASSWORD=你的密码 \
WB_STUDY_TIME="20,5" WB_VIDEO_SPEED=0 ./WeBan-macos-arm64
# 或等价的命令行参数
./WeBan-macos-arm64 --tenant-name "你的学校全称" --username 你的学号 \
  --study-time "20,5" --video-speed 0
```

### 源码运行

不需要代码基础的用户**跳过本节**（直接下载二进制即可）。开发者/想改代码时用：

1. 安装 Python 3.12（项目要求 `>=3.12,<3.13`）、[uv](https://github.com/astral-sh/uv) 和 Git

2. 克隆本仓库

```bash
git clone --depth 1 https://github.com/hangone/WeBan
```

3. 安装依赖

```bash
uv sync --frozen
```

4. 运行

```bash
uv run python main.py
```

运行 `uv run python main.py --help` 可查看全部参数。

### Docker

提供两种镜像变体（多架构 amd64/arm64，仅版本 Tag 发布正式镜像标签）：

| 镜像       | Tag                                            | 说明                           |
| ---------- | ---------------------------------------------- | ------------------------------ |
| 内置浏览器 | `latest` / `with-browser` / `<版本号>`         | 内置 headless Chrome，开箱即用 |
| 轻量镜像   | `without-browser` / `<版本号>-without-browser` | 通过 CDP 连接宿主机浏览器      |

容器默认无交互运行（`ENVIRONMENT=docker` 自动判定），数据全部持久化在 `/app/data`：

```bash
mkdir -p data
docker run --rm \
  -v "$PWD/data":/app/data \
  --cpus 1 \
  hangyi/weban:latest
```

- 建议 `--cpus 1`（详见下方"CPU 配额与验证码"）；首次运行会在 `./data/` 生成 `config.toml` 模板，填写账号后重新运行即可
- 日志在 `./data/logs/<账号>/`，题库在 `./data/answer/`，全部挂载持久化
- 官方镜像固定设置 `ENVIRONMENT=docker`：不弹编辑器、不等待终端输入、不打开可见浏览器，验证码无法自动处理时会跳过或失败，末尾不等待回车
- `docker run -it` **不会**解除上述限制。需要手动答题、输入验证码或操作可见浏览器时，请在宿主机直接运行源码或原生可执行文件
- 不要把 `random_answer=false` 视为 Docker 下的人工确认通道；如不接受无交互环境中的自动降级，请将 `exam_mode=false`
- 内置浏览器的 CDP 只监听容器内 `127.0.0.1:9222`，无需也不应发布 `9222` 端口

所有配置项均可覆盖（命令行参数 > 环境变量 > 配置文件，名称一一对应，见上方参数表）。示例：

```bash
# 环境变量（单账号免配置文件）
docker run --rm -v "$PWD/data":/app/data --cpus 1 \
  -e WB_TENANT_NAME="你的学校全称" -e WB_USERNAME=你的学号 -e WB_PASSWORD=你的密码 \
  -e WB_STUDY_TIME="20,5" -e WB_VIDEO_SPEED=0 \
  hangyi/weban:latest

# 命令行参数（经 entrypoint 透传）
docker run --rm -v "$PWD/data":/app/data --cpus 1 \
  hangyi/weban:latest --tenant-name "你的学校全称" --username 你的学号 \
  --study-time "20,5" --video-speed 0
```

> 上述 `-e WB_PASSWORD=...` 适合临时演示，生产环境请使用受控的秘密注入机制，避免凭据出现在命令历史、部署清单或平台日志中。

#### CPU 配额与验证码

- **docker 下多核正常**：实测（docker 29.x，2 核 1.9GB）`--cpus 1` / `--cpus 2` × 单进程/多进程全部跑通，真实课程点选验证码在 `--cpus 2` 下完整通过（识别 → 点击 → 提交 → 腾讯 SDK 回调成功），无挂起
- 建议 `--cpus 1`：镜像默认单进程 + 单线程识别（`WB_SINGLE_PROCESS` / `WB_CV_THREADS`），1 核即可跑通全部验证码；多核配额没有性能收益（Chrome 单进程受单核限制），1.9GB 小内存机器用 2 核反而容易内存吃紧
- **podman 已知特例**：podman（如 `podman run --cpus 2`）下 headless-shell 点选验证码**提交后可能挂起**（CDP evaluate 无响应 60s+，1 核正常）——这是 podman 的 CPU 配额调度问题，非程序缺陷；podman 部署请用 `--cpus 1`

#### 轻量镜像（CDP 连接宿主机浏览器）

容器会自动检测 Docker 环境并尝试连接宿主机的 Chrome，无需手动配置 CDP。

**第一步：在宿主机启动 Chrome 远程调试**

打开 Chrome，地址栏输入 `chrome://inspect/#remote-debugging`，勾选 **Allow remote debugging for this browser instance**。请使用不含个人账号、Cookie 或敏感标签页的专用浏览器配置。

或者直接命令行启动带远程调试的 Chrome：

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222

# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

> 参考：[Chrome DevTools: Debug your browser session](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)

**第二步：运行容器**

```bash
mkdir -p data
docker run --rm \
  -v "$PWD/data":/app/data \
  hangyi/weban:without-browser
```

如需自定义 CDP 地址，可用 `--cdp-host` / `--cdp-port` 参数或配置文件 `cdp_host` / `cdp_port`。

> CDP 没有面向公网使用的安全边界，任何可访问该端口的程序都可能读取页面、Cookie 并执行脚本。不要使用 `-p 9222:9222` 暴露内置 CDP；外部 CDP 也必须限制在可信主机和网络内。

### 浏览器检测

程序按以下优先级自动检测可用的浏览器，无需手动配置：

1. **用户指定**：配置文件 `browser_path`（或 `--browser-path` / `WB_BROWSER_PATH`）
2. **CDP 远程调试**：配置文件 `cdp_host` + `cdp_port`（或 CLI/env），或 Docker 环境下自动尝试 `host.docker.internal:9222`
3. **Playwright 浏览器**：自动查找 `~/.cache/ms-playwright` 下的 Chromium
4. **系统浏览器**：自动查找已安装的 Chrome / Chromium / Edge

### 验证码模型与降级

`captcha_model.onnx` 是**可选**的登录字符验证码 OCR 资源，不是程序启动或打包的硬依赖。源码、冻结程序和两个 Docker target 在文件缺失时仍可构建并启动，构建日志会给出提示。

- 模型存在且有效：打包时自动嵌入，密码登录可尝试 OCR。
- 模型缺失或加载失败：仅禁用登录字符 OCR；交互式原生运行会回退人工输入。
- 无交互运行（包括官方 Docker 镜像）：无法人工输入，密码登录会明确失败；已有 Token 的登录方式不受该模型影响。

不要从不可信来源下载或替换模型文件。

## 演示

![study](images/study.png)
![exam](images/exam.png)
![old](images/old.png)

## 常见问题

- ### 部分无法直接登录的学校/Token 登录方法

有些从迎新系统跳转的可以试试账号密码都是学号，也可以尝试使用 Token 登录，在电脑浏览器登录后按 F12 或者 Ctrl+Shift+I 打开开发者工具，找到本地存储，复制 user 的内容到 config.json 配置文件

![chrome](images/chrome.png)
![firefox](images/firefox.png)

- ### 学习

1. 学习时长太低不会计入进度
2. 课程点选验证码会自动识别（无头浏览器 + OpenCV，最多 2 轮 × 3 次，可用 WB_CAPTCHA_ROUNDS/WB_CAPTCHA_ATTEMPTS 调整），失败后在交互模式会打开浏览器手动操作，无交互模式（Docker 等）会跳过该课程并告警
3. 学习进度不更新可能是被风控，遇到了需要验证码的课程，请去网页上完成一次后重试

- ### 考试

1. 考试前有腾讯无感验证码，自动处理（headless 浏览器）
2. 据观察，考试未提交是不会消耗考试次数的

## 鸣谢

- [Coaixy/weiban-tool](https://github.com/Coaixy/weiban-tool) 提供题库和一些代码思路
- [pooneyy/WeibanQuestionsBank](https://github.com/pooneyy/WeibanQuestionsBank) 提供题库

## 其他

1. 本项目仅供学习交流使用，请勿用于商业用途。
2. 欢迎 Star 喵，欢迎 PR 喵。
3. 截图时注意打码个人信息。
4. **如果看不懂上面说的也可以直接扫码备注微信号(不要wxid_开头的，搜不到)，乐意效劳。**

   |             微信             |
   | :--------------------------: |
   | ![wechat](images/wechat.png) |
