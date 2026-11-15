"""既存の学習・Memory・Wikiを一つのRuntimeから明示的に呼び分ける。"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from copilot_learning.core import digest, json_bytes, read_archive, normalize_events, load_feedback, build_memory_requests
from copilot_learning.learning import generate_candidates, grade_candidates, independent_llm_grade, memory_records_as_candidates
from copilot_learning.wiki import source_snapshot, wiki_prompt, compile_wiki, export_wiki, verify_wiki, _text, _sensitive
from .contracts import VERSION, Settings, Rejected, command, job, decode, fail


class WaitingMemory(RuntimeError):
    """0件や変動中を成功にも抽出失敗にも変換しない。後続の明示retryで再開する。"""


class Pipeline:
    def __init__(self, settings: Settings, store: Any, models: Any, memory: Any,
                 *, sleep: Callable = time.sleep, clock: Callable = time.monotonic):
        self.settings, self.store, self.models, self.memory = settings, store, models, memory
        self.sleep, self.clock = sleep, clock

    def execute(self, request: dict, row: dict) -> dict:
        request = command(request, self.settings)
        spec = job(decode(self.store.read(request["job_ref"], 65536, ready=True)), self.settings)
        base = self.settings.output_prefix + request["job_id"] + "/"
        started, model_calls = self.clock(), 0
        artifacts, usages = {}, []
        binding = self.settings.memory(spec["memory_profile"], spec["project"]) if spec["memory_profile"] else None
        execution_sha = digest(json_bytes({"version": VERSION, "configured_code_sha256": self.settings.code_sha256, "job_ref": request["job_ref"],
            "settings": {"models": self.settings.model_ids, "memory": binding,
                         "sensitive_literals": self.settings.sensitive_literals,
                         "max_model_calls": self.settings.max_model_calls,
                         "max_output_tokens": self.settings.max_output_tokens,
                         "max_job_seconds": self.settings.max_job_seconds,
                         "memory_polls": self.settings.memory_polls, "poll_seconds": self.settings.poll_seconds}}))
        # 同一jobの途中でモデルallowlist/Memory/機密辞書を変えた場合は新しいjobが必要。
        self.store.put(base + "execution.json", json_bytes({"execution_sha256": execution_sha}))

        def budget():
            if self.clock() - started > self.settings.max_job_seconds:
                fail("TIME_BUDGET_EXCEEDED")

        def put(name: str, value: Any):
            budget()
            artifacts[name] = self.store.put(base + name, json_bytes(value))
            return value

        def checkpoint(name: str, inputs: Any, call: Callable):
            budget()
            signature = digest(json_bytes({"execution_sha256": execution_sha, "inputs": inputs}))
            key = base + "checkpoints/" + name + ".json"
            cached = self.store.result(key)
            if cached is not None:
                saved = decode(cached)
                if set(saved) != {"request_sha256", "value"} or saved["request_sha256"] != signature:
                    fail("CHECKPOINT_MISMATCH")
                return saved["value"]
            value = call()
            budget()
            self.store.put(key, json_bytes({"request_sha256": signature, "value": value}))
            return value

        class Provider:
            def __init__(inner, model_id: str, phase: str):
                inner.model_id, inner.phase = model_id, phase

            def generate(inner, prompt: str, *, system: str):
                nonlocal model_calls
                model_calls += 1
                if model_calls > self.settings.max_model_calls:
                    fail("MODEL_CALL_BUDGET_EXCEEDED")
                _text(prompt, 131072)
                _sensitive(prompt, list(self.settings.sensitive_literals))
                inputs = {"model_id": inner.model_id, "system": system, "prompt": prompt}
                def generate():
                    value, metadata = self.models.generate(inner.model_id, system, prompt)
                    return {"output": value, "metadata": metadata}
                result = checkpoint("model-" + inner.phase, inputs, generate)
                _text(json.dumps(result["output"], ensure_ascii=False), 131072)
                _sensitive(result["output"], list(self.settings.sensitive_literals))
                usages.append({"phase": inner.phase, **result["metadata"], "cost_usd": None})
                return result["output"]

        committed = self.store.result(base + "complete.json")
        if committed is not None:
            previous = decode(committed)
            if (previous.get("execution_sha256") != execution_sha or previous.get("input_ref") != request["job_ref"]
                    or previous.get("job_id") != request["job_id"] or previous.get("approved_for_copilot") is not False
                    or not isinstance(previous.get("artifacts"), dict) or len(previous["artifacts"]) > 512):
                fail("COMPLETION_MANIFEST_MISMATCH")
            for name, artifact in previous["artifacts"].items():
                budget()
                if not isinstance(artifact, dict) or artifact.get("key") != base + name:
                    fail("COMPLETION_ARTIFACT_MISMATCH")
                data = self.store.result(artifact["key"])
                if data is None or digest(data) != artifact.get("sha256"):
                    fail("COMPLETION_ARTIFACT_MISMATCH")
            return self.store.put(base + "complete.json", committed)

        with tempfile.TemporaryDirectory(prefix="copilot-runtime-") as temporary:
            root, fp = Path(temporary), spec["session_fingerprint"]
            for name, suffix, limit in (("archive", ".zip", 60*1024*1024), ("manifest", ".manifest.json", 2*1024*1024)):
                body = self.store.read(spec["artifacts"][name], limit)
                (root / ("session-" + fp + suffix)).write_bytes(body)
            manifest, events = read_archive(root, fp)
            session = normalize_events(manifest, events)
            if not session["messages"] or session["skipped_event_count"]:
                fail("UNSUPPORTED_SESSION_EVENTS")
            if len(session["messages"]) > 100:
                fail("SESSION_EVENT_LIMIT")
            session["source_provenance"]["s3_version_id"] = spec["artifacts"]["archive"]["version_id"]
            for message in session["messages"]:
                _text(message["text"])
                _sensitive(message["text"], list(self.settings.sensitive_literals))
            feedback = []
            if "feedback" in spec["artifacts"]:
                path = root / "feedback.json"
                path.write_bytes(self.store.read(spec["artifacts"]["feedback"], 1024*1024))
                feedback = load_feedback(path, fp)
                _text(json.dumps(feedback, ensure_ascii=False), 131072)
                _sensitive(feedback, list(self.settings.sensitive_literals))
            if len(json_bytes(session)) + len(json_bytes(feedback)) > 120000:
                fail("SESSION_CONTEXT_LIMIT")
            put("normalized_session.json", session)
            mode = spec["mode"]
            if mode in {"custom", "custom_and_original_wiki"}:
                proposals = generate_candidates(Provider(spec["extract_model_id"], "extract"), session, feedback)
                graded = grade_candidates(proposals, session, feedback)
                graded = independent_llm_grade(Provider(spec["grade_model_id"], "grade"), graded, session, feedback)
                put("candidate_grades.json", graded)

            memory_export = None
            if binding:
                self.memory.ready(binding)
                # 元履歴と同じ語り手を維持。手動評価は観測事実と区別したUSER報告として追加。
                memory_session = json.loads(json_bytes(session))
                for index, report in enumerate(feedback):
                    memory_session["messages"].append({
                        "event_index": session["source_event_count"] + index, "role": "USER",
                        "text": "人間からの評価申告（真実性は未検証）: " + json.dumps(report, ensure_ascii=False),
                        "timestamp": manifest["prepared_at_utc"]})
                requests = build_memory_requests(memory_session, binding["memory_id"], binding["actor_id"])
                if len(requests) > 100:
                    fail("MEMORY_EVENT_LIMIT")
                receipts = []
                for index, event in enumerate(requests):
                    receipts.append(checkpoint(f"memory-event-{index:04d}", event,
                                               lambda event=event: self.memory.send(event)))
                put("memory_receipts.json", {"events": receipts, "all_events_accepted": True,
                    "extraction_complete": False, "binding_sha256": digest(json_bytes(binding))})
                if mode != "memory_ingest":
                    cached_export = self.store.result(base + "memory_export.json")
                    if cached_export is not None:
                        memory_export = decode(cached_export)
                    else:
                        previous_sha = None
                        for poll in range(self.settings.memory_polls):
                            budget()
                            observed = self.memory.collect(binding)
                            records = observed["response"]["memoryRecordSummaries"]
                            present = {r["memoryStrategyId"] for r in records}
                            current_sha = digest(json_bytes(observed))
                            if records and set(binding["strategy_ids"]) <= present and current_sha == previous_sha:
                                memory_export = observed
                                break
                            previous_sha = current_sha
                            if poll + 1 < self.settings.memory_polls:
                                self.sleep(self.settings.poll_seconds)
                        if memory_export is None:
                            self.store.put(base + f"attempts/{row['attempt']}/memory_wait.json", json_bytes({
                                "status": "WAITING_MEMORY", "polls": self.settings.memory_polls,
                                "extraction_complete": False, "reason": "no_stable_all_strategy_observation"}))
                            raise WaitingMemory()
                    put("memory_export.json", memory_export)
                    put("memory_snapshot_status.json", {
                        "status": "provisional_observed_snapshot", "extraction_complete": False,
                        "complete_for_comparison": False, "reason": "stable_reads_do_not_prove_all_async_extractions_finished"})
                    # レコードのscope/strategy/引用を既存Wiki検査器でも独立検査。
                    memory_snapshot = source_snapshot("agentcore", memory_export,
                        expected_input_sha256=digest(json_bytes(memory_export)), project=spec["project"],
                        training_sha256=spec["artifacts"]["archive"]["sha256"],
                        sensitive_literals=list(self.settings.sensitive_literals))
                    if mode == "hybrid":
                        proposals = memory_records_as_candidates(memory_export["response"]["memoryRecordSummaries"], session)
                        graded = grade_candidates(proposals, session, feedback, allow_unlinked_memory=True)
                        graded = independent_llm_grade(Provider(spec["grade_model_id"], "grade"), graded, session, feedback)
                        put("hybrid_grades.json", graded)

            if mode in {"original_wiki", "custom_and_original_wiki", "memory_wiki"}:
                if mode == "memory_wiki":
                    snapshot = memory_snapshot
                else:
                    documents = {"documents": [{"source_id": f"event-{m['event_index']}",
                        "title": "Session " + m["role"], "text": m["text"],
                        "sha256": digest(m["text"].encode("utf-8"))} for m in session["messages"]]}
                    snapshot = source_snapshot("original", documents, expected_input_sha256=digest(json_bytes(documents)),
                        project=spec["project"], training_sha256=spec["artifacts"]["archive"]["sha256"],
                        sensitive_literals=list(self.settings.sensitive_literals))
                prompt = wiki_prompt(snapshot)
                proposal = Provider(spec["wiki_model_id"], "wiki").generate(
                    json.dumps(prompt["user_data"], ensure_ascii=False), system=prompt["system"])
                revision = compile_wiki(snapshot, proposal, sensitive_literals=list(self.settings.sensitive_literals),
                                        generator="imported_model_output")
                folder = root / "wiki"
                receipt = export_wiki(revision, folder)
                verify_wiki(folder, snapshot, expected_manifest_sha256=receipt["manifest_sha256"],
                            sensitive_literals=list(self.settings.sensitive_literals))
                put("wiki_source_snapshot.json", snapshot)
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        name = "wiki/" + path.relative_to(folder).as_posix()
                        artifacts[name] = self.store.put(base + name, path.read_bytes())

        report = {"schema_version": VERSION, "job_id": request["job_id"], "mode": spec["mode"],
                  "status": "pipeline_outputs_written", "input_ref": request["job_ref"],
                  "execution_sha256": execution_sha, "configured_code_sha256": self.settings.code_sha256,
                  "artifacts": artifacts, "model_usage": usages, "cost_usd": None,
                  "memory_extraction_complete": False if binding else None,
                  "approved_for_copilot": False, "quality_improvement_verified": False}
        # manifestが保存されたこととジョブ成功を分離。後続台帳更新に失敗しても結果を追跡できる。
        return self.store.put(base + "complete.json", json_bytes(report))
