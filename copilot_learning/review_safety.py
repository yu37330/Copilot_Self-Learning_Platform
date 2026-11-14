"""レビュー第1弾の安全境界。社内認証・DLPの完成まで外部実行は停止する。"""
from __future__ import annotations

import base64
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .core import InvalidData, digest, json_bytes, build_memory_requests, normalize_events


class ExternalExecutionBlocked(RuntimeError):
    """実環境の承認基盤が未完成であるため、外部送受信を許可しない。"""


def require_online_release() -> None:
    # フラグや環境変数で解除しない。社内ID・署名承認・DLPを別レビューで完成させる。
    raise ExternalExecutionBlocked(
        "外部実行は安全レビュー完了まで停止中です。ローカルの合成データ検証のみ可能です。"
        "社内IDによる承認・操作別送信範囲・フィードバック承認・DLPは未完成です。"
    )


def regenerate_memory_plan(manifest: dict[str, Any], events: list[dict[str, Any]],
                           supplied: Any, memory_id: str, actor_id: str) -> list[dict[str, Any]]:
    """検証済み原本から再生成し、比較対象ファイルを送信データとして信用しない。"""
    pattern = r"(?:arn:(?:aws|aws-cn|aws-us-gov):bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:memory/)?[a-zA-Z][a-zA-Z0-9_-]{0,99}-[a-zA-Z0-9]{10}"
    if not isinstance(memory_id, str) or not re.fullmatch(pattern, memory_id):
        raise InvalidData("Memory IDがAWSの制約に適合しません（仮IDはオフライン計画のみ）")
    regenerated = build_memory_requests(normalize_events(manifest, events), memory_id, actor_id)
    if not regenerated or len(regenerated) > 100:
        raise InvalidData("送信計画のイベント数は1〜100件に限定します")
    if not isinstance(supplied, list) or json_bytes(supplied) != json_bytes(regenerated):
        raise InvalidData("Memory計画が検証済み原本からの再生成結果と一致しません")
    return regenerated


def operation_scope(operation: str, target: dict[str, Any], payload: Any,
                    input_digests: dict[str, str]) -> dict[str, Any]:
    """後続の署名承認で使う比較可能な契約。これ自体には承認主体の証明力はない。"""
    if operation not in {"memory-send", "memory-export", "bedrock-converse", "s3-put"}:
        raise InvalidData("未対応の操作です")
    return {"schema_version": "2.0", "operation": operation,
            "target": json.loads(json_bytes(target)),
            "payload_sha256": digest(json_bytes(payload)),
            "input_digests": dict(sorted(input_digests.items()))}


def validate_operation_scope(approved_scope: Any, actual_scope: dict[str, Any]) -> None:
    if not isinstance(approved_scope, dict) or json_bytes(approved_scope) != json_bytes(actual_scope):
        raise InvalidData("承認した操作・送信先・本文・フィードバックの範囲と一致しません")


@dataclass(frozen=True)
class ObjectSnapshot:
    """不変bytesを保持する。元のパスを後から読み直して送信しない。"""
    body: bytes
    sha256: str
    checksum_base64: str

    @classmethod
    def capture(cls, path: Path, approved_sha256: str) -> "ObjectSnapshot":
        if path.is_symlink() or not path.is_file():
            raise InvalidData("送信対象ファイルが不正です")
        with path.open("rb") as file:
            body = file.read(60 * 1024 * 1024 + 1)
        if len(body) > 60 * 1024 * 1024 or digest(body) != approved_sha256:
            raise InvalidData("送信対象が承認時のハッシュと一致しません")
        return cls(body, approved_sha256,
                   base64.b64encode(bytes.fromhex(approved_sha256)).decode("ascii"))


class S3OperationError(RuntimeError):
    """モック/将来のCLIアダプターが渡す、本文を含まない機械判定用エラー。"""
    def __init__(self, code: str):
        super().__init__("S3操作が失敗しました")
        self.code = code


def put_snapshot(snapshot: ObjectSnapshot, *, bucket: str, key: str, account_id: str,
                 kms_key_arn: str, command: Callable[[list[str]], dict[str, Any]]) -> dict[str, Any]:
    """検証済みbytesだけをPUTする通信プロトコル。既定の実通信経路は提供しない。"""
    if (digest(snapshot.body) != snapshot.sha256 or
            snapshot.checksum_base64 != base64.b64encode(bytes.fromhex(snapshot.sha256)).decode("ascii")):
        raise InvalidData("スナップショットの整合性が不正です")
    if not re.fullmatch(r"[0-9]{12}", account_id):
        raise InvalidData("AWS account IDが不正です")
    if not re.fullmatch(r"arn:aws(?:-cn|-us-gov)?:kms:[a-z0-9-]+:" + account_id + r":key/[a-zA-Z0-9-]+", kms_key_arn):
        raise InvalidData("KMSキーARNと所有アカウントを明示してください")
    with tempfile.TemporaryDirectory(prefix="copilot-s3-snapshot-") as directory:
        frozen_path = Path(directory) / "payload.bin"
        frozen_path.write_bytes(snapshot.body)
        arguments = ["s3api", "put-object", "--bucket", bucket, "--key", key,
                     "--body", str(frozen_path), "--if-none-match", "*",
                     "--expected-bucket-owner", account_id,
                     "--server-side-encryption", "aws:kms", "--ssekms-key-id", kms_key_arn,
                     "--checksum-algorithm", "SHA256", "--checksum-sha256", snapshot.checksum_base64]
        try:
            response = command(arguments)
        except S3OperationError as exc:
            if exc.code not in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                raise
            # 利用者が自由に設定できるMetadata.sha256では同一性を判定しない。
            response = command(["s3api", "head-object", "--bucket", bucket, "--key", key,
                                "--expected-bucket-owner", account_id, "--checksum-mode", "ENABLED"])
            if (response.get("ChecksumSHA256") != snapshot.checksum_base64 or
                    response.get("ContentLength") != len(snapshot.body) or
                    response.get("ServerSideEncryption") != "aws:kms" or
                    response.get("SSEKMSKeyId") != kms_key_arn):
                raise InvalidData("既存オブジェクトのサービス側チェックサム・暗号化設定が不一致です")
            return {"status": "already_present", "sha256": snapshot.sha256,
                    "version_id": response.get("VersionId")}
        if response.get("ChecksumSHA256") != snapshot.checksum_base64:
            raise InvalidData("PUT応答のサービス側チェックサムが不一致です")
        return {"status": "stored", "sha256": snapshot.sha256,
                "version_id": response.get("VersionId")}
