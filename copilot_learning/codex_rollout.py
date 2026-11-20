"""Issue #46: Codex rollout JSONLを安全なオフラインSession Retrospectiveへ接続する。

Codex公式rolloutの timestamp/type/payload を厳格に解釈する。
user_message と assistant output_text だけを選別し、tool引数・出力、
reasoning、cwd、base_instructionsはコピー/保存しない。
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import (
    InvalidData, MAX_EVENT_TEXT_CHARS, SHA_PATTERN, assert_external_output,
    content_fingerprint, digest, json_bytes, normalize_events, read_archive,
)
from .privacy_scan import BUILTIN_RULES, load_policy
from .codex_event_telemetry import (
    CodexTelemetry, KNOWN_TOP_METADATA_TYPES, UNREPLAYED_TOP_CONTEXT,
)
from .session_retro import analyze_sessions
from .skill_preview import ROOT, RESERVED, _path
from scripts.prepare_sessions import build_archive

VERSION = "codex-rollout-allowlist-1"
MAX_SOURCE_BYTES = 48 * 1024 * 1024
MAX_LINES = 100_000
MAX_EMITTED_MESSAGES = 8000
MAX_STAGED_BYTES = 12 * 1024 * 1024
RECEIPT_FILE = "codex-import-receipt.json"
PREVIEW_FILE = "codex-analysis.preview.json"
SOURCE_FORMAT = "codex-rollout-jsonl-v1"
KNOWN_TOP_TYPES = frozenset({
    "session_meta", "event_msg", "response_item", "turn_context", "compacted",
}) | KNOWN_TOP_METADATA_TYPES
TOOL_TYPES = frozenset({"function_call", "custom_tool_call"})
OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
EMAIL_ANYWHERE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_ANYWHERE = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    r"|sk-[A-Za-z0-9_-]{20,}"
)


def _safe_file(path: Path) -> Path:
    src = _path(path)
    assert_external_output(src, ROOT)
    if (not src.is_file() or src.is_symlink() or not src.name.startswith("rollout-")
            or src.suffix.lower() != ".jsonl"
            or not 0 < src.stat().st_size <= MAX_SOURCE_BYTES):
        raise InvalidData("Codexのrollout JSONLが不在または容量/形式が不正です")
    return src


def _source(path: Path, min_age_minutes: int) -> bytes:
    if type(min_age_minutes) is not int or not 1 <= min_age_minutes <= 10080:
        raise InvalidData("rollout最終更新からの最短保留時間が不正です")
    src = _safe_file(path)
    before = src.stat()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    if datetime.fromtimestamp(before.st_mtime, timezone.utc) > cutoff:
        raise InvalidData("Codexセッションが使用中の可能性があるため保留します")
    with src.open("rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    after = src.stat()
    if (len(raw) != before.st_size or len(raw) > MAX_SOURCE_BYTES
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
        raise InvalidData("rolloutが読み取り中に更新されたため保留します")
    return raw


def _utc(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 48:
        raise InvalidData("rolloutイベントの日時が欠落/不正です")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise InvalidData("rolloutイベントの日付形式を解釈できません") from exc


def _guard_text(text: Any, literals: list[str]) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_EVENT_TEXT_CHARS:
        raise InvalidData("Codex発言テキストが不正または上限超過です")
    from .core import check_sensitive
    check_sensitive(text)
    if (EMAIL_ANYWHERE.search(text) or TOKEN_ANYWHERE.search(text)
            or any(rule.search(text) for rule in BUILTIN_RULES.values())
            or any(term.strip().casefold() in text.casefold() for term in literals)):
        raise InvalidData("認識したメッセージに機密/個人情報の疑いがあります")
    return text


def _tool_category(name: Any) -> str:
    """未知の内部tool名は外に出さず固定カテゴリにする。"""
    if not isinstance(name, str):
        return "other_tool"
    if name in {"exec_command", "shell_command", "container.exec", "functions.exec", "functions.shell"}:
        return "shell_tool"
    if name in {"apply_patch", "functions.apply_patch"}:
        return "patch_tool"
    if name in {"web.run", "search", "functions.search"}:
        return "search_tool"
    if name.startswith("mcp__"):
        return "mcp_tool"
    return "other_tool"


def _parse(raw: bytes, literals: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise InvalidData("rolloutはUTF-8 JSONLである必要があります") from exc
    if not 1 <= len(lines) <= MAX_LINES:
        raise InvalidData("rolloutの行数が不正または上限超過です")
    # Source line is 1-based. User event_msg is preferred over the duplicate
    # response_item(role=user) produced by Codex; fallback only if no user events.
    native_user: list[tuple[int, str, str, str]] = []
    fallback_user: list[tuple[int, str, str, str]] = []
    assistant: list[tuple[int, str, str, str]] = []
    ignored: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    tool_categories: Counter[str] = Counter()
    telemetry = CodexTelemetry()
    metadata_id: str | None = None
    errored_or_interrupted = 0
    nonempty = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        nonempty += 1
        try:
            record = json.loads(
                line, parse_constant=lambda _: (_ for _ in ()).throw(
                    InvalidData("非有限JSON定数を受け付けません")))
        except json.JSONDecodeError as exc:
            raise InvalidData("rollout JSONLに解釈不能なレコードがあります") from exc
        if (not isinstance(record, dict) or not isinstance(record.get("type"), str)
                or not isinstance(record.get("payload"), dict)):
            raise InvalidData("rolloutのtop-level type/payloadが不正です")
        top = record["type"]
        payload = record["payload"]
        if nonempty == 1 and top != "session_meta":
            raise InvalidData("先頭のsession_metaが確認できません")
        if top == "session_meta":
            if nonempty != 1 or metadata_id is not None:
                raise InvalidData("同じrolloutに複数のsession_metaを含められません")
            sid = payload.get("id")
            if not isinstance(sid, str) or not 4 <= len(sid) <= 128:
                raise InvalidData("session_metaのIDが不正です")
            metadata_id = sid
            ignored["session_meta"] += 1
            continue
        if top not in KNOWN_TOP_TYPES:
            unsupported["unknown_top_type"] += 1
            continue
        if top in KNOWN_TOP_METADATA_TYPES:
            # No raw tool/config/context payload is exported.
            telemetry.top(top, payload)
            ignored["documented_metadata_" + top] += 1
            if top in UNREPLAYED_TOP_CONTEXT:
                unsupported["documented_context_not_replayed"] += 1
            continue
        if top in {"turn_context", "compacted"}:
            ignored[top] += 1
            if top == "compacted":
                unsupported["compacted_context_not_replayed"] += 1
            continue
        if top == "event_msg":
            subtype = payload.get("type")
            known_event = telemetry.event(payload)
            if subtype == "user_message":
                text = payload.get("message")
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_EVENT_TEXT_CHARS:
                    unsupported["invalid_user_message"] += 1
                    continue
                if payload.get("kind") not in (None, "plain"):
                    unsupported["unknown_user_kind"] += 1
                    continue
                native_user.append((line_number, "user", _utc(record.get("timestamp")),
                                    _guard_text(text, literals)))
                if payload.get("local_images") or payload.get("text_elements"):
                    unsupported["user_context_not_replayed"] += 1
            elif subtype in {"error", "turn_aborted"}:
                errored_or_interrupted += 1
                ignored["agent_error_or_interrupt"] += 1
            elif known_event:
                # Lifecycle / token telemetry is counted without interpreting its text.
                ignored["known_event_msg_telemetry_only"] += 1
            else:
                # Previously this was silently ignored and incorrectly looked complete.
                unsupported["unknown_event_msg_type"] += 1
            continue
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                ignored["non_user_assistant_message"] += 1
                continue
            if role == "assistant" and (payload.get("channel") not in (None, "final", "commentary")
                                       or payload.get("recipient") not in (None, "all")):
                ignored["internal_assistant_message"] += 1
                continue
            contents = payload.get("content")
            expected = "input_text" if role == "user" else "output_text"
            if (not isinstance(contents, list) or not contents
                    or any(not isinstance(c, dict) or c.get("type") != expected
                           or not isinstance(c.get("text"), str) for c in contents)):
                unsupported["unsupported_message_content"] += 1
                continue
            text = "\n".join(c["text"] for c in contents)
            if not text.strip() or len(text) > MAX_EVENT_TEXT_CHARS:
                unsupported["message_text_absent_or_long"] += 1
                continue
            item = (line_number, role, _utc(record.get("timestamp")),
                    _guard_text(text, literals))
            (fallback_user if role == "user" else assistant).append(item)
        elif kind in TOOL_TYPES:
            tool_categories[_tool_category(payload.get("name"))] += 1
            ignored["tool_call_arguments_excluded"] += 1
        elif kind in OUTPUT_TYPES:
            ignored["tool_result_excluded"] += 1
        elif kind == "reasoning":
            ignored["reasoning_excluded"] += 1
        else:
            unsupported["unknown_response_item"] += 1
    if metadata_id is None:
        raise InvalidData("Codexのsession_metaを確認できません")
    # Codexは同じユーザー発言をevent_msgとresponse_itemの両方に記録し得る。
    # ただしevent_msgが一部欠けた場合は、その分を無言で捨てない。
    remaining = Counter(item[3] for item in native_user)
    supplemental: list[tuple[int, str, str, str]] = []
    duplicate_count = 0
    for item in fallback_user:
        if remaining[item[3]] > 0:
            remaining[item[3]] -= 1
            duplicate_count += 1
        else:
            supplemental.append(item)
    users = [*native_user, *supplemental]
    if not users:
        raise InvalidData("セッションに安全に認識できるユーザー発言がありません")
    if duplicate_count:
        ignored["duplicate_user_response_items"] += duplicate_count
    if supplemental and native_user:
        unsupported["user_event_msg_missing_somewhere"] += len(supplemental)
    selected = sorted([*users, *assistant], key=lambda x: x[0])
    if len(selected) > MAX_EMITTED_MESSAGES:
        raise InvalidData("取込メッセージの件数が上限超過です")
    events: list[dict[str, Any]] = []
    mappings: list[dict[str, int]] = []
    for source_line, role, timestamp, text in selected:
        events.append({"type": "message", "role": role, "text": text,
                       "timestamp": timestamp})
        mappings.append({"event_index": len(events) - 1, "source_line": source_line})
    if unsupported:
        # 既存のNormalizer/Retrospectiveにも入力カバレッジ不足を伝える。
        events.append({"type": "codex.unrecognized_records"})
    return events, {
        "source_format": SOURCE_FORMAT,
        "adapter_version": VERSION,
        "line_count": nonempty,
        "recognized_message_count": len(selected),
        "native_user_message_count": len(native_user),
        "fallback_user_response_count": len(supplemental),
        "assistant_output_text_count": len(assistant),
        "tool_calls_by_category": dict(sorted(tool_categories.items())),
        "tool_call_count": sum(tool_categories.values()),
        "tool_results_not_exported": ignored["tool_result_excluded"],
        "agent_error_or_interruption_signal_count": errored_or_interrupted,
        "ignored_event_categories": dict(sorted(ignored.items())),
        "unsupported_categories": dict(sorted(unsupported.items())),
        "source_line_map": mappings,
        "coverage": "partial_unknown_or_context" if unsupported else "known_text_subset_only",
        "tool_success_evaluation": "not_performed",
        "task_outcome_verified": False,
        "event_telemetry": telemetry.report(),
    }, metadata_id


def _prepare(rollout: Path, policy_path: Path, *, min_age_minutes: int,
             prepared_at_utc: str | None = None) -> tuple[str, dict[str, bytes], dict[str, Any]]:
    policy, policy_sha256 = load_policy(_path(policy_path))
    source = _source(rollout, min_age_minutes)
    source_sha = digest(source)
    events, stats, metadata_id = _parse(source, policy["sensitive_literals"])
    event_bytes = "".join(
        json.dumps(x, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        for x in events
    ).encode("utf-8")
    if len(event_bytes) > MAX_STAGED_BYTES:
        raise InvalidData("抽出したメッセージの合計容量が上限超過です")
    parts = [{"name": "events.jsonl", "sha256": digest(event_bytes),
              "size_bytes": len(event_bytes)}]
    fp = content_fingerprint(parts)
    archive_bytes = build_archive([("events.jsonl", event_bytes)])
    if prepared_at_utc is None:
        prepared_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        if _utc(prepared_at_utc) != prepared_at_utc:
            raise InvalidData("保存時刻は正規UTCである必要があります")
    manifest = {
        "schema_version": "1.0", "record_type": "copilot-session-archive",
        "session_fingerprint": fp,
        "source_session_id_sha256": digest(metadata_id.encode("utf-8")),
        "source_format": SOURCE_FORMAT,
        "source_rollout_sha256": source_sha,
        "archive_file": f"session-{fp}.zip",
        "archive_sha256": digest(archive_bytes),
        "contents": parts,
        "review_status": "requires_manual_approval",
        "approved_for_copilot": False,
        "prepared_at_utc": prepared_at_utc,
    }
    normalized = normalize_events(manifest, events)
    retro = analyze_sessions([normalized])
    preview = {
        "schema_version": "1.0",
        "record_type": "offline-codex-rollout-review",
        "source_rollout_sha256": source_sha,
        "session_fingerprint": fp,
        "provenance": stats,
        "retrospective": retro,
        "real_codex_execution_verified": False,
        "completion_verified": False,
        "approved_for_instructions": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
        "limitations": [
            "Codexアプリ・CLIの全バージョンでの一致は未検証",
            "選別したユーザー発言・assistant output_textだけをローカルZIPに保存",
            "ツール引数・実行結果・内部推論・cwd・base_instructionsは出力しない",
            "公式のタスク完了イベントはCodexのターン終了を表し、業務完了ではない",
            "token_countは累積スナップショットであり、各イベントを合計しない",
            "失敗はCodexが明示したイベントのみ兆候として扱い、ツール成功を推測しない",
            "部分的な応答や画像等の隠れた文脈があれば入力カバレッジ不足とする",
            "静的機密検査は情報管理規程・組織DLPの代替にならない",
        ],
    }
    preview_bytes = json_bytes(preview)
    manifest_bytes = json_bytes(manifest)
    receipt = {
        "schema_version": "1.0",
        "record_type": "offline-codex-import-receipt",
        "source_rollout_sha256": source_sha,
        "source_session_id_sha256": manifest["source_session_id_sha256"],
        "policy_sha256": policy_sha256,
        "prepared_at_utc": prepared_at_utc,
        "archive_sha256": digest(archive_bytes),
        "manifest_sha256": digest(manifest_bytes),
        "preview_sha256": digest(preview_bytes),
        "session_fingerprint": fp,
        "source_line_map": stats["source_line_map"],
        "status": "manual_review_required",
        "external_transfer_authorized": False,
        "network_calls": 0,
    }
    contents = {
        f"session-{fp}.zip": archive_bytes,
        f"session-{fp}.manifest.json": manifest_bytes,
        PREVIEW_FILE: preview_bytes,
        RECEIPT_FILE: json_bytes(receipt),
    }
    return fp, contents, receipt


def _destination(path: Path) -> Path:
    out = _path(path)
    assert_external_output(out, ROOT)
    if any(part.rstrip(" .").casefold() in (RESERVED | {".codex"}) for part in out.parts):
        raise InvalidData("Git/エージェント自動読込先には保存できません")
    return out


def import_rollout(rollout: Path, policy: Path, destination: Path, *,
                   min_age_minutes: int = 5) -> dict[str, Any]:
    """選別した安全な本文をGit管理外の新規フォルダへ保存する。"""
    fp, contents, receipt = _prepare(rollout, policy, min_age_minutes=min_age_minutes)
    out = _destination(destination)
    if out.exists():
        raise InvalidData("既存フォルダを上書きしません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    written: list[Path] = []
    try:
        for name, body in contents.items():
            file = out / name
            with file.open("xb") as handle:
                written.append(file)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            file.chmod(0o400)
        read_archive(out, fp)
    except BaseException:
        for file in reversed(written):
            file.chmod(0o600)
            file.unlink(missing_ok=True)
        out.rmdir()
        raise
    return {
        "status": "imported_for_manual_review",
        "session_fingerprint": fp,
        "receipt_sha256": digest(contents[RECEIPT_FILE]),
        "recognized_message_count": json.loads(contents[PREVIEW_FILE])["provenance"]["recognized_message_count"],
        "real_codex_execution_verified": False,
        "network_calls": 0,
    }


def verify_rollout(rollout: Path, policy: Path, directory: Path, *,
                   expected_receipt_sha256: str, min_age_minutes: int = 5) -> dict[str, Any]:
    """独立保管したreceipt SHAを最初に検査し、元rollout/辞書/全成果物を再照合。"""
    if not isinstance(expected_receipt_sha256, str) or not SHA_PATTERN.fullmatch(expected_receipt_sha256):
        raise InvalidData("独立保管したreceipt SHA-256が必要です")
    out = _destination(directory)
    receipt_file = _path(out / RECEIPT_FILE)
    if not receipt_file.is_file() or receipt_file.stat().st_size > 1_000_000:
        raise InvalidData("取込receiptが不在または上限超過です")
    pinned_raw = receipt_file.read_bytes()
    if digest(pinned_raw) != expected_receipt_sha256:
        raise InvalidData("取込receiptのハッシュが一致しません")
    try:
        pinned = json.loads(pinned_raw.decode("utf-8"))
        timestamp = pinned["prepared_at_utc"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidData("保存済みreceiptの形式を検証できません") from exc
    fp, expected, _ = _prepare(rollout, policy, min_age_minutes=min_age_minutes,
                               prepared_at_utc=timestamp)
    if not out.is_dir() or {f.name for f in out.iterdir()} != set(expected):
        raise InvalidData("取込成果物の構成が変わりました")
    for name, body in expected.items():
        target = _path(out / name)
        if not target.is_file() or target.stat().st_size != len(body) or target.read_bytes() != body:
            raise InvalidData("元データ・辞書・取込成果物が一致しません")
    read_archive(out, fp)
    return {
        "status": "verified_offline_codex_import",
        "session_fingerprint": fp,
        "real_codex_execution_verified": False,
        "network_calls": 0,
    }
