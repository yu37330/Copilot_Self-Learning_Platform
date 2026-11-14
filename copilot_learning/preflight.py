"""承認前の外部送信内容を原本に結び付ける、通信しないプレビュー専用モジュール。

この出力は署名済み承認ではない。require_online_release() の停止を解除しない。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .core import (InvalidData, build_memory_requests, digest, json_bytes, load_feedback,
                   normalize_events, read_archive)
from .review_safety import operation_scope, validate_operation_scope

ACCOUNT = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d+$")


def build_memory_preflight(staging: Path, fingerprint: str, *, memory_id: str,
                           actor_id: str, account_id: str, region: str,
                           feedback_file: Path | None = None) -> dict[str, Any]:
    """ZIPを再検証したうえで、Memory送信計画のダイジェストだけを保存する。

    会話本文、投入計画そのもの、フィードバック本文はプレビューへ書き出さない。
    本番用の実在Memory IDかどうかはAWS側でのみ検証できる。
    """
    if not ACCOUNT.fullmatch(account_id) or not REGION.fullmatch(region):
        raise InvalidData("AWSアカウントIDまたはリージョンが不正です")
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise InvalidData("Memory IDが空です")
    manifest, raw = read_archive(staging, fingerprint)
    session = normalize_events(manifest, raw)
    if session["skipped_event_count"] or not session["recognized_event_count"]:
        raise InvalidData("未知・欠落イベントがあるためMemory送信用のプレビューを作成しません")
    # 別途指定されたフィードバックはこの操作の本文には含めないが、同じ入力セットを識別する。
    feedback = load_feedback(feedback_file, fingerprint)
    requests = build_memory_requests(session, memory_id, actor_id)
    manifest_bytes = (Path(staging) / f"session-{fingerprint}.manifest.json").read_bytes()
    inputs = {
        "session_fingerprint": fingerprint,
        "archive_sha256": manifest["archive_sha256"],
        "manifest_sha256": digest(manifest_bytes),
        "feedback_sha256": digest(Path(feedback_file).read_bytes()) if feedback_file else "none",
    }
    scope = operation_scope("memory-send", {
        "account_id": account_id, "region": region,
        "memory_id": memory_id, "actor_id": actor_id,
        "session_id": requests[0]["sessionId"],
    }, requests, inputs)
    return {
        "schema_version": "1.0", "record_type": "offline-preflight",
        "source": "verified_local_session", "mode": "dry_run_only",
        "authorization_status": "not_attested", "requires_organization_approval": True,
        "network_calls": 0, "event_count": len(requests),
        "feedback_count": len(feedback), "scope": scope,
        "warning": "このJSONは送信許可でも署名でもありません。外部通信は停止中です。",
    }


def verify_memory_preflight(supplied: dict[str, Any], staging: Path, fingerprint: str,
                            *, memory_id: str, actor_id: str, account_id: str,
                            region: str, feedback_file: Path | None = None) -> dict[str, Any]:
    """承認時と変わっていないことを、元のバイト列を読み直して照合する。"""
    expected = build_memory_preflight(staging, fingerprint, memory_id=memory_id,
                                      actor_id=actor_id, account_id=account_id,
                                      region=region, feedback_file=feedback_file)
    if not isinstance(supplied, dict) or supplied.get("authorization_status") != "not_attested":
        raise InvalidData("未署名の送信プレビューとして扱えない形式です")
    validate_operation_scope(supplied.get("scope"), expected["scope"])
    if json_bytes(supplied) != json_bytes(expected):
        raise InvalidData("送信プレビューの内容が変更されています")
    return expected
