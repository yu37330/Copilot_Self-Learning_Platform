"""#6: 外部送信を許可しない、オフラインの署名承認・証跡検証器。

この検証は暗号学的な真正性を確認するだけで、会社のIAM/SSO承認やDLPの代替にならない。
Git管理外に保存された信頼設定のSHA-256を別経路から渡す必要がある。
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import InvalidData, SHA_PATTERN, assert_external_output, digest, json_bytes
from .frozen_bundle import REPO_ROOT, verify_memory_bundle

MAX_DOCUMENT_BYTES = 64_000
MAX_KEYS = 20
ALLOWED_OPERATIONS = {"memory-send"}
ATTESTATION_FIELDS = {
    "schema_version", "record_type", "key_id", "principal_id", "decision", "operation",
    "receipt_sha256", "privacy_report_sha256", "policy_sha256", "scope_sha256",
    "issued_at_utc", "expires_at_utc", "signature_base64",
}


def _load_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    path = Path(path)
    assert_external_output(path, REPO_ROOT)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise InvalidData(f"{label}の保存先/ファイルサイズが不正です")
    raw = path.read_bytes()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidData(f"{label}のUTF-8 JSONが不正です") from exc
    if not isinstance(obj, dict):
        raise InvalidData(f"{label}はJSONオブジェクトが必要です")
    return obj, raw


def _decode_base64(value: Any, length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise InvalidData(f"{label}はbase64文字列です")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidData(f"{label}のbase64が不正です") from exc
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise InvalidData(f"{label}の長さ/正規化が不正です")
    return decoded


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidData(f"{label}の時刻が不正です")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidData(f"{label}の時刻が不正です") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise InvalidData(f"{label}にはタイムゾーンが必要です")
    return dt.astimezone(timezone.utc)


def verify_attestation(*, bundle: Path, receipt_sha256: str, privacy_report: Path,
                       attestation_file: Path, trust_store: Path, pinned_trust_sha256: str,
                       memory_id: str, actor_id: str, account_id: str, region: str,
                       now: datetime | None = None) -> dict[str, Any]:
    """署名・DLP検査結果・確定データ範囲を同時に照合。送信権限は返さない。"""
    if not isinstance(pinned_trust_sha256, str) or not SHA_PATTERN.fullmatch(pinned_trust_sha256):
        raise InvalidData("独立保管した信頼設定SHA-256が必要です")
    receipt = verify_memory_bundle(bundle, memory_id=memory_id, actor_id=actor_id,
                                   account_id=account_id, region=region,
                                   expected_receipt_sha256=receipt_sha256)
    policy_report, policy_bytes = _load_document(privacy_report, label="機密検査レポート")
    if (policy_report.get("record_type") != "offline-privacy-scan"
            or policy_report.get("receipt_sha256") != receipt_sha256
            or policy_report.get("session_fingerprint") != receipt["session_fingerprint"]
            or policy_report.get("findings_total") != 0
            or policy_report.get("status") != "manual_review_required"
            or policy_report.get("external_transfer_authorized") is not False
            or policy_report.get("network_calls") != 0
            or not isinstance(policy_report.get("policy_sha256"), str)
            or not SHA_PATTERN.fullmatch(policy_report["policy_sha256"])):
        raise InvalidData("機密検査結果が不一致・未完了・または検出ありです")
    att, _ = _load_document(attestation_file, label="承認証跡")
    trust, trust_bytes = _load_document(trust_store, label="信頼設定")
    if digest(trust_bytes) != pinned_trust_sha256:
        raise InvalidData("信頼設定が別経路で指定されたSHA-256と一致しません")
    if (set(trust) != {"schema_version", "record_type", "keys", "policy_sha256"}
            or trust["schema_version"] != "1.0" or trust["record_type"] != "offline-trust-store"
            or trust["policy_sha256"] != policy_report["policy_sha256"]):
        raise InvalidData("信頼設定と機密検査ポリシーが一致しません")
    keys = trust["keys"]
    if not isinstance(keys, list) or not (1 <= len(keys) <= MAX_KEYS):
        raise InvalidData("信頼鍵の件数が不正です")
    found_keys = {}
    for entry in keys:
        if (not isinstance(entry, dict)
                or set(entry) != {"key_id", "principal_id", "public_key_base64", "operations"}
                or not isinstance(entry["key_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", entry["key_id"])
                or not isinstance(entry["principal_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", entry["principal_id"])
                or not isinstance(entry["operations"], list)
                or set(entry["operations"]) != ALLOWED_OPERATIONS):
            raise InvalidData("信頼鍵の形式または操作範囲が不正です")
        if entry["key_id"] in found_keys:
            raise InvalidData("重複する鍵IDは使用できません")
        _decode_base64(entry["public_key_base64"], 32, "公開鍵")
        found_keys[entry["key_id"]] = entry
    if (set(att) != ATTESTATION_FIELDS
            or att.get("schema_version") != "1.0"
            or att.get("record_type") != "offline-scope-attestation"
            or att.get("decision") != "approved"
            or att.get("operation") != "memory-send"
            or not isinstance(att.get("key_id"), str)):
        raise InvalidData("承認証跡のスキーマまたは操作が不正です")
    key = found_keys.get(att["key_id"])
    if key is None or att["principal_id"] != key["principal_id"]:
        raise InvalidData("承認主体が信頼設定に存在しません")
    expected = {
        "receipt_sha256": receipt_sha256,
        "privacy_report_sha256": digest(policy_bytes),
        "policy_sha256": trust["policy_sha256"],
        "scope_sha256": digest(json_bytes(receipt["scope"])),
    }
    if any(att.get(k) != v for k, v in expected.items()):
        raise InvalidData("承認済みの本文・検査・操作範囲が変更されています")
    issued = _utc(att.get("issued_at_utc"), "発行")
    expires = _utc(att.get("expires_at_utc"), "期限")
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise InvalidData("検証時刻はタイムゾーン付きが必要です")
    current = current.astimezone(timezone.utc)
    if not (issued - timedelta(minutes=5) <= current < expires <= issued + timedelta(days=7)):
        raise InvalidData("承認証跡の有効期間外です")
    signed_data = {k: v for k, v in att.items() if k != "signature_base64"}
    signature = _decode_base64(att["signature_base64"], 64, "署名")
    public_key = _decode_base64(key["public_key_base64"], 32, "公開鍵")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("署名検証にはcryptographyが必要です（外部送信は行いません）") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, json_bytes(signed_data))
    except (InvalidSignature, ValueError) as exc:
        raise InvalidData("承認署名が検証できません") from exc
    return {
        "schema_version": "1.0", "record_type": "offline-attestation-verification",
        "attestation_sha256": digest(json_bytes(att)),
        "receipt_sha256": receipt_sha256,
        "privacy_report_sha256": digest(policy_bytes),
        "trust_store_sha256": pinned_trust_sha256,
        "signature_verified": True,
        "scope_verified": True,
        "privacy_findings_total": 0,
        "organization_identity_verified": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
        "limitation": "ローカル固定鍵による署名検査のみ。社内の真正な承認権限・ID連携は未確認。",
    }
