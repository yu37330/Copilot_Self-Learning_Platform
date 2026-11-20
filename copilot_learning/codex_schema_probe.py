"""Codex rollout JSONLの構造だけを点検する。本文やセッションIDは表示・出力しない。

実セッションの型の違いを安全に確認するためのローカル手動診断。
会話内容・コマンド・ファイルパス・モデル名・tool名は返さない。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .codex_rollout import _source, MAX_LINES
from .codex_event_telemetry import KNOWN_EVENT_TYPES, KNOWN_TOP_METADATA_TYPES
from .core import InvalidData

TOP_TYPES = {"session_meta", "event_msg", "response_item", "turn_context", "compacted"} | KNOWN_TOP_METADATA_TYPES
EVENT_TYPES = KNOWN_EVENT_TYPES
RESPONSE_TYPES = {"message", "function_call", "function_call_output",
                  "custom_tool_call", "custom_tool_call_output", "reasoning"}
MAX_SCHEMA_PROBE = 16 * 1024 * 1024


def inspect_structure(path: Path, *, minimum_age_minutes: int = 1440) -> dict[str, Any]:
    """JSONの固定された型フィールドのみ計数する。任意の文字列は無視する。"""
    # This loads the data *locally*, but never exports raw record text.
    data = _source(path, minimum_age_minutes)
    if len(data) > MAX_SCHEMA_PROBE:
        raise InvalidData("構造診断は16MiB以下のファイルだけを対象とします")
    try:
        raw_lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise InvalidData("UTF-8 JSONL以外のファイルは診断できません") from exc
    if not 1 <= len(raw_lines) <= MAX_LINES:
        raise InvalidData("JSONL件数が上限外です")
    tops: Counter[str] = Counter()
    events: Counter[str] = Counter()
    responses: Counter[str] = Counter()
    elements: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    malformed = 0
    has_meta = False
    for i, line in enumerate(raw_lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (json.JSONDecodeError, ValueError) as exc:
            raise InvalidData("Codex JSONLの形式が不正です") from exc
        if (not isinstance(entry, dict) or not isinstance(entry.get("type"), str)
                or not isinstance(entry.get("payload"), dict)):
            malformed += 1
            continue
        cat = entry["type"] if entry["type"] in TOP_TYPES else "other_top_type"
        tops[cat] += 1
        payload = entry["payload"]
        if cat == "session_meta" and i == 0:
            has_meta = isinstance(payload.get("id"), str)
        if cat == "event_msg":
            event_kind = payload.get("type")
            events[event_kind if isinstance(event_kind, str) and event_kind in EVENT_TYPES
                   else "other_event_type"] += 1
        if cat == "response_item":
            response_kind = payload.get("type")
            responses[response_kind if isinstance(response_kind, str) and response_kind in RESPONSE_TYPES
                      else "other_response_type"] += 1
            if response_kind == "message":
                role = payload.get("role")
                roles[role if role in {"user", "assistant", "system", "developer"}
                      else "other_role"] += 1
                contents = payload.get("content")
                if not isinstance(contents, list):
                    elements["missing_content_array"] += 1
                else:
                    for item in contents:
                        kind = item.get("type") if isinstance(item, dict) else None
                        if isinstance(kind, str) and kind in {"input_text", "output_text"}:
                            elements[kind] += 1
                        else:
                            elements["other_content_part"] += 1
    return {
        "schema_version": "1.0",
        "record_type": "codex-rollout-schema-shape-probe",
        "rollout_bytes": len(data),
        "nonempty_records": sum(tops.values()) + malformed,
        "first_session_meta_with_id": has_meta,
        "malformed_envelopes": malformed,
        "top_categories": dict(sorted(tops.items())),
        "event_categories": dict(sorted(events.items())),
        "response_categories": dict(sorted(responses.items())),
        "message_content_types": dict(sorted(elements.items())),
        "message_role_categories": dict(sorted(roles.items())),
        "result": "structural_metadata_only_not_ingestion",
        "raw_text_exported": False,
        "input_path_exported": False,
        "network_calls": 0,
    }
