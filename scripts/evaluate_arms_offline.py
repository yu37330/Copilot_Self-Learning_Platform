#!/usr/bin/env python3
"""同一Hold-out・同一推論設定のA/B/C/D比較をオフライン集計する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.paired_experiment import evaluate_arms


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True, type=Path)
    p.add_argument("--observations", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        if args.out.exists() or args.out.is_symlink():
            raise InvalidData("比較レポートを上書きできません")
        dataset = json.loads(args.dataset.read_text("utf-8"))
        obs = json.loads(args.observations.read_text("utf-8"))
        result = evaluate_arms(dataset, obs)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as f:
            f.write(json_bytes(result))
        print("A/B/C/D比較をローカル集計。実Copilot/AWS通信なし、効果実証なし")
        return 0
    except (InvalidData, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
