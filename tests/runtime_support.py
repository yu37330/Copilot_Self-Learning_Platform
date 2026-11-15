"""Runtime結合テスト専用の合成入力とメモリ内アダプター。配備ZIPには含めない。"""
from __future__ import annotations

import io
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path

from copilot_learning.core import content_fingerprint, digest, json_bytes
from runtime_service.contracts import Settings, VERSION, reference, fail


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.objects, self.outputs, self.writes = {}, {}, []

    def add(self, key, data, version="v-001"):
        ref = {"bucket": self.settings.bucket, "key": key, "version_id": version, "sha256": digest(data)}
        self.objects[(key, version)] = data
        return ref

    def read(self, ref, limit=60*1024*1024, *, ready=False):
        reference(ref, self.settings, ready=ready)
        data = self.objects[(ref["key"], ref["version_id"])]
        if len(data) > limit or digest(data) != ref["sha256"]:
            fail("OBJECT_SHA_MISMATCH")
        return data

    def ready(self, key, version):
        data = self.objects[(key, version)]
        ref = {"bucket": self.settings.bucket, "key": key, "version_id": version, "sha256": digest(data)}
        reference(ref, self.settings, ready=True)
        return ref, data

    def result(self, key):
        return self.outputs.get(key)

    def put(self, key, data):
        if key in self.outputs and self.outputs[key] != data:
            fail("RESULT_WRITE_CONFLICT")
        self.outputs[key] = data
        self.writes.append(key)
        return {"key": key, "sha256": digest(data), "version_id": "mock-" + digest(data)[:16], "size_bytes": len(data)}


class Ledger:
    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def get(self, jid):
        with self.lock:
            return dict(self.rows[jid]) if jid in self.rows else None

    def claim(self, jid, expected_revision=None):
        with self.lock:
            old = self.rows.get(jid)
            if old and expected_revision is None:
                return dict(old), False
            if expected_revision is not None and (not old or old["revision"] != expected_revision or old["status"] not in {"FAILED", "WAITING_MEMORY"}):
                fail("RETRY_REVISION_CONFLICT")
            row = {"job_id": jid, "status": "RUNNING", "revision": old["revision"]+1 if old else 1,
                   "attempt": old["attempt"]+1 if old else 1, "token": uuid.uuid4().hex,
                   "deadline": int(time.time())+900}
            self.rows[jid] = row
            return dict(row), True

    def finish(self, row, status, code="NONE"):
        with self.lock:
            old = self.rows[row["job_id"]]
            if old["token"] != row["token"] or old["status"] != "RUNNING":
                fail("LEASE_CONFLICT")
            self.rows[row["job_id"]] = {**old, "status": status, "revision": old["revision"]+1, "error_code": code}


class Models:
    def __init__(self):
        self.calls = []
        self.invalid_quote = False
        self.failure = False

    def generate(self, model_id, system, prompt):
        self.calls.append((model_id, system, prompt))
        if self.failure:
            raise RuntimeError("sensitive raw exception must never appear in state")
        data = json.loads(prompt)
        if "source_snapshot" in data:
            source = data["source_snapshot"]["sources"][0]
            result = {"source_snapshot_sha256": data["source_snapshot_sha256"], "pages": [{
                "page_id": "python-env", "title": "Python環境", "links": [], "claims": [{
                    "key": "task", "value": "python-check", "statement": "Python環境の確認を行ったという記録。",
                    "applies_when": ["Python"], "evidence": [{"source_id": source["source_id"],
                    "quote": "捏造された引用" if self.invalid_quote else source["text"]}]}]}]}
        elif "messages" in data:
            result = {"candidates": [{"knowledge_type": "note", "title": "Python環境確認", "description": "実行環境を確認",
                "instruction": "Pythonの実行環境を確認する。", "generalization": "環境差分が疑われる場合の確認",
                "evidence_ids": [data["messages"][-1]["evidence_id"]], "applies_when": ["Python"],
                "not_when": ["API仕様変更"], "counterexamples": []}]}
        else:
            result = {"decisions": [{"knowledge_id": c["knowledge_id"], "decision": "keep", "reason": "合成の判定"}
                                     for c in data["candidates"]]}
        return result, {"model_id": model_id, "usage": {"inputTokens": 10, "outputTokens": 20}, "stop_reason": "end_turn"}


class Memory:
    def __init__(self):
        self.sent, self.collect_calls = [], 0
        self.empty = False
        self.failure_after = None

    def ready(self, profile):
        pass

    def send(self, request):
        if self.failure_after is not None and len(self.sent) == self.failure_after:
            raise RuntimeError("mock network failure")
        self.sent.append(request)
        return {"event_id": "event-" + request["clientToken"][:20], "client_token": request["clientToken"]}

    def collect(self, profile):
        self.collect_calls += 1
        return {"memory_id": profile["memory_id"], "namespace": profile["namespace"],
                "strategy_ids": profile["strategy_ids"], "response": {"memoryRecordSummaries": [] if self.empty else [
                    {"memoryRecordId": "record-" + strategy, "memoryStrategyId": strategy,
                     "namespaces": [profile["namespace"] + "reflections/"],
                     "content": {"text": "Pythonの環境を確認した後、合成テストが成功した。"}}
                    for strategy in profile["strategy_ids"]]}}


def fixture(mode="custom_and_original_wiki", **settings_changes):
    base = dict(bucket="synthetic-learning-bucket", owner="123456789012", region="ap-northeast-1",
        kms_key="arn:aws:kms:ap-northeast-1:123456789012:key/00000000-0000-0000-0000-000000000001",
        model_ids=("synthetic-extractor", "synthetic-grader", "synthetic-wiki"), memory_profiles={"test": {
            "memory_id": "DemoMemory-1234567890", "actor_id": "synthetic-actor", "namespace": "/project/demo/actor/synthetic/",
            "strategy_ids": ["Episodic-1"], "project": "demo"}}, sensitive_literals=("合成機密案件",))
    settings = Settings(**{**base, **settings_changes})
    store = Store(settings)
    events = [{"type": "message", "role": "user", "text": "Pythonの仮想環境不一致を調査", "timestamp": "2026-11-01T10:00:00Z"},
              {"type": "message", "role": "assistant", "text": "実行環境を比較した", "timestamp": "2026-11-01T10:01:00Z"},
              {"type": "message", "role": "tool", "text": "12 tests passed", "timestamp": "2026-11-01T10:02:00Z"}]
    content = ("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n").encode()
    contents = [{"name": "events.jsonl", "size_bytes": len(content), "sha256": digest(content)}]
    fp = content_fingerprint(contents)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("events.jsonl", content)
    archive = bio.getvalue()
    manifest = {"schema_version": "1.0", "record_type": "copilot-session-archive", "session_fingerprint": fp,
                "archive_file": f"session-{fp}.zip", "archive_sha256": digest(archive), "contents": contents,
                "prepared_at_utc": "2026-11-01T10:05:00Z"}
    custom = mode in {"custom", "custom_and_original_wiki"}
    wiki = mode in {"original_wiki", "custom_and_original_wiki", "memory_wiki"}
    spec = {"schema_version": VERSION, "project": "demo", "mode": mode, "session_fingerprint": fp,
        "artifacts": {"archive": store.add("inputs/objects/session.zip", archive),
                      "manifest": store.add("inputs/objects/session.manifest.json", json_bytes(manifest))},
        "extract_model_id": "synthetic-extractor" if custom else None,
        "grade_model_id": "synthetic-grader" if custom or mode == "hybrid" else None,
        "wiki_model_id": "synthetic-wiki" if wiki else None,
        "memory_profile": "test" if mode in {"memory_wiki", "memory_ingest", "hybrid"} else None}
    ref = store.add("inputs/ready/test.job.json", json_bytes(spec))
    request = {"schema_version": VERSION, "action": "run", "job_ref": ref, "job_id": digest(json_bytes(ref))}
    return settings, store, Ledger(), Models(), Memory(), spec, request
