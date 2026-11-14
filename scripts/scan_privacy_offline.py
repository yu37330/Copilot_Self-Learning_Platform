#!/usr/bin/env python3
"""凍結済みデータのオフラインPII・社内機密語スキャン。結果に本文を含めない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.privacy_scan import scan_frozen_bundle


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", required=True, type=Path)
    p.add_argument("--policy", required=True, type=Path)
    p.add_argument("--memory-id", required=True)
    p.add_argument("--actor-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--expected-receipt-sha256", required=True)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        if args.out.exists() or args.out.is_symlink():
            raise InvalidData("出力ファイルの上書きは禁止です")
        result = scan_frozen_bundle(args.bundle, args.policy,
                                    memory_id=args.memory_id, actor_id=args.actor_id,
                                    account_id=args.account_id, region=args.region,
                                    expected_receipt_sha256=args.expected_receipt_sha256)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as handle:
            handle.write(json_bytes(result))
        print(f"OFFLINE PRIVACY CHECK: {result['status']} / findings={result['findings_total']}; 外部送信なし")
        return 0 if result["findings_total"] == 0 else 1
    except (InvalidData, RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
