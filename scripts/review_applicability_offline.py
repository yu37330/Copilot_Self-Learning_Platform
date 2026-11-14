#!/usr/bin/env python3
"""外部モデルの意味レビュー候補を証拠付きでオフライン検証する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.semantic_gate import verify_semantic_review


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--task", type=Path, required=True)
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        review = json.loads(args.review.read_text(encoding="utf-8"))
        task = args.task.read_text(encoding="utf-8")
        result = verify_semantic_review(candidate, task, review)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as stream:
            stream.write(json_bytes(result))
        print(f"REVIEWED (LOCAL ONLY): {result['decision']} - no Copilot skill update")
        return 0
    except (InvalidData, OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
