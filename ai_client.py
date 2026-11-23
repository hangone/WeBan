"""AI 搜题请求、自定义思考强度、流式思考过程展示与参考知识库 Prompt 构建。"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from captcha import is_non_interactive
from knowledge import RetrievalResult, load_knowledge_base

# 多账号并发时的终端流式输出锁，防止多个线程的思考文本交错刷屏
_STREAM_STDOUT_LOCK = threading.Lock()

_ANSI_DIM = "\033[2m"
_ANSI_DIM_CYAN = "\033[2;36m"
_ANSI_RESET = "\033[0m"

_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_bool(value: Any, default: bool = False) -> bool:
    """将配置/CLI/环境变量值规范化为 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


class ThinkTagSplitter:
    """流式分离文本中的 `<think>...</think>` 思考片段与正文内容。

    支持标签跨分片边界切分（例如上一片末尾为 `<thi`，下一片开头为 `nk>`）。
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    @staticmethod
    def _trailing_tag_prefix_len(text_lower: str, tag: str) -> int:
        max_len = min(len(text_lower), len(tag) - 1)
        for length in range(max_len, 0, -1):
            if text_lower.endswith(tag[:length]):
                return length
        return 0

    def feed(self, chunk: str) -> tuple[str, str]:
        """输入增量文本，返回 `(thinking_delta, content_delta)`。"""
        if not chunk:
            return "", ""

        self._buf += chunk
        think_parts: list[str] = []
        content_parts: list[str] = []

        while self._buf:
            low = self._buf.lower()
            if not self._in_think:
                idx = low.find(_THINK_OPEN_TAG)
                if idx != -1:
                    if idx > 0:
                        content_parts.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(_THINK_OPEN_TAG) :]
                    self._in_think = True
                    continue
                hold = self._trailing_tag_prefix_len(low, _THINK_OPEN_TAG)
                emit_end = len(self._buf) - hold
                if emit_end > 0:
                    content_parts.append(self._buf[:emit_end])
                    self._buf = self._buf[emit_end:]
                break
            else:
                idx = low.find(_THINK_CLOSE_TAG)
                if idx != -1:
                    if idx > 0:
                        think_parts.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(_THINK_CLOSE_TAG) :]
                    self._in_think = False
                    continue
                hold = self._trailing_tag_prefix_len(low, _THINK_CLOSE_TAG)
                emit_end = len(self._buf) - hold
                if emit_end > 0:
                    think_parts.append(self._buf[:emit_end])
                    self._buf = self._buf[emit_end:]
                break

        return "".join(think_parts), "".join(content_parts)

    def flush(self) -> tuple[str, str]:
        """结束流式读取后冲刷残留缓冲。"""
        if not self._buf:
            return "", ""
        rest = self._buf
        self._buf = ""
        if self._in_think:
            return rest, ""
        return "", rest


def _can_live_stream_stdout() -> bool:
    """判断当前终端是否适合实时流式打印思考过程。"""
    if is_non_interactive():
        return False
    stdout = sys.stdout
    return bool(stdout and hasattr(stdout, "isatty") and stdout.isatty())


class _ThinkingStreamPrinter:
    """控制思考过程在交互式终端的实时淡色流式输出，并支持多账号锁保护。"""

    def __init__(self, *, enabled: bool, live: bool, header: str = "[AI 思考过程]") -> None:
        self.enabled = enabled
        self.live = enabled and live
        self.header = header
        self._has_lock = False
        self._started = False
        self._ended_with_newline = True
        self.streamed_live = False

    def write(self, text: str) -> None:
        if not self.live or not text:
            return
        if not self._started:
            # 非阻塞尝试获取终端输出锁；若其他账号正在流式打印，则回退为结束后缓冲输出
            if not _STREAM_STDOUT_LOCK.acquire(blocking=False):
                self.live = False
                return
            self._has_lock = True
            self._started = True
            self.streamed_live = True
            sys.stdout.write(f"{_ANSI_DIM_CYAN}{self.header}{_ANSI_RESET}\n{_ANSI_DIM}")
        sys.stdout.write(text)
        sys.stdout.flush()
        self._ended_with_newline = text.endswith("\n")

    def finish(self) -> None:
        if self._started:
            if not self._ended_with_newline:
                sys.stdout.write("\n")
            sys.stdout.write(_ANSI_RESET)
            sys.stdout.flush()
            self._started = False
        if self._has_lock:
            _STREAM_STDOUT_LOCK.release()
            self._has_lock = False


@dataclass(frozen=True)
class AIResponse:
    """单次 LLM 调用解析后的完整结果。"""

    content: str
    thinking: str
    usage: dict[str, Any]
    streamed_live: bool


def summarize_thinking(thinking: str, max_chars: int = 300) -> str:
    """压缩思考过程文本为适合日志记录的紧凑摘要。"""
    compact = re.sub(r"\s+", " ", thinking).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def build_question_prompt(
    *,
    title: str,
    type_label: str,
    options: list[dict[str, Any]],
    knowledge_context: str = "",
) -> str:
    """构造 AI 搜题 Prompt；当命中参考知识库时注入知识片段并强调优先遵循。"""
    opt_count = len(options)
    options_str = "\n".join(
        f"{i + 1}. {opt['content']}" for i, opt in enumerate(options)
    )

    if knowledge_context:
        return (
            "你是一个在线教育考试答题助手。请优先依据下方【参考知识库】中的资料"
            "进行思考和作答，再结合题目与选项给出正确答案。\n\n"
            f"【参考知识库】\n{knowledge_context}\n\n"
            f"【题目类型】{type_label}\n\n"
            f"【题目】{title}\n\n"
            f"【选项】\n{options_str}\n\n"
            "【规则】\n"
            "1. 必须优先参考【参考知识库】中的信息进行推理；若参考知识库与通用常识存在差异，以参考知识库为准\n"
            "2. 若参考知识库未涵盖本题，再结合专业常识作答\n"
            "3. 单选题/判断题：answers 只能包含 1 个选项序号\n"
            "4. 多选题：answers 包含所有正确选项的序号\n"
            f"5. 选项序号为 1-based，有效范围：1~{opt_count}\n"
            "6. 在 reason 中简明说明理由，如依据了参考知识库请注明对应文档名称\n\n"
            "【输出格式】严格输出一个 JSON 对象，不要输出任何其他内容：\n"
            '{"answers":[1],"reason":"理由（含参考文档来源）"}'
        )

    return (
        "你是一个在线教育考试答题助手。请根据题目和选项，给出正确答案。\n\n"
        f"【题目类型】{type_label}\n\n"
        f"【题目】{title}\n\n"
        f"【选项】\n{options_str}\n\n"
        "【规则】\n"
        "1. 单选题/判断题：answers 只能包含 1 个选项序号\n"
        "2. 多选题：answers 包含所有正确选项的序号\n"
        f"3. 选项序号为 1-based，有效范围：1~{opt_count}\n\n"
        "【输出格式】严格输出一个 JSON 对象，不要输出任何其他内容：\n"
        '{"answers":[1],"reason":"理由"}'
    )


def parse_ai_response(content: str) -> tuple[list[Any] | None, str]:
    """从 AI 返回内容中提取 `(answers 列表, reason 文本)`。"""
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    json_str = cleaned
    if "```" in json_str:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", json_str, re.DOTALL)
        if match:
            json_str = match.group(1).strip()

    # 方式一：直接解析 JSON
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            answers = data.get("answers", [])
            reason = str(data.get("reason") or "").strip()
            if isinstance(answers, list) and answers:
                return answers, reason
    except (json.JSONDecodeError, TypeError):
        pass

    # 方式二：提取第一个完整 JSON 对象（兼容回复前后带说明文字）
    obj_match = re.search(r"\{[^{}]*\"answers\"[^{}]*\}", json_str, re.DOTALL)
    if obj_match:
        try:
            data = json.loads(obj_match.group(0))
            if isinstance(data, dict):
                answers = data.get("answers", [])
                reason = str(data.get("reason") or "").strip()
                if isinstance(answers, list) and answers:
                    return answers, reason
        except (json.JSONDecodeError, TypeError):
            pass

    # 方式三：正则兜底提取 answers 序号列表
    match = re.search(r'"answers"\s*:\s*\[([\d\s,]+)\]', json_str)
    if match:
        try:
            indices = [int(x) for x in match.group(1).split(",") if x.strip()]
            if indices:
                reason_match = re.search(r'"reason"\s*:\s*"((?:\\.|[^"\\])*)"', json_str)
                reason = ""
                if reason_match:
                    try:
                        reason = json.loads(f'"{reason_match.group(1)}"')
                    except json.JSONDecodeError:
                        reason = reason_match.group(1)
                return indices, reason.strip()
        except (ValueError, TypeError):
            pass

    return None, ""


def _extract_reasoning_text(obj: dict[str, Any]) -> str:
    """从 OpenAI 兼容 delta 或 message 字典中提取思考字段。

    兼容：
    - `reasoning_content`（DeepSeek / Qwen / SiliconFlow / vLLM）
    - `reasoning`（OpenRouter 及部分中转代理）
    """
    for key in ("reasoning_content", "reasoning"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _parse_non_stream_response(res_data: dict[str, Any]) -> AIResponse:
    usage = res_data.get("usage") or {}
    choices = res_data.get("choices") or []
    message = choices[0].get("message") or {}
    raw_content = message.get("content") or ""
    explicit_reasoning = _extract_reasoning_text(message)

    splitter = ThinkTagSplitter()
    tag_think, clean_content = splitter.feed(raw_content)
    tail_think, tail_content = splitter.flush()

    thinking = (explicit_reasoning + tag_think + tail_think).strip()
    content = (clean_content + tail_content).strip()
    return AIResponse(
        content=content,
        thinking=thinking,
        usage=usage if isinstance(usage, dict) else {},
        streamed_live=False,
    )


def _consume_sse_response(
    resp: Any,
    *,
    show_thinking: bool,
    live_stdout: bool,
) -> AIResponse:
    """解析 OpenAI 兼容 SSE 流式响应，实时打印思考过程并收集完整正文。"""
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type and "text/event-stream" not in content_type:
        return _parse_non_stream_response(resp.json())

    printer = _ThinkingStreamPrinter(enabled=show_thinking, live=live_stdout)
    splitter = ThinkTagSplitter()
    thinking_parts: list[str] = []
    content_parts: list[str] = []
    usage: dict[str, Any] = {}

    try:
        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line).strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue

            data_str = line[5:].strip()
            if not data_str:
                continue
            if data_str == "[DONE]":
                break

            try:
                chunk_data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk_data, dict):
                continue

            chunk_usage = chunk_data.get("usage")
            if isinstance(chunk_usage, dict) and chunk_usage:
                usage = chunk_usage

            choices = chunk_data.get("choices") or []
            if not choices:
                continue
            choice0 = choices[0] if isinstance(choices[0], dict) else {}
            raw_delta = choice0.get("delta")
            if isinstance(raw_delta, dict):
                delta: dict[str, Any] = raw_delta
            else:
                # 某些代理在最后一个 chunk 放 message 而非 delta
                raw_msg = choice0.get("message")
                delta = raw_msg if isinstance(raw_msg, dict) else {}

            reasoning_delta = _extract_reasoning_text(delta)
            if reasoning_delta:
                thinking_parts.append(reasoning_delta)
                printer.write(reasoning_delta)

            raw_content_delta = delta.get("content")
            if isinstance(raw_content_delta, str) and raw_content_delta:
                tag_think, clean_content = splitter.feed(raw_content_delta)
                if tag_think:
                    thinking_parts.append(tag_think)
                    printer.write(tag_think)
                if clean_content:
                    content_parts.append(clean_content)

        tail_think, tail_content = splitter.flush()
        if tail_think:
            thinking_parts.append(tail_think)
            printer.write(tail_think)
        if tail_content:
            content_parts.append(tail_content)
    finally:
        printer.finish()

    return AIResponse(
        content="".join(content_parts).strip(),
        thinking="".join(thinking_parts).strip(),
        usage=usage,
        streamed_live=printer.streamed_live,
    )


def _adapt_payload_on_400(payload: dict[str, Any], error_body: str, log: Any) -> bool:
    """当服务端因推理模型参数限制返回 HTTP 400 时，自动移除不兼容字段重试。"""
    low = error_body.lower()
    changed = False

    if "temperature" in payload and "temperature" in low:
        payload.pop("temperature", None)
        log.warning("AI 服务端不支持当前模型设置 temperature，已自动移除该参数重试")
        changed = True
    if "response_format" in payload and (
        "response_format" in low or "json_object" in low
    ):
        payload.pop("response_format", None)
        log.warning("AI 服务端不支持当前模型设置 response_format，已自动移除该参数重试")
        changed = True
    if payload.get("stream") and "stream" in low and "reasoning" not in low:
        payload.pop("stream", None)
        log.warning("AI 服务端不支持流式传输，已自动回退为非流式请求重试")
        changed = True

    return changed


def request_ai_answer(
    *,
    session: Any,
    ai_config: dict[str, Any],
    question: dict[str, Any],
    base_path: str,
    data_dir: str | None,
    log: Any,
    warned_key_state: list[bool],
) -> list[Any]:
    """执行一次完整的 AI 搜题（含知识库检索、思考强度、流式思考展示、重试与答案映射）。"""
    if not ai_config or not parse_bool(ai_config.get("enable"), default=False):
        return []

    api_key = str(ai_config.get("api_key", "")).strip()
    base_url = str(ai_config.get("base_url", "https://api.deepseek.com")).strip()
    model = str(ai_config.get("model", "deepseek-v4-pro")).strip()
    timeout = int(ai_config.get("timeout", 60))
    max_retries = max(1, int(ai_config.get("max_retries", 2)))
    reasoning_effort = str(ai_config.get("reasoning_effort", "") or "").strip()
    stream_enabled = parse_bool(ai_config.get("stream"), default=True)
    show_thinking = parse_bool(ai_config.get("show_thinking"), default=True)
    knowledge_enable = parse_bool(ai_config.get("knowledge_enable"), default=True)
    knowledge_paths = ai_config.get("knowledge_paths")
    knowledge_max_chars = int(ai_config.get("knowledge_max_chars", 12000))
    knowledge_top_k = int(ai_config.get("knowledge_top_k", 5))

    if not api_key and not warned_key_state[0]:
        log.warning("AI 搜题已启用，但未配置 api_key")
        warned_key_state[0] = True

    title = str(question.get("title", ""))
    type_label = str(question.get("typeLabel", "未知"))
    options = question.get("optionList", [])
    opt_count = len(options)

    # 参考文档知识库检索
    retrieval = RetrievalResult()
    if knowledge_enable:
        kb = load_knowledge_base(
            extra_paths=knowledge_paths,
            base_path=base_path,
            data_dir=data_dir,
            log=log,
        )
        if not kb.is_empty:
            retrieval = kb.retrieve(
                title,
                [str(opt.get("content", "")) for opt in options],
                max_chars=knowledge_max_chars,
                top_k=knowledge_top_k,
            )
            if retrieval.context:
                sources_preview = ", ".join(retrieval.sources[:3])
                if len(retrieval.sources) > 3:
                    sources_preview += f" 等 {len(retrieval.sources)} 篇"
                mode_label = "全文注入" if retrieval.mode == "full" else f"BM25 召回 {retrieval.matched_chunks} 片段"
                log.info(f"参考知识库命中（{mode_label}）：{sources_preview}")
            else:
                log.debug("参考知识库未检索到与本题相关的片段，将由模型基于通用知识作答")

    prompt = build_question_prompt(
        title=title,
        type_label=type_label,
        options=options,
        knowledge_context=retrieval.context,
    )

    effort_tag = f" [reasoning_effort={reasoning_effort}]" if reasoning_effort else ""
    log.info(f"AI 搜题{effort_tag}：{title[:40]}...")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if stream_enabled:
        payload["stream"] = True

    url = f"{base_url.rstrip('/')}/chat/completions"
    ai_resp: AIResponse | None = None
    live_stdout = _can_live_stream_stdout()

    for attempt in range(1, max_retries + 1):
        compat_adjustments = 0
        while True:
            use_stream = bool(payload.get("stream"))
            try:
                with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    stream=use_stream,
                ) as resp:
                    if resp.status_code == 400 and compat_adjustments < 3:
                        err_text = resp.text or ""
                        if _adapt_payload_on_400(payload, err_text, log):
                            compat_adjustments += 1
                            continue
                    resp.raise_for_status()
                    if use_stream:
                        ai_resp = _consume_sse_response(
                            resp,
                            show_thinking=show_thinking,
                            live_stdout=live_stdout,
                        )
                    else:
                        ai_resp = _parse_non_stream_response(resp.json())
                break
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                if attempt < max_retries:
                    wait = attempt * 2
                    log.warning(f"AI 搜题第 {attempt} 次请求失败，{wait}s 后重试：{e}")
                    time.sleep(wait)
                else:
                    log.error(f"AI 搜题请求失败（已重试 {max_retries} 次）：{e}")
                    return []
                break
        if ai_resp is not None:
            break

    if ai_resp is None:
        return []

    if ai_resp.usage:
        log.debug(
            f"AI token 用量 — prompt: {ai_resp.usage.get('prompt_tokens', '?')}, "
            f"completion: {ai_resp.usage.get('completion_tokens', '?')}, "
            f"total: {ai_resp.usage.get('total_tokens', '?')}"
        )

    # 记录思考摘要：若交互终端已实时流式输出，则仅写入日志文件避免终端重复刷屏；
    # 无交互/非 TTY/非流式或锁被占用缓冲场景下，整体输出到日志与终端
    if ai_resp.thinking:
        summary = summarize_thinking(ai_resp.thinking)
        if ai_resp.streamed_live:
            log.bind(file_only=True).info(
                f"AI 思考摘要（共 {len(ai_resp.thinking)} 字）：{summary}"
            )
        elif show_thinking:
            log.info(f"AI 思考过程（共 {len(ai_resp.thinking)} 字）：{summary}")
        else:
            log.bind(file_only=True).info(
                f"AI 思考摘要（共 {len(ai_resp.thinking)} 字）：{summary}"
            )

    if not ai_resp.content:
        log.warning("AI 返回正文为空")
        return []

    raw_indices, reason = parse_ai_response(ai_resp.content)
    if raw_indices is None:
        log.warning(f"AI 返回内容解析失败，原始内容：{ai_resp.content[:200]}")
        return []

    valid_ids = []
    for idx in raw_indices:
        try:
            val = int(idx)
        except (ValueError, TypeError):
            continue
        if 1 <= val <= opt_count:
            valid_ids.append(options[val - 1]["id"])
            log.info(f"AI 推荐：{val}. {options[val - 1]['content']}")
        else:
            log.warning(f"AI 返回的选项序号 {val} 超出范围 1~{opt_count}，忽略")

    if reason:
        log.info(f"AI 理由：{reason}")

    if not valid_ids:
        log.warning("AI 返回的答案未能匹配任何有效选项")

    return valid_ids
