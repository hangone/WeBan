"""AI 搜题与 Agentic 知识库协作引擎。

核心特性：
1. 双协议自适应：
   - 原生支持 OpenAI Responses API (`/v1/responses`)，显式开启 `reasoning.summary="detailed"`、
     原生 `function_call` 工具循环、精确 `reasoning_tokens` 与低冗余 `instructions` 前缀；
   - 自动探测并缓存接口能力，不支持 `/responses` 的接口（如 DeepSeek/Qwen/Ollama）无缝回退
     `/v1/chat/completions`，并兼容 `reasoning_content` / `reasoning` / `reasoning_details` /
     `thinking` / `<think>` 全部主流思考字段。
2. 自动过滤 `sub2api_placeholder: true` 假占位块，确保 TTFT 与正文不受中转保活空格污染。
3. 固定 `system`/`instructions` 前缀 + 自适应知识库（小库放固定前缀复用 KV Cache，大库目录+工具按需检索）。
4. 完整实时遥测：协议类型、TTFT、TPS、Prompt/Cached/Completion/Reasoning/Total Tokens、上下文长度、工具轨迹。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from weban.captcha import is_non_interactive
from weban.knowledge import KnowledgeBase, RetrievalResult, load_knowledge_base

_STREAM_STDOUT_LOCK = threading.Lock()

_ANSI_DIM = "\033[2m"
_ANSI_DIM_CYAN = "\033[2;36m"
_ANSI_RESET = "\033[0m"

_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

_MAX_TOOL_ROUNDS = 3

KB_CHAT_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "在本地参考文档知识库中检索与题目或选项最相关的文档片段。"
                "适合在作答前查找规章、标准答案要点或专业条款。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词或短语（建议提炼题干与关键选项的核心词）",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回片段数量，默认 4，最大 8",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document_chunk",
            "description": (
                "按文档名与段号读取指定段落及其前后相邻段落，用于补全被截断的表格或长条文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "参考文档名（来自目录或 search_knowledge 返回的 source）",
                    },
                    "chunk_index": {
                        "type": "integer",
                        "description": "目标段落编号（1-based）",
                    },
                    "window": {
                        "type": "integer",
                        "description": "前后额外读取的段数（0~2，默认 1）",
                    },
                },
                "required": ["source", "chunk_index"],
            },
        },
    },
]

KB_RESPONSES_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": item["function"]["name"],
        "description": item["function"]["description"],
        "parameters": item["function"]["parameters"],
    }
    for item in KB_CHAT_TOOLS_SCHEMA
]

# 兼容别名
KB_TOOLS_SCHEMA = KB_CHAT_TOOLS_SCHEMA


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


def normalize_chat_url(base_url: str) -> str:
    """规范化 `/chat/completions` 地址，兼容仅填域名/v1 或已含路径。"""
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return "https://api.deepseek.com/chat/completions"
    low = cleaned.lower()
    if low.endswith("/chat/completions"):
        return cleaned
    if low.endswith("/responses"):
        return cleaned[: -len("/responses")] + "/chat/completions"
    return f"{cleaned}/chat/completions"


def normalize_responses_url(base_url: str) -> str:
    """规范化 `/responses` 地址。"""
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return "https://api.openai.com/v1/responses"
    low = cleaned.lower()
    if low.endswith("/responses"):
        return cleaned
    if low.endswith("/chat/completions"):
        return cleaned[: -len("/chat/completions")] + "/responses"
    return f"{cleaned}/responses"


def extract_endpoint_host(base_url: str) -> str:
    """提取用于界面展示的精简接口主机名。"""
    url = normalize_chat_url(base_url)
    parsed = urlparse(url)
    return parsed.netloc or base_url.strip()


def estimate_tokens(text: str) -> int:
    """混合中英文 Token 估算（仅在服务端未返回 usage 或预估上下文长度时使用）。"""
    if not text:
        return 0
    cjk_count = len(_CJK_CHAR_RE.findall(text))
    other_count = len(text) - cjk_count
    return max(1, round(cjk_count * 0.72 + other_count / 3.8))


def _messages_char_len(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        for key in ("content", "output", "arguments"):
            val = msg.get(key)
            if isinstance(val, str):
                total += len(val)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            total += len(json.dumps(tool_calls, ensure_ascii=False))
    return total


def _extract_usage_numbers(usage: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """提取 `(prompt, completion, cached, reasoning, total)`。"""
    if not isinstance(usage, dict) or not usage:
        return 0, 0, 0, 0, 0
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))

    cached = 0
    in_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(in_details, dict) and in_details.get("cached_tokens") is not None:
        cached = int(in_details.get("cached_tokens") or 0)
    elif usage.get("prompt_cache_hit_tokens") is not None:
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
    elif usage.get("cache_read_input_tokens") is not None:
        cached = int(usage.get("cache_read_input_tokens") or 0)

    reasoning = 0
    out_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    if isinstance(out_details, dict) and out_details.get("reasoning_tokens") is not None:
        reasoning = int(out_details.get("reasoning_tokens") or 0)

    return prompt, completion, cached, reasoning, total


@dataclass
class _EndpointCapabilities:
    """按 (base_key, model) 缓存的接口能力协商状态。"""

    supports_responses: bool | None = None  # None=未探测, True=可用, False=不可用
    supports_reasoning_summary: bool = True
    supports_stream: bool = True
    supports_stream_options: bool = True
    supports_temperature: bool = True
    supports_response_format: bool = True
    supports_tools: bool = True
    supports_reasoning_effort: bool = True


_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY_CACHE: dict[tuple[str, str], _EndpointCapabilities] = {}


def _get_capabilities(base_url: str, model: str) -> _EndpointCapabilities:
    key = (normalize_chat_url(base_url), model)
    with _CAPABILITY_LOCK:
        cap = _CAPABILITY_CACHE.get(key)
        if cap is None:
            cap = _EndpointCapabilities()
            _CAPABILITY_CACHE[key] = cap
        return cap


def clear_capability_cache() -> None:
    """清空接口能力协商缓存（供测试或切换配置时使用）。"""
    with _CAPABILITY_LOCK:
        _CAPABILITY_CACHE.clear()


class ThinkTagSplitter:
    """流式分离文本中的 `<think>...</think>` 思考片段与正文内容。"""

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
        if not self._buf:
            return "", ""
        rest = self._buf
        self._buf = ""
        if self._in_think:
            return rest, ""
        return "", rest


def _can_live_stream_stdout() -> bool:
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
    """单轮 LLM HTTP 调用解析后的结果与原始计时。"""

    content: str
    thinking: str
    usage: dict[str, Any]
    streamed_live: bool
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    ttft_ms: float | None = None
    gen_seconds: float = 0.0
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class AIMetrics:
    """单题 AI 求解的完整性能与上下文遥测。"""

    endpoint_host: str = ""
    api_protocol: str = ""  # responses | chat
    model: str = ""
    reasoning_effort: str = ""
    ttft_ms: float | None = None
    tps: float | None = None
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    usage_estimated: bool = False
    context_chars: int = 0
    context_tokens: int = 0
    rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_host": self.endpoint_host,
            "api_protocol": self.api_protocol,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "ttft_ms": round(self.ttft_ms, 1) if self.ttft_ms is not None else None,
            "tps": round(self.tps, 1) if self.tps is not None else None,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "usage_estimated": self.usage_estimated,
            "context_chars": self.context_chars,
            "context_tokens": self.context_tokens,
            "rounds": self.rounds,
        }


@dataclass(frozen=True)
class AISolveResult:
    """完整 AI 搜题结果（供终端模式与交互式考试页面共用）。"""

    selected_ids: list[Any] = field(default_factory=list)
    selected_indices: list[int] = field(default_factory=list)
    reason: str = ""
    thinking: str = ""
    kb_mode: str = "empty"  # empty | full_cached | agent_tools | bm25 | bm25_empty
    kb_sources: list[str] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    metrics: AIMetrics = field(default_factory=AIMetrics)
    model: str = ""
    reasoning_effort: str = ""
    error: str = ""


def summarize_thinking(thinking: str, max_chars: int = 300) -> str:
    compact = re.sub(r"\s+", " ", thinking).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def build_system_prompt(
    *,
    kb: KnowledgeBase | None = None,
    mode: str = "empty",
) -> str:
    """构造固定 `system` / `instructions` 提示词（同场考试内保持完全不变以命中 KV Cache）。"""
    base = (
        "你是一个严谨的在线考试答题助手。\n"
        "作答规则：\n"
        "1. 单选题/判断题：如认为多个选项存在合理性，可在 reason 中对比分析，但必须选出最恰当的 1 项作为最终答案（若列出多个候选序号，务必把最恰当的一项排在首位）。\n"
        "2. 多选题：answers 包含所有正确选项的序号。\n"
        "3. 选项序号为 1-based（从 1 开始）。\n"
        "4. 最终只输出一个合法 JSON 对象，不要附加 JSON 之外的多余文本：\n"
        '{"answers":[1],"reason":"使用 Markdown 格式书写详细理由"}'
    )
    citation_rule = (
        "- **严格 Markdown 引证要求**：只要参考了知识库文档，必须在 `reason` 字段中按 Markdown 格式详细列出：\n"
        "  1. **参考位置**：文档名、具体页码（如 `[第 N 页]`）、章节条款或段落编号；\n"
        "  2. **原文摘录**：使用 Markdown 引用块（`> ...`）摘录具体段落中的关键原句；\n"
        "  3. **选择原因**：结合上述原文详细解释为何选择该选项（以及为何排除其他干扰项）。"
    )
    if kb is None or kb.is_empty or mode == "empty":
        return base

    if mode == "full_cached":
        return (
            base
            + "\n\n参考知识库使用规则：\n"
            "- 下方【参考知识库】为权威依据，作答前必须先核对其中内容。\n"
            "- 若参考知识库与通用常识存在差异，以参考知识库为准；若未涵盖本题，再结合专业知识作答。\n"
            + citation_rule
            + f"\n\n【参考知识库】\n{kb.full_text_bundle()}"
        )

    if mode == "agent_tools":
        return (
            base
            + "\n\n参考知识库工具协作规则：\n"
            "- 本地已加载参考文档（目录如下），答题时必须优先核对库内资料，若与通用常识冲突以库内资料为准。\n"
            "- 如题目涉及库内主题，先调用 `search_knowledge` 检索相关段落；若片段不完整，可调用 `read_document_chunk` 读取相邻段落。\n"
            "- 检索最多 2~3 次即可，拿到依据后直接输出最终 JSON。\n"
            + citation_rule
            + f"\n\n【参考文档目录】\n{kb.catalog_summary()}"
        )

    return (
        base
        + "\n\n参考知识库使用规则：\n"
        "- 用户消息中若附带【参考片段】，必须优先以其中资料为最高依据进行作答，冲突时以参考片段为准。\n"
        + citation_rule
    )


def build_user_prompt(
    *,
    title: str,
    type_label: str,
    options: list[dict[str, Any]],
    bm25_context: str = "",
) -> str:
    opt_count = len(options)
    options_str = "\n".join(
        f"{i + 1}. {opt['content']}" for i, opt in enumerate(options)
    )
    parts: list[str] = []
    if bm25_context:
        parts.append(f"【参考片段】\n{bm25_context}")
    parts.append(
        f"【题型】{type_label}（有效序号：1~{opt_count}）\n"
        f"【题目】{title}\n"
        f"【选项】\n{options_str}"
    )
    return "\n\n".join(parts)


def build_question_prompt(
    *,
    title: str,
    type_label: str,
    options: list[dict[str, Any]],
    knowledge_context: str = "",
) -> str:
    sys_text = build_system_prompt(mode="bm25" if knowledge_context else "empty")
    user_text = build_user_prompt(
        title=title,
        type_label=type_label,
        options=options,
        bm25_context=knowledge_context,
    )
    return f"{sys_text}\n\n{user_text}"


def parse_ai_response(content: str) -> tuple[list[Any] | None, str]:
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    json_str = cleaned
    if "```" in json_str:
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", json_str, re.DOTALL)
        if match:
            json_str = match.group(1).strip()

    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            answers = data.get("answers", [])
            reason = str(data.get("reason") or "").strip()
            if isinstance(answers, list) and answers:
                return answers, reason
    except (json.JSONDecodeError, TypeError):
        pass

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
    """从 Chat Completions delta/message 中提取所有主流思考字段。

    支持：
    - `reasoning_content`（DeepSeek / vLLM / Qwen / sub2api）
    - `reasoning`（OpenRouter / vLLM）
    - `thinking`（部分 Anthropic-compat 代理）
    - `reasoning_details`（OpenRouter / Vercel AI Gateway 结构化列表）
    """
    for key in ("reasoning_content", "reasoning", "thinking"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val

    details = obj.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            for field_name in ("text", "summary", "content"):
                val = item.get(field_name)
                if isinstance(val, str) and val:
                    parts.append(val)
                    break
        if parts:
            return "".join(parts)
    return ""


# ── Chat Completions 解析 ─────────────────────────────────────

def _parse_chat_non_stream(
    res_data: dict[str, Any],
    *,
    req_start: float,
) -> AIResponse:
    now = time.perf_counter()
    elapsed_ms = (now - req_start) * 1000.0
    usage = res_data.get("usage") or {}
    choices = res_data.get("choices") or []
    message = choices[0].get("message") or {} if choices else {}
    raw_content = message.get("content") or ""
    explicit_reasoning = _extract_reasoning_text(message)
    raw_tool_calls = message.get("tool_calls") or []
    tool_calls = [tc for tc in raw_tool_calls if isinstance(tc, dict)]

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
        tool_calls=tool_calls,
        ttft_ms=elapsed_ms,
        gen_seconds=max(0.001, now - req_start),
        elapsed_ms=elapsed_ms,
    )


def _consume_chat_sse(
    resp: Any,
    *,
    req_start: float,
    show_thinking: bool,
    live_stdout: bool,
    on_thinking_delta: Callable[[str], None] | None = None,
) -> AIResponse:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type and "text/event-stream" not in content_type:
        non_stream = _parse_chat_non_stream(resp.json(), req_start=req_start)
        if non_stream.thinking and on_thinking_delta is not None:
            on_thinking_delta(non_stream.thinking)
        return non_stream

    printer = _ThinkingStreamPrinter(enabled=show_thinking, live=live_stdout)
    splitter = ThinkTagSplitter()
    thinking_parts: list[str] = []
    content_parts: list[str] = []
    tool_call_acc: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    first_token_ts: float | None = None

    def _mark_first_token() -> None:
        nonlocal first_token_ts
        if first_token_ts is None:
            first_token_ts = time.perf_counter()

    def _emit_thinking(piece: str) -> None:
        if not piece:
            return
        _mark_first_token()
        thinking_parts.append(piece)
        printer.write(piece)
        if on_thinking_delta is not None:
            on_thinking_delta(piece)

    try:
        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = (
                raw_line.decode("utf-8", errors="replace").strip()
                if isinstance(raw_line, bytes)
                else str(raw_line).strip()
            )
            if not line or line.startswith(":") or not line.startswith("data:"):
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
            # 过滤 sub2api 网关首包保活空格占位块，避免污染正文与伪造 TTFT
            if chunk_data.get("sub2api_placeholder"):
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
                raw_msg = choice0.get("message")
                delta = raw_msg if isinstance(raw_msg, dict) else {}

            reasoning_delta = _extract_reasoning_text(delta)
            if reasoning_delta:
                _emit_thinking(reasoning_delta)

            raw_content_delta = delta.get("content")
            if isinstance(raw_content_delta, str) and raw_content_delta:
                _mark_first_token()
                tag_think, clean_content = splitter.feed(raw_content_delta)
                if tag_think:
                    _emit_thinking(tag_think)
                if clean_content:
                    content_parts.append(clean_content)

            raw_tcs = delta.get("tool_calls")
            if isinstance(raw_tcs, list):
                for tc_delta in raw_tcs:
                    if not isinstance(tc_delta, dict):
                        continue
                    _mark_first_token()
                    idx = int(tc_delta.get("index", 0) or 0)
                    entry = tool_call_acc.setdefault(
                        idx,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if tc_delta.get("id"):
                        entry["id"] = str(tc_delta["id"])
                    fn = tc_delta.get("function")
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            entry["function"]["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += str(fn["arguments"])

        tail_think, tail_content = splitter.flush()
        if tail_think:
            _emit_thinking(tail_think)
        if tail_content:
            content_parts.append(tail_content)
    finally:
        printer.finish()

    end_ts = time.perf_counter()
    elapsed_ms = (end_ts - req_start) * 1000.0
    ttft_ms = (first_token_ts - req_start) * 1000.0 if first_token_ts is not None else elapsed_ms
    gen_seconds = max(0.001, end_ts - (first_token_ts if first_token_ts is not None else req_start))

    tool_calls: list[dict[str, Any]] = []
    for idx in sorted(tool_call_acc.keys()):
        entry = tool_call_acc[idx]
        if entry["function"]["name"]:
            if not entry["id"]:
                entry["id"] = f"call_{idx}"
            tool_calls.append(entry)

    return AIResponse(
        content="".join(content_parts).strip(),
        thinking="".join(thinking_parts).strip(),
        usage=usage,
        streamed_live=printer.streamed_live,
        tool_calls=tool_calls,
        ttft_ms=ttft_ms,
        gen_seconds=gen_seconds,
        elapsed_ms=elapsed_ms,
    )


# ── OpenAI Responses API (`/v1/responses`) 解析 ───────────────

def _sanitize_reasoning_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """提取可安全回传给 Responses API 下一轮 `input` 的 `reasoning` 项。"""
    if item.get("type") != "reasoning":
        return None
    item_id = item.get("id")
    encrypted = item.get("encrypted_content")
    summary = item.get("summary")
    if not item_id and not encrypted:
        return None
    cleaned: dict[str, Any] = {"type": "reasoning"}
    if item_id:
        cleaned["id"] = str(item_id)
    cleaned["summary"] = summary if isinstance(summary, list) else []
    if encrypted:
        cleaned["encrypted_content"] = encrypted
    return cleaned


def _parse_responses_non_stream(
    res_data: dict[str, Any],
    *,
    req_start: float,
) -> AIResponse:
    now = time.perf_counter()
    elapsed_ms = (now - req_start) * 1000.0
    usage = res_data.get("usage") or {}
    thinking_parts: list[str] = []
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []

    for item in res_data.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            sanitized = _sanitize_reasoning_item(item)
            if sanitized is not None:
                reasoning_items.append(sanitized)
            for part in item.get("summary") or []:
                if isinstance(part, dict) and part.get("text"):
                    thinking_parts.append(str(part["text"]))
        elif itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("text"):
                    content_parts.append(str(part["text"]))
        elif itype == "function_call":
            tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or "call_0"),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )

    if not content_parts and isinstance(res_data.get("output_text"), str):
        content_parts.append(res_data["output_text"])

    return AIResponse(
        content="".join(content_parts).strip(),
        thinking="\n".join(thinking_parts).strip(),
        usage=usage if isinstance(usage, dict) else {},
        streamed_live=False,
        tool_calls=tool_calls,
        reasoning_items=reasoning_items,
        ttft_ms=elapsed_ms,
        gen_seconds=max(0.001, now - req_start),
        elapsed_ms=elapsed_ms,
    )


def _consume_responses_sse(
    resp: Any,
    *,
    req_start: float,
    show_thinking: bool,
    live_stdout: bool,
    on_thinking_delta: Callable[[str], None] | None = None,
) -> AIResponse:
    """解析 OpenAI `/v1/responses` SSE 流（含 reasoning_summary_text / output_text / function_call）。"""
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type and "text/event-stream" not in content_type:
        non_stream = _parse_responses_non_stream(resp.json(), req_start=req_start)
        if non_stream.thinking and on_thinking_delta is not None:
            on_thinking_delta(non_stream.thinking)
        return non_stream

    printer = _ThinkingStreamPrinter(enabled=show_thinking, live=live_stdout)
    splitter = ThinkTagSplitter()
    thinking_parts: list[str] = []
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    reasoning_items: list[dict[str, Any]] = []
    seen_reasoning_ids: set[str] = set()
    usage: dict[str, Any] = {}
    first_token_ts: float | None = None

    def _mark_first_token() -> None:
        nonlocal first_token_ts
        if first_token_ts is None:
            first_token_ts = time.perf_counter()

    def _emit_thinking(piece: str) -> None:
        if not piece:
            return
        _mark_first_token()
        thinking_parts.append(piece)
        printer.write(piece)
        if on_thinking_delta is not None:
            on_thinking_delta(piece)

    try:
        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = (
                raw_line.decode("utf-8", errors="replace").strip()
                if isinstance(raw_line, bytes)
                else str(raw_line).strip()
            )
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue

            try:
                ev = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            # 过滤 sub2api 网关首包占位块
            if ev.get("sub2api_placeholder"):
                continue

            ev_type = str(ev.get("type") or "")
            if ev_type in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            ):
                delta = ev.get("delta")
                if isinstance(delta, str) and delta:
                    _emit_thinking(delta)
            elif ev_type == "response.reasoning_summary_part.done":
                if thinking_parts and not thinking_parts[-1].endswith("\n"):
                    _emit_thinking("\n")
            elif ev_type == "response.output_text.delta":
                delta = ev.get("delta")
                if isinstance(delta, str) and delta:
                    _mark_first_token()
                    tag_think, clean_content = splitter.feed(delta)
                    if tag_think:
                        _emit_thinking(tag_think)
                    if clean_content:
                        content_parts.append(clean_content)
            elif ev_type == "response.function_call_arguments.delta":
                _mark_first_token()
            elif ev_type == "response.output_item.done":
                item = ev.get("item")
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    sanitized = _sanitize_reasoning_item(item)
                    if sanitized is not None:
                        rid = str(sanitized.get("id") or f"r_{len(reasoning_items)}")
                        if rid not in seen_reasoning_ids:
                            seen_reasoning_ids.add(rid)
                            reasoning_items.append(sanitized)
                elif isinstance(item, dict) and item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}")
                    if call_id not in seen_call_ids:
                        seen_call_ids.add(call_id)
                        tool_calls.append(
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": str(item.get("name") or ""),
                                    "arguments": str(item.get("arguments") or "{}"),
                                },
                            }
                        )
            elif ev_type == "response.completed":
                resp_obj = ev.get("response")
                if isinstance(resp_obj, dict):
                    u = resp_obj.get("usage")
                    if isinstance(u, dict) and u:
                        usage = u
                    for item in resp_obj.get("output") or []:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "reasoning" and not reasoning_items:
                            sanitized = _sanitize_reasoning_item(item)
                            if sanitized is not None:
                                rid = str(sanitized.get("id") or f"r_{len(reasoning_items)}")
                                if rid not in seen_reasoning_ids:
                                    seen_reasoning_ids.add(rid)
                                    reasoning_items.append(sanitized)
                        elif item.get("type") == "function_call":
                            call_id = str(item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}")
                            if call_id not in seen_call_ids:
                                seen_call_ids.add(call_id)
                                tool_calls.append(
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": str(item.get("name") or ""),
                                            "arguments": str(item.get("arguments") or "{}"),
                                        },
                                    }
                                )
            elif ev_type in ("response.failed", "error"):
                err_obj = ev.get("error") or (ev.get("response") or {}).get("error") or ev
                raise OSError(f"Responses API 错误: {str(err_obj)[:220]}")

        tail_think, tail_content = splitter.flush()
        if tail_think:
            _emit_thinking(tail_think)
        if tail_content:
            content_parts.append(tail_content)
    finally:
        printer.finish()

    end_ts = time.perf_counter()
    elapsed_ms = (end_ts - req_start) * 1000.0
    ttft_ms = (first_token_ts - req_start) * 1000.0 if first_token_ts is not None else elapsed_ms
    gen_seconds = max(0.001, end_ts - (first_token_ts if first_token_ts is not None else req_start))

    return AIResponse(
        content="".join(content_parts).strip(),
        thinking="".join(thinking_parts).strip(),
        usage=usage,
        streamed_live=printer.streamed_live,
        tool_calls=tool_calls,
        reasoning_items=reasoning_items,
        ttft_ms=ttft_ms,
        gen_seconds=gen_seconds,
        elapsed_ms=elapsed_ms,
    )


def _adapt_chat_on_client_error(
    payload: dict[str, Any],
    cap: _EndpointCapabilities,
    error_body: str,
    log: Any,
) -> tuple[bool, bool]:
    low = error_body.lower()
    changed = False
    tools_rejected = False

    if "stream_options" in payload and ("stream_options" in low or "include_usage" in low):
        payload.pop("stream_options", None)
        cap.supports_stream_options = False
        log.warning("接口不支持 stream_options，已缓存该能力并自动移除重试")
        changed = True

    if "temperature" in payload and "temperature" in low:
        payload.pop("temperature", None)
        cap.supports_temperature = False
        log.warning("接口不支持当前模型设置 temperature，已缓存该能力并自动移除重试")
        changed = True

    if "response_format" in payload and (
        "response_format" in low or "json_object" in low
    ):
        payload.pop("response_format", None)
        cap.supports_response_format = False
        log.warning("接口不支持 response_format，已缓存该能力并自动移除重试")
        changed = True

    if "tools" in payload and (
        "tools" in low or "tool_choice" in low or "function" in low
    ):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        cap.supports_tools = False
        tools_rejected = True
        log.warning("接口不支持 Function Calling (tools)，已自动切换为本地 BM25 检索模式")
        changed = True

    if "reasoning_effort" in payload and "reasoning_effort" in low:
        payload.pop("reasoning_effort", None)
        cap.supports_reasoning_effort = False
        log.warning("接口不支持 reasoning_effort 参数，已自动移除并重试")
        changed = True

    if payload.get("stream") and "stream" in low and "stream_options" not in low and "reasoning" not in low:
        payload.pop("stream", None)
        payload.pop("stream_options", None)
        cap.supports_stream = False
        log.warning("接口不支持流式输出，已自动回退为非流式重试")
        changed = True

    return changed, tools_rejected


def _adapt_responses_on_client_error(
    payload: dict[str, Any],
    cap: _EndpointCapabilities,
    error_body: str,
    log: Any,
) -> tuple[bool, bool]:
    low = error_body.lower()
    changed = False
    tools_rejected = False

    if (
        isinstance(payload.get("reasoning"), dict)
        and "summary" in payload["reasoning"]
        and "summary" in low
    ):
        payload["reasoning"].pop("summary", None)
        cap.supports_reasoning_summary = False
        if not payload["reasoning"]:
            payload.pop("reasoning", None)
        log.warning("Responses 接口不支持 reasoning.summary，已自动移除重试")
        changed = True

    if "reasoning" in payload and "reasoning" in low and "summary" not in low:
        payload.pop("reasoning", None)
        cap.supports_reasoning_effort = False
        log.warning("Responses 接口不支持 reasoning 参数，已自动移除重试")
        changed = True

    if "tools" in payload and ("tools" in low or "function" in low):
        payload.pop("tools", None)
        cap.supports_tools = False
        tools_rejected = True
        log.warning("Responses 接口不支持 tools，已自动切换为本地 BM25 检索模式")
        changed = True

    # 若中转网关拒收回传的 reasoning input item，自动剥离后重试
    if isinstance(payload.get("input"), list) and any(
        isinstance(item, dict) and item.get("type") == "reasoning"
        for item in payload["input"]
    ) and ("reasoning" in low or "input" in low or "item" in low):
        payload["input"] = [
            item
            for item in payload["input"]
            if not (isinstance(item, dict) and item.get("type") == "reasoning")
        ]
        log.warning("Responses 接口拒收回传 reasoning 项，已自动剥离并重试")
        changed = True

    return changed, tools_rejected


def _safe_parse_int(
    value: Any,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """宽容解析 LLM 工具调用中的整数参数（容忍数字字符串、'第3段' 等格式异常）。"""
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = int(text)
        except ValueError:
            match = re.search(r"-?\d+", text)
            if match:
                try:
                    parsed = int(match.group(0))
                except ValueError:
                    parsed = None
    result = default if parsed is None else parsed
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


def _execute_kb_tool(
    kb: KnowledgeBase,
    tool_call: dict[str, Any],
    *,
    default_top_k: int,
) -> tuple[str, dict[str, Any]]:
    t0 = time.perf_counter()
    fn = tool_call.get("function") or {}
    name = str(fn.get("name") or "")
    raw_args = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        if not isinstance(args, dict):
            args = {}
    except (json.JSONDecodeError, TypeError, ValueError):
        args = {}

    if name == "search_knowledge":
        raw_query = args.get("query")
        query = (
            " ".join(str(x) for x in raw_query).strip()
            if isinstance(raw_query, list)
            else str(raw_query or "").strip()
        )
        top_k = _safe_parse_int(args.get("top_k"), default_top_k, min_value=1, max_value=10)
        if not query:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return json.dumps(
                {"error": "query 不能为空，请传入检索关键词", "hits": []},
                ensure_ascii=False,
            ), {
                "name": name,
                "query": "",
                "hits": 0,
                "sources": [],
                "duration_ms": duration_ms,
            }
        hits = kb.search_chunks(query, top_k=top_k)
        sources = list(dict.fromkeys(h["source"] for h in hits))
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        payload = json.dumps({"query": query, "hits": hits}, ensure_ascii=False)
        event = {
            "name": name,
            "query": query,
            "hits": len(hits),
            "sources": sources,
            "duration_ms": duration_ms,
        }
        return payload, event

    if name == "read_document_chunk":
        source = str(args.get("source") or "").strip()
        chunk_index = _safe_parse_int(args.get("chunk_index"), 1, min_value=1)
        window = _safe_parse_int(args.get("window"), 1, min_value=0, max_value=2)
        chunks = kb.get_chunk(source, chunk_index, window=window)
        sources = list(dict.fromkeys(c["source"] for c in chunks))
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        resp_obj: dict[str, Any] = {
            "source": source,
            "chunk_index": chunk_index,
            "chunks": chunks,
        }
        if not chunks:
            resp_obj["hint"] = (
                "未找到匹配段落，可用文档名："
                + ", ".join(doc.source for doc in kb.documents[:10])
            )
        payload = json.dumps(resp_obj, ensure_ascii=False)
        event = {
            "name": name,
            "query": f"{source}#{chunk_index}",
            "hits": len(chunks),
            "sources": sources,
            "duration_ms": duration_ms,
        }
        return payload, event

    duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False), {
        "name": name,
        "query": "",
        "hits": 0,
        "sources": [],
        "duration_ms": duration_ms,
    }


def _send_chat_round(
    *,
    session: Any,
    url: str,
    headers: dict[str, str],
    model: str,
    messages: list[dict[str, Any]],
    reasoning_effort: str,
    stream_enabled: bool,
    show_thinking: bool,
    live_stdout: bool,
    timeout: int,
    max_retries: int,
    use_tools: bool,
    cap: _EndpointCapabilities,
    log: Any,
    on_thinking_delta: Callable[[str], None] | None,
) -> tuple[AIResponse | None, bool, str]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if cap.supports_temperature:
        payload["temperature"] = 0
    if use_tools and cap.supports_tools:
        payload["tools"] = KB_CHAT_TOOLS_SCHEMA
        payload["tool_choice"] = "auto"
    elif cap.supports_response_format:
        payload["response_format"] = {"type": "json_object"}
    if reasoning_effort and cap.supports_reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if stream_enabled and cap.supports_stream:
        payload["stream"] = True
        if cap.supports_stream_options:
            payload["stream_options"] = {"include_usage": True}

    last_error = ""
    for attempt in range(1, max_retries + 1):
        compat_adjustments = 0
        while True:
            use_stream = bool(payload.get("stream"))
            req_start = time.perf_counter()
            try:
                with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    stream=use_stream,
                ) as resp:
                    if resp.status_code in (400, 422) and compat_adjustments < 5:
                        err_text = resp.text or ""
                        changed, tools_rejected = _adapt_chat_on_client_error(
                            payload, cap, err_text, log
                        )
                        if tools_rejected:
                            return None, True, err_text[:240]
                        if changed:
                            compat_adjustments += 1
                            continue
                    if resp.status_code >= 400:
                        body_snippet = (resp.text or "").strip().replace("\n", " ")[:220]
                        raise OSError(f"HTTP {resp.status_code}: {body_snippet}")
                    if use_stream:
                        ai_resp = _consume_chat_sse(
                            resp,
                            req_start=req_start,
                            show_thinking=show_thinking,
                            live_stdout=live_stdout,
                            on_thinking_delta=on_thinking_delta,
                        )
                    else:
                        ai_resp = _parse_chat_non_stream(
                            resp.json(), req_start=req_start
                        )
                        if ai_resp.thinking and on_thinking_delta is not None:
                            on_thinking_delta(ai_resp.thinking)
                    return ai_resp, False, ""
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                last_error = str(e)
                if attempt < max_retries:
                    wait = attempt * 2
                    log.warning(f"AI(chat) 第 {attempt} 次请求失败，{wait}s 后重试：{e}")
                    time.sleep(wait)
                else:
                    log.error(f"AI(chat) 请求失败（已重试 {max_retries} 次）：{e}")
                    return None, False, last_error
                break
    return None, False, last_error


def _send_responses_round(
    *,
    session: Any,
    url: str,
    headers: dict[str, str],
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    reasoning_effort: str,
    stream_enabled: bool,
    show_thinking: bool,
    live_stdout: bool,
    timeout: int,
    max_retries: int,
    use_tools: bool,
    cap: _EndpointCapabilities,
    log: Any,
    on_thinking_delta: Callable[[str], None] | None,
) -> tuple[AIResponse | None, bool, bool, str]:
    """发送单轮 OpenAI `/v1/responses` 请求。

    返回 `(ai_resp, unsupported_endpoint, tools_rejected, last_error)`。
    """
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
    }
    if use_tools and cap.supports_tools:
        payload["tools"] = KB_RESPONSES_TOOLS_SCHEMA
    if reasoning_effort and cap.supports_reasoning_effort:
        reasoning_obj: dict[str, Any] = {"effort": reasoning_effort}
        if cap.supports_reasoning_summary and reasoning_effort.lower() != "none":
            reasoning_obj["summary"] = "detailed"
        payload["reasoning"] = reasoning_obj
    if stream_enabled and cap.supports_stream:
        payload["stream"] = True

    last_error = ""
    for attempt in range(1, max_retries + 1):
        compat_adjustments = 0
        while True:
            use_stream = bool(payload.get("stream"))
            req_start = time.perf_counter()
            try:
                with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                    stream=use_stream,
                ) as resp:
                    if resp.status_code in (404, 405, 501):
                        cap.supports_responses = False
                        return None, True, False, f"HTTP {resp.status_code}"
                    if resp.status_code in (400, 422) and compat_adjustments < 4:
                        err_text = resp.text or ""
                        low = err_text.lower()
                        if "not found" in low or "unknown url" in low or "invalid url" in low:
                            cap.supports_responses = False
                            return None, True, False, err_text[:200]
                        changed, tools_rejected = _adapt_responses_on_client_error(
                            payload, cap, err_text, log
                        )
                        if tools_rejected:
                            return None, False, True, err_text[:240]
                        if changed:
                            compat_adjustments += 1
                            continue
                    if resp.status_code >= 400:
                        body_snippet = (resp.text or "").strip().replace("\n", " ")[:220]
                        raise OSError(f"HTTP {resp.status_code}: {body_snippet}")
                    cap.supports_responses = True
                    if use_stream:
                        ai_resp = _consume_responses_sse(
                            resp,
                            req_start=req_start,
                            show_thinking=show_thinking,
                            live_stdout=live_stdout,
                            on_thinking_delta=on_thinking_delta,
                        )
                    else:
                        ai_resp = _parse_responses_non_stream(
                            resp.json(), req_start=req_start
                        )
                        if ai_resp.thinking and on_thinking_delta is not None:
                            on_thinking_delta(ai_resp.thinking)
                    return ai_resp, False, False, ""
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                last_error = str(e)
                if attempt < max_retries:
                    wait = attempt * 2
                    log.warning(f"AI(responses) 第 {attempt} 次请求失败，{wait}s 后重试：{e}")
                    time.sleep(wait)
                else:
                    log.error(f"AI(responses) 请求失败（已重试 {max_retries} 次）：{e}")
                    return None, False, False, last_error
                break
    return None, False, False, last_error


def _resolve_protocol(base_url: str, api_mode: str, cap: _EndpointCapabilities) -> str:
    """根据用户配置、URL 后缀与探测缓存决定优先使用 `responses` 还是 `chat`。"""
    mode = (api_mode or "auto").strip().lower()
    if mode == "responses":
        return "responses"
    if mode == "chat":
        return "chat"
    low = base_url.strip().rstrip("/").lower()
    if low.endswith("/chat/completions"):
        return "chat"
    if low.endswith("/responses"):
        return "responses"
    if cap.supports_responses is False:
        return "chat"
    return "responses"


def solve_question_with_ai(
    *,
    session: Any,
    ai_config: dict[str, Any],
    question: dict[str, Any],
    base_path: str,
    data_dir: str | None,
    log: Any,
    warned_key_state: list[bool],
    reasoning_effort_override: str | None = None,
    on_thinking_delta: Callable[[str], None] | None = None,
    on_kb_result: Callable[[RetrievalResult], None] | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    suppress_stdout_stream: bool = False,
) -> AISolveResult:
    """执行一次完整的 Agentic AI 搜题，返回答案、思考、工具轨迹与完整遥测。"""
    if not ai_config or not parse_bool(ai_config.get("enable"), default=False):
        return AISolveResult(error="AI 搜题未启用")

    api_key = str(ai_config.get("api_key", "")).strip()
    base_url = str(ai_config.get("base_url", "https://api.deepseek.com")).strip()
    model = str(ai_config.get("model", "deepseek-v4-pro")).strip()
    api_mode = str(ai_config.get("api_mode", "auto") or "auto").strip().lower()
    timeout = int(ai_config.get("timeout", 60))
    max_retries = max(1, int(ai_config.get("max_retries", 2)))
    if reasoning_effort_override is not None:
        reasoning_effort = reasoning_effort_override.strip()
    else:
        reasoning_effort = str(ai_config.get("reasoning_effort", "") or "").strip()
    stream_enabled = parse_bool(ai_config.get("stream"), default=True)
    show_thinking = parse_bool(ai_config.get("show_thinking"), default=True)
    knowledge_enable = parse_bool(ai_config.get("knowledge_enable"), default=True)
    knowledge_paths = ai_config.get("knowledge_paths")
    knowledge_max_chars = int(ai_config.get("knowledge_max_chars", 12000))
    knowledge_top_k = int(ai_config.get("knowledge_top_k", 5))

    chat_url = normalize_chat_url(base_url)
    responses_url = normalize_responses_url(base_url)
    endpoint_host = extract_endpoint_host(base_url)
    cap = _get_capabilities(base_url, model)
    protocol = _resolve_protocol(base_url, api_mode, cap)

    if not api_key and not warned_key_state[0]:
        log.warning("AI 搜题未配置 api_key（若为本地/免密接口可忽略）")
        warned_key_state[0] = True

    title = str(question.get("title", ""))
    type_label = str(question.get("typeLabel", "未知"))
    options = question.get("optionList", [])
    opt_count = len(options)

    # ── 1. 知识库协作模式 ──
    kb: KnowledgeBase | None = None
    kb_mode = "empty"
    kb_sources: list[str] = []
    bm25_context = ""

    if knowledge_enable:
        kb = load_knowledge_base(
            extra_paths=knowledge_paths,
            base_path=base_path,
            data_dir=data_dir,
            log=log,
        )
        if not kb.is_empty:
            if kb.total_chars <= max(500, knowledge_max_chars):
                kb_mode = "full_cached"
                kb_sources = [doc.source for doc in kb.documents]
                if on_kb_result is not None:
                    on_kb_result(
                        RetrievalResult(
                            context="",
                            mode="full_cached",
                            sources=list(kb_sources),
                            total_docs=len(kb.documents),
                            total_chars=kb.total_chars,
                            matched_chunks=len(kb.documents),
                        )
                    )
            elif cap.supports_tools:
                kb_mode = "agent_tools"
                if on_kb_result is not None:
                    on_kb_result(
                        RetrievalResult(
                            context="",
                            mode="agent_tools",
                            sources=[],
                            total_docs=len(kb.documents),
                            total_chars=kb.total_chars,
                            matched_chunks=0,
                        )
                    )
            else:
                retrieval = kb.retrieve(
                    title,
                    [str(opt.get("content", "")) for opt in options],
                    max_chars=knowledge_max_chars,
                    top_k=knowledge_top_k,
                )
                kb_mode = retrieval.mode
                kb_sources = list(retrieval.sources)
                bm25_context = retrieval.context
                if on_kb_result is not None:
                    on_kb_result(retrieval)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    effort_tag = f" [effort={reasoning_effort}]" if reasoning_effort else ""
    log.info(f"AI({model}@{endpoint_host}/{protocol}){effort_tag}：{title[:40]}...")

    live_stdout = False if suppress_stdout_stream else _can_live_stream_stdout()
    solve_start = time.perf_counter()

    # ── 2. 构造两套消息状态并执行 Agent 循环 ──
    sys_prompt = build_system_prompt(kb=kb, mode=kb_mode)
    user_prompt = build_user_prompt(
        title=title,
        type_label=type_label,
        options=options,
        bm25_context=bm25_context,
    )
    chat_messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]
    resp_input: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt},
    ]

    all_thinking: list[str] = []
    tool_events: list[dict[str, Any]] = []
    final_content = ""
    any_streamed_live = False

    first_ttft_ms: float | None = None
    total_gen_seconds = 0.0
    sum_prompt_tokens = 0
    sum_completion_tokens = 0
    sum_cached_tokens = 0
    sum_reasoning_tokens = 0
    sum_total_tokens = 0
    any_api_usage = False
    rounds = 0
    last_error = ""
    last_raw_usage: dict[str, Any] = {}

    def _switch_to_bm25() -> None:
        nonlocal kb_mode, kb_sources, bm25_context, sys_prompt, user_prompt, chat_messages, resp_input
        assert kb is not None
        retrieval = kb.retrieve(
            title,
            [str(opt.get("content", "")) for opt in options],
            max_chars=knowledge_max_chars,
            top_k=knowledge_top_k,
        )
        kb_mode = retrieval.mode
        kb_sources = list(retrieval.sources)
        bm25_context = retrieval.context
        if on_kb_result is not None:
            on_kb_result(retrieval)
        sys_prompt = build_system_prompt(kb=kb, mode=kb_mode)
        user_prompt = build_user_prompt(
            title=title,
            type_label=type_label,
            options=options,
            bm25_context=bm25_context,
        )
        chat_messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]
        resp_input = [{"role": "user", "content": user_prompt}]

    step = 0
    while step <= _MAX_TOOL_ROUNDS:
        allow_tools = kb_mode == "agent_tools" and kb is not None and step < _MAX_TOOL_ROUNDS

        if protocol == "responses":
            ai_resp, unsupported_ep, tools_rejected, err = _send_responses_round(
                session=session,
                url=responses_url,
                headers=headers,
                model=model,
                instructions=sys_prompt,
                input_items=resp_input,
                reasoning_effort=reasoning_effort,
                stream_enabled=stream_enabled,
                show_thinking=show_thinking and not suppress_stdout_stream,
                live_stdout=live_stdout,
                timeout=timeout,
                max_retries=max_retries,
                use_tools=allow_tools,
                cap=cap,
                log=log,
                on_thinking_delta=on_thinking_delta,
            )
            if unsupported_ep and api_mode == "auto":
                log.info(f"接口 {endpoint_host} 未提供 /responses，已自动回退 /chat/completions")
                protocol = "chat"
                continue
        else:
            ai_resp, tools_rejected, err = _send_chat_round(
                session=session,
                url=chat_url,
                headers=headers,
                model=model,
                messages=chat_messages,
                reasoning_effort=reasoning_effort,
                stream_enabled=stream_enabled,
                show_thinking=show_thinking and not suppress_stdout_stream,
                live_stdout=live_stdout,
                timeout=timeout,
                max_retries=max_retries,
                use_tools=allow_tools,
                cap=cap,
                log=log,
                on_thinking_delta=on_thinking_delta,
            )

        if tools_rejected and kb is not None:
            _switch_to_bm25()
            continue

        if ai_resp is None:
            last_error = err or "请求失败"
            break

        rounds += 1
        step += 1
        if first_ttft_ms is None and ai_resp.ttft_ms is not None:
            first_ttft_ms = ai_resp.ttft_ms
        total_gen_seconds += ai_resp.gen_seconds
        any_streamed_live = any_streamed_live or ai_resp.streamed_live
        if ai_resp.thinking:
            all_thinking.append(ai_resp.thinking)

        p_tok, c_tok, cache_tok, r_tok, t_tok = _extract_usage_numbers(ai_resp.usage)
        if p_tok or c_tok or t_tok:
            any_api_usage = True
            last_raw_usage = ai_resp.usage
            sum_prompt_tokens += p_tok
            sum_completion_tokens += c_tok
            sum_cached_tokens += cache_tok
            sum_reasoning_tokens += r_tok
            sum_total_tokens += t_tok

        if ai_resp.tool_calls and kb is not None and step <= _MAX_TOOL_ROUNDS:
            if protocol == "responses" and ai_resp.reasoning_items:
                resp_input.extend(ai_resp.reasoning_items)
            if protocol == "chat":
                chat_messages.append(
                    {
                        "role": "assistant",
                        "content": ai_resp.content or None,
                        "tool_calls": ai_resp.tool_calls,
                    }
                )
            for tc in ai_resp.tool_calls:
                tool_output, ev = _execute_kb_tool(kb, tc, default_top_k=knowledge_top_k)
                tool_events.append(ev)
                for src in ev.get("sources", []):
                    if src not in kb_sources:
                        kb_sources.append(src)
                if on_tool_event is not None:
                    on_tool_event(ev)
                if on_kb_result is not None:
                    on_kb_result(
                        RetrievalResult(
                            context="",
                            mode="agent_tools",
                            sources=list(kb_sources),
                            total_docs=len(kb.documents),
                            total_chars=kb.total_chars,
                            matched_chunks=sum(e.get("hits", 0) for e in tool_events),
                        )
                    )
                call_id = str(tc.get("id") or "call_0")
                fn = tc.get("function") or {}
                if protocol == "responses":
                    resp_input.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": str(fn.get("name") or ""),
                            "arguments": str(fn.get("arguments") or "{}"),
                        }
                    )
                    resp_input.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": tool_output,
                        }
                    )
                else:
                    chat_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_output,
                        }
                    )
            continue

        final_content = ai_resp.content
        break

    # ── 3. 汇总遥测指标 ──
    combined_thinking = "\n".join(all_thinking).strip()
    elapsed_ms = (time.perf_counter() - solve_start) * 1000.0
    if protocol == "responses":
        context_chars = len(sys_prompt) + _messages_char_len(resp_input)
        est_context_tokens = estimate_tokens(
            sys_prompt + "\n" + "\n".join(str(m.get("content") or m.get("output") or "") for m in resp_input)
        )
    else:
        context_chars = _messages_char_len(chat_messages)
        est_context_tokens = estimate_tokens(
            "\n".join(str(m.get("content") or "") for m in chat_messages)
        )

    usage_estimated = not any_api_usage
    if usage_estimated:
        sum_prompt_tokens = est_context_tokens
        sum_completion_tokens = estimate_tokens(combined_thinking + "\n" + final_content)
        sum_total_tokens = sum_prompt_tokens + sum_completion_tokens

    tps = (
        sum_completion_tokens / total_gen_seconds
        if sum_completion_tokens > 0 and total_gen_seconds > 0
        else None
    )
    last_prompt_tok = _extract_usage_numbers(last_raw_usage)[0] if any_api_usage else 0
    context_tokens = last_prompt_tok or est_context_tokens

    metrics = AIMetrics(
        endpoint_host=endpoint_host,
        api_protocol=protocol,
        model=model,
        reasoning_effort=reasoning_effort,
        ttft_ms=first_ttft_ms,
        tps=tps,
        elapsed_ms=elapsed_ms,
        prompt_tokens=sum_prompt_tokens,
        completion_tokens=sum_completion_tokens,
        cached_tokens=sum_cached_tokens,
        reasoning_tokens=sum_reasoning_tokens,
        total_tokens=sum_total_tokens,
        usage_estimated=usage_estimated,
        context_chars=context_chars,
        context_tokens=context_tokens,
        rounds=rounds,
    )

    if last_error and not final_content:
        return AISolveResult(
            thinking=combined_thinking,
            kb_mode=kb_mode,
            kb_sources=kb_sources,
            tool_events=tool_events,
            usage=last_raw_usage,
            metrics=metrics,
            model=model,
            reasoning_effort=reasoning_effort,
            error=last_error,
        )

    ttft_str = f"{first_ttft_ms:.0f}ms" if first_ttft_ms is not None else "-"
    tps_str = f"{tps:.1f}t/s" if tps is not None else "-"
    cache_str = f", cached={sum_cached_tokens}" if sum_cached_tokens else ""
    reason_str = f", reasoning_tok={sum_reasoning_tokens}" if sum_reasoning_tokens else ""
    log.debug(
        f"AI 遥测 [{protocol}] — model={model}, TTFT={ttft_str}, TPS={tps_str}, "
        f"tokens={sum_prompt_tokens}+{sum_completion_tokens}={sum_total_tokens}{cache_str}{reason_str}, "
        f"ctx={context_tokens}t/{context_chars}c, rounds={rounds}"
    )

    if combined_thinking:
        summary = summarize_thinking(combined_thinking)
        if any_streamed_live or suppress_stdout_stream or not show_thinking:
            log.bind(file_only=True).info(
                f"AI 思考摘要（共 {len(combined_thinking)} 字）：{summary}"
            )
        else:
            log.info(f"AI 思考过程（共 {len(combined_thinking)} 字）：{summary}")

    if not final_content:
        log.warning("AI 返回正文为空")
        return AISolveResult(
            thinking=combined_thinking,
            kb_mode=kb_mode,
            kb_sources=kb_sources,
            tool_events=tool_events,
            usage=last_raw_usage,
            metrics=metrics,
            model=model,
            reasoning_effort=reasoning_effort,
            error="AI 返回正文为空",
        )

    raw_indices, reason = parse_ai_response(final_content)
    if raw_indices is None:
        log.warning(f"AI 返回内容解析失败，原始内容：{final_content[:200]}")
        return AISolveResult(
            thinking=combined_thinking,
            kb_mode=kb_mode,
            kb_sources=kb_sources,
            tool_events=tool_events,
            usage=last_raw_usage,
            metrics=metrics,
            model=model,
            reasoning_effort=reasoning_effort,
            error=f"返回内容无法解析为有效答案：{final_content[:120]}",
        )

    valid_ids: list[Any] = []
    valid_indices: list[int] = []
    for idx in raw_indices:
        try:
            val = int(idx)
        except (ValueError, TypeError):
            continue
        if 1 <= val <= opt_count:
            if val not in valid_indices:
                valid_indices.append(val)
                valid_ids.append(options[val - 1]["id"])
                log.info(f"AI 推荐：{val}. {options[val - 1]['content']}")
        else:
            log.warning(f"AI 返回的选项序号 {val} 超出范围 1~{opt_count}，忽略")

    if reason:
        log.info(f"AI 理由：{reason}")

    # 单选题/判断题：提示词允许模型对比多个候选，但最终只提交最恰当的首项以防接口报错
    question_type = question.get("type")
    if question_type is not None and int(question_type) != 2 and len(valid_ids) > 1:
        log.warning(
            f"AI 对非多选题返回了多个候选 {valid_indices}，已采用最恰当的首项 {valid_indices[0]}"
        )
        valid_ids = [valid_ids[0]]
        valid_indices = [valid_indices[0]]

    error_msg = ""
    if not valid_ids:
        log.warning("AI 返回的答案未能匹配任何有效选项")
        error_msg = "AI 返回的答案未能匹配任何有效选项"

    return AISolveResult(
        selected_ids=valid_ids,
        selected_indices=valid_indices,
        reason=reason,
        thinking=combined_thinking,
        kb_mode=kb_mode,
        kb_sources=kb_sources,
        tool_events=tool_events,
        usage=last_raw_usage,
        metrics=metrics,
        model=model,
        reasoning_effort=reasoning_effort,
        error=error_msg,
    )


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
    """执行一次完整的 AI 搜题（含双协议自适应、知识库协作、思考强度、流式思考与遥测）。"""
    return solve_question_with_ai(
        session=session,
        ai_config=ai_config,
        question=question,
        base_path=base_path,
        data_dir=data_dir,
        log=log,
        warned_key_state=warned_key_state,
    ).selected_ids
