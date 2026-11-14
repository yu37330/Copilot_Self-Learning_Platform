"""#6: 凍結済みセッションをオフラインでDLP前処理する。

本文・一致文字列は絶対に結果へ書かない。組織DLPや送信承認を代替しない。
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .core import InvalidData, digest, json_bytes, assert_external_output
from .frozen_bundle import REPO_ROOT, verify_memory_bundle

POLICY_VERSION = "1.0"
SCAN_VERSION = "offline-sensitive-scanner-1"
MAX_POLICY_BYTES = 64_000
MAX_TERMS = 100
MAX_TERM_CHARS = 160
# あえて保存先側の承認条件には使わず、オフラインの二次検査だけに利用する。
BUILTIN_RULES = {
    "email_address": re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"),
    "jp_mobile_number": re.compile(r"(?<!\d)0[789]0[- ]?\d{4}[- ]?\d{4}(?!\d)"),
    "jwt_like": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
}


def load_policy(policy_path: Path) -> tuple[dict[str, Any], str]:
    """正規表現を任意指定させず、組織固有の機密文字列は完全一致の辞書として読む。"""
    path = Path(policy_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_POLICY_BYTES:
        raise InvalidData("機密語辞書がありません、リンク、または容量超過です")
    # 社内固有語を公開リポジトリへ誤コミットしないため入力もGit管理外に限定。
    assert_external_output(path, REPO_ROOT)
    raw = path.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidData("機密語辞書はUTF-8 JSONで指定してください") from exc
    if (not isinstance(doc, dict) or set(doc) != {"schema_version", "record_type", "sensitive_literals"}
            or doc.get("schema_version") != POLICY_VERSION
            or doc.get("record_type") != "offline-sensitive-literals"):
        raise InvalidData("機密語辞書の形式が不正です")
    terms = doc.get("sensitive_literals")
    if not isinstance(terms, list) or len(terms) > MAX_TERMS:
        raise InvalidData("機密語リストの件数が不正です")
    unique: set[str] = set()
    for term in terms:
        if (not isinstance(term, str) or not (3 <= len(term.strip()) <= MAX_TERM_CHARS)
                or any(ord(ch) < 32 for ch in term)):
            raise InvalidData("機密語リストの文字数・形式が不正です")
        normalized = term.strip().casefold()
        if normalized in unique:
            raise InvalidData("機密語リストに重複があります")
        unique.add(normalized)
    return doc, digest(raw)


def _counts(text: str, terms: list[str]) -> dict[str, int]:
    counts = {key: len(list(pattern.finditer(text))) for key, pattern in BUILTIN_RULES.items()}
    folded = text.casefold()
    count = 0
    for term in terms:
        # 各機密語の一致箇所自体は記録しない。
        count += folded.count(term.strip().casefold())
    counts["organization_literal"] = count
    return {key: value for key, value in counts.items() if value > 0}


def scan_frozen_bundle(directory: Path, policy_path: Path, *, memory_id: str, actor_id: str,
                       account_id: str, region: str, expected_receipt_sha256: str) -> dict[str, Any]:
    """承認ではなくアラート。検査不能・ハッシュ不一致なら例外で停止。"""
    if not isinstance(expected_receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256):
        raise InvalidData("独立保管したreceipt SHA-256が必須です")
    verified = verify_memory_bundle(directory, memory_id=memory_id, actor_id=actor_id,
                                    account_id=account_id, region=region,
                                    expected_receipt_sha256=expected_receipt_sha256)
    policy, policy_sha = load_policy(policy_path)
    targets: dict[str, bytes] = {}
    with zipfile.ZipFile(Path(directory) / f"session-{verified['session_fingerprint']}.zip") as archive:
        for filename in archive.namelist():
            targets["archive/" + filename] = archive.read(filename)
    for filename in ("feedback.json", "memory_create_event_plan.json"):
        path = Path(directory) / filename
        if path.is_file():
            targets[filename] = path.read_bytes()
    results: dict[str, dict[str, int]] = {}
    for name, data in sorted(targets.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidData("対象にUTF-8以外のデータがあります") from exc
        found = _counts(text, policy["sensitive_literals"])
        if found:
            results[name] = found
    return {
        "schema_version": "1.0", "record_type": "offline-privacy-scan",
        "scanner_version": SCAN_VERSION,
        "session_fingerprint": verified["session_fingerprint"],
        "receipt_sha256": expected_receipt_sha256,
        "policy_sha256": policy_sha,
        "scanned_files": len(targets),
        "findings_by_file_and_rule": results,
        "findings_total": sum(sum(x.values()) for x in results.values()),
        "status": "blocked_findings" if results else "manual_review_required",
        "external_transfer_authorized": False,
        "network_calls": 0,
        "limitations": "ルールにない機密情報は検出できません。0件でも組織DLP・正式承認は必要です。",
    }
