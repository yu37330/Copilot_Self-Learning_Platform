#!/usr/bin/env python3
"""S3原本から作成されたMemory投入計画を、許可後だけAWSに送る/結果を取得する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.aws_bridge import (assert_account, list_memory_records,
                                         send_memory_plan, validate_approval)
from copilot_learning.core import (InvalidData, assert_external_output, digest, json_bytes, read_archive)

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_once(path: Path, data: object) -> None:
    assert_external_output(path, REPO_ROOT)
    content = json_bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as file:
        file.write(content)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["send", "export"])
    p.add_argument("--memory-plan", type=Path)
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--approval-file", type=Path, required=True)
    p.add_argument("--expected-account-id", required=True)
    p.add_argument("--memory-id", required=True)
    p.add_argument("--namespace")
    p.add_argument("--region", default="ap-northeast-1")
    p.add_argument("--profile", default="")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm-approved-data", action="store_true")
    args = p.parse_args(argv)
    try:
        manifest, _ = read_archive(args.staging, args.fingerprint)
        validate_approval(args.approval_file, args.fingerprint, manifest["archive_sha256"],
                          args.expected_account_id,
                          manifest_sha256=digest((args.staging / f"session-{args.fingerprint}.manifest.json").read_bytes()))
        if args.operation == "send":
            if args.memory_plan is None:
                raise InvalidData("--memory-planが必須です")
            requests = json.loads(args.memory_plan.read_text("utf-8"))
            if not isinstance(requests, list) or not requests:
                raise InvalidData("Memoryのイベント配列が必要です")
            for item in requests:
                if item.get("memoryId") != args.memory_id or item.get("sessionId") != f"s_{args.fingerprint[:48]}":
                    raise InvalidData("Memory ID / 対象セッションが一致しません")
            if not args.execute:
                print(f"DRY RUN: {len(requests)} eventsを{args.memory_id}へ送信する計画。AWS通信はありません。")
                return 0
            if not args.confirm_approved_data:
                raise InvalidData("送信には--execute --confirm-approved-dataが両方必要です")
            assert_account(args.expected_account_id, region=args.region, profile=args.profile)
            response = send_memory_plan(requests, region=args.region, profile=args.profile)
            write_once(args.out, {"memory_id": args.memory_id, "session_fingerprint": args.fingerprint,
                                  "receipts": response})
            print(f"Memoryへイベント{len(response)}件を送信。反映は非同期です。")
            return 0
        if not args.namespace:
            raise InvalidData("exportには--namespaceが必須です")
        if not args.execute:
            print(f"DRY RUN: namespace={args.namespace}のMemoryレコード取得計画。AWS通信はありません。")
            return 0
        if not args.confirm_approved_data:
            raise InvalidData("取得には--execute --confirm-approved-dataが両方必要です")
        assert_account(args.expected_account_id, region=args.region, profile=args.profile)
        results = list_memory_records(args.memory_id, args.namespace, region=args.region, profile=args.profile)
        write_once(args.out, results)
        print(f"Memoryレコード取得結果をローカル保存: {args.out}")
        return 0
    except (InvalidData, RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
