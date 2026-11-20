"""Issue #52: 自宅用3つのモデル実行経路を合成データでテストする。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.local_llm_router import (
    API_URL, LocalModelProvider, _cli, _model_json, _openai, make_preflight, read_private_json,
    run_with_approved_scope,
)
from copilot_learning.model_review_contract import validate_model_plan
from copilot_learning.privacy_scan import load_policy
from scripts.run_personal_model_route import main as command


def model_plan() -> dict:
    def model(backend, family, mid):
        return {"backend": backend, "provider_family": family,
                "model_id": mid, "max_input_tokens": 4096}
    return {
        "schema_version": "1.0", "record_type": "offline-dual-track-model-plan",
        "require_cross_vendor": False, "external_transfer_authorized": False,
        "tracks": {
            "knowledge": {
                "generator": model("openai_api", "openai", "synthetic-openai-generator"),
                "reviewer": model("codex_cli", "openai", "synthetic-codex-reviewer"),
            },
            "retrospective": {
                "generator": model("copilot_cli", "other", "synthetic-copilot-generator"),
                "reviewer": model("openai_api", "openai", "synthetic-openai-reviewer"),
            },
        },
    }


def request(track="knowledge", role="generator"):
    return {
        "schema_version": "1.0", "record_type": "personal-local-model-request",
        "track": track, "role": role,
        "system": "You are an advisor. Return only a JSON object.",
        "prompt": '{"task":"Provide synthetic learning advice"}',
        "source_classification": "personal_or_synthetic_only",
        "manual_content_reviewed": True,
    }


class Stream:
    def __init__(self, data):
        self.data = json_bytes(data)
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return None
    def read(self, limit):
        return self.data


class RouterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.plan = model_plan()
        self.request = request()
        self.policy = self.root / "policy.json"
        self.planpath = self.root / "plan.json"
        self.requestpath = self.root / "request.json"
        self.preflight = self.root / "scope"
        self.out = self.root / "answer"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内限定データ"],
        }))
        self._write()

    def _write(self):
        self.planpath.write_bytes(json_bytes(self.plan))
        self.requestpath.write_bytes(json_bytes(self.request))

    def _scope(self):
        _, sha = load_policy(self.policy)
        return make_preflight(self.plan, self.request, policy_sha256=sha,
                              literals=["社内限定データ"])

    def _args(self):
        return ["--plan", str(self.planpath),
                "--request", str(self.requestpath),
                "--policy", str(self.policy)]

    def _preflight(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(command(["preflight", *self._args(),
                                      "--out", str(self.preflight)]), 0)
        return json.loads(output.getvalue())["scope_sha256"]

    def _runner(self, argv, *, input, capture_output, text, encoding, cwd, env, timeout, check):
        self.last_cli = {"argv": argv, "input": input, "cwd": cwd, "env": env}
        return SimpleNamespace(returncode=0, stdout='{"candidates":[]}', stderr="PRIVATE")

    def _mock_response(self):
        return {"status": "completed", "model": "synthetic-openai-generator",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": '{"candidates":[]}'}
                ]}], "usage": {"input_tokens": 33, "output_tokens": 8, "total_tokens": 41}}

    def test_plan_includes_three_home_backends(self):
        result = validate_model_plan(self.plan)
        self.assertEqual(result["models"]["knowledge"]["generator"]["backend"], "openai_api")
        self.assertEqual(result["models"]["knowledge"]["reviewer"]["backend"], "codex_cli")
        self.assertEqual(result["models"]["retrospective"]["generator"]["backend"], "copilot_cli")

    def test_direct_provider_call_without_preflight_is_blocked(self):
        with self.assertRaises(InvalidData):
            LocalModelProvider("openai_api", "synthetic",
                               sensitive_literals=[])

    def test_changed_prompt_cannot_reuse_provider_scope(self):
        scope = self._scope()
        with patch("copilot_learning.local_llm_router.urllib.request.urlopen") as network:
            model = LocalModelProvider(
                "openai_api", "synthetic-openai-generator", sensitive_literals=[],
                env={"OPENAI_API_KEY": "SYNTH"},
                approved_scope_sha256=scope["scope_sha256"],
                approved_payload_sha256=scope["scope"]["payload_sha256"])
            with self.assertRaises(InvalidData):
                model.generate("MUTATED PROMPT", system=self.request["system"])
            network.assert_not_called()

    def test_same_generator_reviewer_model_disallowed(self):
        changed = deepcopy(self.plan)
        changed["tracks"]["knowledge"]["reviewer"]["model_id"] = "synthetic-openai-generator"
        with self.assertRaises(InvalidData):
            make_preflight(changed, self.request, policy_sha256="a"*64, literals=[])

    def test_preflight_never_contacts_models_or_exports_prompt(self):
        with patch("socket.socket.connect") as net, patch("subprocess.run") as run:
            doc = self._scope()
            self.assertEqual(doc["scope_sha256"], digest(json_bytes(doc["scope"])))
            self.assertFalse(doc["external_transfer_authorized"])
            self.assertEqual(doc["network_calls"], 0)
            self.assertNotIn("Provide synthetic learning advice", json.dumps(doc))
            net.assert_not_called()
            run.assert_not_called()

    def test_cli_preflight_is_persisted_and_no_approval_blocks_run(self):
        sha = self._preflight()
        self.assertEqual(len(sha), 64)
        self.assertTrue((self.preflight / "scope.preflight.json").exists())
        with patch("copilot_learning.local_llm_router.subprocess.run") as worker:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(command([
                    "run", *self._args(), "--preflight", str(self.preflight),
                    "--approved-scope-sha256", sha, "--out", str(self.out),
                ]), 2)
            worker.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_changed_prompt_and_model_fail_before_execution(self):
        scope = self._scope()
        changed_req = deepcopy(self.request)
        changed_req["prompt"] += " another task"
        changed_plan = deepcopy(self.plan)
        changed_plan["tracks"]["knowledge"]["generator"]["model_id"] = "another-model"
        for p, q in ((self.plan, changed_req), (changed_plan, self.request)):
            with self.assertRaises(InvalidData):
                run_with_approved_scope(
                    p, q, self.policy, preflight=scope,
                    approved_scope_sha256=scope["scope_sha256"], acknowledged=True)

    def test_policy_change_fails_before_execution(self):
        scope = self._scope()
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内限定データ", "追加の秘密"],
        }))
        with self.assertRaises(InvalidData):
            run_with_approved_scope(
                self.plan, self.request, self.policy, preflight=scope,
                approved_scope_sha256=scope["scope_sha256"], acknowledged=True)

    def test_secrets_and_non_personal_requests_fail(self):
        for secret in ("test@example.org", "090-1234-5678",
                       "sk-123456789012345678901234567890",
                       "社内限定データ", "password:SuperSecret123"):
            data = deepcopy(self.request)
            data["prompt"] = "please analyze " + secret
            with self.subTest(l=len(secret)), self.assertRaises(InvalidData) as exc:
                make_preflight(self.plan, data, policy_sha256="a"*64,
                               literals=["社内限定データ"])
            self.assertNotIn(secret, str(exc.exception))
        for wrong in ({"source_classification": "company"},
                      {"manual_content_reviewed": False}, {"extra": True}):
            data = deepcopy(self.request)
            data.update(wrong)
            with self.assertRaises(InvalidData):
                make_preflight(self.plan, data, policy_sha256="a"*64, literals=[])

    def test_openai_uses_post_responses_store_false_no_tools(self):
        calls = []
        def opener(req, *, timeout):
            calls.append((req, timeout))
            return Stream(self._mock_response())
        scope = self._scope()
        result, _ = run_with_approved_scope(
            self.plan, self.request, self.policy, preflight=scope,
            approved_scope_sha256=scope["scope_sha256"],
            acknowledged=True, env={"OPENAI_API_KEY": "SYNTHETIC"},
            opener=opener)
        self.assertEqual(result["response"], {"candidates": []})
        self.assertFalse(result["approved_for_copilot"])
        self.assertEqual(result["metadata"]["reported_usage"]["input_tokens"], 33)
        req, timeout = calls[0]
        self.assertEqual(req.full_url, API_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 120)
        body = json.loads(req.data.decode("utf-8"))
        self.assertIs(body["store"], False)
        self.assertNotIn("tools", body)
        self.assertEqual(body["model"], "synthetic-openai-generator")

    def test_openai_errors_are_redacted(self):
        def fail(req, *, timeout):
            raise RuntimeError("PRIVATE KEY in response")
        scope = self._scope()
        with self.assertRaises(InvalidData) as exc:
            run_with_approved_scope(
                self.plan, self.request, self.policy, preflight=scope,
                approved_scope_sha256=scope["scope_sha256"],
                acknowledged=True, env={"OPENAI_API_KEY": "SYNTHETIC"},
                opener=fail)
        self.assertNotIn("PRIVATE KEY", str(exc.exception))

    def test_openai_invalid_or_tool_response_is_rejected(self):
        cases = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "tool_call", "content": []}]},
        ]
        for data in cases:
            with self.assertRaises(InvalidData):
                _openai("model", "system", "prompt",
                        env={"OPENAI_API_KEY": "SYNTHETIC"},
                        timeout=3, opener=lambda *a, **k: Stream(data))

    def test_codex_cli_readonly_ephemeral_and_stdin(self):
        req = request("knowledge", "reviewer")
        policy, sha = load_policy(self.policy)
        scope = make_preflight(self.plan, req, policy_sha256=sha,
                               literals=policy["sensitive_literals"])
        result, _ = run_with_approved_scope(
            self.plan, req, self.policy,
            preflight=scope, approved_scope_sha256=scope["scope_sha256"],
            acknowledged=True, env={"PATH": "/synthetic", "COPILOT_ALLOW_ALL": "true"},
            runner=self._runner)
        argv = self.last_cli["argv"]
        self.assertEqual(argv[:2], ["codex", "exec"])
        for item in ("--ephemeral", "--ignore-user-config", "read-only",
                     "--model", "synthetic-codex-reviewer", "-"):
            self.assertIn(item, argv)
        self.assertNotIn(req["prompt"], " ".join(argv))
        self.assertIn(req["prompt"], self.last_cli["input"])
        self.assertNotIn("COPILOT_ALLOW_ALL", self.last_cli["env"])
        self.assertFalse(result["independent_reviewer_passed"])

    def test_copilot_cli_no_all_access_and_isolated_home(self):
        req = request("retrospective", "generator")
        _, sha = load_policy(self.policy)
        scope = make_preflight(self.plan, req, policy_sha256=sha,
                               literals=["社内限定データ"])
        result, _ = run_with_approved_scope(
            self.plan, req, self.policy, preflight=scope,
            approved_scope_sha256=scope["scope_sha256"], acknowledged=True,
            env={"PATH": "/synthetic", "COPILOT_ALLOW_ALL": "true",
                 "COPILOT_GITHUB_TOKEN": "SYNTHETIC-GH"}, runner=self._runner)
        argv = self.last_cli["argv"]
        self.assertEqual(argv[0], "copilot")
        for item in ("-s", "--no-ask-user", "--model",
                     "--deny-tool=shell,write,read,url,memory"):
            self.assertIn(item, argv)
        self.assertNotIn(req["prompt"], " ".join(argv))
        self.assertIn(req["prompt"], self.last_cli["input"])
        self.assertNotIn("COPILOT_ALLOW_ALL", self.last_cli["env"])
        self.assertTrue(self.last_cli["env"]["COPILOT_HOME"].endswith("copilot-home"))
        self.assertFalse(result["approved_for_copilot"])

    def test_cli_errors_do_not_expose_stdout_or_stderr(self):
        def failed(*a, **k):
            return SimpleNamespace(returncode=1, stdout="", stderr="PRIVATE TOKEN")
        with self.assertRaises(InvalidData) as exc:
            _cli("codex_cli", "model", "system", "prompt",
                 env={"PATH": "/temp"}, runner=failed, timeout=3)
        self.assertNotIn("PRIVATE", str(exc.exception))
        def timed(*a, **k):
            raise subprocess.TimeoutExpired("PRIVATE", 3, output="PRIVATE")
        with self.assertRaises(InvalidData):
            _cli("copilot_cli", "model", "system", "prompt",
                 env={"PATH": "/temp"}, runner=timed, timeout=3)

    def test_model_output_requires_one_json_object_and_scans_secrets(self):
        self.assertEqual(_model_json('{"decisions":[]}', []), {"decisions": []})
        for bad in ("[1,2]", "not-json", "", '{"foo":"test@example.org"}',
                    '{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(bad=bad), self.assertRaises(InvalidData):
                _model_json(bad, [])

    def test_cli_preflight_run_verify_mock_api_roundtrip(self):
        approved = self._preflight()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC"}, clear=True):
            with patch("copilot_learning.local_llm_router.urllib.request.urlopen",
                       side_effect=lambda *a, **k: Stream(self._mock_response())):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(command([
                        "run", *self._args(), "--preflight", str(self.preflight),
                        "--approved-scope-sha256", approved,
                        "--acknowledge-external-send", "--out", str(self.out),
                    ]), 0)
            manifest_sha = json.loads(output.getvalue())["manifest_sha256"]
        self.assertEqual(len(manifest_sha), 64)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(command([
                "verify", *self._args(), "--preflight", str(self.preflight),
                "--expected-manifest-sha256", manifest_sha, "--out", str(self.out),
            ]), 0)
        file = self.out / "model-response.preview.json"
        report = json.loads(file.read_text("utf-8"))
        self.assertEqual(report["response"], {"candidates": []})
        self.assertFalse(report["learning_effect_verified"])
        export = self.root / "raw-qa-proposal.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(command([
                "export-json", *self._args(), "--preflight", str(self.preflight),
                "--expected-manifest-sha256", manifest_sha, "--out", str(self.out),
                "--output-json", str(export),
            ]), 0)
        self.assertEqual(json.loads(export.read_text("utf-8")), {"candidates": []})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(command([
                "export-json", *self._args(), "--preflight", str(self.preflight),
                "--expected-manifest-sha256", manifest_sha, "--out", str(self.out),
                "--output-json", str(self.out / "extra.json"),
            ]), 2)
        self.assertFalse((self.out / "extra.json").exists())

    def test_modified_response_breaks_local_verify(self):
        approved = self._preflight()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC"}, clear=True):
            with patch("copilot_learning.local_llm_router.urllib.request.urlopen",
                       side_effect=lambda *a, **k: Stream(self._mock_response())):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(command([
                        "run", *self._args(), "--preflight", str(self.preflight),
                        "--approved-scope-sha256", approved,
                        "--acknowledge-external-send", "--out", str(self.out),
                    ]), 0)
        sha = json.loads(output.getvalue())["manifest_sha256"]
        f = self.out / "model-response.preview.json"
        f.chmod(0o600)
        f.write_text('{"tampered":true}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(command([
                "verify", *self._args(), "--preflight", str(self.preflight),
                "--expected-manifest-sha256", sha, "--out", str(self.out),
            ]), 2)

    def test_export_json_before_verified_response_is_blocked(self):
        self._preflight()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(command([
                "export-json", *self._args(), "--preflight", str(self.preflight),
                "--expected-manifest-sha256", "a"*64, "--out", str(self.out),
                "--output-json", str(self.root / "stolen.json"),
            ]), 2)
        self.assertFalse((self.root / "stolen.json").exists())

    def test_git_directories_are_rejected(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        file = repo / "model-request.json"
        file.write_bytes(json_bytes(self.request))
        with self.assertRaises(InvalidData):
            read_private_json(file)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(command([
                "preflight", *self._args(), "--out", str(repo / "out"),
            ]), 2)

    def test_offline_retro_draft_requires_separate_manual_review(self):
        from scripts.prepare_personal_model_request import draft_from_artifact
        source = {
            "schema_version": "1.0", "record_type": "offline-retro-review-request",
            "system": "Return safe synthetic JSON proposals", "data": {"findings": []},
            "external_transfer_authorized": False,
        }
        draft = draft_from_artifact(source, "generator")
        self.assertFalse(draft["manual_content_reviewed"])
        self.assertEqual(draft["role"], "generator")
        with self.assertRaises(InvalidData):
            make_preflight(self.plan, draft, policy_sha256="a"*64, literals=[])
        draft["source_classification"] = "personal_or_synthetic_only"
        draft["manual_content_reviewed"] = True
        scoped = make_preflight(self.plan, draft, policy_sha256="a"*64, literals=[])
        self.assertEqual(scoped["scope"]["target"]["backend"], "copilot_cli")

    def test_offline_reviewer_draft_preserves_review_hash(self):
        from scripts.prepare_personal_model_request import draft_from_artifact
        source = {
            "schema_version": "1.0",
            "record_type": "offline-retro-independent-review-request",
            "system": "Review synthetic evidence; return JSON decisions",
            "candidates": [{"proposal_id": "synthetic"}],
            "external_transfer_authorized": False,
        }
        draft = draft_from_artifact(source, "reviewer")
        payload = json.loads(draft["prompt"])
        self.assertEqual(payload["request_sha256"], digest(json_bytes(source)))
        self.assertFalse(draft["manual_content_reviewed"])
        with self.assertRaises(InvalidData):
            make_preflight(self.plan, draft, policy_sha256="a"*64, literals=[])

    def test_draft_rejects_unexpected_online_source(self):
        from scripts.prepare_personal_model_request import draft_from_artifact
        source = {
            "record_type": "offline-retro-review-request",
            "system": "Synthetic system", "data": {},
            "external_transfer_authorized": True,
        }
        with self.assertRaises(InvalidData):
            draft_from_artifact(source, "generator")

    def test_bedrock_backend_is_not_modified_or_invoked(self):
        p = deepcopy(self.plan)
        p["tracks"]["knowledge"]["generator"]["backend"] = "bedrock"
        with self.assertRaises(InvalidData):
            make_preflight(p, self.request, policy_sha256="a"*64, literals=[])

    def test_preflight_no_network_or_subprocess(self):
        with patch("socket.socket.connect") as net, patch("subprocess.run") as run:
            self._preflight()
            net.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
