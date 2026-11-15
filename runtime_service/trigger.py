"""S3のready manifestだけを起点に、単一Runtimeへ受付要求を送るLambda。"""
from __future__ import annotations

import os
from urllib.parse import unquote_plus
from typing import Any

from copilot_learning.core import digest, json_bytes
from copilot_learning.review_safety import require_online_release
from .contracts import Settings, VERSION, decode, fail, job, valid_key
from .aws_adapters import S3Store


def dispatch(event: dict, settings: Settings, store: Any, runtime: Any, *, runtime_arn: str,
             qualifier: str = "DEFAULT") -> dict:
    if isinstance(event, dict) and event.get("Event") == "s3:TestEvent":
        if event.get("Bucket") != settings.bucket or event.get("Service") != "Amazon S3":
            fail("INVALID_S3_TEST_EVENT")
        return {"accepted": [], "ignored": 1, "processing_complete": False}
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        fail("INVALID_S3_EVENT")
    accepted, ignored = [], 0
    # バッチ全体の構造を先に検査。途中の不正レコードで先行ジョブだけ送らない。
    sources = []
    for record in records:
        if (not isinstance(record, dict) or record.get("eventSource") != "aws:s3"
                or record.get("awsRegion") != settings.region or not isinstance(record.get("eventName"), str)):
            fail("INVALID_S3_EVENT")
        if not record["eventName"].startswith("ObjectCreated:"):
            ignored += 1
            continue
        s3 = record.get("s3", {})
        if s3.get("bucket", {}).get("name") != settings.bucket:
            fail("EVENT_BUCKET_NOT_ALLOWED")
        obj = s3.get("object", {})
        raw_key = obj.get("key")
        if not isinstance(raw_key, str):
            fail("INVALID_EVENT_KEY")
        key = unquote_plus(raw_key, encoding="utf-8", errors="strict")
        if not key.startswith(settings.ready_prefix) or not key.endswith(".job.json"):
            ignored += 1  # 成果物や原本ZIPのイベントは起動しない。
            continue
        if not valid_key(key, settings.ready_prefix) or not isinstance(obj.get("versionId"), str) or obj["versionId"] == "null" or not obj["versionId"]:
            fail("VERSIONED_JOB_REQUIRED")
        sources.append((key, obj["versionId"]))
    for key, version in sources:
        ref, body = store.ready(key, version)
        job(decode(body), settings)
        jid = digest(json_bytes(ref))
        payload = {"schema_version": VERSION, "action": "run", "job_id": jid, "job_ref": ref}
        response = runtime.invoke_agent_runtime(agentRuntimeArn=runtime_arn, qualifier=qualifier,
            runtimeSessionId="job-" + jid, contentType="application/json", accept="application/json", payload=json_bytes(payload))
        stream = response["response"]
        try:
            answer = stream.read(32769)
        finally:
            stream.close()
        if len(answer) > 32768 or response.get("statusCode", 200) not in {200, 202}:
            fail("RUNTIME_REJECTED")
        result = decode(answer)
        if result.get("job_id") != jid or result.get("status") not in {"ACCEPTED", "RUNNING", "SUCCEEDED", "WAITING_MEMORY"}:
            fail("RUNTIME_NOT_ACCEPTED")
        accepted.append({"job_id": jid, "status": result["status"]})
    return {"accepted": accepted, "ignored": ignored, "processing_complete": False}


def lambda_handler(event, context):
    # テスト用の注入関数dispatchはあるが、環境変数でproduction安全停止を解除する経路はない。
    require_online_release()
    import boto3
    from botocore.config import Config
    settings = Settings.from_env()
    sdk = boto3.Session(region_name=settings.region)
    config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 2})
    try:
        if isinstance(event, dict) and event.get("action") == "resume_memory_waits":
            from .resume import EVENT, ResumeLedger, dispatch_due
            if event != EVENT:
                fail("INVALID_RESUME_EVENT")
            if os.environ.get("ENABLE_MEMORY_RESUME", "false") != "true":
                return {"status": "MEMORY_RESUME_DISABLED", "processing_complete": False}
            # 定期再開はSDK内再試行を重ねず、永続台帳の次回期限で再確認する。
            resume_config = Config(connect_timeout=5, read_timeout=15,
                                   retries={"mode": "standard", "total_max_attempts": 1})
            ledger = ResumeLedger(sdk.resource("dynamodb", config=resume_config).Table(os.environ["JOB_TABLE"]), settings)
            result = dispatch_due(event, settings, ledger, sdk.client("bedrock-agentcore", config=resume_config),
                                  runtime_arn=os.environ["RUNTIME_ARN"],
                                  qualifier=os.environ.get("RUNTIME_QUALIFIER", "DEFAULT"),
                                  remaining_ms=context.get_remaining_time_in_millis)
            # 本文・S3キー・Memory IDを出さず、運用上必要な件数だけCloudWatchへ残す。
            print(json_bytes({"event": "memory_resume_tick", **result}).decode("utf-8"))
            return result
        return dispatch(event, settings, S3Store(sdk.client("s3", config=config), settings),
                        sdk.client("bedrock-agentcore", config=config), runtime_arn=os.environ["RUNTIME_ARN"],
                        qualifier=os.environ.get("RUNTIME_QUALIFIER", "DEFAULT"))
    except Exception:
        # Lambdaの再試行・DLQに渡すが、S3キーや入力内容を例外メッセージに含めない。
        raise RuntimeError("RUNTIME_DISPATCH_FAILED") from None
