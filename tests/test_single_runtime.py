"""単一Runtimeの入力契約・分岐・再開・HTTP・Lambdaの合成結合テスト。"""
from __future__ import annotations

import copy
from dataclasses import replace
import io
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.review_safety import ExternalExecutionBlocked
from runtime_service.contracts import MODES, Settings, VERSION, Rejected, command, decode, job, reference
from runtime_service.pipeline import Pipeline, WaitingMemory
from runtime_service.server import JobService, RuntimeHTTPServer
from runtime_service.trigger import dispatch, lambda_handler
from runtime_support import fixture


class PipelineTests(unittest.TestCase):
    def setup_mode(self, mode="custom_and_original_wiki", **settings):
        self.settings, self.store, self.ledger, self.models, self.memory, self.spec, self.request = fixture(mode, **settings)
        self.row, _ = self.ledger.claim(self.request["job_id"])
        self.pipeline = Pipeline(self.settings, self.store, self.models, self.memory, sleep=lambda _:None)

    def run_pipeline(self):
        return self.pipeline.execute(self.request, self.row)

    def update_spec(self):
        ref = self.store.add("inputs/ready/test.job.json", json_bytes(self.spec), "v-002")
        self.request.update(job_ref=ref, job_id=digest(json_bytes(ref)))
        self.row, _ = self.ledger.claim(self.request["job_id"])

    def test_all_six_modes_integrate_existing_components(self):
        for mode in sorted(MODES):
            with self.subTest(mode=mode):
                self.setup_mode(mode)
                with patch("socket.socket.connect") as network:
                    receipt = self.run_pipeline()
                    network.assert_not_called()
                report = decode(self.store.outputs[receipt["key"]])
                self.assertFalse(report["approved_for_copilot"])
                self.assertFalse(report["quality_improvement_verified"])
                self.assertIsNone(report["cost_usd"])
                if mode in {"original_wiki", "custom_and_original_wiki", "memory_wiki"}:
                    self.assertTrue(any(k.endswith("/wiki/index.md") for k in self.store.outputs))
                    body = next(v for k,v in self.store.outputs.items() if k.endswith("/wiki/python-env.md"))
                    self.assertIn(b'type: "Reference"', body)
                    self.assertIn(b'status: "draft"', body)

    def test_original_wiki_does_not_use_memory(self):
        self.setup_mode("original_wiki")
        self.run_pipeline()
        self.assertEqual(self.memory.sent, [])
        self.assertEqual(self.memory.collect_calls, 0)
        self.assertEqual(len(self.models.calls), 1)

    def test_custom_and_wiki_are_not_a_memory_chain(self):
        self.setup_mode()
        self.run_pipeline()
        self.assertEqual(len(self.models.calls), 3)
        self.assertEqual(self.memory.sent, [])
        wiki_input = json.loads(self.models.calls[-1][2])
        self.assertEqual(wiki_input["source_snapshot"]["mode"], "original")
        self.assertNotIn("candidate_grades", wiki_input)

    def test_model_generated_keep_does_not_override_missing_feedback(self):
        self.setup_mode("custom")
        self.run_pipeline()
        candidates = json.loads(next(v for k,v in self.store.outputs.items() if k.endswith("/candidate_grades.json")))
        self.assertEqual(candidates[0]["decision"], "pending")

    def test_memory_receipt_is_not_extraction_completion(self):
        self.setup_mode("memory_ingest")
        self.run_pipeline()
        receipt = decode(next(v for k,v in self.store.outputs.items() if k.endswith("/memory_receipts.json")))
        self.assertTrue(receipt["all_events_accepted"])
        self.assertFalse(receipt["extraction_complete"])
        self.assertEqual(self.memory.collect_calls, 0)
        self.assertEqual(self.models.calls, [])

    def test_memory_snapshot_is_explicitly_provisional(self):
        self.setup_mode("memory_wiki")
        self.run_pipeline()
        state = decode(next(v for k,v in self.store.outputs.items() if k.endswith("/memory_snapshot_status.json")))
        self.assertFalse(state["extraction_complete"])
        self.assertFalse(state["complete_for_comparison"])
        self.assertGreaterEqual(self.memory.collect_calls, 2)

    def test_empty_memory_waits_and_later_retry_reuses_event_receipts(self):
        self.setup_mode("memory_wiki")
        self.memory.empty = True
        with self.assertRaises(WaitingMemory): self.run_pipeline()
        self.assertEqual(len(self.memory.sent), 3)
        self.assertFalse(any(k.endswith("complete.json") for k in self.store.outputs))
        self.memory.empty = False
        self.row["attempt"] = 2
        self.run_pipeline()
        self.assertEqual(len(self.memory.sent), 3)

    def test_partial_memory_send_retry_does_not_repeat_checkpointed_events(self):
        self.setup_mode("memory_ingest")
        self.memory.failure_after = 1
        with self.assertRaises(RuntimeError): self.run_pipeline()
        self.memory.failure_after = None
        self.run_pipeline()
        self.assertEqual(len(self.memory.sent), 3)
        self.assertEqual(len({r["clientToken"] for r in self.memory.sent}), 3)

    def test_replay_does_not_regenerate_successful_models(self):
        self.setup_mode()
        first = self.run_pipeline()
        second = self.run_pipeline()
        self.assertEqual(first, second)
        self.assertEqual(len(self.models.calls), 3)

    def test_checkpoint_tampering_fails(self):
        self.setup_mode("custom")
        self.run_pipeline()
        del self.store.outputs[self.settings.output_prefix+self.request["job_id"]+"/complete.json"]
        key = next(k for k in self.store.outputs if k.endswith("model-extract.json"))
        data = decode(self.store.outputs[key]); data["request_sha256"] = "0"*64
        self.store.outputs[key] = json_bytes(data)
        with self.assertRaises(Rejected): self.run_pipeline()

    def test_committed_manifest_recovers_without_memory_or_model_calls(self):
        self.setup_mode("memory_wiki")
        first = self.run_pipeline()
        self.models.failure = True
        self.memory.ready = lambda _: self.fail("Memory must not be called after commit")
        self.assertEqual(first, self.run_pipeline())

    def test_committed_artifact_corruption_is_rejected(self):
        self.setup_mode("original_wiki")
        self.run_pipeline()
        key = next(k for k in self.store.outputs if k.endswith("/wiki/python-env.md"))
        self.store.outputs[key] = b"changed"
        with self.assertRaises(Rejected): self.run_pipeline()

    def test_wrong_object_sha_fails_before_model(self):
        self.setup_mode()
        self.spec["artifacts"]["archive"]["sha256"] = "0"*64
        self.update_spec()
        with self.assertRaises(Rejected): self.run_pipeline()
        self.assertEqual(self.models.calls, [])

    def test_feedback_for_other_session_fails_before_model(self):
        self.setup_mode()
        ref = self.store.add("inputs/objects/feedback.json", json_bytes({"session_fingerprint": "0"*64,
            "outcome": "verified_success", "reusable_lesson": "合成"}))
        self.spec["artifacts"]["feedback"] = ref
        self.update_spec()
        with self.assertRaises(InvalidData): self.run_pipeline()
        self.assertEqual(self.models.calls, [])

    def test_fabricated_wiki_citation_is_not_saved(self):
        self.setup_mode("original_wiki")
        self.models.invalid_quote = True
        with self.assertRaises(InvalidData): self.run_pipeline()
        self.assertFalse(any("/wiki/" in k for k in self.store.outputs))

    def test_model_call_budget_stops_before_extra_model(self):
        self.setup_mode(max_model_calls=2)
        with self.assertRaisesRegex(Rejected, "MODEL_CALL_BUDGET_EXCEEDED"): self.run_pipeline()
        self.assertEqual(len(self.models.calls), 2)

    def test_time_budget_is_enforced_before_operations(self):
        self.setup_mode()
        ticks = iter([0, 99999])
        self.pipeline.clock = lambda: next(ticks, 99999)
        with self.assertRaisesRegex(Rejected, "TIME_BUDGET_EXCEEDED"): self.run_pipeline()
        self.assertEqual(self.models.calls, [])

    def test_binding_changes_cannot_silently_reuse_cache(self):
        self.setup_mode("memory_wiki")
        self.run_pipeline()
        self.settings.memory_profiles["test"]["namespace"] = "/different/"
        with self.assertRaises(Rejected): self.run_pipeline()

    def test_output_budget_change_cannot_reuse_previous_execution(self):
        self.setup_mode("custom")
        self.run_pipeline()
        changed = replace(self.settings, max_output_tokens=1000)
        pipeline = Pipeline(changed, self.store, self.models, self.memory, sleep=lambda _:None)
        with self.assertRaises(Rejected): pipeline.execute(self.request, self.row)
        self.assertEqual(len(self.models.calls), 2)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.s, self.store, _, _, _, self.spec, self.request = fixture()

    def test_request_job_sha_and_unknown_fields(self):
        for change in ({"job_id":"0"*64}, {"extra": True}, {"action":"shell"}):
            with self.subTest(change=change), self.assertRaises(Rejected):
                command({**self.request, **change}, self.s)

    def test_reference_requires_version_and_allowed_scope(self):
        ref = self.spec["artifacts"]["archive"]
        for change in ({"version_id":"null"}, {"bucket":"other-bucket"}, {"key":"results/file"},
                       {"key":"inputs/objects/../ready/a"}, {"key":"inputs/objects/a\\b"}):
            with self.subTest(change=change), self.assertRaises(Rejected): reference({**ref,**change}, self.s)

    def test_models_are_allowlisted_and_unused_settings_rejected(self):
        for change in ({"extract_model_id":"not-allowed"}, {"memory_profile":"test"}):
            with self.subTest(change=change), self.assertRaises(Rejected): job({**self.spec, **change}, self.s)

    def test_memory_binding_cannot_cross_project(self):
        with self.assertRaises(Rejected): self.s.memory("test", "another-project")

    def test_retry_requires_non_boolean_revision(self):
        for n in (True, 0, -1, None):
            with self.subTest(n=n), self.assertRaises(Rejected):
                command({**self.request,"action":"retry","expected_revision":n}, self.s)

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(Rejected): decode(raw)

    def test_input_output_prefixes_must_not_overlap(self):
        with self.assertRaises(Rejected): fixture(output_prefix="inputs/")

    def test_kms_account_must_match(self):
        with self.assertRaises(Rejected): fixture(owner="999999999999")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s, self.store, self.ledger, self.models, self.mem, self.spec, self.req = fixture("original_wiki")
        self.pipeline = Pipeline(self.s,self.store,self.models,self.mem,sleep=lambda _:None)
        self.service = JobService(self.s,self.pipeline,self.ledger)
        self.addCleanup(self.service.close)

    def wait(self):
        for _ in range(300):
            row = self.ledger.get(self.req["job_id"])
            if row and row["status"] != "RUNNING": return row
            time.sleep(.01)
        self.fail("worker did not finish")

    def test_background_accept_then_persist_success_and_duplicate_noop(self):
        code, body = self.service.invoke(self.req)
        self.assertEqual(code,202);self.assertFalse(body["result_ready"])
        self.assertEqual(self.wait()["status"], "SUCCEEDED")
        count=len(self.models.calls)
        code, body=self.service.invoke(self.req)
        self.assertEqual(code,200);self.assertEqual(body["status"],"SUCCEEDED")
        self.assertEqual(count,len(self.models.calls))

    def test_worker_errors_do_not_log_raw_exceptions(self):
        self.models.failure=True
        self.service.invoke(self.req)
        row=self.wait()
        self.assertEqual(row["status"],"FAILED")
        self.assertNotIn("sensitive raw",json.dumps(row))

    def test_failed_job_requires_explicit_revision_retry(self):
        self.models.failure=True;self.service.invoke(self.req);row=self.wait()
        self.models.failure=False
        self.assertEqual(self.service.invoke(self.req)[1]["status"],"FAILED")
        self.service.invoke({**self.req,"action":"retry","expected_revision":row["revision"]})
        self.assertEqual(self.wait()["status"],"SUCCEEDED")
        self.assertEqual(self.ledger.get(self.req["job_id"])["attempt"],2)

    def test_stale_running_is_recovery_required_not_auto_restarted(self):
        row,_=self.ledger.claim(self.req["job_id"])
        self.ledger.rows[row["job_id"]]["deadline"]=0
        self.assertEqual(self.service.invoke(self.req)[1]["status"],"RECOVERY_REQUIRED")
        self.assertEqual(self.models.calls,[])

    def test_busy_health_is_responsive_during_work(self):
        started,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        class Slow:
            def execute(inner,*args): started.set();release.wait(5)
        self.service.pipeline=Slow()
        self.service.invoke(self.req)
        self.assertTrue(started.wait(1));self.assertEqual(self.service.health(),"HealthyBusy")
        self.assertEqual(self.service.invoke(self.req)[1]["status"],"RUNNING")
        release.set();self.wait()

    def test_unstarted_job_status_not_found(self):
        self.assertEqual(self.service.invoke({**self.req,"action":"status"})[1]["status"],"NOT_FOUND")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.s,self.store,self.ledger,self.models,self.mem,_,self.req=fixture("custom")
        self.service=JobService(self.s,Pipeline(self.s,self.store,self.models,self.mem,sleep=lambda _:None),self.ledger)
        self.server=RuntimeHTTPServer(("127.0.0.1",0),lambda:self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.cleanup)
        self.url=f"http://127.0.0.1:{self.server.server_address[1]}"

    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.service.close();self.thread.join(2)

    def call(self,path,data=None):
        req=urllib.request.Request(self.url+path, data=data, headers={"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req,timeout=2) as r:return r.status,decode(r.read())
        except urllib.error.HTTPError as e:return e.code,decode(e.read())

    def test_actual_http_ping_and_invocation(self):
        self.assertEqual(self.call("/ping"),(200,{"status":"Healthy"}))
        code,body=self.call("/invocations",json_bytes(self.req))
        self.assertEqual(code,202);self.assertEqual(body["status"],"ACCEPTED")

    def test_unknown_route_and_oversized_request(self):
        self.assertEqual(self.call("/unknown")[0],404)
        self.assertEqual(self.call("/invocations",b"x"*40000)[0],413)

    def test_default_production_factory_blocks_before_aws(self):
        import main
        self.server.factory=main.production_service
        with patch("boto3.Session") as sdk:
            self.assertEqual(self.call("/invocations",json_bytes(self.req)),(503,{"error":"ONLINE_RELEASE_BLOCKED"}))
            sdk.assert_not_called()


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.s,self.store,self.ledger,self.models,self.mem,self.spec,self.req=fixture("original_wiki")
        self.service=JobService(self.s,Pipeline(self.s,self.store,self.models,self.mem,sleep=lambda _:None),self.ledger)
        self.addCleanup(self.service.close)
        self.calls=[]
        outer=self
        class Runtime:
            def invoke_agent_runtime(inner,**kwargs):
                outer.calls.append(kwargs)
                code,body=outer.service.invoke(decode(kwargs["payload"]))
                return {"statusCode":code,"response":io.BytesIO(json_bytes(body))}
        self.runtime=Runtime()
        self.event={"Records":[{"eventSource":"aws:s3","awsRegion":self.s.region,"eventName":"ObjectCreated:Put",
                    "s3":{"bucket":{"name":self.s.bucket},"object":{"key":"inputs/ready/test.job.json","versionId":"v-001"}}}]}

    def dispatch(self,event=None):
        return dispatch(event or self.event,self.s,self.store,self.runtime,runtime_arn="synthetic-runtime")

    def test_s3_notification_test_message_does_not_invoke(self):
        result = self.dispatch({"Event":"s3:TestEvent","Service":"Amazon S3","Bucket":self.s.bucket})
        self.assertEqual(result["ignored"],1)
        self.assertEqual(self.calls,[])

    def test_s3_event_reaches_runtime_pipeline(self):
        value=self.dispatch()
        self.assertEqual(len(value["accepted"]),1)
        self.assertGreaterEqual(len(self.calls[0]["runtimeSessionId"]),33)
        self.service.close()
        self.assertEqual(self.ledger.get(self.req["job_id"])["status"],"SUCCEEDED")

    def test_output_or_archive_event_never_restarts(self):
        for key in ("results/one.job.json","inputs/objects/session.zip"):
            self.event["Records"][0]["s3"]["object"]["key"]=key
            self.assertEqual(self.dispatch()["ignored"],1)
        self.assertEqual(self.calls,[])

    def test_event_without_version_or_wrong_bucket_fails_before_invoke(self):
        for field,value in (("versionId","null"),("versionId",None)):
            self.event["Records"][0]["s3"]["object"][field]=value
            with self.assertRaises(Rejected):self.dispatch()
        self.assertEqual(self.calls,[])

    def test_url_encoded_s3_key_is_decoded(self):
        key="inputs/ready/合成 input.job.json"
        self.store.add(key,json_bytes(self.spec))
        from urllib.parse import quote_plus
        self.event["Records"][0]["s3"]["object"]["key"]=quote_plus(key)
        self.assertEqual(len(self.dispatch()["accepted"]),1)

    def test_production_lambda_always_blocks_without_release(self):
        with patch("boto3.Session") as sdk:
            with self.assertRaises(ExternalExecutionBlocked):lambda_handler(self.event,None)
            sdk.assert_not_called()


if __name__=="__main__":unittest.main()
