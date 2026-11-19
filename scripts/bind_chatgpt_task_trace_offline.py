#!/usr/bin/env python3
"""Issue #41: ChatGPTとDesktop CommanderのTaskを明示IDと原本SHAで紐付ける。

indexは参照番号・ハッシュだけ出力。bindは利用者が選んだ参照のみ集計。
verifyは独立保管したmanifest SHAと原本を再照合する。オンライン接続は行わない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, SHA_PATTERN, digest, json_bytes
from copilot_learning.privacy_scan import load_policy
from copilot_learning.skill_preview import _path
from copilot_learning.task_trace_binding import bind_explicit_tasks, index_sources
from scripts.analyze_chatgpt_desktop_offline import (
    CHAT_MAX_BYTES, DESKTOP_MAX_BYTES, _decode, _destination, _history, _source,
)

INDEX_FILE = "task-trace.index.json"
REVIEW_FILE = "task-trace.review.json"
MANIFEST_FILE = "manifest.json"
MAX_BINDING_BYTES = 256 * 1024


def _prepare(action: str, chat_path: Path, desktop_path: Path,
             policy_path: Path, binding_path: Path | None) -> tuple[str, bytes, bytes]:
    policy, policy_sha = load_policy(_path(policy_path))
    chat_bytes = _source(chat_path, CHAT_MAX_BYTES)
    desktop_bytes = _source(desktop_path, DESKTOP_MAX_BYTES)
    chat, desktop = _decode(chat_bytes), _history(desktop_bytes)
    params = {
        "chat_sha256": digest(chat_bytes), "desktop_sha256": digest(desktop_bytes),
        "literals": policy["sensitive_literals"],
    }
    if action == "index":
        report = index_sources(chat, desktop, **params)
        name = INDEX_FILE
        binding_sha = None
    else:
        if binding_path is None:
            raise InvalidData("明示的なTask紐付けmanifestが必要です")
        raw_binding = _source(binding_path, MAX_BINDING_BYTES)
        binding = _decode(raw_binding)
        report = bind_explicit_tasks(chat, desktop, binding, **params)
        name = REVIEW_FILE
        binding_sha = digest(raw_binding)
    body = json_bytes(report)
    manifest = {
        "schema_version": "1.0",
        "record_type": "offline-explicit-task-review-manifest",
        "report_file": name, "report_sha256": digest(body),
        "source_sha256": {
            "chatgpt": params["chat_sha256"],
            "desktop": params["desktop_sha256"],
            "policy": policy_sha, "binding": binding_sha,
        },
        "status": "manual_review_not_applied",
        "network_calls": 0,
    }
    return name, body, json_bytes(manifest)


def _export(out: Path, name: str, body: bytes, manifest: bytes) -> dict[str, Any]:
    if out.exists():
        raise InvalidData("既存の出力先は上書きしません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    created: list[Path] = []
    try:
        for filename, content in ((name, body), (MANIFEST_FILE, manifest)):
            target = out / filename
            with target.open("xb") as handle:
                created.append(target)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o400)
    except BaseException:
        for path in reversed(created):
            path.chmod(0o600)
            path.unlink()
        out.rmdir()
        raise
    return {"status": "preview_written_unapproved",
            "report_sha256": digest(body), "manifest_sha256": digest(manifest),
            "network_calls": 0}


def _verify(out: Path, name: str, body: bytes,
            manifest: bytes, expected_sha256: str | None) -> dict[str, Any]:
    if not isinstance(expected_sha256, str) or not SHA_PATTERN.fullmatch(expected_sha256):
        raise InvalidData("独立保管したSHA-256が必要です")
    if digest(manifest) != expected_sha256:
        raise InvalidData("入力またはmanifestのSHAが一致しません")
    if not out.is_dir() or {x.name for x in out.iterdir()} != {name, MANIFEST_FILE}:
        raise InvalidData("プレビュー構成が一致しません")
    for filename, expected in ((name, body), (MANIFEST_FILE, manifest)):
        path = _path(out / filename)
        if not path.is_file() or path.stat().st_size != len(expected) or path.read_bytes() != expected:
            raise InvalidData("プレビューの内容が改変されています")
    return {"status": "verified_preview_not_applied",
            "business_outcome_verified": False, "network_calls": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("index", "bind", "verify"))
    parser.add_argument("--chatgpt-json", type=Path, required=True)
    parser.add_argument("--desktop-history", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--acknowledge-local-analysis", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if not args.acknowledge_local_analysis:
            raise InvalidData("ローカルの会話/ツール履歴処理には明示的な同意が必要です")
        if args.action == "index" and args.binding is not None:
            raise InvalidData("indexではTask紐付けmanifestは指定しません")
        if args.action in {"bind", "verify"} and args.binding is None:
            raise InvalidData("bind/verifyではTask紐付けmanifestが必要です")
        if (args.action == "verify") != bool(args.expected_manifest_sha256):
            raise InvalidData("verify時だけ保存済みmanifest SHAを指定します")
        action = "index" if args.action == "index" else "bind"
        name, body, manifest = _prepare(
            action, args.chatgpt_json, args.desktop_history, args.policy, args.binding)
        out = _destination(args.out)
        receipt = (_verify(out, name, body, manifest, args.expected_manifest_sha256)
                   if args.action == "verify" else _export(out, name, body, manifest))
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        print("ERROR: オフラインTask紐付け処理を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
