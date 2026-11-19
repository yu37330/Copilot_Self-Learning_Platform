"""Issue #41 Phase 2: 明示Task IDでChatGPTとDesktop Commanderの記録を結び付ける。

推定で関連付けない。選択済みソースのSHA、会話IDのハッシュ、
Desktop呼び出しの配列位置+内容SHAが一致する場合だけリンクする。
「完了した」という人手申告も正式な業務完了証拠としては扱わない。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .chatgpt_task_trace import (
    CHAT_RULES, KNOWN_TOOLS, _chat_items, _check_text, _failure,
    _history_records, _messages,
)
from .core import InvalidData, SHA_PATTERN, digest, json_bytes

VERSION = "explicit-task-binding-1"
MAX_TASKS = 150
MAX_REFS_PER_TASK = 400
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
PROPOSALS = {
    "missing_verification": "完了報告の前に別系統の成果物と検収結果を確認する候補",
    "explicit_correction": "依頼文の受入条件と再実行の判断基準を明示する候補",
    "instruction_conflict": "Project指示と依頼の優先順位・適用範囲を人手で確認する候補",
    "repeatable_workflow": "繰り返し作業を手順書・Skill候補として見直す",
    "role_boundary": "ツールと担当エージェントの役割境界を整理する候補",
}


def _fingerprint(kind: str, text: str) -> str:
    return digest((kind + "\x00" + text).encode("utf-8"))


def _chat_index(raw: Any, literals: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    items = _chat_items(raw)
    lookup: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for convo in items:
        conversation_id = convo.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise InvalidData("会話の識別子がありません")
        ref = _fingerprint("chatgpt-conversation-id-v1", conversation_id)
        if ref in lookup:
            raise InvalidData("重複した会話の識別子があります")
        messages, skipped = _messages(convo)
        signals: Counter[str] = Counter()
        user_count = 0
        for role, message in messages:
            if role == "user":
                user_count += 1
                _check_text(message, literals)
                for rule in CHAT_RULES:
                    if rule["pattern"].search(message):
                        signals[rule["id"]] += 1
        record = {
            "conversation_ref": ref,
            "user_messages": user_count,
            "assistant_messages": sum(role == "assistant" for role, _ in messages),
            "skipped_nodes": skipped,
            "correction_signals": dict(sorted(signals.items())),
        }
        lookup[ref] = record
        result.append({key: record[key] for key in ("conversation_ref", "user_messages",
                                                     "assistant_messages", "skipped_nodes")})
    return lookup, sorted(result, key=lambda x: x["conversation_ref"])


def _desktop_index(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _history_records(raw)
    lookup: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        tool = item.get("toolName")
        if not isinstance(tool, str) or not tool.strip():
            raise InvalidData("履歴のツール名が不正です")
        ref = digest(json_bytes(item))
        entry = {
            "index": index, "call_sha256": ref,
            "tool": tool if tool in KNOWN_TOOLS else "other_tool",
            "outcome": _failure(item),
        }
        lookup.append(entry)
        result.append({"index": index, "call_sha256": ref, "tool": entry["tool"]})
    return lookup, result


def index_sources(chat: Any, desktop: Any, *, chat_sha256: str, desktop_sha256: str,
                  literals: list[str]) -> dict[str, Any]:
    """利用者が選択するための参照インデックス。会話名・コマンドは含まない。"""
    if not SHA_PATTERN.fullmatch(chat_sha256) or not SHA_PATTERN.fullmatch(desktop_sha256):
        raise InvalidData("ソースSHAが不正")
    _, conversations = _chat_index(chat, literals)
    _, calls = _desktop_index(desktop)
    return {
        "schema_version": "1.0",
        "record_type": "offline-explicit-task-source-index",
        "analyzer_version": VERSION,
        "source_sha256": {"chatgpt": chat_sha256, "desktop": desktop_sha256},
        "conversations": conversations, "desktop_calls": calls,
        "task_linking": "unlinked_index_only",
        "business_outcome_verified": False,
        "body_exported": False,
        "network_calls": 0,
    }


def bind_explicit_tasks(chat: Any, desktop: Any, manifest: dict[str, Any], *,
                        chat_sha256: str, desktop_sha256: str,
                        literals: list[str]) -> dict[str, Any]:
    """申告されたTask IDと、実在する出典位置+ハッシュのみ結び付ける。"""
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "record_type", "source_sha256", "tasks"
    }:
        raise InvalidData("Task紐付けmanifestの形式が不正です")
    if (manifest["schema_version"] != "1.0"
            or manifest["record_type"] != "explicit-chatgpt-desktop-task-bindings"
            or manifest["source_sha256"] != {"chatgpt": chat_sha256, "desktop": desktop_sha256}):
        raise InvalidData("原本SHAとTask紐付けmanifestが一致しません")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise InvalidData("紐付けTaskは1〜150件を指定してください")
    conversations, _ = _chat_index(chat, literals)
    calls, _ = _desktop_index(desktop)
    used_calls: set[int] = set()
    used_conversations: set[str] = set()
    used_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {
            "task_id", "conversation_ref", "desktop_calls", "completion_claim"
        }:
            raise InvalidData("Task必須フィールドが不一致です")
        tid = task["task_id"]
        cref = task["conversation_ref"]
        selections = task["desktop_calls"]
        if (not isinstance(tid, str) or not ID_PATTERN.fullmatch(tid) or tid in used_ids
                or not isinstance(cref, str) or cref not in conversations or cref in used_conversations
                or not isinstance(selections, list) or not 1 <= len(selections) <= MAX_REFS_PER_TASK
                or task["completion_claim"] not in {"not_claimed", "reported_complete"}):
            raise InvalidData("Task識別子、対応会話、ツール範囲、完了申告に不整合があります")
        used_ids.add(tid)
        used_conversations.add(cref)
        counts: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        fingerprints: list[str] = []
        for selected in selections:
            if not isinstance(selected, dict) or set(selected) != {"index", "call_sha256"}:
                raise InvalidData("ツール呼び出し参照が不正")
            ordinal = selected["index"]
            ref = selected["call_sha256"]
            if (type(ordinal) is not int or ordinal < 0 or ordinal >= len(calls)
                    or not isinstance(ref, str) or not SHA_PATTERN.fullmatch(ref)
                    or calls[ordinal]["call_sha256"] != ref or ordinal in used_calls):
                raise InvalidData("実行参照が存在しないか、別Taskと重複しています")
            used_calls.add(ordinal)
            row = calls[ordinal]
            counts[row["tool"]] += 1
            outcomes[row["outcome"]] += 1
            fingerprints.append(f"{ordinal}:{ref}")
        chat_record = conversations[cref]
        hits = chat_record["correction_signals"]
        improvements = [PROPOSALS[k] for k in PROPOSALS if hits.get(k, 0)]
        if outcomes["explicit_error"]:
            improvements.append("明示エラーの後に原因特定と再実行判断を行う手順を確認する候補")
        result.append({
            "task_id_sha256": _fingerprint("task-id-v1", tid),
            "conversation_ref": cref,
            "bound_refs_sha256": digest(json_bytes(sorted(fingerprints))),
            "user_messages": chat_record["user_messages"],
            "assistant_messages": chat_record["assistant_messages"],
            "chat_skipped_nodes": chat_record["skipped_nodes"],
            "correction_signals": hits,
            "desktop_call_count": len(selections),
            "desktop_calls_by_tool": dict(sorted(counts.items())),
            "desktop_outcomes": {key: outcomes[key] for key in
                                 ("explicit_error", "explicit_success", "unknown")},
            "reported_complete": task["completion_claim"] == "reported_complete",
            "completion_verification": "not_verified_requires_independent_artifact",
            "causal_conclusion": None,
            "improvement_candidates": improvements[:6],
        })
    return {
        "schema_version": "1.0",
        "record_type": "offline-explicit-task-binding-review",
        "analyzer_version": VERSION,
        "source_sha256": {"chatgpt": chat_sha256, "desktop": desktop_sha256},
        "binding_manifest_sha256": digest(json_bytes(manifest)),
        "task_count": len(result),
        "bound_conversation_count": len(used_conversations),
        "bound_desktop_call_count": len(used_calls),
        "task_linking": "explicit_source_hash_and_index_only",
        "tasks": sorted(result, key=lambda item: item["task_id_sha256"]),
        "task_completion_verified": False,
        "external_evidence_checked": False,
        "unlinked_calls": len(calls) - len(used_calls),
        "unlinked_conversations": len(conversations) - len(used_conversations),
        "status": "manual_review_required",
        "approved_for_instructions": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
        "limitations": [
            "明示Task IDによる申告は、会話と実行が実際に同一業務だった証明ではない",
            "実行ログのハッシュは真正な操作者や実行環境を証明しない",
            "成功を報告しても成果物・公開先・GitHub状態を独立確認するまでは未検証",
            "会話本文・実行引数・ツール結果はレポートに含めない",
        ],
    }
