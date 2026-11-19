"""#36: VS Code Chat: Export Chat JSONの限定フィールドを安全にローカルZIP化する。

公式実装ChatModel.toExport()の requests[].message.text と
response[].value（kindなしのMarkdown）のみを扱う。
内部プロンプト・ツール引数・結果・隠しメッセージ・思考・添付を復元しない。
実ファイルへのアクセスは明示的オプトインと外部ポリシー必須。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import (
    InvalidData, MAX_EVENT_TEXT_CHARS, assert_external_output,
    check_sensitive, content_fingerprint, digest, json_bytes,
    normalize_events, read_archive,
)
from .privacy_scan import BUILTIN_RULES, load_policy
from .skill_preview import ROOT, RESERVED, _path
from scripts.prepare_sessions import build_archive

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 2000
MAX_PARTS_PER_REQUEST = 100
MAX_STAGED_EVENTS = 10000
VERSION = "vscode-export-adapter-1"
# 日本語に隣接するASCIIメールも保護する。既存の汎用regexの\w境界に依存しない。
EMAIL_ANYWHERE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KEY_ANYWHERE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}|(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})|sk-[A-Za-z0-9_-]{20,}")
RECEIPT_FILE = "import-receipt.json"
# 公式IExportableChatDataの最低限の判別キー。モデル.toJSONとの混同を禁止。
EXPECTED_EXPORT_KEYS = frozenset({"responderUsername", "requests"})

def _safe_input(path: Path) -> Path:
    src = _path(path)
    assert_external_output(src, ROOT)
    if not src.is_file() or src.is_symlink():
        raise InvalidData("指定された手動エクスポートは通常ファイルである必要があります")
    if not 0 < src.stat().st_size <= MAX_SOURCE_BYTES:
        raise InvalidData("エクスポートのファイルサイズが上限外です")
    return src


def _load_export(path: Path, literals: list[str]) -> tuple[bytes, dict[str, Any]]:
    src = _safe_input(path)
    initial = src.stat()
    with src.open("rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    final = src.stat()
    if (not raw or len(raw) > MAX_SOURCE_BYTES or initial.st_size != len(raw)
            or initial.st_size != final.st_size or initial.st_mtime_ns != final.st_mtime_ns):
        raise InvalidData("エクスポートが読み取り中に変更された可能性があります")
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(
            InvalidData("非有限のJSON値を含むエクスポートは受け付けません")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidData("エクスポートはUTF-8 JSONが必要です") from exc
    # 保管するのは許可フィールドだけでも、原本全体の既知機密を一次検査する。
    check_sensitive(text)
    if (EMAIL_ANYWHERE.search(text) or KEY_ANYWHERE.search(text)
            or any(pattern.search(text) for pattern in BUILTIN_RULES.values())
            or any(word.casefold() in text.casefold() for word in literals)):
        raise InvalidData("機密・個人情報の疑いがあるため処理を拒否しました")
    if (not isinstance(payload, dict) or not EXPECTED_EXPORT_KEYS <= payload.keys()
            or not isinstance(payload["responderUsername"], str)
            or not isinstance(payload["requests"], list)):
        raise InvalidData("VS Code Chatの公式JSONエクスポート形式が確認できません")
    return raw, payload


def _time(ms: Any) -> str | None:
    # VS Code ChatModelはミリ秒epoch。数値以外と不合理な値を推定補完しない。
    if type(ms) is not int or not 0 <= ms < 4_102_444_800_000:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError, OSError):
        return None


def _valid_content(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > MAX_EVENT_TEXT_CHARS:
        return None
    # 二次検査は入力全体で実施済み。ここでも対象本文の既知Secretを拒否する。
    check_sensitive(value)
    return value


def build_conversion(raw: bytes, payload: dict[str, Any],
                     policy_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """検証済みexportからメッセージと不明イベントの指標を固定的に生成する。"""
    requests = payload["requests"]
    if not 1 <= len(requests) <= MAX_REQUESTS:
        raise InvalidData("VS Code JSONのrequest件数は1〜2000件に制限しています")
    events: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    response_time_fallbacks = 0
    for idx, req in enumerate(requests):
        if not isinstance(req, dict):
            raise InvalidData("requestの構造が不正です")
        # 隠された会話・system起点の要求は未知として扱い、引用可能な本文にしない。
        hidden = any(req.get(k) is True for k in (
            "isSystemInitiated", "hiddenFromTranscript", "requestHiddenFromTranscript"))
        raw_message = req.get("message")
        if isinstance(raw_message, dict):
            message = raw_message.get("text")
        elif isinstance(raw_message, str):  # 公式旧形式の後方互換
            message = raw_message
        else:
            message = None
        request_time = _time(req.get("timestamp"))
        if hidden or request_time is None or _valid_content(message) is None:
            reason = "hidden_or_system" if hidden else (
                "request_timestamp_missing" if request_time is None else "request_text_missing")
            skipped[reason] = skipped.get(reason, 0) + 1
            events.append({"type": "vscode.unsupported_request",
                           "source_request_index": idx, "reason": reason})
            mapping.append({"event_index": len(events)-1, "request_index": idx,
                            "response_part_index": None, "timestamp_basis": None})
            continue
        events.append({"type": "message", "role": "user", "text": message,
                       "timestamp": request_time,
                       "timestamp_basis": "vscode_request_timestamp"})
        mapping.append({"event_index": len(events)-1, "request_index": idx,
                        "response_part_index": None, "timestamp_basis": "request_timestamp"})
        parts = req.get("response")
        if not isinstance(parts, list) or len(parts) > MAX_PARTS_PER_REQUEST:
            skipped["response_missing_or_invalid"] = skipped.get("response_missing_or_invalid", 0) + 1
            events.append({"type": "vscode.response_unavailable"})
            mapping.append({"event_index": len(events)-1, "request_index": idx,
                            "response_part_index": None, "timestamp_basis": None})
            continue
        response_time = _time(req.get("responseTimestamp"))
        if response_time is None:
            response_time = request_time
            response_time_fallbacks += 1
        if not parts:
            skipped["response_empty"] = skipped.get("response_empty", 0) + 1
            events.append({"type": "vscode.response_empty"})
            mapping.append({"event_index": len(events)-1, "request_index": idx,
                            "response_part_index": None, "timestamp_basis": None})
            continue
        for part_index, part in enumerate(parts):
            # toExportはMarkdown partsを{value:"..."}で保存。
            # kind付きの工具/思考/編集内容には、valueがあってもアクセスしない。
            value = (part.get("value") if isinstance(part, dict)
                     and "kind" not in part and isinstance(part.get("value"), str)
                     else None)
            safe_text = _valid_content(value)
            if safe_text is None:
                skipped["unsupported_response_part"] = skipped.get("unsupported_response_part", 0) + 1
                events.append({"type": "vscode.unsupported_response_part"})
                ts_origin = None
            else:
                events.append({"type": "message", "role": "assistant",
                               "text": safe_text, "timestamp": response_time,
                               "timestamp_basis": (
                                   "vscode_response_timestamp" if _time(req.get("responseTimestamp"))
                                   else "vscode_request_timestamp_fallback")})
                ts_origin = ("response_timestamp" if _time(req.get("responseTimestamp"))
                             else "request_timestamp_fallback")
            mapping.append({"event_index": len(events)-1, "request_index": idx,
                            "response_part_index": part_index, "timestamp_basis": ts_origin})
        if len(events) > MAX_STAGED_EVENTS:
            raise InvalidData("変換後イベント数が上限を超えています")
    if not any(e.get("type") == "message" and e.get("role") == "user" for e in events):
        raise InvalidData("ユーザーの発言を安全に認識できないため取り込みません")
    return events, {
        "source_format": "vscode-chat-export-model-toExport",
        "adapter_version": VERSION,
        "source_export_sha256": digest(raw),
        "source_request_count": len(requests),
        "converted_event_count": len(events),
        "conversion_skipped_reasons": dict(sorted(skipped.items())),
        "response_time_fallback_request_count": response_time_fallbacks,
        "source_event_index_map": mapping,
        "policy_sha256": policy_sha256,
        "real_vscode_export_verified": False,
        "approved_for_copilot": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
    }


def prepare_import(export_path: Path, policy_path: Path, *,
                   prepared_at_utc: str | None = None) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    """S3なし。ZIP本文は再現可能、保存時刻は実際の取込時刻を記録する。"""
    if prepared_at_utc is None:
        prepared_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        try:
            parsed = datetime.fromisoformat(prepared_at_utc.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone")
        except (ValueError, AttributeError) as exc:
            raise InvalidData("保存時刻の形式が不正です") from exc
        if parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != prepared_at_utc:
            raise InvalidData("保存時刻はUTCの正規形式が必要です")
    policy, policy_sha = load_policy(_path(policy_path))
    raw, payload = _load_export(export_path, policy["sensitive_literals"])
    events, provenance = build_conversion(raw, payload, policy_sha)
    # JSONLは1イベントを1物理行で保存。json_bytes()は整形JSONのため利用しない。
    events_body = "".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    ).encode("utf-8")
    components = [{"name": "events.jsonl", "sha256": digest(events_body),
                   "size_bytes": len(events_body)}]
    fp = content_fingerprint(components)
    archive = build_archive([("events.jsonl", events_body)])
    # archive作成日時は実際の時刻を記録し、会話開始日時と混同しない。
    manifest = {
        "schema_version": "1.0", "record_type": "copilot-session-archive",
        "session_fingerprint": fp, "source_session_id_sha256": None,
        "source_session_id_status": "not_in_export_format",
        "archive_file": f"session-{fp}.zip",
        "archive_sha256": digest(archive),
        "contents": components,
        "review_status": "requires_manual_approval",
        "prepared_at_utc": prepared_at_utc,
        "source_export_format": provenance["source_format"],
        "source_export_sha256": provenance["source_export_sha256"],
        "adapter_version": VERSION,
    }
    # 実際の書き込み前に同じ正規化器で受け付けられるか確認。
    normalized = normalize_events(manifest, events)
    if normalized["recognized_event_count"] < 1:
        raise InvalidData("安全に認識できるイベントがありません")
    receipt = {
        "schema_version": "1.0",
        "record_type": "offline-vscode-export-import-receipt",
        **provenance,
        "session_fingerprint": fp,
        "archive_sha256": digest(archive),
        "manifest_sha256": digest(json_bytes(manifest)),
        "prepared_at_utc": prepared_at_utc,
        "recognized_event_count": normalized["recognized_event_count"],
        "skipped_event_count": normalized["skipped_event_count"],
        "status": ("partial_requires_manual_review" if normalized["skipped_event_count"]
                   else "manual_review_required"),
        "installation_status": "not_installed",
    }
    return receipt, archive, json_bytes(manifest), json_bytes(receipt)


def _destination(path: Path) -> Path:
    dest = _path(path)
    assert_external_output(dest, ROOT)
    if any(x.rstrip(" .").casefold() in RESERVED for x in dest.parts):
        raise InvalidData("Copilotの自動読込先へ出力できません")
    if dest.exists():
        raise InvalidData("既存のフォルダは上書きできません")
    return dest


def export_import(export_path: Path, policy_path: Path, destination: Path) -> dict[str, Any]:
    """Git管理外の新規フォルダにだけZIP/manifest/receiptを排他的に書く。"""
    receipt, archive, manifest, receipt_body = prepare_import(export_path, policy_path)
    dest = _destination(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _path(dest.parent)
    dest.mkdir(mode=0o700)
    fp = receipt["session_fingerprint"]
    entries = ((f"session-{fp}.zip", archive),
               (f"session-{fp}.manifest.json", manifest),
               (RECEIPT_FILE, receipt_body))
    created: list[Path] = []
    try:
        for name, body in entries:
            out = dest / name
            with out.open("xb") as handle:
                created.append(out)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            out.chmod(0o400)
        read_archive(dest, fp)
    except BaseException:
        for out in reversed(created):
            out.chmod(0o600)
            out.unlink(missing_ok=True)
        dest.rmdir()
        raise
    return {
        "status": receipt["status"], "session_fingerprint": fp,
        "receipt_sha256": digest(receipt_body),
        "skipped_event_count": receipt["skipped_event_count"],
        "network_calls": 0, "approved_for_copilot": False,
    }


def verify_import(export_path: Path, policy_path: Path, directory: Path,
                  expected_receipt_sha256: str) -> dict[str, Any]:
    """原本の現行bytesと独立保管のreceipt SHAを再照合する（通信なし）。"""
    if not isinstance(expected_receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256):
        raise InvalidData("独立保管したreceipt SHA-256が必要です")
    dest = _path(directory)
    assert_external_output(dest, ROOT)
    pinned_receipt = _path(dest / RECEIPT_FILE)
    if not pinned_receipt.is_file() or pinned_receipt.stat().st_size > 2_000_000:
        raise InvalidData("保存済みreceiptがありません")
    raw_receipt = pinned_receipt.read_bytes()
    # 改ざん済みの保存時刻を使わないよう、独立保管したSHAを先に照合。
    if digest(raw_receipt) != expected_receipt_sha256:
        raise InvalidData("receiptの独立SHAが一致しません")
    try:
        saved = json.loads(raw_receipt.decode("utf-8"))
        prepared = saved["prepared_at_utc"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise InvalidData("receiptの保存時刻を検証できません") from exc
    receipt, archive, manifest, receipt_body = prepare_import(
        export_path, policy_path, prepared_at_utc=prepared)
    fp = receipt["session_fingerprint"]
    expected = {f"session-{fp}.zip": archive,
                f"session-{fp}.manifest.json": manifest, RECEIPT_FILE: receipt_body}
    if digest(receipt_body) != expected_receipt_sha256:
        raise InvalidData("receiptの独立SHAが一致しません")
    if not dest.is_dir() or {p.name for p in dest.iterdir()} != set(expected):
        raise InvalidData("保存済み取込成果物の構成が一致しません")
    for name, body in expected.items():
        target = _path(dest / name)
        if not target.is_file() or target.stat().st_size != len(body) or target.read_bytes() != body:
            raise InvalidData("原本・ポリシー・取込成果物の同一性が失われています")
    read_archive(dest, fp)
    return {"status": "verified_offline_import_only",
            "session_fingerprint": fp,
            "approved_for_copilot": False, "network_calls": 0}
