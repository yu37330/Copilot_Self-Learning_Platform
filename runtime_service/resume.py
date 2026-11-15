"""Memory待ちだけを有限回再確認する。単一Runtime・同じLambdaを使い、実送信停止は維持。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable

from copilot_learning.core import digest, json_bytes
from .aws_adapters import DynamoLedger, error_code
from .contracts import Settings, command, decode, fail

EVENT = {"schema_version": "memory-resume-1", "action": "resume_memory_waits"}
INDEX = "memory-resume-due-v1"
QUEUE = "MEMORY_V1"
HOLD_CODES = {"MEMORY_WAIT_LIMIT", "MEMORY_WAIT_DEADLINE", "RESUME_DISPATCH_LIMIT",
              "RESUME_BINDING_CHANGED", "RESUME_STATE_INVALID"}


def _number(value: Any) -> int:
    # DynamoDB resourceは整数もDecimalで返す。文字列・bool・小数を黙って変換しない。
    if (isinstance(value, bool) or not isinstance(value, (int, Decimal))
            or not 0 <= value <= 10**12 or value != int(value)):
        fail("RESUME_STATE_INVALID")
    return int(value)


@dataclass(frozen=True)
class ResumePolicy:
    initial_seconds: int = 60
    maximum_seconds: int = 900
    max_wait_cycles: int = 8
    max_elapsed_seconds: int = 7200
    max_dispatches: int = 16
    dispatch_lease_seconds: int = 120
    batch_size: int = 5

    def __post_init__(self):
        bounds = ((self.initial_seconds, 1, 900), (self.maximum_seconds, 1, 3600),
                  (self.max_wait_cycles, 1, 32), (self.max_elapsed_seconds, 60, 86400),
                  (self.max_dispatches, 1, 64), (self.dispatch_lease_seconds, 60, 900),
                  (self.batch_size, 1, 10))
        if any(type(v) is not int or not lo <= v <= hi for v, lo, hi in bounds):
            fail("INVALID_RESUME_POLICY")
        if self.initial_seconds > self.maximum_seconds:
            fail("INVALID_RESUME_POLICY")

    def delay(self, wait_count: int) -> int:
        return min(self.initial_seconds * 2 ** min(max(wait_count - 1, 0), 20), self.maximum_seconds)


def context_sha(settings: Settings, policy: ResumePolicy) -> str:
    """コード・モデル・Memory・機密辞書・予算をハッシュで固定。本文は台帳へ写さない。"""
    return digest(json_bytes({"settings": asdict(settings), "resume_policy": asdict(policy)}))


def canonical_request(request: dict, settings: Settings) -> dict:
    command(request, settings)
    result = {k: v for k, v in request.items() if k != "expected_revision"}
    result["action"] = "run"
    return decode(json_bytes(result))


def wait_update(row: dict, now: int, policy: ResumePolicy) -> dict:
    """待機情報の計算だけを行う。上限到達は成功や抽出失敗ではなく要確認にする。"""
    count = _number(row.get("resume_wait_count")) + 1
    started = _number(row.get("resume_started_at"))
    dispatches = _number(row.get("resume_dispatch_count"))
    code = None
    if count > policy.max_wait_cycles:
        code = "MEMORY_WAIT_LIMIT"
    elif now >= started + policy.max_elapsed_seconds:
        code = "MEMORY_WAIT_DEADLINE"
    elif dispatches >= policy.max_dispatches:
        code = "RESUME_DISPATCH_LIMIT"
    result = {"resume_wait_count": count, "status": "RECOVERY_REQUIRED" if code else "WAITING_MEMORY",
              "error_code": code or "ASYNC_EXTRACTION_NOT_CONFIRMED"}
    if code is None:
        result.update(resume_queue=QUEUE,
                      resume_due_at=min(now + policy.delay(count), started + policy.max_elapsed_seconds))
    return result


class ResumeLedger(DynamoLedger):
    """GSIは候補検索だけに利用。再開判断は強整合読取と条件付き更新で行う。"""
    def __init__(self, table, settings, guard=None, *, policy=None, clock=time.time):
        if guard is None:
            super().__init__(table, settings)
        else:
            super().__init__(table, settings, guard)
        self.policy, self.clock = policy or ResumePolicy(), clock

    def bind_request(self, row: dict, request: dict) -> dict:
        self.guard()
        value = canonical_request(request, self.settings)
        if value["job_id"] != row["job_id"]:
            fail("RESUME_BINDING_CHANGED")
        response = self.table.update_item(
            Key={"job_id": row["job_id"]},
            ConditionExpression="#tok=:token AND #st=:running AND "
                "(attribute_not_exists(#sha) OR #sha=:sha) AND "
                "(attribute_not_exists(#ctx) OR #ctx=:ctx)",
            UpdateExpression="SET #req=:request, #sha=:sha, #ctx=:ctx, "
                "resume_started_at=if_not_exists(resume_started_at,:now), "
                "resume_wait_count=if_not_exists(resume_wait_count,:zero), "
                "resume_dispatch_count=if_not_exists(resume_dispatch_count,:zero) "
                "REMOVE resume_queue, resume_due_at, resume_dispatch_token",
            ExpressionAttributeNames={"#tok": "token", "#st": "status", "#req": "resume_request",
                                      "#sha": "resume_request_sha256", "#ctx": "resume_context_sha256"},
            ExpressionAttributeValues={":token": row["token"], ":running": "RUNNING", ":request": value,
                ":sha": digest(json_bytes(value)), ":ctx": context_sha(self.settings, self.policy),
                ":now": int(self.clock()), ":zero": 0}, ReturnValues="ALL_NEW")
        return response["Attributes"]

    def finish(self, row: dict, status: str, code: str = "NONE") -> None:
        self.guard()
        if status not in {"SUCCEEDED", "FAILED", "WAITING_MEMORY"}:
            fail("INVALID_FINAL_STATUS")
        now = int(self.clock())
        changes = {"status": status, "error_code": code, "finished_at": now}
        if status == "WAITING_MEMORY":
            changes.update(wait_update(row, now, self.policy))
        names = {"#tok": "token", "#st": "status", "#rev": "revision"}
        values = {":token": row["token"], ":running": "RUNNING", ":one": 1}
        clauses = ["#rev=#rev+:one"]
        for i, (key, value) in enumerate(changes.items()):
            names[f"#u{i}"], values[f":u{i}"] = key, value
            clauses.append(f"#u{i}=:u{i}")
        remove = ["resume_dispatch_token"]
        if changes["status"] != "WAITING_MEMORY":
            remove += ["resume_queue", "resume_due_at"]
        self.table.update_item(Key={"job_id": row["job_id"]},
            ConditionExpression="#tok=:token AND #st=:running",
            UpdateExpression="SET " + ", ".join(clauses) + " REMOVE " + ", ".join(remove),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def due(self) -> dict:
        self.guard()
        return self.table.query(IndexName=INDEX,
            KeyConditionExpression="#queue=:queue AND #due<=:now",
            ExpressionAttributeNames={"#queue": "resume_queue", "#due": "resume_due_at"},
            ExpressionAttributeValues={":queue": QUEUE, ":now": int(self.clock())},
            ScanIndexForward=True, Limit=self.policy.batch_size)

    def reserve(self, row: dict) -> dict | None:
        self.guard()
        now = int(self.clock())
        try:
            result = self.table.update_item(Key={"job_id": row["job_id"]},
                ConditionExpression="#st=:waiting AND #rev=:rev AND resume_queue=:queue "
                    "AND resume_due_at<=:now AND resume_dispatch_count=:count",
                UpdateExpression="SET resume_due_at=:later, resume_dispatch_token=:lease, "
                    "resume_dispatch_count=resume_dispatch_count+:one",
                ExpressionAttributeNames={"#st": "status", "#rev": "revision"},
                ExpressionAttributeValues={":waiting": "WAITING_MEMORY", ":rev": _number(row["revision"]),
                    ":queue": QUEUE, ":now": now, ":count": _number(row["resume_dispatch_count"]),
                    ":later": now + self.policy.dispatch_lease_seconds, ":lease": uuid.uuid4().hex, ":one": 1},
                ReturnValues="ALL_NEW")
            return result["Attributes"]
        except Exception as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return None
            raise

    def hold(self, row: dict, code: str) -> bool:
        self.guard()
        if code not in HOLD_CODES:
            fail("RESUME_STATE_INVALID")
        try:
            self.table.update_item(Key={"job_id": row["job_id"]},
                ConditionExpression="#st=:waiting AND #rev=:rev AND resume_queue=:queue AND resume_due_at<=:now",
                UpdateExpression="SET #st=:hold, error_code=:code, #rev=#rev+:one, finished_at=:now "
                    "REMOVE resume_queue, resume_due_at, resume_dispatch_token",
                ExpressionAttributeNames={"#st": "status", "#rev": "revision"},
                ExpressionAttributeValues={":waiting": "WAITING_MEMORY", ":rev": _number(row["revision"]),
                    ":queue": QUEUE, ":now": int(self.clock()), ":hold": "RECOVERY_REQUIRED",
                    ":code": code, ":one": 1})
            return True
        except Exception as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return False
            raise


def retry_for(row: dict, settings: Settings, policy: ResumePolicy, now: int) -> tuple[dict | None, str | None]:
    """期限・版・送信元を再確認。FAILED/RUNNING/SUCCEEDEDは自動再実行しない。"""
    if row.get("status") != "WAITING_MEMORY" or row.get("resume_queue") != QUEUE:
        return None, None
    if _number(row.get("resume_due_at")) > now:
        return None, None
    if _number(row.get("resume_wait_count")) > policy.max_wait_cycles:
        return None, "MEMORY_WAIT_LIMIT"
    if now >= _number(row.get("resume_started_at")) + policy.max_elapsed_seconds:
        return None, "MEMORY_WAIT_DEADLINE"
    if _number(row.get("resume_dispatch_count")) >= policy.max_dispatches:
        return None, "RESUME_DISPATCH_LIMIT"
    original = canonical_request(row.get("resume_request"), settings)
    if (original["job_id"] != row.get("job_id")
            or digest(json_bytes(original)) != row.get("resume_request_sha256")
            or row.get("resume_context_sha256") != context_sha(settings, policy)):
        return None, "RESUME_BINDING_CHANGED"
    value = {**original, "action": "retry", "expected_revision": _number(row["revision"])}
    command(value, settings)
    return value, None


def dispatch_due(event: dict, settings: Settings, ledger: ResumeLedger, runtime: Any,
                 *, runtime_arn: str, qualifier: str = "DEFAULT",
                 remaining_ms: Callable[[], int] = lambda: 120000) -> dict:
    """定期イベント一回分。結果の本文を保存/記録せず、集計だけを返す。"""
    if event != EVENT:
        fail("INVALID_RESUME_EVENT")
    summary = {"dispatched": 0, "held": 0, "skipped": 0, "errors": 0,
               "time_budget_deferred": False, "more_pending": False, "processing_complete": False}
    if remaining_ms() < 65000:
        summary["time_budget_deferred"] = True
        return summary
    page = ledger.due()
    summary["more_pending"] = bool(page.get("LastEvaluatedKey"))
    for indexed in page.get("Items", []):
        if remaining_ms() < 65000:
            summary["time_budget_deferred"] = True
            break
        row = None
        try:
            # GSIの行をそのまま信じず、台帳本体の最新状態を読む。
            row = ledger.get(indexed["job_id"])
            if row is None:
                summary["skipped"] += 1
                continue
            try:
                payload, reason = retry_for(row, settings, ledger.policy, int(ledger.clock()))
            except (TypeError, KeyError, ValueError):
                payload, reason = None, "RESUME_STATE_INVALID"
            if reason:
                summary["held"] += int(ledger.hold(row, reason))
                continue
            if payload is None or ledger.reserve(row) is None:
                summary["skipped"] += 1
                continue
            # 応答消失後も同じrevisionは同じsession。台帳の条件付きclaimが二重実行を抑止。
            session = "resume-" + digest(json_bytes([payload["job_id"], payload["expected_revision"]]))
            response = runtime.invoke_agent_runtime(agentRuntimeArn=runtime_arn, qualifier=qualifier,
                runtimeSessionId=session, contentType="application/json", accept="application/json",
                payload=json_bytes(payload))
            stream = response["response"]
            try:
                body = stream.read(32769)
            finally:
                stream.close()
            if len(body) > 32768 or response.get("statusCode", 200) not in {200, 202}:
                fail("RUNTIME_REJECTED")
            result = decode(body)
            if result.get("job_id") != row["job_id"] or result.get("status") not in {"ACCEPTED", "RUNNING", "SUCCEEDED", "WAITING_MEMORY"}:
                fail("RUNTIME_NOT_ACCEPTED")
            summary["dispatched"] += 1
        except Exception:
            # 起動結果不明ではWAITINGへ巻き戻さない。リース満了後に最新状態を再確認する。
            summary["errors"] += 1
    return summary
