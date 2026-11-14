#!/usr/bin/env python3
"""学習入力の確定・再検証（AWS通信なし、実送信許可なし）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, digest
from copilot_learning.frozen_bundle import freeze_memory_bundle, verify_memory_bundle


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["freeze", "verify"])
    p.add_argument("--staging", type=Path, help="既存ZIPとmanifestのフォルダ（freezeのみ）")
    p.add_argument("--fingerprint", help="セッションSHA（freezeのみ）")
    p.add_argument("--feedback", type=Path, help="フィードバックJSON（任意、freezeのみ）")
    p.add_argument("--out", type=Path, required=True, help="Git管理外の確定先フォルダ")
    p.add_argument("--memory-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--expected-receipt-sha256", help="独立記録したreceipt.jsonのSHA（verifyのみ）")
    args = p.parse_args(argv)
    try:
        kw = dict(memory_id=args.memory_id, actor_id=args.actor_id,
                  account_id=args.account_id, region=args.region)
        if args.action == "freeze":
            if args.staging is None or args.fingerprint is None:
                raise InvalidData("freezeには--stagingと--fingerprintが必要です")
            freeze_memory_bundle(args.staging, args.fingerprint, args.out,
                                 feedback_file=args.feedback, **kw)
            print("FROZEN (LOCAL ONLY): 未承認・外部通信なし")
        else:
            verify_memory_bundle(args.out, expected_receipt_sha256=args.expected_receipt_sha256, **kw)
            print("VERIFIED (LOCAL ONLY): 未承認・外部通信なし")
        receipt = args.out / "receipt.json"
        print("receipt_sha256=" + digest(receipt.read_bytes()))
        return 0
    except (InvalidData, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
