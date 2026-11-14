#!/usr/bin/env python3
"""社内承認を得たセッションについて、送信先と内容ハッシュに紐づく確認記録を作る。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.aws_bridge import validate_approval
from copilot_learning.core import InvalidData, assert_external_output, digest, json_bytes, read_archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--classification", required=True, choices=["synthetic", "approved_non_sensitive"])
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--confirm-internal-policy", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest, events = read_archive(args.staging, args.fingerprint)
        manifest_path = args.staging / f"session-{args.fingerprint}.manifest.json"
        approval = {
            "schema_version": "1.0", "record_type": "upload-approval", "status": "approved",
            "session_fingerprint": args.fingerprint,
            "archive_sha256": manifest["archive_sha256"], "manifest_sha256": digest(manifest_path.read_bytes()),
            "target_account_id": args.account_id, "target_bucket": args.bucket,
            "reviewer": args.reviewer, "data_classification": args.classification,
            "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
            "review_scope": "S3 storage and optional approved Bedrock/AgentCore learning",
        }
        if not (args.approve and args.confirm_internal_policy):
            print(f"DRY RUN: {len(events)}イベント。セッションデータを手作業で確認し、社内許可後に両フラグを指定してください。")
            return 0
        if args.reviewer.strip() == "":
            raise InvalidData("reviewerを指定してください")
        dest = args.staging / f"session-{args.fingerprint}.approval.json"
        assert_external_output(dest, Path(__file__).resolve().parents[1])
        # 実際にアップロード先・AWSアカウントを検証。
        tmp = dest.with_name(dest.name + ".tmp")
        with tmp.open("xb") as f:
            f.write(json_bytes(approval))
        try:
            validate_approval(tmp, args.fingerprint, manifest["archive_sha256"], args.account_id,
                              bucket=args.bucket, manifest_sha256=approval["manifest_sha256"])
            tmp.rename(dest)  # 上書き前に存在を確認する
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        print(f"承認記録を作成（Gitにはコミットしない）: {dest.name}")
        return 0
    except (InvalidData, FileExistsError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
