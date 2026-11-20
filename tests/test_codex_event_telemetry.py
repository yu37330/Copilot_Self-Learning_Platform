"""Issue #46: Codex公式イベントの安全なローカルテレメトリ検証。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from copy import deepcopy

from copilot_learning.codex_event_telemetry import CodexTelemetry
from copilot_learning.codex_rollout import _parse
from copilot_learning.codex_schema_probe import TOP_TYPES, EVENT_TYPES, inspect_structure
from scripts.inspect_codex_rollout_schema import main as schema_cli
from copilot_learning.core import InvalidData


def event(kind: str, payload: dict) -> dict:
    return {"timestamp": "2026-11-20T02:00:00Z", "type": kind, "payload": payload}


def message_fixture() -> list[dict]:
    return [
        event("session_meta", {"id": "synthetic-session-id", "cwd": "PRIVATE PATH"}),
        event("event_msg", {"type": "user_message", "kind": "plain",
                            "message": "テスト結果を確認してから完了報告して"}),
        event("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "検証状況を確認します"}]}),
    ]


def encoded(rows: list[dict]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def tokens(input_t: int, output_t: int) -> dict:
    return {"input_tokens": input_t, "cached_input_tokens": 0,
            "output_tokens": output_t, "reasoning_output_tokens": 0,
            "total_tokens": input_t + output_t}


class CodexTelemetryTests(unittest.TestCase):
    def test_turn_lifecycle_does_not_claim_business_completion(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "task_started", "turn_id": "SECRET-ID"}),
            event("event_msg", {"type": "task_complete",
                                "turn_id": "SECRET-ID", "last_agent_message": "PRIVATE"}),
            event("event_msg", {"type": "task_complete", "turn_id": "SECOND-ID",
                                "error": {"message": "PRIVATE ERROR"}}),
            event("event_msg", {"type": "turn_aborted",
                                "reason": "interrupted", "turn_id": "SECRET-ID"}),
            event("event_msg", {"type": "warning", "message": "PRIVATE CAUTION"}),
            event("event_msg", {"type": "error", "message": "PRIVATE FATAL DETAIL"}),
            event("event_msg", {"type": "stream_error", "message": "PRIVATE RETRY"}),
        ]
        _, stat, _ = _parse(encoded(rows), [])
        t = stat["event_telemetry"]
        self.assertEqual(t["turn_started_count"], 1)
        self.assertEqual(t["turn_complete_record_count"], 2)
        self.assertEqual(t["turn_complete_with_structured_error_count"], 1)
        self.assertEqual(t["turn_aborted_event_count"], 1)
        self.assertEqual(t["explicit_error_event_count"], 1)
        self.assertEqual(t["warning_event_count"], 1)
        self.assertEqual(t["stream_error_event_count"], 1)
        self.assertFalse(t["task_outcome_verified"])
        self.assertFalse(t["business_completion_verified"])
        self.assertFalse(t["causal_improvement_established"])
        review = {row["rule_id"]: row for row in t["review_candidates"]}
        self.assertEqual(review["turn_complete_with_error"]["source_lines"], [6])
        self.assertEqual(review["turn_aborted"]["source_lines"], [7])
        self.assertEqual(review["error"]["source_lines"], [9])
        self.assertEqual(review["stream_error"]["source_lines"], [10])
        self.assertTrue(all(row["assessment"] == "heuristic_review_only"
                            for row in review.values()))
        self.assertNotIn("unknown_event_msg_type", stat["unsupported_categories"])
        for forbidden in ("PRIVATE", "SECRET-ID", "SECOND-ID"):
            self.assertNotIn(forbidden, json.dumps(stat))

    def test_cumulative_token_snapshots_replace_not_sum(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "token_count",
                                "info": {"total_token_usage": tokens(100, 20)},
                                "rate_limits": {"plan_type": "PRIVATE BUSINESS"}}),
            event("event_msg", {"type": "token_count",
                                "info": {"total_token_usage": tokens(150, 25)}}),
            event("token_usage_record", {
                "thread_token_usage": tokens(200, 60),
                "turn_token_usage": tokens(50, 35),
                "root_turn_id": "PRIVATE ID",
            }),
        ]
        _, stat, _ = _parse(encoded(rows), [])
        t = stat["event_telemetry"]
        self.assertEqual(t["usage_snapshot_count"], 3)
        self.assertEqual(t["latest_cumulative_token_usage"]["input_tokens"], 200)
        self.assertEqual(t["latest_cumulative_token_usage"]["output_tokens"], 60)
        self.assertEqual(t["latest_cumulative_token_usage"]["total_tokens"], 260)
        self.assertEqual(t["latest_token_usage_source"], "token_usage_record.thread_token_usage")
        self.assertTrue(t["token_usage_is_cumulative_not_additive"])
        self.assertIsNone(t["cost_usd"])
        self.assertNotIn("unknown_top_type", stat["unsupported_categories"])
        self.assertNotIn("PRIVATE", json.dumps(stat))

    def test_absent_or_invalid_usage_is_null_never_zero(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "token_count", "info": None}),
            event("event_msg", {"type": "token_count",
                                "info": {"total_token_usage": {
                                    "input_tokens": True, "output_tokens": 4,
                                    "cached_input_tokens": 0,
                                    "reasoning_output_tokens": 0, "total_tokens": 4,
                                }}}),
        ]
        _, stat, _ = _parse(encoded(rows), [])
        t = stat["event_telemetry"]
        self.assertEqual(t["usage_snapshot_count"], 0)
        self.assertEqual(t["usage_snapshot_unavailable_count"], 2)
        self.assertIsNone(t["latest_cumulative_token_usage"])
        self.assertIsNone(t["latest_token_usage_source"])

    def test_shell_exit_code_is_only_a_tool_signal(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "exec_command_end", "exit_code": 0,
                                "command": ["PRIVATE COMMAND"], "aggregated_output": "PRIVATE"}),
            event("event_msg", {"type": "exec_command_end", "exit_code": 137,
                                "cwd": "PRIVATE PATH"}),
            event("event_msg", {"type": "exec_command_end", "exit_code": "1",
                                "status": "PRIVATE"}),
        ]
        _, stat, _ = _parse(encoded(rows), [])
        t = stat["event_telemetry"]
        self.assertEqual(t["exec_command_end_zero_count"], 1)
        self.assertEqual(t["exec_command_end_nonzero_count"], 1)
        self.assertEqual(t["exec_command_end_without_numeric_exit_code"], 1)
        self.assertFalse(t["tool_outcome_verified"])
        self.assertFalse(t["task_outcome_verified"])
        self.assertEqual(t["review_candidates"][0]["rule_id"], "exec_command_nonzero")
        self.assertEqual(t["review_candidates"][0]["source_lines"], [5])
        self.assertNotIn("PRIVATE", json.dumps(stat))

    def test_known_rollout_metadata_ignored_unknown_context_marked(self):
        rows = message_fixture() + [
            event("dynamic_tools_updated", {"tools": [{"name": "PRIVATE TOOL"}]}),
            event("security_risk_score", {"message": "PRIVATE"}),
            event("retained_context", {"user_prompt": "PRIVATE"}),
            event("inter_agent_communication", {"prompt": "PRIVATE"}),
            event("world_state", {"state": "PRIVATE"}),
        ]
        events, stat, _ = _parse(encoded(rows), [])
        self.assertEqual(stat["ignored_event_categories"]["documented_metadata_dynamic_tools_updated"], 1)
        self.assertEqual(stat["unsupported_categories"]["documented_context_not_replayed"], 3)
        self.assertNotIn("unknown_top_type", stat["unsupported_categories"])
        self.assertEqual(events[-1]["type"], "codex.unrecognized_records")
        self.assertNotIn("PRIVATE", json.dumps(stat))

    def test_unknown_event_msg_no_longer_silent(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "unrecognized_confidential_workflow",
                                "message": "PRIVATE"}),
            event("other_confidential_top", {"type": "PRIVATE"}),
        ]
        events, stat, _ = _parse(encoded(rows), [])
        self.assertEqual(stat["unsupported_categories"]["unknown_event_msg_type"], 1)
        self.assertEqual(stat["unsupported_categories"]["unknown_top_type"], 1)
        self.assertEqual(events[-1]["type"], "codex.unrecognized_records")
        self.assertEqual(stat["coverage"], "partial_unknown_or_context")
        self.assertNotIn("unrecognized_confidential_workflow", json.dumps(stat))
        self.assertNotIn("other_confidential_top", json.dumps(stat))

    def test_malformed_event_type_is_not_crash_or_leak(self):
        rows = message_fixture() + [
            event("event_msg", {"type": {"untrusted": "PRIVATE"}, "body": "PRIVATE"}),
            event("event_msg", {"type": ["PRIVATE"], "body": "PRIVATE"}),
        ]
        events, stats, _ = _parse(encoded(rows), [])
        self.assertEqual(stats["unsupported_categories"]["unknown_event_msg_type"], 2)
        self.assertEqual(events[-1]["type"], "codex.unrecognized_records")
        self.assertNotIn("PRIVATE", json.dumps(stats))

    def test_documented_v2_aliases_counted(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "turn_started", "turn_id": "PRIVATE"}),
            event("event_msg", {"type": "turn_complete", "turn_id": "PRIVATE"}),
        ]
        _, stat, _ = _parse(encoded(rows), [])
        self.assertEqual(stat["event_telemetry"]["turn_started_count"], 1)
        self.assertEqual(stat["event_telemetry"]["turn_complete_record_count"], 1)

    def test_documented_item_completed_is_known_but_content_partial(self):
        rows = message_fixture() + [
            event("event_msg", {"type": "task_started", "turn_id": "PRIVATE"}),
            event("event_msg", {"type": "item_completed",
                                "item": {"arguments": "PRIVATE TOOL ARGUMENTS"}}),
            event("event_msg", {"type": "item_completed",
                                "item": {"result": "PRIVATE TOOL RESULT"}}),
            event("world_state", {"state": "PRIVATE WORKSPACE STATE"}),
            event("token_usage_record", {
                "thread_token_usage": tokens(100, 12),
                "root_turn_id": "PRIVATE",
            }),
        ]
        records, stats, _ = _parse(encoded(rows), [])
        self.assertNotIn("unknown_event_msg_type", stats["unsupported_categories"])
        self.assertNotIn("unknown_top_type", stats["unsupported_categories"])
        self.assertEqual(stats["unsupported_categories"]["documented_event_context_not_replayed"], 2)
        self.assertEqual(stats["unsupported_categories"]["documented_context_not_replayed"], 1)
        self.assertEqual(stats["event_telemetry"]["turn_started_count"], 1)
        self.assertEqual(stats["event_telemetry"]["latest_cumulative_token_usage"]["total_tokens"], 112)
        self.assertEqual(stats["coverage"], "partial_unknown_or_context")
        self.assertEqual(records[-1]["type"], "codex.unrecognized_records")
        for forbidden in ("PRIVATE", "WORKSPACE STATE", "TOOL RESULT"):
            self.assertNotIn(forbidden, json.dumps(stats))

    def test_telemetry_probe_is_explicit_and_never_outputs_content(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "rollout-synthetic.jsonl"
            rows = message_fixture() + [
                event("event_msg", {"type": "exec_command_end", "exit_code": 2,
                                    "command": ["PRIVATE SHELL COMMAND"],
                                    "aggregated_output": "PRIVATE COMMAND OUTPUT"}),
                event("event_msg", {"type": "token_count",
                                    "info": {"total_token_usage": tokens(900, 100)},
                                    "rate_limits": {"plan_type": "PRIVATE"}}),
            ]
            source.write_bytes(encoded(rows))
            os.utime(source, (time.time() - 86400 * 3, time.time() - 86400 * 3))
            without = inspect_structure(source)
            self.assertNotIn("event_telemetry", without)
            with_telemetry = inspect_structure(source, include_telemetry=True)
            t = with_telemetry["event_telemetry"]
            self.assertEqual(t["latest_cumulative_token_usage"]["total_tokens"], 1000)
            self.assertEqual(t["exec_command_end_nonzero_count"], 1)
            self.assertEqual(t["review_candidates"][0]["source_lines"], [4])
            self.assertFalse(t["business_completion_verified"])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(schema_cli([
                    "--rollout", str(source), "--acknowledge-local-inspection",
                    "--include-telemetry",
                ]), 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["telemetry_scope"], "fixed_numeric_fields_and_event_types_only")
            self.assertNotIn("PRIVATE", stdout.getvalue())
            self.assertNotIn(str(source), stdout.getvalue())

    def test_schema_probe_taxonomy_is_explicit_and_bounded(self):
        for known in ("token_usage_record", "dynamic_tools_updated",
                      "retained_context", "security_risk_score"):
            self.assertIn(known, TOP_TYPES)
        for known in ("task_started", "task_complete", "token_count",
                      "exec_command_end", "error", "stream_error"):
            self.assertIn(known, EVENT_TYPES)
        self.assertNotIn("PRIVATE_ANY_RANDOM_EVENT", EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
