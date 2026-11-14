"""AWS CLIの明示的な操作だけを行う境界。インポート時にはAWS通信しない。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .core import InvalidData, SHA_PATTERN, digest
from .review_safety import require_online_release

AWS_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")


def validate_approval(approval_path: Path, fingerprint: str, archive_sha256: str,
                      expected_account: str, bucket: str | None = None,
                      manifest_sha256: str | None = None) -> dict[str, Any]:
    """手作業による確認の記録。社内の承認プロセスそのものを代替しない。"""
    if not AWS_ACCOUNT_RE.fullmatch(expected_account):
        raise InvalidData("AWS account IDは12桁で指定してください")
    if not approval_path.is_file() or approval_path.is_symlink():
        raise InvalidData("承認証跡ファイルがありません")
    approval = json.loads(approval_path.read_text("utf-8"))
    required = {
        "schema_version": "1.0", "record_type": "upload-approval", "status": "approved",
        "session_fingerprint": fingerprint, "archive_sha256": archive_sha256,
        "target_account_id": expected_account,
    }
    for key, expected in required.items():
        if approval.get(key) != expected:
            raise InvalidData(f"承認記録の{key}が一致しません")
    if bucket is not None and approval.get("target_bucket") != bucket:
        raise InvalidData("承認したバケットと送信先が一致しません")
    if manifest_sha256 is not None and approval.get("manifest_sha256") != manifest_sha256:
        raise InvalidData("承認時のmanifestと現在のmanifestが異なります")
    if not approval.get("reviewer") or not approval.get("reviewed_at_utc"):
        raise InvalidData("承認者・レビュー日時が必要です")
    if approval.get("data_classification") not in {"synthetic", "approved_non_sensitive"}:
        raise InvalidData("未承認のデータ区分です")
    return approval


def aws_command(args: list[str], *, region: str, profile: str = "") -> dict[str, Any]:
    require_online_release()
    if shutil.which("aws") is None:
        raise RuntimeError("AWS CLI v2が見つかりません")
    cmd = ["aws", *args, "--region", region, "--no-cli-pager", "--output", "json"]
    if profile:
        cmd.extend(["--profile", profile])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    if result.returncode != 0:
        # AWSのエラー全文に機密が混入する可能性があるため、標準エラーはログ出力しない。
        raise RuntimeError(f"AWS CLI操作に失敗しました（exit={result.returncode}）")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def assert_account(account_id: str, *, region: str, profile: str = "") -> None:
    if not AWS_ACCOUNT_RE.fullmatch(account_id):
        raise InvalidData("AWSアカウントIDが不正です")
    identity = aws_command(["sts", "get-caller-identity"], region=region, profile=profile)
    if identity.get("Account") != account_id:
        raise InvalidData("指定AWSアカウントと実接続先が一致しません")


def send_memory_plan(requests: list[dict[str, Any]], *, region: str,
                     profile: str, max_events: int = 100) -> list[dict[str, Any]]:
    if len(requests) > max_events:
        raise InvalidData(f"イベント件数の上限超過: {len(requests)} > {max_events}")
    receipt = []
    for index, payload in enumerate(requests):
        # AWS CLIはfile://形式でJSONを受け取れる。パスはOS標準の絶対パスを使用する。
        with tempfile.TemporaryDirectory(prefix="copilot-memory-") as tempdir:
            request_file = Path(tempdir) / "event.json"
            request_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            response = aws_command([
                "bedrock-agentcore", "create-event", "--cli-input-json", f"file://{request_file.resolve().as_posix()}"
            ], region=region, profile=profile)
        receipt.append({"event_index": index, "client_token": payload["clientToken"],
                        "event_id": response.get("event", {}).get("eventId")})
    return receipt


def list_memory_records(memory_id: str, namespace: str, *, region: str, profile: str = "") -> dict[str, Any]:
    if not namespace or not namespace.startswith("/"):
        raise InvalidData("AWSのMemory namespaceを明示してください")
    # CLIのデフォルト自動ページングを使用する。
    return aws_command(["bedrock-agentcore", "list-memory-records",
                        "--memory-id", memory_id, "--namespace", namespace],
                       region=region, profile=profile)
