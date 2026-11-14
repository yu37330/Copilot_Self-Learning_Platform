#!/usr/bin/env python3
"""既存セッションに紐づくフィードバックをGit管理外へ保存する（AWS通信なし）。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import (InvalidData, assert_external_output,
                                   check_sensitive, digest, json_bytes, read_archive)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--out", type=Path, required=True, help="Git管理外のフィードバック保存ディレクトリ")
    p.add_argument("--outcome", required=True, choices=["verified_success", "verified_failure", "unverified"])
    p.add_argument("--rating", type=int, choices=range(1, 6), metavar="1..5")
    p.add_argument("--lesson", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--classification", choices=["synthetic", "approved_non_sensitive"], required=True)
    try:
        args = p.parse_args(argv)
        read_archive(args.staging, args.fingerprint)
        for field in (args.lesson, args.evidence):
            if not field.strip() or len(field) > 3000:
                raise InvalidData("フィードバックは1〜3000文字が必要です")
            check_sensitive(field)
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        now = datetime.now(timezone.utc).isoformat()
        content = {
            "schema_version": "1.0", "record_type": "human-feedback",
            "session_fingerprint": args.fingerprint,
            "created_at_utc": now, "outcome": args.outcome,
            "rating": args.rating, "reusable_lesson": args.lesson,
            "evidence": args.evidence, "data_classification": args.classification,
            "review_status": "requires_manual_approval",
        }
        content["feedback_id"] = digest(json_bytes(content))[:24]
        args.out.mkdir(parents=True, exist_ok=True)
        output = args.out / f"feedback-{content['feedback_id']}.json"
        with output.open("xb") as file:
            file.write(json_bytes(content))
        print(f"ローカルフィードバック作成（未送信・未承認）: {output.name}")
        return 0
    except (InvalidData, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
