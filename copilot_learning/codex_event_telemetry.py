"""Codexの公式EventMsgから安全なカウンタ/累積トークンsnapshotだけを抽出する。

任意文字列（プロンプト・stdout・cwd・コマンド・モデル名・ID）は返さない。
メッセージとtool引数は従来のrolloutアダプターが別途処理する。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# Codex公式 codex-rs/protocol/src/protocol.rs の EventMsg
# 将来追加の型は未知として扱い、勝手に成功・完了と解釈しない。
KNOWN_EVENT_TYPES = frozenset({
    "task_started", "turn_started", "task_complete", "turn_complete",
    "token_count", "user_message", "agent_message", "agent_message_delta",
    "agent_reasoning", "agent_reasoning_raw_content",
    "agent_reasoning_section_break", "error", "warning", "turn_aborted",
    "stream_error", "context_compacted", "thread_rolled_back",
    "exec_command_begin", "exec_command_output_delta", "exec_command_end",
    "terminal_interaction", "exec_approval_request", "request_permissions",
    "mcp_tool_call_begin", "mcp_tool_call_end", "mcp_startup_update",
    "mcp_startup_complete", "web_search_begin", "web_search_end",
    "apply_patch_begin", "apply_patch_end", "request_user_input",
    "model_reroute", "session_configured", "thread_settings_applied",
    "auth_recovery_started", "auth_recovery_completed",
    "guardian_warning", "guardian_assessment", "deprecation_notice",
    "thread_queue_changed", "thread_goal_updated", "environment_connected",
    "environment_disconnected", "image_generation_begin", "image_generation_end",
    "turn_moderation_metadata", "safety_buffering", "model_verification",
    "realtime_conversation_started", "realtime_conversation_closed",
    "realtime_conversation_realtime", "realtime_conversation_sdp",
    "view_image_tool_call", "dynamic_tool_call_request",
    "dynamic_tool_call_response", "elicitation_request",
    "elicitation_abandoned", "apply_patch_approval_request",
    # Codex protocol.rs 公式EventMsgの追加型。元payloadを内容として復元しない。
    "network_approval_resolved", "patch_apply_begin", "patch_apply_updated",
    "patch_apply_end", "turn_diff", "realtime_conversation_list_voices_response",
    "plan_update", "shutdown_complete", "entered_review_mode",
    "exited_review_mode", "raw_response_item", "raw_response_completed",
    "private_session_event", "item_started", "item_completed",
    "hook_started", "hook_completed", "agent_message_content_delta",
    "plan_delta", "reasoning_content_delta", "reasoning_raw_content_delta",
    "collab_agent_spawn_begin", "collab_agent_spawn_end",
    "collab_agent_interaction_begin", "collab_agent_interaction_end",
    "collab_waiting_begin", "collab_waiting_end",
    "collab_close_begin", "collab_close_end",
    "collab_resume_begin", "collab_resume_end", "sub_agent_activity",
})
# これらは本来の文脈（ツール、会話、思考、差分等）を含み得る。
# 型を認識しても全会話を復元したと誤認しない。
UNREPLAYED_EVENT_TYPES = frozenset({
    "agent_message", "agent_message_delta", "agent_message_content_delta",
    "agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break",
    "raw_response_item", "raw_response_completed", "private_session_event",
    "plan_update", "plan_delta", "reasoning_content_delta",
    "reasoning_raw_content_delta", "turn_diff", "item_started", "item_completed",
    "hook_started", "hook_completed", "sub_agent_activity",
    "collab_agent_spawn_begin", "collab_agent_spawn_end",
    "collab_agent_interaction_begin", "collab_agent_interaction_end",
    "collab_waiting_begin", "collab_waiting_end",
    "collab_close_begin", "collab_close_end",
    "collab_resume_begin", "collab_resume_end",
})
KNOWN_TOP_METADATA_TYPES = frozenset({
    "dynamic_tools_updated", "inter_agent_communication",
    "inter_agent_communication_metadata", "token_usage_record", "world_state",
    "security_risk_score", "retained_context", "realtime_item",
})
UNREPLAYED_TOP_CONTEXT = frozenset({
    "inter_agent_communication", "world_state", "retained_context",
    "realtime_item",
})
USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    checked: dict[str, int] = {}
    for field in USAGE_FIELDS:
        v = value.get(field)
        if type(v) is not int or not 0 <= v < 10**13:
            return None
        checked[field] = v
    write = value.get("cache_write_input_tokens", 0)
    if type(write) is not int or not 0 <= write < 10**13:
        return None
    checked["cache_write_input_tokens"] = write
    return checked


class CodexTelemetry:
    """観測値をスナップショットとして保持し、成功率に変換しない。"""

    def __init__(self) -> None:
        self.events: Counter[str] = Counter()
        self.usage_snapshots = 0
        self.usage_missing_or_invalid = 0
        self.usage_latest: dict[str, int] | None = None
        self.usage_source: str | None = None
        self.explicit_turn_error = 0
        self.exec_command_nonzero = 0
        self.exec_command_zero = 0
        self.exec_command_status_unknown = 0
        self.evidence_lines: dict[str, list[int]] = {}

    def _mark(self, category: str, source_line: int | None) -> None:
        if type(source_line) is int and source_line > 0:
            collected = self.evidence_lines.setdefault(category, [])
            if len(collected) < 12:
                collected.append(source_line)

    def snapshot(self, data: Any, source: str) -> None:
        found = _usage(data)
        if found is None:
            self.usage_missing_or_invalid += 1
        else:
            self.usage_snapshots += 1
            self.usage_latest = found
            self.usage_source = source

    def event(self, payload: dict[str, Any], *, source_line: int | None = None) -> bool:
        """認識済みEventMsgならTrue。未知型は呼出側が欠落として計上する。"""
        kind = payload.get("type")
        if not isinstance(kind, str) or kind not in KNOWN_EVENT_TYPES:
            return False
        # ユーザー発言は既存の文章解析器が扱う。ここでは件数のみ。
        self.events[kind] += 1
        if kind in {"error", "turn_aborted", "stream_error"}:
            self._mark(kind, source_line)
        if kind == "token_count":
            info = payload.get("info")
            self.snapshot(info.get("total_token_usage") if isinstance(info, dict) else None,
                          "event_msg.token_count.total_token_usage")
        elif kind in {"task_complete", "turn_complete"}:
            error = payload.get("error")
            if isinstance(error, dict):
                self.explicit_turn_error += 1
                self._mark("turn_complete_with_error", source_line)
            elif error is not None:
                self.events["invalid_terminal_error_shape"] += 1
        elif kind == "exec_command_end":
            exit_code = payload.get("exit_code")
            if type(exit_code) is int:
                if exit_code == 0:
                    self.exec_command_zero += 1
                else:
                    self.exec_command_nonzero += 1
                    self._mark("exec_command_nonzero", source_line)
            else:
                self.exec_command_status_unknown += 1
        return True

    def top(self, top: str, payload: dict[str, Any]) -> None:
        if top == "token_usage_record":
            self.snapshot(payload.get("thread_token_usage"),
                          "token_usage_record.thread_token_usage")

    def report(self) -> dict[str, Any]:
        """nullは未観測を意味し、0トークンや失敗と解釈しない。"""
        candidate_defs = (
            ("exec_command_nonzero", self.exec_command_nonzero,
             "コマンドの非ゼロ終了理由と再試行の妥当性を原本で確認する"),
            ("turn_complete_with_error", self.explicit_turn_error,
             "Codexが報告したターン終了エラーの原因を原本で確認する"),
            ("turn_aborted", self.events["turn_aborted"],
             "中断が利用者操作か回復不能かを原本で確認する"),
            ("stream_error", self.events["stream_error"],
             "ストリーム通信エラー後の回復・再試行を確認する"),
            ("error", self.events["error"],
             "Codexの明示エラーと最終的な回復を区別して確認する"),
        )
        suggestions = [
            {"rule_id": key, "signal_count": count,
             "source_lines": self.evidence_lines.get(key, []),
             "evidence_sample_truncated": count > len(self.evidence_lines.get(key, [])),
             "suggestion": text, "assessment": "heuristic_review_only"}
            for key, count, text in candidate_defs if count
        ]
        return {
            "review_candidates": suggestions,
            "observed_event_types": dict(sorted(self.events.items())),
            "turn_started_count": (self.events["task_started"] +
                                   self.events["turn_started"]),
            "turn_complete_record_count": (self.events["task_complete"] +
                                           self.events["turn_complete"]),
            "turn_complete_with_structured_error_count": self.explicit_turn_error,
            "explicit_error_event_count": self.events["error"],
            "turn_aborted_event_count": self.events["turn_aborted"],
            "stream_error_event_count": self.events["stream_error"],
            "warning_event_count": self.events["warning"],
            "exec_command_end_nonzero_count": self.exec_command_nonzero,
            "exec_command_end_zero_count": self.exec_command_zero,
            "exec_command_end_without_numeric_exit_code": self.exec_command_status_unknown,
            "usage_snapshot_count": self.usage_snapshots,
            "usage_snapshot_unavailable_count": self.usage_missing_or_invalid,
            "latest_cumulative_token_usage": self.usage_latest,
            "latest_token_usage_source": self.usage_source,
            "token_usage_is_cumulative_not_additive": True,
            "cost_usd": None,
            "tool_outcome_verified": False,
            "task_outcome_verified": False,
            "business_completion_verified": False,
            "causal_improvement_established": False,
        }
