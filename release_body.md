## v3.12.0 更新日志

- **自定义思考强度**：支持配置标准 `reasoning_effort` 参数（`none`/`low`/`medium`/`high`/`xhigh`），并提供 OpenAI Responses API (`/v1/responses`) 与 `/v1/chat/completions` 双协议自适应协商与能力缓存。
- **流式思考过程展示**：支持 SSE 流式接收并兼容 `response.reasoning_summary_text.delta`、`reasoning_content`、`reasoning`、`reasoning_details`、`thinking` 与 `<think>` 标签；自动过滤中转占位块并精确统计 TTFT、TPS、推理 Token 与缓存命中。
- **多格式参考文档知识库**：支持 `.pdf`、`.docx`、`.pptx`、`.xlsx`、`.md`、`.txt`、`.csv`、`.json`、`.html` 等格式，采用固定前缀 KV Cache（小库全文）+ Agentic Function Calling 工具按需检索（大库，不支持 `tools` 时自动降级本地 BM25），并在作答理由中按 Markdown 详细列出引证位置与原文句子。
- **键鼠交互式答题与核对页面**：进入考试后后台自动执行题库匹配与 AI 搜题，支持键盘/鼠标随时暂停、切题、人工纠正、单题重跑 AI，且**绝不自动提交试卷**，需人工二次确认后才交卷；新增 `--exam-ui-demo` 实测模式。
