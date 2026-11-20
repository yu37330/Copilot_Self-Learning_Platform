#!/usr/bin/env python3
"""Codex Task IDとJUnit/GitHub参照を匿名照合するオフラインCLI（Issue #46）。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.codex_task_evidence import (
    MAX_BIND_BYTES, _read_private, evaluate_task, inspect_sources,
)
from copilot_learning.codex_rollout import _destination
from copilot_learning.core import InvalidData, SHA_PATTERN, digest, json_bytes
from copilot_learning.skill_preview import _path

INDEX_FILE = "codex-task-sources.index.json"
REVIEW_FILE = "codex-task-evidence.preview.json"
MANIFEST_FILE = "manifest.json"


def _prepare(args: argparse.Namespace) -> tuple[str, bytes, bytes]:
    index = inspect_sources(args.rollout, args.test_report, args.policy,
                            minimum_age_minutes=args.minimum_age_minutes)
    if args.action == "index":
        name, body = INDEX_FILE, json_bytes(index)
        binding_sha = None
    else:
        data = _read_private(args.binding, MAX_BIND_BYTES)
        try:
            binding = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidData("タスク紐付けmanifestが不正です") from exc
        name, body = REVIEW_FILE, json_bytes(evaluate_task(index, binding))
        binding_sha = digest(data)
    manifest = {
        "schema_version": "1.0", "record_type": "codex-task-evidence-offline-manifest",
        "data_file": name, "data_sha256": digest(body),
        "source_rollout_sha256": index["rollout_sha256"],
        "test_report_sha256": index["test_report_sha256"],
        "policy_sha256": index["policy_sha256"],
        "binding_sha256": binding_sha,
        "status": "unapproved_unverified_business_outcome", "network_calls": 0,
    }
    return name, body, json_bytes(manifest)


def _export(directory: Path, name: str, report: bytes, manifest: bytes) -> dict[str, Any]:
    out = _destination(directory)
    if out.exists():
        raise InvalidData("既存の保存先には上書きできません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    saved: list[Path] = []
    try:
        for filename, content in ((name, report), (MANIFEST_FILE, manifest)):
            target = out / filename
            with target.open("xb") as f:
                saved.append(target)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            target.chmod(0o400)
    except BaseException:
        for target in reversed(saved):
            target.chmod(0o600)
            target.unlink()
        out.rmdir()
        raise
    return {"status": "local_preview_written_unapproved",
            "manifest_sha256": digest(manifest), "report_sha256": digest(report),
            "network_calls": 0}


def _verify(directory: Path, name: str, report: bytes, manifest: bytes,
            expected_sha256: str | None) -> dict[str, Any]:
    if not isinstance(expected_sha256, str) or not SHA_PATTERN.fullmatch(expected_sha256):
        raise InvalidData("独立保管のmanifest SHA-256が必要です")
    if digest(manifest) != expected_sha256:
        raise InvalidData("原本の検査条件とmanifest SHAが一致しません")
    out = _destination(directory)
    if not out.is_dir() or {p.name for p in out.iterdir()} != {name, MANIFEST_FILE}:
        raise InvalidData("出力ファイル構成が一致しません")
    for filename, content in ((name, report), (MANIFEST_FILE, manifest)):
        source = _path(out / filename)
        if (source.is_symlink() or not source.is_file()
                or source.stat().st_size != len(content)
                or source.read_bytes() != content):
            raise InvalidData("出力が改変されているか入力条件が異なります")
    return {"status": "source_and_output_bytes_verified_only",
            "business_completion_verified": False, "github_pr_verified": False,
            "network_calls": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("index", "review", "verify"))
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--test-report", type=Path)
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--minimum-age-minutes", type=int, default=1440)
    parser.add_argument("--acknowledge-local-analysis", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if not args.acknowledge_local_analysis:
            raise InvalidData("選択したローカル証拠を読むには同意が必要です")
        if args.action == "index" and args.binding is not None:
            raise InvalidData("indexでbindingを使うことはできません")
        if args.action != "index" and args.binding is None:
            raise InvalidData("review/verifyは明示的なタスク紐付けが必須")
        if (args.action == "verify") != bool(args.expected_manifest_sha256):
            raise InvalidData("verify時だけ独立したmanifest SHAを指定します")
        name, report, manifest = _prepare(args)
        result = (_verify(args.out, name, report, manifest, args.expected_manifest_sha256)
                  if args.action == "verify"
                  else _export(args.out, name, report, manifest))
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        # 任意のファイル名・メッセージ・testcase名・PR名を外部に表示しない。
        print("ERROR: オフラインCodex Task Evidenceの処理に失敗しました",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
