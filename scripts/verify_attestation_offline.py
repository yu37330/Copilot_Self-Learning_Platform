#!/usr/bin/env python3
"""Git管理外の署名・DLP・信頼設定を検証する（AWS送信なし）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.approval_attestation import verify_attestation
from copilot_learning.core import InvalidData, assert_external_output, json_bytes


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("bundle", "privacy-report", "attestation", "trust-store", "out"):
        p.add_argument("--"+key, type=Path, required=True)
    for key in ("receipt-sha256", "pinned-trust-sha256", "memory-id", "actor-id", "account-id", "region"):
        p.add_argument("--"+key, required=True)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        if args.out.exists() or args.out.is_symlink():
            raise InvalidData("出力ファイルは上書きできません")
        result = verify_attestation(bundle=args.bundle, receipt_sha256=args.receipt_sha256,
                                    privacy_report=args.privacy_report, attestation_file=args.attestation,
                                    trust_store=args.trust_store, pinned_trust_sha256=args.pinned_trust_sha256,
                                    memory_id=args.memory_id, actor_id=args.actor_id,
                                    account_id=args.account_id, region=args.region)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as f:
            f.write(json_bytes(result))
        print("署名・操作範囲のローカル検証成功。外部通信・会社承認は未実施です")
        return 0
    except (InvalidData, ValueError, OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
