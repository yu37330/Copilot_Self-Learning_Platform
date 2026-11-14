#!/usr/bin/env python3
"""Memory送信のプレビューを外部通信せず生成・再照合する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.preflight import build_memory_preflight, verify_memory_preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["preview", "verify"])
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--memory-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--feedback", type=Path)
    parser.add_argument("--out", type=Path, required=True,
                        help="preview時は出力先、verify時は既存プレビューJSON")
    args = parser.parse_args(argv)
    try:
        kwargs = dict(memory_id=args.memory_id, actor_id=args.actor_id,
                      account_id=args.account_id, region=args.region, feedback_file=args.feedback)
        if args.operation == "preview":
            assert_external_output(args.out, Path(__file__).resolve().parents[1])
            result = build_memory_preflight(args.staging, args.fingerprint, **kwargs)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("xb") as output:
                output.write(json_bytes(result))
            print(f"PREVIEW SAVED (no AWS calls): {args.out}")
        else:
            proposal = json.loads(args.out.read_text("utf-8"))
            verify_memory_preflight(proposal, args.staging, args.fingerprint, **kwargs)
            print("PREVIEW VERIFIED (not an approval, no AWS calls)")
        return 0
    except (InvalidData, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
