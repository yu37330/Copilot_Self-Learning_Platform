#!/usr/bin/env python3
"""適用条件の明示一致と意味レビューゲートを、未学習タスクでオフライン比較する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.semantic_holdout import evaluate_semantic_holdout


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        if args.out.exists() or args.out.is_symlink():
            raise InvalidData("出力結果の上書きは禁止です")
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
        report = evaluate_semantic_holdout(dataset)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as handle:
            handle.write(json_bytes(report))
        print(f"OFFLINE EVAL: cases={report['total_cases']}; 実モデル・AWS・Copilot実行なし")
        return 0
    except (InvalidData, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
