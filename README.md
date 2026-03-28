# WeBan

> 面向安全微课 / 安全微伴平台的浏览器自动化学习与考试工具。

如果本项目帮到了你，可以在右上角点亮 Star，谢谢你！

本项目基于 Playwright 模拟真实浏览器操作，支持自动学习、自动考试、多账号并发运行、本地题库合并，以及登录页文字验证码与学习/考试过程中的点选验证码自动处理。

## 功能特性

- 浏览器自动化运行，尽量贴近真实操作流程
- 支持账号密码登录，也支持 `userId + token` 直接登录
- 首次运行无配置时可自动生成 `config.toml`
- 支持多账号并发执行
- 支持学习与考试分开控制
- 支持强制学习 / 强制重考模式
- 支持本地题库 + 云端题库合并
- 支持从历史考试记录补充题库
- 支持登录页文字验证码识别
- 支持课程内点选验证码自动处理
- 支持调试日志与验证码截图落盘

## 运行环境

- Python `3.12`
- 推荐使用 [uv](https://github.com/astral-sh/uv) 管理环境
- 首次源码运行需要安装 Playwright Chromium 内核

## 快速开始

### 方式一：直接使用发行版

如果你只是想直接运行，优先使用 [Releases](https://github.com/hangone/WeBan/releases) 中的打包版本。

下载对应系统压缩包，解压后直接运行即可。打包版通常已内置 Chromium 与相关模型，更适合普通用户。

## 源码运行

### 1. 克隆项目

```bash
git clone --depth 1 -b v4 https://github.com/hangone/WeBan.git
cd WeBan
```

如果 GitHub 访问较慢，也可以自行使用镜像地址。

### 2. 安装依赖

推荐使用 `uv`：

```bash
uv sync
```

如果你不使用 `uv`，也可以使用 `pip`：

```bash
pip install -r requirements.txt
```

### 3. 安装 Playwright 浏览器

```bash
uv run playwright install chromium
```

或：

```bash
playwright install chromium
```

### 4. 启动程序

```bash
uv run python main.py
```

也可以直接执行：

```bash
python main.py
```

## 使用流程

### 首次运行

程序启动后会在项目根目录查找 `config.toml`：

- 如果不存在，会自动生成默认配置文件
- 如果账号信息不完整，程序会打开浏览器，等待你手动完成登录
- 登录成功后，程序会把学校名、账号等信息回填到 `config.toml`

### 后续运行

当 `config.toml` 已填写完成后：

- 程序会按配置依次处理每个账号
- 可自动执行学习流程
- 可自动执行考试流程
- 可根据配置决定学习时长、是否随机答题等

## 配置文件说明

配置文件名为 `config.toml`。

你可以手动复制 `config.example.toml` 为 `config.toml` 后再修改：

```bash
cp config.example.toml config.toml
```

配置由两部分组成：

- `[settings]`：全局默认配置
- `[[account]]`：账号配置，可写多个，支持覆盖全局设置

### 最小可用示例

```toml
[settings]
study_mode = "true"
exam_mode = "true"
study_time = 20

[[account]]
tenant_name = "学校名称"
username = "你的学号"
password = "你的密码"
```

### 推荐配置方式

如果你不确定怎么填写，建议：

1. 先直接运行程序
2. 程序生成 `config.toml`
3. 在浏览器中手动完成一次登录
4. 再检查和补全配置文件

### 常用配置项

#### `[settings]`

- `study_mode`
  - `"false"`：不学习
  - `"true"`：正常学习，已完成内容会跳过
  - `"force"`：强制学习，已完成内容也会再次学习

- `exam_mode`
  - `"false"`：不考试
  - `"true"`：通过后跳过考试
  - `"force"`：已通过也重新考试

- `random_answer`
  - `true`：未知题自动随机作答
  - `false`：未知题在终端等待手动输入

- `browser_headless`
  - `true`：后台无头运行
  - `false`：显示浏览器窗口

- `study_time`
  - 每个学习任务默认停留秒数

- `exam_question_time`
  - 每道题基础答题时间

- `exam_question_time_offset`
  - 每道题额外随机延迟上限

- `exam_submit_match_rate`
  - 允许提交试卷的最低题库匹配率

- `max_workers`
  - 多账号并发线程数上限

- `browser_timeout_ms`
  - 页面加载和元素等待超时

- `manual_login_timeout_sec`
  - 手动登录最长等待时间

- `close_browser_on_finish`
  - 任务结束后是否关闭浏览器

- `continue_on_invalid_token`
  - 当 `token` 失效时，是否自动回退到账号密码登录

- `debug`
  - 是否开启调试日志与验证码截图保存

#### `[[account]]`

每个账号块常用字段：

- `tenant_name`：学校全称
- `username`：学号或平台账号
- `password`：登录密码
- `userId`：可选，配合 `token` 使用
- `token`：可选，优先用于直接登录

账号块内还可以单独覆盖全局设置，例如：

```toml
[[account]]
tenant_name = "学校名称"
username = "20240001"
password = "password"
study_mode = "false"
exam_mode = "force"
browser_headless = true
```

完整字段说明请查看 `config.example.toml`。

## 题库说明

项目中的 `answer/answer.json` 是本地题库。

程序运行考试前会尝试：

1. 加载本地 `answer/answer.json`
2. 从云端拉取最新题库并合并
3. 从历史考试记录中提取题目与答案并补充到内存题库

这意味着：

- 首次考试不一定满分
- 未命中的题会在后续运行中逐步补全
- 如果开启 `random_answer`，未知题会自动作答继续流程

## 日志与调试

默认日志会输出到控制台，并写入：

- `logs/weban.log`

开启 `debug = true` 后：

- 日志会更详细
- 验证码截图会保存到账号对应目录
- 单账号日志会保存到类似目录：

```text
logs/<账号>/weban_时间戳.log
```

## 常见问题

### 1. 首次运行为什么会弹出浏览器？

这是正常行为。程序需要你完成一次登录，或者在没有完整配置时等待你手动处理验证码、学校选择等步骤。

### 2. 配了 `browser_headless = true` 为什么有时仍然显示浏览器？

当账号信息和 `token` 都缺失时，程序会优先确保你能完成首次手动登录，因此会强制显示浏览器窗口。

### 3. 为什么考试被跳过？

可能原因包括：

- `exam_mode = "false"`
- 当前项目没有在线考试入口
- 当前考试已通过且 `exam_mode = "true"`
- 学习进度未达标，平台禁止考试
- 题库匹配率低于 `exam_submit_match_rate`

### 4. 为什么学习进度没有变化？

这通常和平台页面结构变化、课程状态异常、学校侧风控或网络环境有关。浏览器自动化依赖前端页面结构，平台改版后可能需要调整选择器和流程。

### 5. `token` 登录失败怎么办？

如果你设置了：

```toml
continue_on_invalid_token = true
```

程序会自动回退到账号密码登录。

如果设置为 `false`，则 `token` 失效后会直接失败。

## 项目结构

```text
.
├── main.py                  # 程序入口
├── config.example.toml      # 配置文件示例
├── answer/
│   └── answer.json          # 本地题库
├── weban/
│   ├── app/                 # 应用编排层
│   │   ├── bootstrap.py     # 启动流程与运行入口
│   │   ├── config.py        # 配置加载与回填
│   │   ├── runtime.py       # 运行时工具
│   │   └── task_engine.py   # 多账号任务调度
│   ├── logger.py            # 日志初始化
│   ├── updater.py           # 更新检查
│   └── core/
│       ├── client.py        # 客户端主类
│       ├── browser.py       # 浏览器封装
│       ├── auth.py          # 登录流程
│       ├── study.py         # 学习流程
│       ├── exam.py          # 考试流程
│       ├── answer.py        # 题库处理
│       └── captcha.py       # 验证码处理
```

## 鸣谢

- [Coaixy/weiban-tool](https://github.com/Coaixy/weiban-tool)
- [pooneyy/WeibanQuestionsBank](https://github.com/pooneyy/WeibanQuestionsBank)
- [AmethystDev-Labs/TenVision](https://github.com/AmethystDev-Labs/TenVision)

## 免责声明

1. 本项目仅供学习与技术交流使用
2. 请自行承担使用本项目带来的后果与风险
3. 请勿用于商业用途或违反校方、平台规则的场景
4. **如果看不懂上面说的也可以直接扫码备注学校和账号密码（建议留言微信号），乐意效劳。**

   |             微信             |            支付宝            |
   | :--------------------------: | :--------------------------: |
   | ![wechat](images/wechat.png) | ![alipay](images/alipay.png) |