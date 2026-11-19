"""#36: Copilot CLIのネイティブJSONLイベントをオフラインで正規化する。

GitHub CLI仕様の events.jsonl は message ではなく user.message 等の
type/data envelope を利用する。本文と実ツール結果は出力先へログしない。
未知スキーマや形の違うイベントを推測してメッセージへ変換しない。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .core import InvalidData, MAX_EVENT_TEXT_CHARS, check_sensitive

VERSION = "cli-native-allowlist-1"

# 取り込む会話型イベント：失敗したtool出力は秘密を持つため定型のエラーに置換。
MESSAGE_TYPES = frozenset({"user.message", "assistant.message", "tool.execution_complete"})
# 収集対象ではないことがわかっている管理・テレメトリーイベント。
# ここにない型は、無視したことが分かるように未知として記録する。
NON_MESSAGE_TYPES = frozenset({
    "session.start", "session.shutdown", "session.task_complete",
    "session.model_change", "session.mode_changed", "session.plan_changed",
    "session.compaction_start", "session.compaction_complete",
    "session.title_changed", "session.usage_record",
    "assistant.turn_start", "assistant.turn_end", "assistant.message_delta",
    "assistant.reasoning", "model.model_call_started",
    "model.model_call_success", "model.turn_started", "model.turn_ended",
    "tool.execution_start", "subagent.started", "subagent.completed",
    "subagent.failed", "skill.invoked", "hook.start", "hook.end",
    "permission.requested", "permission.completed",
    "system.message", "system.notification", "abort",
})
NATIVE_TYPES = MESSAGE_TYPES | NON_MESSAGE_TYPES
MAX_EVENTS = 150_000


def is_native_cli_event_stream(events: list[dict[str, Any]]) -> bool:
    """従来の平坦なmessage系イベントとネイティブCLIを混同しない。"""
    return any(isinstance(item, dict) and item.get("type") in NATIVE_TYPES for item in events)


def _timestamp(event: dict[str, Any]) -> str:
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        raise InvalidData("ネイティブメッセージの日時が欠落しています")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidData("ネイティブメッセージの日時が不正です") from exc
    if parsed.tzinfo is None:
        raise InvalidData("ネイティブメッセージの日時にはタイムゾーンが必要です")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_native_cli_events(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """検証済みZIPから得られたリストのみを対象に、出典付き会話を復元する。

    Native schema:
      {"type":"user.message","timestamp":"...Z","data":{"content":"..."}}
      {"type":"assistant.message","timestamp":"...Z","data":{"content":"..."}}
      {"type":"tool.execution_complete","timestamp":"...Z","data":{"success":false, ...}}
    """
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise InvalidData("CLIイベント件数が不正または上限超過です")
    fp = manifest.get("session_fingerprint")
    sha = manifest.get("archive_sha256")
    if not isinstance(fp, str) or len(fp) != 64 or not isinstance(sha, str) or len(sha) != 64:
        raise InvalidData("正規化元セッションの識別情報がありません")
    messages: list[dict[str, Any]] = []
    ignored = 0
    skipped: list[dict[str, Any]] = []
    observed = Counter()
    for index, record in enumerate(events):
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise InvalidData("CLIイベントのenvelopeが不正です")
        kind = record["type"]
        observed[kind if kind in NATIVE_TYPES else "unknown"] += 1
        if kind in {"message", "conversation_message"}:
            # ネイティブ形式と旧形式を混在させたセッションは許可しない。
            raise InvalidData("CLIネイティブ形式と旧message形式が混在しています")
        if kind not in NATIVE_TYPES:
            skipped.append({"event_index": index, "reason": "unsupported_type"})
            continue
        if not isinstance(record.get("data"), dict):
            raise InvalidData("既知のCLIイベントのdataがJSONオブジェクトではありません")
        data = record["data"]
        if kind in NON_MESSAGE_TYPES:
            ignored += 1
            continue
        if kind == "tool.execution_complete":
            success = data.get("success")
            if type(success) is not bool:
                skipped.append({"event_index": index, "reason": "tool_result_status_missing"})
                continue
            if success:
                ignored += 1  # 生tool結果や引数を学習候補へ流さない。
                continue
            role = "TOOL"
            text = "ERROR: Copilot tool execution reported failure"
        else:
            role = "USER" if kind == "user.message" else "ASSISTANT"
            text = data.get("content")
            if not isinstance(text, str) or not text.strip():
                skipped.append({"event_index": index, "reason": "missing_text"})
                continue
            if len(text) > MAX_EVENT_TEXT_CHARS:
                skipped.append({"event_index": index, "reason": "text_too_long"})
                continue
        check_sensitive(text)
        messages.append({
            "event_index": index, "role": role, "text": text,
            "timestamp": _timestamp(record), "timestamp_source": "native_event_timestamp",
            "evidence_id": f"{fp}:{index}",
        })
    if not any(msg["role"] == "USER" for msg in messages):
        raise InvalidData("ユーザー発言が認識できないためネイティブ形式を保留します")
    return {
        "schema_version": "1.0", "record_type": "normalized-session",
        "session_fingerprint": fp, "source_event_count": len(events),
        "recognized_event_count": len(messages), "skipped_event_count": len(skipped),
        "messages": messages,
        "format_status": "partial_unrecognized_events" if skipped else "recognized",
        "normalizer_version": VERSION,
        "skipped_details": skipped, "timestamp_fallback_count": 0,
        "ignored_known_event_count": ignored,
        "native_event_counts": dict(sorted(observed.items())),
        "tool_result_redaction": "constant_failure_signal_only",
        "source_provenance": {
            "archive_sha256": sha,
            "manifest_sha256": manifest.get("_snapshot_manifest_sha256"),
            "s3_version_id": None,
        },
    }
