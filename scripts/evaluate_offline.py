#!/usr/bin/env python3
"""外部送信なしで適用条件のHold-out評価を行う。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.evals import evaluate_holdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        report = evaluate_holdout(json.loads(args.cases.read_text("utf-8")),
                                  json.loads(args.predictions.read_text("utf-8")))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = json_bytes(report)
        with args.out.open("xb") as file:
            file.write(payload)
        print(f"保存しました（AWS送信なし）: {args.out}")
        return 0
    except (InvalidData, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
