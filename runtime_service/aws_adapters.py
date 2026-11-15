"""AWS SDKとの境界。production factoryと各アダプターに既存の送信停止を適用する。"""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from copilot_learning.core import digest, json_bytes
from copilot_learning.learning import BedrockConverseProvider
from copilot_learning.review_safety import require_online_release
from .contracts import Settings, Rejected, decode, fail, reference, valid_key


def error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


class S3Store:
    """版指定読取と、チェックサム付き条件付きPUT。自己申告metadataだけを信用しない。"""
    def __init__(self, client: Any, settings: Settings, guard: Callable = require_online_release):
        self.client, self.settings, self.guard = client, settings, guard

    def _get(self, key: str, version: str | None, limit: int) -> tuple[bytes, dict]:
        self.guard()
        args = {"Bucket": self.settings.bucket, "Key": key,
                "ExpectedBucketOwner": self.settings.owner, "ChecksumMode": "ENABLED"}
        if version is not None:
            args["VersionId"] = version
        result = self.client.get_object(**args)
        stream = result["Body"]
        try:
            if result.get("ContentLength", limit + 1) > limit:
                fail("OBJECT_TOO_LARGE")
            data = stream.read(limit + 1)
        finally:
            stream.close()
        if len(data) > limit or result.get("ContentLength") != len(data):
            fail("OBJECT_SIZE_MISMATCH")
        if version is not None and result.get("VersionId") != version:
            fail("OBJECT_VERSION_MISMATCH")
        if result.get("ServerSideEncryption") != "aws:kms" or result.get("SSEKMSKeyId") != self.settings.kms_key:
            fail("OBJECT_ENCRYPTION_MISMATCH")
        return data, result

    def read(self, ref: dict, limit: int = 60 * 1024 * 1024, *, ready: bool = False) -> bytes:
        reference(ref, self.settings, ready=ready)
        data, _ = self._get(ref["key"], ref["version_id"], limit)
        if digest(data) != ref["sha256"]:
            fail("OBJECT_SHA_MISMATCH")
        return data

    def ready(self, key: str, version: str) -> tuple[dict, bytes]:
        if not valid_key(key, self.settings.ready_prefix) or not key.endswith(".job.json") or not version or version == "null":
            fail("NOT_A_VERSIONED_JOB")
        data, _ = self._get(key, version, 65536)
        ref = {"bucket": self.settings.bucket, "key": key, "version_id": version, "sha256": digest(data)}
        reference(ref, self.settings, ready=True)
        return ref, data

    def result(self, key: str) -> bytes | None:
        if not valid_key(key, self.settings.output_prefix):
            fail("INVALID_RESULT_KEY")
        try:
            data, _ = self._get(key, None, 60 * 1024 * 1024)
            return data
        except Exception as exc:
            if error_code(exc) in {"NoSuchKey", "404"}:
                return None
            raise

    def put(self, key: str, data: bytes) -> dict:
        self.guard()
        if not valid_key(key, self.settings.output_prefix) or not isinstance(data, bytes) or len(data) > 60 * 1024 * 1024:
            fail("INVALID_RESULT")
        sha = digest(data)
        checksum = base64.b64encode(bytes.fromhex(sha)).decode("ascii")
        try:
            result = self.client.put_object(
                Bucket=self.settings.bucket, Key=key, Body=data,
                ExpectedBucketOwner=self.settings.owner, IfNoneMatch="*",
                ServerSideEncryption="aws:kms", SSEKMSKeyId=self.settings.kms_key,
                ChecksumSHA256=checksum, ContentType="application/octet-stream")
            if result.get("ChecksumSHA256") != checksum or not result.get("VersionId") or result["VersionId"] == "null":
                fail("PUT_RECEIPT_UNVERIFIED")
        except Exception as exc:
            if error_code(exc) not in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                raise
            # 同じSHAというmetadataがあっても、実バイト列を再取得して一致しなければ拒否。
            existing, result = self._get(key, None, len(data) + 1)
            if existing != data or result.get("ChecksumSHA256") != checksum:
                fail("RESULT_WRITE_CONFLICT")
        return {"key": key, "sha256": sha, "version_id": result.get("VersionId"), "size_bytes": len(data)}


class DynamoLedger:
    """競合検出付きジョブ台帳。期限切れRUNNINGを勝手に再実行しない。"""
    def __init__(self, table: Any, settings: Settings, guard: Callable = require_online_release):
        self.table, self.settings, self.guard = table, settings, guard

    def get(self, job_id: str) -> dict | None:
        self.guard()
        return self.table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")

    def claim(self, job_id: str, expected_revision: int | None = None) -> tuple[dict, bool]:
        self.guard()
        now, token = int(time.time()), uuid.uuid4().hex
        if expected_revision is None:
            item = {"job_id": job_id, "status": "RUNNING", "revision": 1, "attempt": 1,
                    "token": token, "started_at": now, "deadline": now + self.settings.max_job_seconds}
            try:
                self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
                return item, True
            except Exception as exc:
                if error_code(exc) != "ConditionalCheckFailedException":
                    raise
                old = self.get(job_id)
                if old is None:
                    fail("LEDGER_READ_CONFLICT")
                return old, False
        try:
            response = self.table.update_item(
                Key={"job_id": job_id},
                ConditionExpression="#rev=:rev AND (#st=:failed OR #st=:waiting)",
                UpdateExpression="SET #st=:running, #rev=#rev+:one, attempt=attempt+:one, #tok=:token, started_at=:now, deadline=:deadline REMOVE error_code, finished_at",
                ExpressionAttributeNames={"#rev": "revision", "#st": "status", "#tok": "token"},
                ExpressionAttributeValues={":rev": expected_revision, ":failed": "FAILED", ":waiting": "WAITING_MEMORY",
                                           ":running": "RUNNING", ":one": 1, ":token": token,
                                           ":now": now, ":deadline": now + self.settings.max_job_seconds},
                ReturnValues="ALL_NEW")
            return response["Attributes"], True
        except Exception as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                fail("RETRY_REVISION_CONFLICT")
            raise

    def finish(self, row: dict, status: str, code: str = "NONE") -> None:
        self.guard()
        if status not in {"SUCCEEDED", "FAILED", "WAITING_MEMORY"}:
            fail("INVALID_FINAL_STATUS")
        self.table.update_item(
            Key={"job_id": row["job_id"]}, ConditionExpression="#tok=:token AND #st=:running",
            UpdateExpression="SET #st=:status, error_code=:code, #rev=#rev+:one, finished_at=:now",
            ExpressionAttributeNames={"#tok": "token", "#st": "status", "#rev": "revision"},
            ExpressionAttributeValues={":token": row["token"], ":running": "RUNNING", ":status": status,
                                       ":code": code, ":one": 1, ":now": int(time.time())})


class Models:
    def __init__(self, client: Any, settings: Settings, guard: Callable = require_online_release):
        self.client, self.settings, self.guard = client, settings, guard

    def generate(self, model_id: str, system: str, data: str) -> tuple[dict, dict]:
        self.guard()
        self.settings.model(model_id)
        if len((system + data).encode("utf-8")) > 131072:
            fail("MODEL_INPUT_TOO_LARGE")
        # 既存Providerの正常終了・JSON・secret検査を利用。出力上限はProviderの2300を維持。
        owner = self
        class BoundedClient:
            def converse(inner, **request):
                request["inferenceConfig"]["maxTokens"] = min(request["inferenceConfig"]["maxTokens"], owner.settings.max_output_tokens)
                return owner.client.converse(**request)
        provider = BedrockConverseProvider(model_id, self.settings.region, client=BoundedClient())
        result = provider.generate(data, system=system)
        return result, provider.last_metadata


class Memory:
    def __init__(self, client: Any, control: Any, guard: Callable = require_online_release):
        self.client, self.control, self.guard = client, control, guard

    def ready(self, profile: dict) -> None:
        self.guard()
        memory = self.control.get_memory(memoryId=profile["memory_id"])["memory"]
        active = {s.get("strategyId") for s in memory.get("strategies", []) if s.get("status") == "ACTIVE"}
        if memory.get("status") != "ACTIVE" or not set(profile["strategy_ids"]) <= active:
            fail("MEMORY_STRATEGY_NOT_ACTIVE")

    def send(self, request: dict) -> dict:
        self.guard()
        params = dict(request)
        params["eventTimestamp"] = datetime.fromisoformat(params["eventTimestamp"].replace("Z", "+00:00"))
        event = self.client.create_event(**params).get("event", {})
        if not isinstance(event.get("eventId"), str) or not event["eventId"]:
            fail("MEMORY_RECEIPT_MISSING")
        return {"event_id": event["eventId"], "client_token": request["clientToken"]}

    def collect(self, profile: dict) -> dict:
        self.guard()
        records, seen_ids = [], set()
        for strategy in profile["strategy_ids"]:
            token, tokens = None, set()
            for _ in range(20):
                args = {"memoryId": profile["memory_id"], "namespace": profile["namespace"],
                        "memoryStrategyId": strategy, "maxResults": 100}
                if token:
                    args["nextToken"] = token
                result = self.client.list_memory_records(**args)
                rows = result.get("memoryRecordSummaries")
                if not isinstance(rows, list):
                    fail("INVALID_MEMORY_PAGE")
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get("memoryRecordId"), str)
                            or row["memoryRecordId"] in seen_ids or row.get("memoryStrategyId") != strategy
                            or not isinstance(row.get("namespaces"), list) or not row["namespaces"]
                            or any(not isinstance(n, str) or not n.startswith(profile["namespace"]) for n in row["namespaces"])):
                        fail("MEMORY_SCOPE_OR_DUPLICATE")
                    seen_ids.add(row["memoryRecordId"])
                    # SDKのdatetimeを保存可能にする。サービス出力をその他の点では変更しない。
                    records.append(json.loads(json.dumps(row, default=lambda x: x.isoformat() if isinstance(x, datetime) else fail("INVALID_MEMORY_TYPE"))))
                    if len(records) > 128:
                        fail("MEMORY_RECORD_LIMIT")
                token = result.get("nextToken")
                if not token:
                    break
                if not isinstance(token, str) or token in tokens:
                    fail("MEMORY_PAGINATION_LOOP")
                tokens.add(token)
            else:
                fail("MEMORY_PAGINATION_LIMIT")
        return {"memory_id": profile["memory_id"], "namespace": profile["namespace"],
                "strategy_ids": profile["strategy_ids"],
                "response": {"memoryRecordSummaries": sorted(records, key=lambda x: x["memoryRecordId"])}}


def production(settings: Settings):
    """設定や環境変数だけでは送信停止を解除しない。接続前に必ず停止する。"""
    require_online_release()
    import boto3
    from botocore.config import Config
    sdk = boto3.Session(region_name=settings.region)
    config = Config(connect_timeout=5, read_timeout=90, retries={"mode": "standard", "total_max_attempts": 2})
    from .resume import ResumeLedger
    return (S3Store(sdk.client("s3", config=config), settings),
            ResumeLedger(sdk.resource("dynamodb", config=config).Table(__import__("os").environ["JOB_TABLE"]), settings),
            Models(sdk.client("bedrock-runtime", config=config), settings),
            Memory(sdk.client("bedrock-agentcore", config=config), sdk.client("bedrock-agentcore-control", config=config)))
