# _WeBan_

**由于完成课程的验证码换成了腾讯云，暂时无法完成需要验证码的课程。目前考虑从两个方案解决，使用浏览器油猴脚本或使用无头浏览器模拟**

## 介绍

_WeBan_ 安全微伴-大学安全教育 学习工具

实现了课程学习和根据题库自动考试，支持多用户多线程运行（配置 config.json），自动验证码识别（需要安装 ddddocr）。

运行前后会自动合并题库，如果一次没满分可以再考一次。 可将 answer/answer.json 文件提交 PR 一起完善题库。

## 直接使用

从 [Releases](https://github.com/hangone/WeBan/releases) 下载 WeBan.exe 单文件运行，根据提示输入。

[Github 下载地址](https://github.com/hangone/WeBan/releases/latest/download/WeBan.exe)

[镜像下载地址](https://ghfast.top/https://github.com/hangone/WeBan/releases/latest/download/WeBan.exe)

## 配置说明
config.json
```json
[
  {
    "tenant_name": "学校名称",
    "account": "账号",
    "password": "密码",
    "study": true, // 是否学习课程
    "study_time": 15, // 每节课学习时间，单位（秒）
    "exam": true, // 是否考试
    "exam_use_time": 600 // 考试总时间，单位（秒），会平均到每到题上
  },
  {
    "tenant_name": "学校名称",
    "account": "账号2",
    "password": "密码2",
    "study": true,
    "study_time": 15,
    "exam": true,
    "exam_use_time": 600
  }
]
```

## 源码运行

1. 安装 Python3 （可选使用 [uv](https://github.com/astral-sh/uv)）

2. 打开终端，克隆本仓库 `git clone https://github.com/hangone/WeBan`

3. 在终端运行 `pip install -r requirements-ocr.txt` 或者 `uv sync`

4. 运行 `python main.py`，按提示输入学校和账号密码。每个任务大概需要 ≥13 秒才不会触发限制。

### 演示

![image1](images/image1.png)

### 其他

本项目仅供学习交流使用，请勿用于商业用途，否则后果自负。

截图时注意打码个人信息

欢迎 PR

### 鸣谢

[Coaixy/weiban-tool](https://github.com/Coaixy/weiban-tool) 提供题库和一些代码思路

[pooneyy/WeibanQuestionsBank](https://github.com/pooneyy/WeibanQuestionsBank) 提供题库
