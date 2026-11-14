"""AWS送信前に入力と派生計画をローカルで確定する。外部I/Oは行わない。

ファイルモードは補助的な事故防止策であり、企業の承認・暗号化・DLPの代替ではない。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .core import (InvalidData, SHA_PATTERN, assert_external_output, build_memory_requests, digest,
                   json_bytes, normalize_events, read_archive)
from .preflight import build_memory_preflight, verify_memory_preflight
from .review_safety import validate_operation_scope

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORD_TYPE = "frozen-memory-bundle"


def _read_regular(path: Path) -> bytes:
    """リンク/不正なファイルを拒否して、検証対象のbytesを取得する。"""
    if path.is_symlink() or not path.is_file():
        raise InvalidData("固定対象が通常ファイルではありません")
    return path.read_bytes()


def _payloads(staging: Path, fingerprint: str, *, memory_id: str, actor_id: str,
              account_id: str, region: str, feedback_file: Path | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    preview = build_memory_preflight(staging, fingerprint, memory_id=memory_id,
                                     actor_id=actor_id, account_id=account_id,
                                     region=region, feedback_file=feedback_file)
    manifest, events = read_archive(staging, fingerprint)
    session = normalize_events(manifest, events)
    if session["skipped_event_count"] or not session["recognized_event_count"]:
        raise InvalidData("欠落イベントがあるためスナップショットを作成できません")
    plan = build_memory_requests(session, memory_id, actor_id)
    plan_bytes = json_bytes(plan)
    if digest(plan_bytes) != preview["scope"]["payload_sha256"]:
        raise InvalidData("プレビューと派生Memory計画が一致しません")
    names = {
        f"session-{fingerprint}.zip": _read_regular(staging / f"session-{fingerprint}.zip"),
        f"session-{fingerprint}.manifest.json": _read_regular(staging / f"session-{fingerprint}.manifest.json"),
        "memory_create_event_plan.json": plan_bytes,
        "preflight.json": json_bytes(preview),
    }
    if feedback_file is not None:
        names["feedback.json"] = _read_regular(feedback_file)
    return preview, names


def freeze_memory_bundle(staging: Path, fingerprint: str, destination: Path, *,
                         memory_id: str, actor_id: str, account_id: str,
                         region: str, feedback_file: Path | None = None) -> dict[str, Any]:
    """検証済み入力を一度だけ確定する。既存フォルダの上書きは禁止。"""
    destination = Path(destination).expanduser().absolute()
    assert_external_output(destination, REPO_ROOT)
    if destination.exists() or destination.is_symlink():
        raise InvalidData("既存のスナップショットは上書きできません")
    preview, payloads = _payloads(Path(staging), fingerprint, memory_id=memory_id,
                                  actor_id=actor_id, account_id=account_id,
                                  region=region, feedback_file=feedback_file)
    receipt = {
        "schema_version": "1.0", "record_type": RECORD_TYPE,
        "authorization_status": "not_attested", "network_calls": 0,
        "session_fingerprint": fingerprint,
        "scope": preview["scope"],
        "files": {name: {"sha256": digest(body), "size_bytes": len(body)}
                  for name, body in sorted(payloads.items())},
    }
    payloads["receipt.json"] = json_bytes(receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # ディレクトリ全体を作成してからrenameする。途中失敗したものを完成扱いしない。
    stage = Path(tempfile.mkdtemp(prefix=".frozen-staging-", dir=destination.parent))
    try:
        os.chmod(stage, 0o700)
        for name, body in payloads.items():
            path = stage / name
            with path.open("xb") as f:
                f.write(body)
            os.chmod(path, 0o600)
        _verify_internal(stage, fingerprint)
        if destination.exists() or destination.is_symlink():
            raise InvalidData("出力先が実行中に作成されました")
        stage.rename(destination)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return receipt


def _verify_internal(directory: Path, fingerprint: str) -> dict[str, Any]:
    if directory.is_symlink() or not directory.is_dir():
        raise InvalidData("不正なスナップショットディレクトリです")
    receipt = json.loads(_read_regular(directory / "receipt.json").decode("utf-8"))
    if (not isinstance(receipt, dict) or receipt.get("record_type") != RECORD_TYPE
            or receipt.get("authorization_status") != "not_attested"
            or receipt.get("network_calls") != 0 or receipt.get("session_fingerprint") != fingerprint):
        raise InvalidData("確定済みファイルの管理情報が不正です")
    expected = receipt.get("files")
    if not isinstance(expected, dict) or not expected:
        raise InvalidData("スナップショットの構成が不正です")
    actual = {p.name for p in directory.iterdir()}
    if actual != set(expected) | {"receipt.json"}:
        raise InvalidData("スナップショットに未承認の追加・欠落ファイルがあります")
    for name, metadata in expected.items():
        if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
            raise InvalidData("スナップショットのファイル名が不正です")
        body = _read_regular(directory / name)
        if (not isinstance(metadata, dict) or metadata.get("sha256") != digest(body)
                or metadata.get("size_bytes") != len(body)):
            raise InvalidData("確定済みファイルのハッシュまたはサイズが不一致です")
    return receipt


def verify_memory_bundle(directory: Path, *, memory_id: str, actor_id: str,
                         account_id: str, region: str,
                         expected_receipt_sha256: str | None = None) -> dict[str, Any]:
    """元のstagingがなくても、凍結したデータを独立して検証する。"""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise InvalidData("スナップショットディレクトリがありません")
    receipt_file = directory / "receipt.json"
    if not receipt_file.is_file() or receipt_file.is_symlink():
        raise InvalidData("スナップショットの管理情報がありません")
    receipt_bytes = _read_regular(receipt_file)
    if expected_receipt_sha256 is not None:
        if not SHA_PATTERN.fullmatch(expected_receipt_sha256) or digest(receipt_bytes) != expected_receipt_sha256:
            raise InvalidData("独立保管したreceipt SHA-256が不一致です")
    minimal = json.loads(receipt_bytes.decode("utf-8"))
    if not isinstance(minimal, dict) or not isinstance(minimal.get("session_fingerprint"), str):
        raise InvalidData("スナップショットの識別情報が不正です")
    fp = minimal["session_fingerprint"]
    receipt = _verify_internal(directory, fp)
    feedback = directory / "feedback.json"
    preview = build_memory_preflight(directory, fp, memory_id=memory_id,
                                     actor_id=actor_id, account_id=account_id, region=region,
                                     feedback_file=feedback if feedback.exists() else None)
    saved_preview = json.loads(_read_regular(directory / "preflight.json").decode("utf-8"))
    validate_operation_scope(saved_preview.get("scope"), preview["scope"])
    if json_bytes(saved_preview) != json_bytes(preview) or receipt["scope"] != preview["scope"]:
        raise InvalidData("確定済みプレビューが再計算値と一致しません")
    manifest, events = read_archive(directory, fp)
    regenerated = build_memory_requests(normalize_events(manifest, events), memory_id, actor_id)
    if json_bytes(regenerated) != _read_regular(directory / "memory_create_event_plan.json"):
        raise InvalidData("確定済みMemory計画と原本が一致しません")
    return receipt
