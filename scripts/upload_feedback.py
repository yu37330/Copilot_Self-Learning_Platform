#!/usr/bin/env python3
"""承認済みフィードバックJSONをS3へ。デフォルトはDry Run。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.aws_bridge import assert_account, aws_command, validate_approval
from copilot_learning.core import (InvalidData, check_sensitive, digest, load_feedback, read_archive)


from copilot_learning.review_safety import require_online_release


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--feedback", type=Path, required=True)
    p.add_argument("--approval-file", type=Path, required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--kms-key-id", required=True)
    p.add_argument("--region", default="ap-northeast-1")
    p.add_argument("--profile", default="")
    p.add_argument("--prefix", default="copilot-learning/v1")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--confirm-feedback-reviewed", action="store_true")
    args = p.parse_args(argv)
    try:
        manifest, _ = read_archive(args.staging, args.fingerprint)
        validate_approval(args.approval_file, args.fingerprint, manifest["archive_sha256"],
                          args.account_id, args.bucket,
                          manifest_sha256=digest((args.staging / f"session-{args.fingerprint}.manifest.json").read_bytes()))
        feedback = load_feedback(args.feedback, args.fingerprint)
        if len(feedback) != 1 or feedback[0].get("record_type") != "human-feedback":
            raise InvalidData("フィードバックは単一のhuman-feedbackレコードにしてください")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_.-]*", args.prefix) or ".." in args.prefix:
            raise InvalidData("S3のprefixが不正です")
        hash_value = digest(args.feedback.read_bytes())
        key = f"{args.prefix}/feedback/{args.fingerprint}/{hash_value}.json"
        if not args.execute:
            print(f"DRY RUN: 1 feedback -> s3://{args.bucket}/{key}。AWS通信はありません。")
            return 0
        require_online_release()
        if not args.confirm_feedback_reviewed:
            raise InvalidData("フィードバック本文の目視確認後に--confirm-feedback-reviewedが必要です")
        assert_account(args.account_id, region=args.region, profile=args.profile)
        response = aws_command([
            "s3api", "put-object", "--bucket", args.bucket, "--key", key,
            "--body", str(args.feedback.resolve()), "--if-none-match", "*",
            "--server-side-encryption", "aws:kms", "--ssekms-key-id", args.kms_key_id,
            "--metadata", f"sha256={hash_value}",
        ], region=args.region, profile=args.profile)
        print("S3へのフィードバック送信が完了しました（AgentCoreへの学習投入は別操作）。")
        return 0
    except (InvalidData, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
