"""Issue #41: ChatGPT会話とDesktop Commander呼び出し履歴の第3のオフライン振り返り。

入力はユーザーが明示的に選んだローカルJSON / JSONL。会話本文・コマンド・
ツール結果・トークンをレポートに転載しない。時刻だけでタスクを結びつけない。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .core import InvalidData, check_sensitive, digest, json_bytes
from .privacy_scan import BUILTIN_RULES
from .session_retro import RULES

VERSION = "chatgpt-task-improvement-1"
MAX_CONVERSATIONS = 3000
MAX_NODES = 200_000
MAX_CALLS = 15_000
MAX_TEXT = 15_000
KNOWN_TOOLS = {
    "start_process", "read_process_output", "interact_with_process", "read_file",
    "read_multiple_files", "write_file", "edit_block", "list_directory",
    "start_search", "get_more_search_results", "stop_search", "move_file",
    "get_file_info", "create_directory", "list_processes", "kill_process",
    "force_terminate", "get_recent_tool_calls", "get_usage_stats",
}
CHAT_RULES = tuple(r for r in RULES if r["role"] == "USER")
CORRECTIONS = ("explicit_correction", "missing_verification", "instruction_conflict",
               "repeatable_workflow", "role_boundary")
OMISSION = "[output omitted from history:"
FAILED_STATUSES = {"error", "failed", "failure"}
SUPPORTED_ROLE = {"user", "assistant"}


def _check_text(text: str, terms: list[str]) -> None:
    """分析対象の既知機密・PIIを検出。検出本文を例外に含めない。"""
    if len(text) > MAX_TEXT:
        raise InvalidData("メッセージが上限長を超過")
    check_sensitive(text)
    if any(rule.search(text) for rule in BUILTIN_RULES.values()):
        raise InvalidData("既知の個人情報を含むため分析を保留")
    if any(word.strip().casefold() in text.casefold() for word in terms):
        raise InvalidData("機密語辞書に一致するため分析を保留")


def _chat_items(data: Any) -> list[dict[str, Any]]:
    # ChatGPTのエクスポートは版差がある。未確認構造を推測変換しない。
    items = data if isinstance(data, list) else data.get("conversations") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > MAX_CONVERSATIONS or not all(
        isinstance(item, dict) for item in items
    ):
        raise InvalidData("会話エクスポート形式が未対応")
    return items


def _current_branch(mapping: dict[str, Any], current_node: str) -> list[dict[str, Any]]:
    if not isinstance(mapping, dict) or len(mapping) > MAX_NODES or not isinstance(current_node, str):
        raise InvalidData("会話ノードの形式が未対応")
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    pointer: str | None = current_node
    while pointer is not None:
        if (not isinstance(pointer, str) or pointer in seen or len(chain) > len(mapping)
                or pointer not in mapping):
            raise InvalidData("会話ツリーに欠落または循環")
        seen.add(pointer)
        node = mapping[pointer]
        if not isinstance(node, dict):
            raise InvalidData("会話ノードがJSONオブジェクトではありません")
        chain.append(node)
        pointer = node.get("parent")
    return list(reversed(chain))


def _messages(conversation: dict[str, Any]) -> tuple[list[tuple[str, str]], int]:
    if not isinstance(conversation.get("id"), str) or not conversation["id"]:
        raise InvalidData("会話IDが存在しません")
    chain = _current_branch(conversation.get("mapping"), conversation.get("current_node"))
    messages: list[tuple[str, str]] = []
    skipped = 0
    for node in chain:
        msg = node.get("message")
        if msg is None:
            continue
        if not isinstance(msg, dict):
            skipped += 1
            continue
        author = msg.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        content = msg.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        # tool/system/developerの情報は一切取り込まない。
        if role not in SUPPORTED_ROLE or not isinstance(parts, list) or not parts:
            skipped += 1
            continue
        if any(not isinstance(part, str) for part in parts):
            skipped += 1
            continue
        text = "\n".join(parts)
        if not text.strip():
            skipped += 1
            continue
        messages.append((role, text))
    return messages, skipped


def summarize_chatgpt(data: Any, literals: list[str]) -> dict[str, Any]:
    """会話はアクティブな系統のみ。訂正ルールの件数を返し、本文/会話名は返さない。"""
    items = _chat_items(data)
    ids: set[str] = set()
    role_counts: Counter[str] = Counter()
    hit_counts: Counter[str] = Counter()
    skipped = 0
    for conversation in items:
        if conversation.get("id") in ids:
            raise InvalidData("重複会話IDが見つかりました")
        ids.add(conversation.get("id"))
        messages, omissions = _messages(conversation)
        skipped += omissions
        for role, content in messages:
            if role == "user":
                _check_text(content, literals)
                for rule in CHAT_RULES:
                    if rule["pattern"].search(content):
                        hit_counts[rule["id"]] += 1
            role_counts[role] += 1
    return {
        "input_format": "chatgpt-export-conversations-mapping",
        "conversations": len(items),
        "user_messages": role_counts["user"],
        "assistant_messages": role_counts["assistant"],
        "skipped_nodes_on_selected_branches": skipped,
        "other_conversation_branches_examined": False,
        "correction_signals": dict(sorted(hit_counts.items())),
        "coverage": "partial" if skipped else "selected_branches_only",
        "task_success_verified": False,
        "source_text_exported": False,
    }


def _history_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("calls"), list):
        records = data["calls"]
    else:
        raise InvalidData("Desktop Commander履歴はJSON配列またはcalls配列が必要")
    if len(records) > MAX_CALLS or not all(isinstance(item, dict) for item in records):
        raise InvalidData("履歴レコード形式または件数の上限が不正")
    return records


def _omitted(output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    content = output.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and isinstance(item.get("text"), str)
               and item["text"].startswith(OMISSION) for item in content)


def _failure(record: dict[str, Any]) -> str:
    """曖昧な通常出力は成功扱いしない。明示的なエラーフラグのみ判定。"""
    if record.get("isError") is True:
        return "explicit_error"
    output = record.get("output")
    if isinstance(output, dict):
        if output.get("isError") is True or output.get("status") in FAILED_STATUSES:
            return "explicit_error"
        if output.get("isError") is False or output.get("status") == "success":
            return "explicit_success"
    return "unknown"


def summarize_desktop_history(data: Any) -> dict[str, Any]:
    """実行引数・出力は読み取っても検査/生成しない。厳格な匿名集計のみ。"""
    records = _history_records(data)
    tools: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    unknown = 0
    omissions = 0
    for record in records:
        name = record.get("toolName")
        if not isinstance(name, str) or not name.strip():
            raise InvalidData("toolNameの形式が不正")
        key = name if name in KNOWN_TOOLS else "other_tool"
        tools[key] += 1
        unknown += key == "other_tool"
        flags[_failure(record)] += 1
        omissions += int(_omitted(record.get("output")))
    return {
        "input_format": "desktop-commander-tool-history",
        "calls": len(records),
        "calls_by_tool": dict(sorted(tools.items())),
        "explicit_error": flags["explicit_error"],
        "explicit_success": flags["explicit_success"],
        "outcome_unknown": flags["unknown"],
        "output_truncated": omissions,
        "unclassified_tool_names": unknown,
        "duration_units_verified": False,
        "task_success_verified": False,
        "arguments_exported": False,
        "output_text_exported": False,
    }


def _suggestions(chat: dict[str, Any] | None, desktop: dict[str, Any] | None) -> list[dict[str, Any]]:
    """改善策は固定ラベルの候補。根本原因・効果・タスク達成を認定しない。"""
    corrections = chat["correction_signals"] if chat else {}
    suggestions: list[dict[str, Any]] = []
    def add(code: str, count: int, target: str, action: str) -> None:
        suggestions.append({"rule_id": code, "signal_count": count, "target": target,
                            "proposal": action, "assessment": "heuristic_review_only"})
    n = corrections.get("missing_verification", 0)
    if n:
        add("completion_evidence", n, "ChatGPT Project instructions",
            "作業完了前に成果物・検収結果を照合し、未実施は未実施と報告する候補を検討する")
    n = corrections.get("explicit_correction", 0) + corrections.get("instruction_conflict", 0)
    if n:
        add("clarify_task_contract", n, "ChatGPT request template",
            "依頼時に目的・禁止事項・完了条件・検証方法を明記するテンプレートを検討する")
    if desktop:
        n = desktop["explicit_error"]
        if n:
            add("failure_recovery", n, "Desktop Commander workflow",
                "明示的なツール失敗の原因分類、再試行上限、証拠記録の標準手順を検討する")
        polls = desktop["calls_by_tool"].get("read_process_output", 0)
        if polls >= 3:
            add("polling_review", polls, "Desktop Commander workflow",
                "プロセス出力の再取得が適切な監視頻度か、個別タスクの根拠で確認する")
    if chat and desktop:
        add("explicit_task_trace", 0, "ChatGPT Task Trace",
            "会話とツール実行には共通のtask IDがないため、今後の作業に明示的なIDを付ける")
    if not suggestions:
        add("coverage_review", 0, "ChatGPT Task Improvement",
            "検出できた兆候がなくても対象データの範囲・欠落・成功証拠を人が確認する")
    return suggestions[:5]


def evaluate_sources(chat: Any | None, desktop: Any | None, *,
                     chat_source_sha256: str | None, desktop_source_sha256: str | None,
                     policy_sha256: str, literals: list[str]) -> dict[str, Any]:
    if chat is None and desktop is None:
        raise InvalidData("分析対象は少なくとも1件必要")
    counts_chat = summarize_chatgpt(chat, literals) if chat is not None else None
    counts_dc = summarize_desktop_history(desktop) if desktop is not None else None
    return {
        "schema_version": "1.0", "record_type": "chatgpt-task-improvement-preview",
        "analyzer_version": VERSION,
        "inputs": {
            "chatgpt_sha256": chat_source_sha256, "desktop_sha256": desktop_source_sha256,
            "policy_sha256": policy_sha256,
        },
        "chatgpt": counts_chat, "desktop_commander": counts_dc,
        "task_linking": "unlinked_requires_explicit_task_id",
        "cross_source_claims": [],
        "recommendations": _suggestions(counts_chat, counts_dc),
        "status": "manual_review_required", "causal_conclusion": None,
        "business_outcome_verified": False, "model_review_performed": False,
        "approved_for_instructions": False, "files_modified": False,
        "external_transfer_authorized": False, "network_calls": 0,
        "limitations": [
            "ChatGPTエクスポートの版別実機形式は未検証。選択中の会話分岐のみ扱う",
            "Desktop Commander履歴は保持件数・長文出力が省略され得る",
            "フラグがないツール呼び出しは成功でも失敗でもなく不明として集計する",
            "会話とツール履歴は自動関連付けせず、業務完了や原因は検証しない",
            "既知の機密パターン検査は組織DLPの代わりにならない",
        ],
    }
