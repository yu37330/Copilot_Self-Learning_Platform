#!/usr/bin/env python3
"""Issue #41: ChatGPT会話・Desktop Commander履歴をオフラインで匿名集計する。

会話、コマンド引数、ツール出力、ファイル名を成果物/コンソールへ出さない。
明示的な同意・ローカル機密語辞書が必要。外部LLM・ネットワークは使用しない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.chatgpt_task_trace import evaluate_sources
from copilot_learning.core import InvalidData, assert_external_output, digest, json_bytes
from copilot_learning.privacy_scan import load_policy
from copilot_learning.skill_preview import ROOT, RESERVED, _path

CHAT_MAX_BYTES = 32 * 1024 * 1024
DESKTOP_MAX_BYTES = 16 * 1024 * 1024
REPORT_NAME = "chatgpt-task-improvement.preview.json"
MANIFEST_NAME = "manifest.json"


def _source(path: Path, limit: int) -> bytes:
    source = _path(path)
    assert_external_output(source, ROOT)
    if not source.is_file() or source.is_symlink() or not 0 < source.stat().st_size <= limit:
        raise InvalidData("選択した入力ファイルが不正またはサイズ超過です")
    before = source.stat()
    with source.open("rb") as reader:
        data = reader.read(limit + 1)
    after = source.stat()
    if (len(data) != before.st_size or len(data) > limit
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
        raise InvalidData("入力ファイルが読み込み中に変更された可能性があります")
    return data


def _decode(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(
        InvalidData("非有限値は利用できません")))


def _history(raw: bytes) -> Any:
    # ユーザーがAPIの返却内容をJSON配列で保存した場合も、ローカルJSONLも受け付ける。
    # textラッパーの人間向け文字列はパースしない（取得エラー・本文混入防止）。
    text = raw.decode("utf-8")
    try:
        return _decode(raw)
    except json.JSONDecodeError:
        records = []
        for line in text.splitlines():
            if line.strip():
                value = _decode(line.encode("utf-8"))
                if not isinstance(value, dict):
                    raise InvalidData("Desktop履歴のJSONL行にオブジェクトが必要")
                records.append(value)
                if len(records) > 15_000:
                    raise InvalidData("Desktop履歴の件数上限超過")
        return records


def _destination(path: Path) -> Path:
    out = _path(path)
    assert_external_output(out, ROOT)
    if any(part.rstrip(" .").casefold() in RESERVED for part in out.parts):
        raise InvalidData("自動読込先は成果物の出力対象外")
    return out


def _prepare(chat_path: Path | None, desktop_path: Path | None,
             policy_path: Path) -> tuple[bytes, bytes]:
    policy, policy_sha = load_policy(_path(policy_path))
    chat_raw = _source(chat_path, CHAT_MAX_BYTES) if chat_path else None
    desktop_raw = _source(desktop_path, DESKTOP_MAX_BYTES) if desktop_path else None
    report = evaluate_sources(
        _decode(chat_raw) if chat_raw is not None else None,
        _history(desktop_raw) if desktop_raw is not None else None,
        chat_source_sha256=digest(chat_raw) if chat_raw is not None else None,
        desktop_source_sha256=digest(desktop_raw) if desktop_raw is not None else None,
        policy_sha256=policy_sha,
        literals=policy["sensitive_literals"],
    )
    body = json_bytes(report)
    manifest = {
        "schema_version": "1.0", "record_type": "chatgpt-task-improvement-manifest",
        "report_sha256": digest(body), "report_size": len(body),
        "source_sha256": report["inputs"], "status": "unapproved_preview",
        "external_transfer_authorized": False, "network_calls": 0,
    }
    return body, json_bytes(manifest)


def _create(out: Path, contents: tuple[bytes, bytes]) -> dict[str, Any]:
    if out.exists():
        raise InvalidData("既存の出力先は上書きしません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    written: list[Path] = []
    try:
        for name, body in ((REPORT_NAME, contents[0]), (MANIFEST_NAME, contents[1])):
            target = out / name
            with target.open("xb") as handle:
                written.append(target)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o400)
    except BaseException:
        for file in reversed(written):
            file.chmod(0o600)
            file.unlink()
        out.rmdir()
        raise
    return {
        "status": "preview_written_not_applied",
        "report_sha256": digest(contents[0]),
        "manifest_sha256": digest(contents[1]),
        "network_calls": 0,
    }


def _verify(out: Path, contents: tuple[bytes, bytes], expected_sha: str) -> dict[str, Any]:
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise InvalidData("独立保管したSHAが必要です")
    if digest(contents[1]) != expected_sha:
        raise InvalidData("独立したmanifest SHAと合致しません")
    if not out.is_dir() or {p.name for p in out.iterdir()} != {REPORT_NAME, MANIFEST_NAME}:
        raise InvalidData("成果物の構成が一致しません")
    for name, data in ((REPORT_NAME, contents[0]), (MANIFEST_NAME, contents[1])):
        path = _path(out / name)
        if not path.is_file() or path.read_bytes() != data:
            raise InvalidData("入力・成果物・機密辞書が一致しません")
    return {"status": "verified_preview_only", "approved_for_instructions": False,
            "network_calls": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("analyze", "verify"))
    parser.add_argument("--chatgpt-json", type=Path)
    parser.add_argument("--desktop-history", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--acknowledge-local-analysis", action="store_true")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if not args.acknowledge_local_analysis:
            raise InvalidData("会話・実行履歴のローカル分析には明示的な同意が必要です")
        if args.chatgpt_json is None and args.desktop_history is None:
            raise InvalidData("ChatGPTまたはDesktop履歴を少なくとも1件指定してください")
        if (args.action == "verify") != bool(args.expected_manifest_sha256):
            raise InvalidData("verify時だけ独立保管済みmanifest SHAを指定してください")
        contents = _prepare(args.chatgpt_json, args.desktop_history, args.policy)
        out = _destination(args.out)
        receipt = (_verify(out, contents, args.expected_manifest_sha256)
                   if args.action == "verify" else _create(out, contents))
        print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        # 例外に元会話/コマンド/ファイル名を含めずに通知する。
        print("ERROR: ローカル業務履歴分析を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
