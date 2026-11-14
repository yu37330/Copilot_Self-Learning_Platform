"""S3保存済みCopilotセッションの検証と学習向けイベント正規化。"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWED_ARCHIVE_FILES = {"events.jsonl", "workspace.yaml", "plan.md"}
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_EVENT_TEXT_CHARS = 10000
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bAuthorization\s*[:=]\s*[\"']?(?:Bearer|Basic)\s+\S+"),
    re.compile(r"(?i)\baws_secret_access_key\b\s*[=:]\s*[\"']?[A-Za-z0-9/+]{24,}"),
)


class InvalidData(ValueError):
    """入力のスキーマ、整合性、機密情報に関するエラー。"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def check_sensitive(text: str) -> None:
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise InvalidData("秘密情報の疑いを検出。対象本文は表示せず処理を停止しました")


def assert_external_output(path: Path, repository_root: Path | None = None) -> None:
    """Git管理内への実データ書き込みを防止する。"""
    resolved = path.expanduser().resolve()
    if repository_root is not None and (resolved == repository_root.resolve() or repository_root.resolve() in resolved.parents):
        raise InvalidData("出力先はリポジトリの外を指定してください")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise InvalidData("Git管理下への実データ出力は禁止です")


def read_archive(staging: Path, fingerprint: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """ZIPとmanifestを独立検証し、JSONLを読む（ネットワーク通信なし）。"""
    if not SHA_PATTERN.fullmatch(fingerprint):
        raise InvalidData("session_fingerprintはSHA-256形式が必要です")
    manifest_path = staging / f"session-{fingerprint}.manifest.json"
    archive_path = staging / f"session-{fingerprint}.zip"
    if not manifest_path.is_file() or not archive_path.is_file() or manifest_path.is_symlink() or archive_path.is_symlink():
        raise InvalidData("ZIP / manifestがありません、またはシンボリックリンクです")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if (manifest.get("schema_version") != "1.0" or
            manifest.get("record_type") != "copilot-session-archive" or
            manifest.get("session_fingerprint") != fingerprint or
            manifest.get("archive_file") != archive_path.name):
        raise InvalidData("manifestの識別情報が不一致です")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise InvalidData("ZIPの上限サイズを超過")
    if digest(archive_path.read_bytes()) != manifest.get("archive_sha256"):
        raise InvalidData("ZIPのSHA-256が一致しません")
    expected = manifest.get("contents")
    if not isinstance(expected, list) or not expected:
        raise InvalidData("manifestのcontentsが不正です")
    names = [item.get("name") for item in expected if isinstance(item, dict)]
    if len(names) != len(expected) or len(set(names)) != len(names) or "events.jsonl" not in names or set(names) - ALLOWED_ARCHIVE_FILES:
        raise InvalidData("ZIP内ファイル名に重複・禁止ファイル・必須項目不足があります")
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) != len(names) or set(item.filename for item in infos) != set(names):
            raise InvalidData("ZIPとmanifestでファイル構成が違います")
        files: dict[str, bytes] = {}
        total = 0
        for info in infos:
            if info.is_dir() or info.filename not in ALLOWED_ARCHIVE_FILES or info.file_size > MAX_ARCHIVE_BYTES:
                raise InvalidData("ZIP内に不正なエントリがあります")
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise InvalidData("ZIP展開サイズの上限超過")
            files[info.filename] = archive.read(info)
    for item in expected:
        content = files[item["name"]]
        if digest(content) != item.get("sha256") or len(content) != item.get("size_bytes"):
            raise InvalidData("manifestのファイルハッシュ・サイズが一致しません")
        check_sensitive(content.decode("utf-8"))
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(files["events.jsonl"].decode("utf-8").splitlines(), start=1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise InvalidData(f"JSONLの{line_no}行目がオブジェクトではありません")
            events.append(value)
    if not events:
        raise InvalidData("セッションにイベントがありません")
    return manifest, events


def _text_from_fields(value: dict[str, Any]) -> str | None:
    """曖昧なイベントは捨てる。ツール引数などの任意JSON文字列化は禁止。"""
    for name in ("text", "content", "message"):
        if isinstance(value.get(name), str) and value[name].strip():
            return value[name]
    return None


def _timestamp(value: dict[str, Any], fallback: str) -> tuple[str, str]:
    for key in ("timestamp", "created_at", "createdAt"):
        raw = value.get(key)
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), key
            except ValueError:
                pass
    return fallback, "manifest_prepared_at_utc"


def normalize_events(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """厳密な許可リストで変換。未知のCopilot eventは未対応と記録する。"""
    fp = manifest["session_fingerprint"]
    fallback = manifest.get("prepared_at_utc")
    if not isinstance(fallback, str):
        raise InvalidData("manifestにprepared_at_utcがありません")
    try:
        if datetime.fromisoformat(fallback.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise InvalidData("prepared_at_utcがタイムゾーン付きISO時刻ではありません") from exc
    messages: list[dict[str, Any]] = []
    skipped = 0
    for index, record in enumerate(events):
        # イベントのフォーマットを憶測せず、messageとして明示されたものだけを受け取る。
        if record.get("type") not in {"message", "conversation_message"}:
            skipped += 1
            continue
        role = record.get("role")
        if not isinstance(role, str) or role.lower() not in {"user", "assistant", "tool"}:
            skipped += 1
            continue
        text = _text_from_fields(record)
        if text is None or len(text) > MAX_EVENT_TEXT_CHARS:
            skipped += 1
            continue
        check_sensitive(text)
        timestamp, source = _timestamp(record, fallback)
        messages.append({
            "event_index": index, "role": role.upper(), "text": text,
            "timestamp": timestamp, "timestamp_source": source,
            "evidence_id": f"{fp}:{index}",
        })
    return {
        "schema_version": "1.0", "record_type": "normalized-session",
        "session_fingerprint": fp, "source_event_count": len(events),
        "recognized_event_count": len(messages), "skipped_event_count": skipped,
        "messages": messages, "format_status": "recognized" if messages else "unsupported_format",
        "normalizer_version": "allowlist-1.0",
    }


def load_feedback(path: Path | None, fingerprint: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    data = json.loads(path.read_text("utf-8"))
    records = data if isinstance(data, list) else [data]
    for item in records:
        if not isinstance(item, dict) or item.get("session_fingerprint") != fingerprint:
            raise InvalidData("フィードバックの対象セッションが不一致です")
        if item.get("outcome") not in {"verified_success", "verified_failure", "unverified"}:
            raise InvalidData("outcomeの値が不正です")
        if not isinstance(item.get("reusable_lesson", ""), str) or len(item.get("reusable_lesson", "")) > MAX_EVENT_TEXT_CHARS:
            raise InvalidData("reusable_lessonの形式・長さが不正です")
        check_sensitive(json.dumps(item, ensure_ascii=False))
    return records


def build_memory_requests(session: dict[str, Any], memory_id: str, actor_id: str) -> list[dict[str, Any]]:
    """AWS CreateEvent の引数だけを作る（呼び出しは行わない）。"""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-_/]{0,254}", actor_id):
        raise InvalidData("actor_idがAWSの制約に適合しません")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise InvalidData("Memory IDが指定されていません")
    fp = session["session_fingerprint"]
    session_id = f"s_{fp[:48]}"
    result = []
    for msg in session["messages"]:
        # 同一要求の再送が重複イベントにならないようclientTokenを固定。
        token = digest(f"{memory_id}:{actor_id}:{fp}:{msg['event_index']}".encode("utf-8"))
        result.append({
            "memoryId": memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "eventTimestamp": msg["timestamp"],
            "payload": [{"conversational": {"content": {"text": msg["text"]}, "role": msg["role"]}}],
            "clientToken": token,
        })
    return result
