"""WeBan 交互式答题与人工核对控制台（本地 HTTP + SSE 服务）。

核心设计原则：
1. 自动执行：打开考试后自动完成题库匹配、Agentic AI 搜题（含实时流式思考、工具检索轨迹与遥测）和单题同步；
2. 键鼠双控、随时人工纠正：支持鼠标与键盘快捷键随时暂停、切题、修改选项、重跑 AI，且绝不覆盖人工修改；
3. 绝不自动提交：全部作答完成后进入待交卷状态，必须人工二次确认后才调用交卷接口。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from random import randint
from typing import Any

import requests
from loguru import logger

from weban.ai_client import (
    AIMetrics,
    AISolveResult,
    extract_endpoint_host,
    parse_bool,
    solve_question_with_ai,
)
from weban.exam_ui_page import EXAM_UI_HTML
from weban.knowledge import RetrievalResult

AISolverFn = Callable[
    [
        dict[str, Any],
        str | None,
        Callable[[str], None],
        Callable[[RetrievalResult], None],
        Callable[[dict[str, Any]], None],
    ],
    AISolveResult,
]
RecordAnswerFn = Callable[[str, int, list[str]], bool]
SubmitExamFn = Callable[[], dict[str, Any]]


@dataclass
class QuestionState:
    """单道考试题在交互控制台中的实时状态。"""

    index: int
    id: str
    title: str
    type: int
    type_label: str
    options: list[dict[str, str]]
    raw_question: dict[str, Any] = field(repr=False)
    status: str = "pending"
    source: str = "none"  # none | bank | ai | manual
    selected_ids: list[str] = field(default_factory=list)
    ai_suggested_ids: list[str] = field(default_factory=list)
    thinking: str = ""
    reason: str = ""
    kb_mode: str = "empty"
    kb_sources: list[str] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    synced: bool = False
    sync_error: str = ""
    reviewed: bool = False
    flagged: bool = False
    error: str = ""
    use_time: int = 3
    edit_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "type_label": self.type_label,
            "options": self.options,
            "status": self.status,
            "source": self.source,
            "selected_ids": list(self.selected_ids),
            "ai_suggested_ids": list(self.ai_suggested_ids),
            "thinking": self.thinking,
            "reason": self.reason,
            "kb_mode": self.kb_mode,
            "kb_sources": list(self.kb_sources),
            "tool_events": list(self.tool_events),
            "metrics": dict(self.metrics),
            "synced": self.synced,
            "sync_error": self.sync_error,
            "reviewed": self.reviewed,
            "flagged": self.flagged,
            "error": self.error,
            "use_time": self.use_time,
        }


class ExamSessionController:
    """管理单场考试的自动答题、人工纠正、Agent 遥测、SSE 推送与手动交卷流程。"""

    def __init__(
        self,
        *,
        plan_name: str,
        user_label: str,
        paper_score: Any,
        answer_time_minutes: int,
        questions: list[dict[str, Any]],
        bank_answers_map: dict[str, list[str]],
        ai_solver: AISolverFn | None,
        record_fn: RecordAnswerFn,
        submit_fn: SubmitExamFn,
        question_base_time: int = 3,
        question_random_upper: int = 3,
        default_reasoning_effort: str = "",
        ai_model: str = "",
        ai_endpoint_host: str = "",
        ai_enabled: bool = True,
        demo_mode: bool = False,
        mock_mode: bool = False,
        log: Any = logger,
    ) -> None:
        self.plan_name = plan_name
        self.user_label = user_label
        self.paper_score = paper_score
        self.answer_time_minutes = int(answer_time_minutes or 0)
        self.ai_solver = ai_solver
        self.record_fn = record_fn
        self.submit_fn = submit_fn
        self.question_base_time = max(0, int(question_base_time))
        self.question_random_upper = max(0, int(question_random_upper))
        self.default_reasoning_effort = default_reasoning_effort
        self.ai_model = ai_model
        self.ai_endpoint_host = ai_endpoint_host
        self.ai_enabled = ai_enabled and ai_solver is not None
        self.demo_mode = demo_mode
        self.mock_mode = mock_mode
        self.log = log

        self.started_at_ms = int(time.time() * 1000)
        self.phase = "running"
        self.paused = False
        self.active_index: int | None = None
        self.submit_result: dict[str, Any] | None = None
        self.outcome: dict[str, Any] = {"action": "aborted"}

        # 本场累计 LLM 遥测
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cached_tokens = 0
        self._total_reasoning_tokens = 0
        self._total_tokens = 0
        self._api_protocol = ""
        self._ttft_samples: list[float] = []
        self._tps_samples: list[float] = []
        self._last_context_tokens = 0
        self._last_context_chars = 0
        self._ai_calls = 0
        self._any_estimated = False

        self._lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._subscribers: list[queue.Queue[str]] = []
        self._rerunning_indices: set[int] = set()
        self._worker_thread: threading.Thread | None = None

        self.questions: list[QuestionState] = []
        for idx, raw in enumerate(questions):
            qid = str(raw["id"])
            opts = [
                {"id": str(opt["id"]), "content": str(opt.get("content", ""))}
                for opt in raw.get("optionList", [])
            ]
            qtype = int(raw.get("type", 1) or 1)
            type_label = str(
                raw.get("typeLabel") or ("多选题" if qtype == 2 else "单选题")
            )
            bank_ids = [str(x) for x in bank_answers_map.get(qid, [])]
            if bank_ids:
                q_state = QuestionState(
                    index=idx,
                    id=qid,
                    title=str(raw.get("title", "")),
                    type=qtype,
                    type_label=type_label,
                    options=opts,
                    raw_question=raw,
                    status="bank_pending",
                    source="bank",
                    selected_ids=bank_ids,
                    reviewed=True,
                )
            else:
                q_state = QuestionState(
                    index=idx,
                    id=qid,
                    title=str(raw.get("title", "")),
                    type=qtype,
                    type_label=type_label,
                    options=opts,
                    raw_question=raw,
                    status="pending",
                    source="none",
                    selected_ids=[],
                    reviewed=False,
                )
            self.questions.append(q_state)

    # ── SSE Pub/Sub ───────────────────────────────────────────

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self._lock:
            dead: list[queue.Queue[str]] = []
            for sub in self._subscribers:
                try:
                    sub.put_nowait(payload)
                except queue.Full:
                    dead.append(sub)
            for sub in dead:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    def _compute_stats_locked(self) -> dict[str, int]:
        total = len(self.questions)
        answered = 0
        bank = 0
        ai = 0
        manual = 0
        ai_unreviewed = 0
        reviewed_non_bank = 0
        flagged = 0
        synced = 0
        for q in self.questions:
            if q.selected_ids:
                answered += 1
            if q.source == "bank":
                bank += 1
            elif q.source == "ai":
                ai += 1
                if q.reviewed:
                    reviewed_non_bank += 1
                else:
                    ai_unreviewed += 1
            elif q.source == "manual":
                manual += 1
                if q.reviewed:
                    reviewed_non_bank += 1
            if q.flagged:
                flagged += 1
            if q.synced:
                synced += 1
        return {
            "total": total,
            "answered": answered,
            "unanswered": total - answered,
            "bank": bank,
            "ai": ai,
            "manual": manual,
            "ai_unreviewed": ai_unreviewed,
            "reviewed_non_bank": reviewed_non_bank,
            "flagged": flagged,
            "synced": synced,
        }

    def _telemetry_summary_locked(self) -> dict[str, Any]:
        avg_ttft = (
            round(sum(self._ttft_samples) / len(self._ttft_samples), 1)
            if self._ttft_samples
            else None
        )
        avg_tps = (
            round(sum(self._tps_samples) / len(self._tps_samples), 1)
            if self._tps_samples
            else None
        )
        return {
            "calls": self._ai_calls,
            "api_protocol": self._api_protocol,
            "prompt_tokens": self._total_prompt_tokens,
            "completion_tokens": self._total_completion_tokens,
            "cached_tokens": self._total_cached_tokens,
            "reasoning_tokens": self._total_reasoning_tokens,
            "total_tokens": self._total_tokens,
            "avg_ttft_ms": avg_ttft,
            "avg_tps": avg_tps,
            "last_context_tokens": self._last_context_tokens,
            "last_context_chars": self._last_context_chars,
            "usage_estimated": self._any_estimated,
        }

    def _record_metrics_locked(self, metrics: AIMetrics) -> None:
        if metrics.rounds <= 0 and metrics.total_tokens <= 0:
            return
        self._ai_calls += 1
        if metrics.api_protocol:
            self._api_protocol = metrics.api_protocol
        self._total_prompt_tokens += metrics.prompt_tokens
        self._total_completion_tokens += metrics.completion_tokens
        self._total_cached_tokens += metrics.cached_tokens
        self._total_reasoning_tokens += metrics.reasoning_tokens
        self._total_tokens += metrics.total_tokens
        if metrics.ttft_ms is not None:
            self._ttft_samples.append(metrics.ttft_ms)
        if metrics.tps is not None:
            self._tps_samples.append(metrics.tps)
        if metrics.context_tokens:
            self._last_context_tokens = metrics.context_tokens
        if metrics.context_chars:
            self._last_context_chars = metrics.context_chars
        if metrics.usage_estimated:
            self._any_estimated = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "meta": {
                    "plan_name": self.plan_name,
                    "user_label": self.user_label,
                    "paper_score": self.paper_score,
                    "answer_time_minutes": self.answer_time_minutes,
                    "started_at_ms": self.started_at_ms,
                    "default_reasoning_effort": self.default_reasoning_effort,
                    "ai_model": self.ai_model,
                    "ai_endpoint_host": self.ai_endpoint_host,
                    "ai_enabled": self.ai_enabled,
                    "demo_mode": self.demo_mode,
                    "mock_mode": self.mock_mode,
                },
                "phase": self.phase,
                "paused": self.paused,
                "active_index": self.active_index,
                "questions": [q.to_dict() for q in self.questions],
                "stats": self._compute_stats_locked(),
                "telemetry": self._telemetry_summary_locked(),
                "submit_result": self.submit_result,
            }

    def _broadcast_question_locked(self, q: QuestionState) -> None:
        self._broadcast(
            "question",
            {
                "question": q.to_dict(),
                "stats": self._compute_stats_locked(),
                "telemetry": self._telemetry_summary_locked(),
            },
        )

    def _broadcast_status_locked(self) -> None:
        self._broadcast(
            "status",
            {
                "phase": self.phase,
                "paused": self.paused,
                "active_index": self.active_index,
                "stats": self._compute_stats_locked(),
                "telemetry": self._telemetry_summary_locked(),
                "submit_result": self.submit_result,
            },
        )

    # ── Autonomous Background Runner ──────────────────────────

    def start(self) -> None:
        if self._worker_thread is not None:
            return
        self._worker_thread = threading.Thread(
            target=self._auto_worker,
            daemon=True,
            name="exam-ui-auto-worker",
        )
        self._worker_thread.start()

    def _wait_if_paused(self) -> bool:
        while not self._stop_event.is_set():
            with self._lock:
                if not self.paused:
                    return True
            time.sleep(0.15)
        return False

    def _interruptible_sleep(self, seconds: float) -> bool:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if not self._wait_if_paused():
                return False
            step = min(0.15, remaining)
            time.sleep(step)
            remaining -= step
        return not self._stop_event.is_set()

    def _record_question_sync(
        self,
        q: QuestionState,
        expected_version: int,
        answer_ids: list[str],
        use_time: int,
    ) -> bool:
        if not answer_ids:
            return False
        ok = False
        err_msg = ""
        try:
            ok = bool(self.record_fn(q.id, max(1, int(use_time)), list(answer_ids)))
            if not ok:
                err_msg = "单题同步失败"
        except Exception as e:  # noqa: BLE001
            ok = False
            err_msg = f"同步异常：{e}"
            self.log.error(f"[交互答题] 第 {q.index + 1} 题同步失败：{e}")

        with self._lock:
            if q.edit_version == expected_version:
                q.synced = ok
                q.sync_error = "" if ok else err_msg
                q.use_time = max(1, int(use_time))
                if ok and q.status == "bank_pending":
                    q.status = "bank_matched"
                self._broadcast_question_locked(q)
        return ok

    def _solve_one_with_ai(
        self,
        q: QuestionState,
        *,
        reasoning_effort_override: str | None = None,
        is_manual_rerun: bool = False,
    ) -> None:
        if not self.ai_enabled or self.ai_solver is None:
            with self._lock:
                if q.source == "none":
                    q.status = "ai_failed"
                    q.error = "未启用 AI，请手动选择"
                    self._broadcast_question_locked(q)
            return

        with self._lock:
            if not is_manual_rerun and q.source == "manual":
                return
            start_version = q.edit_version
            q.status = "ai_thinking"
            q.thinking = ""
            q.error = ""
            q.tool_events = []
            if not is_manual_rerun:
                self.active_index = q.index
            self._broadcast_question_locked(q)
            self._broadcast_status_locked()

        def _on_delta(piece: str) -> None:
            if not piece:
                return
            with self._lock:
                q.thinking += piece
            self._broadcast("thinking_delta", {"index": q.index, "delta": piece})

        def _on_kb(retrieval: RetrievalResult) -> None:
            with self._lock:
                q.kb_mode = retrieval.mode
                q.kb_sources = list(retrieval.sources)
                self._broadcast_question_locked(q)

        def _on_tool(ev: dict[str, Any]) -> None:
            with self._lock:
                q.tool_events.append(dict(ev))
                for src in ev.get("sources", []):
                    if src not in q.kb_sources:
                        q.kb_sources.append(src)
                self._broadcast_question_locked(q)

        try:
            res = self.ai_solver(
                q.raw_question,
                reasoning_effort_override,
                _on_delta,
                _on_kb,
                _on_tool,
            )
        except Exception as e:  # noqa: BLE001
            res = AISolveResult(error=f"AI 调用异常：{e}")

        with self._lock:
            q.thinking = res.thinking or q.thinking
            q.reason = res.reason
            q.kb_mode = res.kb_mode
            q.kb_sources = list(res.kb_sources)
            if res.tool_events:
                q.tool_events = list(res.tool_events)
            q.metrics = res.metrics.to_dict()
            self._record_metrics_locked(res.metrics)
            q.ai_suggested_ids = [str(x) for x in res.selected_ids]

            if q.edit_version != start_version and not is_manual_rerun:
                if q.source == "manual":
                    q.status = "manual"
                self._broadcast_question_locked(q)
                return

            if res.selected_ids:
                q.selected_ids = [str(x) for x in res.selected_ids]
                q.source = "ai"
                q.status = "ai_answered"
                q.reviewed = False
                q.error = ""
                q.synced = False
                q.edit_version += 1
                sync_version = q.edit_version
                chosen_ids = list(q.selected_ids)
                self._broadcast_question_locked(q)
            else:
                q.status = "ai_failed"
                q.error = res.error or "AI 未返回有效选项"
                q.reviewed = False
                self._broadcast_question_locked(q)
                return

        use_time = self.question_base_time + randint(0, self.question_random_upper)
        if not is_manual_rerun and not self._interruptible_sleep(use_time):
            return

        with self._lock:
            if q.edit_version != sync_version:
                return
        self._record_question_sync(q, sync_version, chosen_ids, max(1, use_time))

    def _auto_worker(self) -> None:
        try:
            ai_targets = [q for q in self.questions if q.source == "none"]
            for q in ai_targets:
                if not self._wait_if_paused():
                    return
                with self._lock:
                    if q.source == "manual":
                        continue
                self._solve_one_with_ai(q, is_manual_rerun=False)

            bank_targets = [q for q in self.questions if q.status == "bank_pending"]
            for q in bank_targets:
                if not self._wait_if_paused():
                    return
                with self._lock:
                    if q.synced or q.source == "manual":
                        continue
                    self.active_index = q.index
                    sync_version = q.edit_version
                    chosen_ids = list(q.selected_ids)
                    self._broadcast_status_locked()

                use_time = self.question_base_time + randint(0, self.question_random_upper)
                if not self._interruptible_sleep(use_time):
                    return

                with self._lock:
                    if q.edit_version != sync_version:
                        continue
                self._record_question_sync(q, sync_version, chosen_ids, max(1, use_time))

            # 绝不自动提交，停留在待人工交卷阶段
            with self._lock:
                if self.phase == "running":
                    self.phase = "ready_for_review"
                    self.active_index = None
                    self._broadcast_status_locked()
            self._broadcast(
                "toast",
                {
                    "level": "success",
                    "message": "自动答题完成，请核对后交卷",
                },
            )
        except Exception as e:  # noqa: BLE001
            self.log.error(f"[交互答题] 自动执行线程异常：{e}")
            self._broadcast("toast", {"level": "error", "message": f"自动答题异常：{e}"})

    # ── Human Interactive Actions ─────────────────────────────

    def _get_question(self, index: int) -> QuestionState:
        if not isinstance(index, int) or index < 0 or index >= len(self.questions):
            raise ValueError(f"无效题号：{index}")
        return self.questions[index]

    def _ensure_editable(self) -> None:
        if self.phase in ("submitted", "aborted"):
            raise RuntimeError("当前考试已结束")

    def select_options(self, index: int, option_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            q = self._get_question(index)
            valid_opt_ids = {opt["id"] for opt in q.options}
            cleaned = [str(oid) for oid in option_ids if str(oid) in valid_opt_ids]
            if q.type != 2 and len(cleaned) > 1:
                cleaned = [cleaned[-1]]

            q.selected_ids = cleaned
            q.source = "manual"
            q.status = "manual" if cleaned else "pending"
            q.reviewed = bool(cleaned)
            q.error = ""
            q.synced = False
            q.sync_error = ""
            q.edit_version += 1
            ver = q.edit_version
            self._broadcast_question_locked(q)
            snap = q.to_dict()

        if cleaned:
            threading.Thread(
                target=self._debounced_manual_sync,
                args=(q, ver, list(cleaned)),
                daemon=True,
                name=f"exam-ui-sync-{index}",
            ).start()
        return snap

    def _debounced_manual_sync(
        self, q: QuestionState, version: int, option_ids: list[str]
    ) -> None:
        time.sleep(0.25)
        with self._lock:
            if q.edit_version != version or self.phase in ("submitted", "aborted"):
                return
        self._record_question_sync(q, version, option_ids, max(2, self.question_base_time))

    def set_reviewed(self, index: int, reviewed: bool) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            q = self._get_question(index)
            q.reviewed = bool(reviewed)
            self._broadcast_question_locked(q)
            return q.to_dict()

    def set_flagged(self, index: int, flagged: bool) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            q = self._get_question(index)
            q.flagged = bool(flagged)
            self._broadcast_question_locked(q)
            return q.to_dict()

    def set_paused(self, paused: bool) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            self.paused = bool(paused)
            self._broadcast_status_locked()
            return {"paused": self.paused, "phase": self.phase}

    def rerun_ai(self, index: int, reasoning_effort: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            if not self.ai_enabled or self.ai_solver is None:
                raise RuntimeError("当前未启用 AI")
            q = self._get_question(index)
            if index in self._rerunning_indices or q.status == "ai_thinking":
                raise RuntimeError("该题正在 AI 思考中")
            self._rerunning_indices.add(index)

        def _runner() -> None:
            try:
                self._solve_one_with_ai(
                    q,
                    reasoning_effort_override=reasoning_effort,
                    is_manual_rerun=True,
                )
            finally:
                with self._lock:
                    self._rerunning_indices.discard(index)

        threading.Thread(
            target=_runner, daemon=True, name=f"exam-ui-rerun-{index}"
        ).start()
        return {"started": True, "index": index}

    def apply_ai_suggestion(self, index: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_editable()
            q = self._get_question(index)
            if not q.ai_suggested_ids:
                raise RuntimeError("该题没有 AI 推荐答案")
            ids = list(q.ai_suggested_ids)
            q.selected_ids = ids
            q.source = "ai"
            q.status = "ai_answered"
            q.reviewed = True
            q.synced = False
            q.sync_error = ""
            q.edit_version += 1
            ver = q.edit_version
            self._broadcast_question_locked(q)
            snap = q.to_dict()

        threading.Thread(
            target=self._debounced_manual_sync,
            args=(q, ver, ids),
            daemon=True,
            name=f"exam-ui-apply-ai-{index}",
        ).start()
        return snap

    def submit_exam(self, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("必须由人工确认（confirm=true）才能交卷")

        with self._submit_lock:
            with self._lock:
                if self.phase == "submitted":
                    return self.submit_result or {}
                if self.phase == "aborted":
                    raise RuntimeError("考试已放弃退出")
                self._stop_event.set()
                pending_sync = [
                    (q, q.edit_version, list(q.selected_ids))
                    for q in self.questions
                    if q.selected_ids and not q.synced
                ]

            for q, ver, ans_ids in pending_sync:
                ok = self._record_question_sync(
                    q, ver, ans_ids, max(2, self.question_base_time)
                )
                if not ok:
                    raise RuntimeError(f"第 {q.index + 1} 题同步失败，已中止交卷")

            res = self.submit_fn()
            data = res.get("data") if isinstance(res, dict) else {}
            score = data.get("score") if isinstance(data, dict) else None
            submit_summary = {"score": score, "raw": res}

            with self._lock:
                self.phase = "submitted"
                self.active_index = None
                self.submit_result = submit_summary
                self.outcome = {
                    "action": "submitted",
                    "score": score,
                    "response": res,
                }
                self._broadcast_status_locked()

            threading.Timer(1.2, self._done_event.set).start()
            return submit_summary

    def abort_exam(self, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ValueError("必须确认放弃（confirm=true）")
        with self._lock:
            if self.phase in ("submitted", "aborted"):
                return {"phase": self.phase}
            self._stop_event.set()
            self.phase = "aborted"
            self.active_index = None
            self.outcome = {"action": "aborted"}
            self._broadcast_status_locked()
        threading.Timer(0.4, self._done_event.set).start()
        return {"phase": "aborted"}

    def wait_until_done(self) -> dict[str, Any]:
        try:
            while not self._done_event.is_set():
                self._done_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            self.log.warning("收到中断信号，已退出（未交卷）")
            self.abort_exam(confirm=True)
        return self.outcome


def _make_handler(controller: ExamSessionController) -> type[BaseHTTPRequestHandler]:
    class ExamUIHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(
            self,
            body: bytes,
            *,
            content_type: str,
            status: int = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send_bytes(
                body, content_type="application/json; charset=utf-8", status=status
            )

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise TypeError("请求体必须为 JSON 对象")
            return data

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send_bytes(
                    EXAM_UI_HTML.encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
                return
            if path == "/api/state":
                self._send_json(controller.snapshot())
                return
            if path == "/api/events":
                self._handle_sse()
                return
            self._send_json({"ok": False, "error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

        def _handle_sse(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            sub = controller.subscribe()
            try:
                init_payload = (
                    f"event: init\ndata: {json.dumps(controller.snapshot(), ensure_ascii=False)}\n\n"
                )
                self.wfile.write(init_payload.encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        msg = sub.get(timeout=15.0)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            finally:
                controller.unsubscribe(sub)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json_body()
                if path == "/api/select":
                    res = controller.select_options(
                        int(body["index"]), list(body.get("option_ids", []))
                    )
                    self._send_json({"ok": True, "question": res})
                elif path == "/api/review":
                    res = controller.set_reviewed(
                        int(body["index"]), bool(body.get("reviewed", True))
                    )
                    self._send_json({"ok": True, "question": res})
                elif path == "/api/flag":
                    res = controller.set_flagged(
                        int(body["index"]), bool(body.get("flagged", True))
                    )
                    self._send_json({"ok": True, "question": res})
                elif path == "/api/pause":
                    res = controller.set_paused(bool(body.get("paused", True)))
                    self._send_json({"ok": True, **res})
                elif path == "/api/rerun-ai":
                    effort = body.get("reasoning_effort")
                    effort_str = str(effort).strip() if effort is not None else None
                    res = controller.rerun_ai(
                        int(body["index"]), reasoning_effort=effort_str
                    )
                    self._send_json({"ok": True, **res})
                elif path == "/api/apply-ai":
                    res = controller.apply_ai_suggestion(int(body["index"]))
                    self._send_json({"ok": True, "question": res})
                elif path == "/api/submit":
                    res = controller.submit_exam(confirm=bool(body.get("confirm")))
                    self._send_json({"ok": True, "result": res})
                elif path == "/api/abort":
                    res = controller.abort_exam(confirm=bool(body.get("confirm")))
                    self._send_json({"ok": True, **res})
                else:
                    self._send_json(
                        {"ok": False, "error": "Not Found"}, status=HTTPStatus.NOT_FOUND
                    )
            except (KeyError, ValueError, TypeError) as e:
                self._send_json(
                    {"ok": False, "error": str(e)}, status=HTTPStatus.BAD_REQUEST
                )
            except RuntimeError as e:
                self._send_json(
                    {"ok": False, "error": str(e)}, status=HTTPStatus.CONFLICT
                )
            except Exception as e:  # noqa: BLE001
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return ExamUIHandler


class _ExamHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# 多账号并发开启交互页时串行排队，避免端口冲突与同时弹出多个浏览器窗口
_EXAM_UI_SESSION_LOCK = threading.Lock()


def start_exam_ui_server(
    controller: ExamSessionController,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    log: Any = logger,
) -> tuple[_ExamHTTPServer, str]:
    """启动交互式答题本地 HTTP/SSE 服务并返回 `(server, url)`。"""
    target_port = int(port)
    try:
        server = _ExamHTTPServer((host, target_port), _make_handler(controller))
    except OSError as e:
        if target_port == 0:
            raise
        log.warning(f"端口 {target_port} 已被占用（{e}），已自动回退为空闲端口")
        server = _ExamHTTPServer((host, 0), _make_handler(controller))
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        daemon=True,
        name=f"exam-ui-http-{actual_port}",
    ).start()
    return server, url


def run_interactive_exam_ui(
    controller: ExamSessionController,
    *,
    port: int = 0,
    open_browser: bool = True,
    log: Any = logger,
) -> dict[str, Any]:
    """启动交互式答题页面，自动开始答题并等待人工核对后手动交卷或放弃。"""
    if _EXAM_UI_SESSION_LOCK.locked():
        log.info("已有另一账号正在使用交互式答题页面，当前账号排队等待...")
    with _EXAM_UI_SESSION_LOCK:
        server, url = start_exam_ui_server(controller, port=port, log=log)
        log.success(f"交互式答题页面：{url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as e:  # noqa: BLE001
                log.warning(f"自动打开浏览器失败，请手动访问 {url}：{e}")

        controller.start()
        try:
            return controller.wait_until_done()
        finally:
            server.shutdown()
            server.server_close()


# ── 演示模式（--exam-ui-demo：默认真实调用用户配置的 LLM API 与知识库）──

def run_demo_exam_ui(
    *,
    ai_config: dict[str, Any] | None = None,
    base_path: str = ".",
    data_dir: str | None = None,
    port: int = 0,
    open_browser: bool = True,
    use_mock: bool = False,
    log: Any = logger,
) -> dict[str, Any]:
    """启动模拟试卷进行端到端调试。

    默认直接调用 `config.toml` / CLI / 环境变量中配置的真实 LLM API 与 `knowledge/` 知识库，
    可实时观测 TTFT、TPS、Token 消耗、KV Cache 命中及 Agent 工具调用；
    仅在 `use_mock=True`（`--mock`）时使用离线模拟响应。
    """
    demo_questions: list[dict[str, Any]] = [
        {
            "id": "demo-q1",
            "title": "实验室发生电器火灾时，以下哪项处置措施最正确？",
            "type": 1,
            "typeLabel": "单选题",
            "optionList": [
                {"id": "q1-a", "content": "立即使用自来水直接泼灭火焰"},
                {"id": "q1-b", "content": "先切断电源，再使用干粉或二氧化碳灭火器扑救"},
                {"id": "q1-c", "content": "打开门窗通风以降低火焰温度"},
                {"id": "q1-d", "content": "使用泡沫灭火器带电灭火"},
            ],
        },
        {
            "id": "demo-q2",
            "title": "接到自称“公检法”机关电话，称你涉嫌洗钱并要求将资金转入“安全账户”自证清白时，应该怎么做？",
            "type": 1,
            "typeLabel": "单选题",
            "optionList": [
                {"id": "q2-a", "content": "立即挂断电话，不转账，可拨打 96110 核实"},
                {"id": "q2-b", "content": "按对方指示先转少量资金配合调查"},
                {"id": "q2-c", "content": "将银行卡密码与短信验证码告知对方核验"},
                {"id": "q2-d", "content": "下载对方发来的“安全防护”屏幕共享 App"},
            ],
        },
        {
            "id": "demo-q3",
            "title": "以下哪些属于高校学生宿舍严禁使用的违规电器或危险行为？（多选题）",
            "type": 2,
            "typeLabel": "多选题",
            "optionList": [
                {"id": "q3-a", "content": "在宿舍使用“热得快”、电热杯等大功率发热电器"},
                {"id": "q3-b", "content": "私拉乱接电线或将插线板放在床铺被褥上"},
                {"id": "q3-c", "content": "使用经 3C 认证的合格手机原装充电器"},
                {"id": "q3-d", "content": "离开宿舍时未拔掉高功率发热设备电源"},
            ],
        },
        {
            "id": "demo-q4",
            "title": "实验操作中若不慎将浓硫酸溅到皮肤上，应当立即采取的正确步骤是？",
            "type": 1,
            "typeLabel": "单选题",
            "optionList": [
                {"id": "q4-a", "content": "直接用氢氧化钠浓溶液进行中和"},
                {"id": "q4-b", "content": "先用干布或纸巾迅速拭去，再用大量流动清水冲洗并涂稀碳酸氢钠溶液"},
                {"id": "q4-c", "content": "先涂抹红药水再包扎"},
                {"id": "q4-d", "content": "无需处理，等待自然风干"},
            ],
        },
        {
            "id": "demo-q5",
            "title": "【判断题】发现有人触电倒地且尚未脱离电源时，应立即冲上前徒手将其拉开。",
            "type": 3,
            "typeLabel": "判断题",
            "optionList": [
                {"id": "q5-a", "content": "正确"},
                {"id": "q5-b", "content": "错误"},
            ],
        },
        {
            "id": "demo-q6",
            "title": "在公共场所遇到突发火灾、浓烟弥漫时，下列逃生做法正确的是？（多选题）",
            "type": 2,
            "typeLabel": "多选题",
            "optionList": [
                {"id": "q6-a", "content": "用湿毛巾捂住口鼻，采取低姿弯腰沿疏散指示标志撤离"},
                {"id": "q6-b", "content": "迅速乘坐普通客运电梯下楼以节约时间"},
                {"id": "q6-c", "content": "切勿贪恋财物或重返火场"},
                {"id": "q6-d", "content": "疏散通道被大火封堵时退回相对安全房间封堵门缝并呼救"},
            ],
        },
    ]

    bank_answers = {
        "demo-q2": ["q2-a"],
        "demo-q5": ["q5-b"],
    }

    effective_ai: dict[str, Any] = {
        "enable": True,
        "base_url": "https://opencode.ai/zen/v1",
        "api_key": "",
        "model": "nemotron-3-ultra-free",
        "timeout": 60,
        "max_retries": 2,
        "reasoning_effort": "",
        "stream": True,
        "show_thinking": True,
        "knowledge_enable": True,
        "knowledge_max_chars": 12000,
        "knowledge_top_k": 5,
    }
    if ai_config:
        effective_ai.update(ai_config)

    recorded: dict[str, list[str]] = {}
    correct_key: dict[str, set[str]] = {
        "demo-q1": {"q1-b"},
        "demo-q2": {"q2-a"},
        "demo-q3": {"q3-a", "q3-b", "q3-d"},
        "demo-q4": {"q4-b"},
        "demo-q5": {"q5-b"},
        "demo-q6": {"q6-a", "q6-c", "q6-d"},
    }

    def _demo_record(qid: str, use_time: int, answer_ids: list[str]) -> bool:
        recorded[qid] = list(answer_ids)
        return True

    def _demo_submit() -> dict[str, Any]:
        hit = sum(1 for qid, ans in correct_key.items() if set(recorded.get(qid, [])) == ans)
        score = round(hit / len(correct_key) * 100)
        return {"code": "0", "data": {"score": score}}

    if use_mock:
        def _mock_solver(
            question: dict[str, Any],
            effort: str | None,
            on_delta: Callable[[str], None],
            on_kb: Callable[[RetrievalResult], None],
            on_tool: Callable[[dict[str, Any]], None],
        ) -> AISolveResult:
            t0 = time.perf_counter()
            qid = str(question["id"])
            on_tool(
                {
                    "name": "search_knowledge",
                    "query": str(question["title"])[:16],
                    "hits": 2,
                    "sources": ["安全手册.pdf"],
                    "duration_ms": 1.2,
                }
            )
            on_kb(
                RetrievalResult(
                    context="",
                    mode="agent_tools",
                    sources=["安全手册.pdf"],
                    total_docs=1,
                    total_chars=8000,
                    matched_chunks=2,
                )
            )
            thought = f"按安全规范逐项分析本题（effort={effort or 'default'}），核对参考文档后确定正确选项。"
            first_ts = None
            for i in range(0, len(thought), 4):
                if first_ts is None:
                    first_ts = time.perf_counter()
                on_delta(thought[i : i + 4])
                time.sleep(0.03)
            end_ts = time.perf_counter()
            ttft_ms = ((first_ts or end_ts) - t0) * 1000.0
            gen_s = max(0.01, end_ts - (first_ts or t0))
            ans_ids = sorted(correct_key.get(qid, {question["optionList"][0]["id"]}))
            metrics = AIMetrics(
                endpoint_host="mock.local",
                model="mock-model",
                reasoning_effort=effort or "",
                ttft_ms=ttft_ms,
                tps=42.0 / gen_s,
                elapsed_ms=(end_ts - t0) * 1000.0,
                prompt_tokens=620,
                completion_tokens=42,
                cached_tokens=480,
                total_tokens=662,
                usage_estimated=False,
                context_chars=960,
                context_tokens=620,
                rounds=2,
            )
            return AISolveResult(
                selected_ids=ans_ids,
                reason="离线模拟结果（依据 安全手册.pdf）",
                thinking=thought,
                kb_mode="agent_tools",
                kb_sources=["安全手册.pdf"],
                metrics=metrics,
                model="mock-model",
                reasoning_effort=effort or "",
            )

        ai_solver: AISolverFn | None = _mock_solver
        model_name = "mock-model"
        endpoint_host = "mock.local"
        ai_enabled = True
    else:
        ai_enabled = parse_bool(effective_ai.get("enable"), default=True)
        model_name = str(effective_ai.get("model") or "").strip()
        endpoint_host = extract_endpoint_host(str(effective_ai.get("base_url") or ""))
        warned_state = [False]
        http_session = requests.Session()

        def _real_demo_solver(
            question: dict[str, Any],
            effort: str | None,
            on_delta: Callable[[str], None],
            on_kb: Callable[[RetrievalResult], None],
            on_tool: Callable[[dict[str, Any]], None],
        ) -> AISolveResult:
            return solve_question_with_ai(
                session=http_session,
                ai_config=effective_ai,
                question=question,
                base_path=base_path,
                data_dir=data_dir,
                log=log,
                warned_key_state=warned_state,
                reasoning_effort_override=effort,
                on_thinking_delta=on_delta,
                on_kb_result=on_kb,
                on_tool_event=on_tool,
                suppress_stdout_stream=True,
            )

        ai_solver = _real_demo_solver if ai_enabled else None
        log.info(
            f"演示模式使用真实 AI 接口：model={model_name} endpoint={endpoint_host} "
            f"effort={effective_ai.get('reasoning_effort') or '默认'}"
        )

    controller = ExamSessionController(
        plan_name="模拟考试",
        user_label="Demo",
        paper_score=100,
        answer_time_minutes=45,
        questions=demo_questions,
        bank_answers_map=bank_answers,
        ai_solver=ai_solver,
        record_fn=_demo_record,
        submit_fn=_demo_submit,
        question_base_time=0,
        question_random_upper=0,
        default_reasoning_effort=str(effective_ai.get("reasoning_effort") or ""),
        ai_model=model_name,
        ai_endpoint_host=endpoint_host,
        ai_enabled=ai_enabled,
        demo_mode=True,
        mock_mode=use_mock,
        log=log,
    )
    return run_interactive_exam_ui(controller, port=port, open_browser=open_browser, log=log)
