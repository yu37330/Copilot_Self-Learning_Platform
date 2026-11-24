"""Issue #55: 実HTTPを使って家庭用モデルUIの事前検査・承認・プライバシーを確認。"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from copilot_learning.core import InvalidData, json_bytes
from copilot_learning.personal_model_webui import (
    ASSETS, BODY_LIMIT, PENDING_TTL_SECONDS, SAMPLE_PROMPT, SAMPLE_SYSTEM,
    TestUI, build_plan, build_request, make_server, readiness,
)


def sample_values() -> dict:
    return {
        "backend": "codex_cli",
        "model_id": "synthetic-model-for-ui",
        "provider_family": "openai",
        "track": "retrospective", "role": "generator",
        "system": SAMPLE_SYSTEM, "prompt": SAMPLE_PROMPT,
        "reviewed": True,
    }


class UIServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Path(self.tmp.name) / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0",
            "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["秘密の社内語"],
        }))
        self.calls = 0
        self.current_time = 100.0

    def clock(self):
        return self.current_time

    def fake_executor(self, plan, request, policy, *,
                      preflight, approved_scope_sha256, acknowledged):
        self.calls += 1
        self.assertTrue(acknowledged)
        self.assertEqual(approved_scope_sha256, preflight["scope_sha256"])
        self.assertEqual(request["prompt"], SAMPLE_PROMPT)
        target = preflight["scope"]["target"]
        return ({
            "backend": target["backend"], "model_id": target["model_id"],
            "scope_sha256": approved_scope_sha256,
            "response": {"summary": "MOCK EXECUTOR RESPONSE", "approved": False},
            "metadata": {"reported_usage": {"input_tokens": 2}, "cost_usd": None},
        }, preflight)

    def test_readiness_exposes_presence_not_credentials(self):
        status = readiness(
            {"OPENAI_API_KEY": "PRIVATE_OPENAI_KEY", "CODEX_API_KEY": "",
             "GH_TOKEN": "PRIVATE_GH_TOKEN"},
            which=lambda name: "synthetic-cli" if name == "codex" else None)
        self.assertTrue(status["openai_api"]["key_configured"])
        self.assertTrue(status["copilot_cli"]["key_configured"])
        self.assertFalse(status["copilot_cli"]["installed"])
        self.assertFalse(status["authentication_verified"])
        self.assertNotIn("PRIVATE", json.dumps(status))

    def test_preflight_is_offline_and_scope_pinned(self):
        service = TestUI(self.policy, executor=self.fake_executor, clock=self.clock)
        summary = service.preflight(sample_values())
        self.assertFalse(summary["external_send_performed"])
        self.assertFalse(summary["allow_live"])
        self.assertEqual(summary["model_id"], "synthetic-model-for-ui")
        self.assertEqual(len(summary["scope_sha256"]), 64)
        self.assertNotIn(SAMPLE_PROMPT, json.dumps(summary))
        self.assertEqual(self.calls, 0)

    def test_mock_returns_synthetic_only_and_keeps_preflight(self):
        service = TestUI(self.policy, executor=self.fake_executor, clock=self.clock)
        pending = service.preflight(sample_values())
        params = {"pending_id": pending["pending_id"],
                  "scope_sha256": pending["scope_sha256"]}
        result = service.mock(params)
        self.assertFalse(result["external_model_invoked"])
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(self.calls, 0)
        self.assertEqual(service.mock(params)["status"], "synthetic_mock_only")

    def test_live_run_not_allowed_by_default(self):
        service = TestUI(self.policy, executor=self.fake_executor, clock=self.clock)
        pending = service.preflight(sample_values())
        with self.assertRaises(InvalidData):
            service.run({"pending_id": pending["pending_id"],
                         "scope_sha256": pending["scope_sha256"],
                         "confirmation": "SEND"})
        self.assertEqual(self.calls, 0)

    def test_live_requires_exact_confirmation_and_consumes_once(self):
        service = TestUI(self.policy, allow_live=True,
                         executor=self.fake_executor, clock=self.clock)
        pending = service.preflight(sample_values())
        args = {"pending_id": pending["pending_id"],
                "scope_sha256": pending["scope_sha256"]}
        for text in ("send", "", "ACCEPT", "SEND\n"):
            with self.assertRaises(InvalidData):
                service.run({**args, "confirmation": text})
        self.assertEqual(self.calls, 0)
        result = service.run({**args, "confirmation": "SEND"})
        self.assertTrue(result["external_model_invoked"])
        self.assertFalse(result["business_completion_verified"])
        self.assertFalse(result["approved_for_instructions"])
        self.assertEqual(self.calls, 1)
        with self.assertRaises(InvalidData):
            service.run({**args, "confirmation": "SEND"})
        self.assertEqual(self.calls, 1)

    def test_failed_executor_still_consumes_confirmation(self):
        def fail(*_args, **_kwargs):
            self.calls += 1
            raise InvalidData("synthetic provider failure")
        service = TestUI(self.policy, allow_live=True, executor=fail, clock=self.clock)
        p = service.preflight(sample_values())
        args = {"pending_id": p["pending_id"], "scope_sha256": p["scope_sha256"],
                "confirmation": "SEND"}
        with self.assertRaises(InvalidData):
            service.run(args)
        with self.assertRaises(InvalidData):
            service.run(args)
        self.assertEqual(self.calls, 1)

    def test_new_preflight_invalidates_old_approval(self):
        service = TestUI(self.policy, allow_live=True,
                         executor=self.fake_executor, clock=self.clock)
        old = service.preflight(sample_values())
        values = sample_values()
        values["prompt"] = "Synthetic different example: check unit tests first."
        new = service.preflight(values)
        self.assertNotEqual(old["scope_sha256"], new["scope_sha256"])
        with self.assertRaises(InvalidData):
            service.run({"pending_id": old["pending_id"],
                         "scope_sha256": old["scope_sha256"], "confirmation": "SEND"})
        self.assertEqual(self.calls, 0)

    def test_approval_expires_even_if_it_was_preflighted(self):
        service = TestUI(self.policy, allow_live=True,
                         executor=self.fake_executor, clock=self.clock)
        p = service.preflight(sample_values())
        self.current_time += PENDING_TTL_SECONDS + 1
        with self.assertRaises(InvalidData):
            service.run({"pending_id": p["pending_id"],
                         "scope_sha256": p["scope_sha256"], "confirmation": "SEND"})
        self.assertEqual(self.calls, 0)

    def test_sensitive_or_unreviewed_inputs_block_before_provider(self):
        service = TestUI(self.policy, allow_live=True,
                         executor=self.fake_executor, clock=self.clock)
        for update in ({"reviewed": False},
                       {"prompt": "please use someone@example.org"},
                       {"prompt": "秘密の社内語が含まれた文章"},
                       {"backend": "bedrock"},
                       {"model_id": "invalid with spaces"},
                       {"provider_family": "untrusted-family"}):
            item = sample_values()
            item.update(update)
            with self.subTest(update=update), self.assertRaises(InvalidData):
                service.preflight(item)
        self.assertEqual(self.calls, 0)

    def test_ui_plan_maintains_distinct_generator_and_reviewer(self):
        from copilot_learning.model_review_contract import validate_model_plan
        for track in ("knowledge", "retrospective"):
            for role in ("generator", "reviewer"):
                selected = build_plan("codex_cli", "my_model", "openai", track, role)
                valid = validate_model_plan(selected)
                self.assertEqual(valid["models"][track][role]["model_id"], "my_model")
                self.assertNotEqual(selected["tracks"][track]["generator"]["model_id"],
                                    selected["tracks"][track]["reviewer"]["model_id"])


class UIHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.policy = Path(temp.name) / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": [],
        }))
        self.calls = 0
        self.server = make_server(self.policy, port=0, allow_live=True,
                                  executor=self.executor)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.status, self.headers = self._get("/api/status")
        self.csrf = self.status["csrf_token"]

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)

    def executor(self, plan, request, policy, *,
                 preflight, approved_scope_sha256, acknowledged):
        self.calls += 1
        target = preflight["scope"]["target"]
        return {
            "backend": target["backend"],
            "model_id": target["model_id"],
            "scope_sha256": approved_scope_sha256,
            "response": {"dangerous-looking-value": "<script>alert(1)</script>"},
            "metadata": {},
        }, preflight

    def _get(self, route: str):
        with urlopen(self.base + route, timeout=3) as r:
            data = r.read()
            headers = dict(r.headers)
        if route.startswith("/api/"):
            return json.loads(data), headers
        return data.decode("utf-8"), headers

    def _post(self, route: str, data: dict, *,
              csrf: str | None = None, origin: str | None = None,
              size_override: int | None = None) -> tuple[int, dict]:
        raw = json_bytes(data)
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base if origin is None else origin,
        }
        if csrf is not None:
            headers["X-Local-UI-Token"] = csrf
        if size_override is not None:
            raw = b"x" * size_override
        req = Request(self.base + route, data=raw,
                      headers=headers, method="POST")
        try:
            with urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def _preflight(self):
        code, data = self._post("/api/preflight", sample_values(), csrf=self.csrf)
        self.assertEqual(code, 200)
        return data

    def test_server_only_listens_on_ipv4_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertTrue(self.status["allow_live"])
        self.assertEqual(len(self.csrf), 43)
        self.assertFalse(self.status["credentials_verified"])

    def test_static_ui_and_security_headers(self):
        html, headers = self._get("/")
        css, _ = self._get("/app.css")
        js, _ = self._get("/app.js")
        self.assertIn("Local Model Test Lab", html)
        self.assertIn("OpenAI API", html)
        self.assertIn("--allow-live", html)
        self.assertIn("@media(max-width:650px)", css)
        self.assertIn("/api/preflight", js)
        self.assertIn("textContent", js)
        self.assertNotIn(".innerHTML", js)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_cross_origin_and_missing_csrf_rejected(self):
        for status, _ in (
            self._post("/api/preflight", sample_values()),
            self._post("/api/preflight", sample_values(), csrf=self.csrf,
                       origin="https://malicious.invalid"),
            self._post("/api/preflight", sample_values(), csrf="fake"),
        ):
            self.assertEqual(status, 403)
        self.assertEqual(self.calls, 0)

    def test_host_header_rejects_dns_rebinding(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/status", headers={"Host": "evil.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()

    def test_http_preflight_mock_and_explicit_live_run(self):
        item = self._preflight()
        code, mocked = self._post(
            "/api/mock", {"pending_id": item["pending_id"],
                           "scope_sha256": item["scope_sha256"]}, csrf=self.csrf)
        self.assertEqual(code, 200)
        self.assertFalse(mocked["external_model_invoked"])
        self.assertEqual(self.calls, 0)
        code, outcome = self._post("/api/run", {
            "pending_id": item["pending_id"], "scope_sha256": item["scope_sha256"],
            "confirmation": "SEND",
        }, csrf=self.csrf)
        self.assertEqual(code, 200)
        self.assertEqual(self.calls, 1)
        self.assertFalse(outcome["business_completion_verified"])
        self.assertEqual(outcome["response"]["dangerous-looking-value"],
                         "<script>alert(1)</script>")
        code, _ = self._post("/api/run", {
            "pending_id": item["pending_id"], "scope_sha256": item["scope_sha256"],
            "confirmation": "SEND",
        }, csrf=self.csrf)
        self.assertEqual(code, 400)
        self.assertEqual(self.calls, 1)

    def test_max_content_length_is_enforced(self):
        code, _ = self._post("/api/preflight", sample_values(), csrf=self.csrf,
                             size_override=BODY_LIMIT + 1)
        self.assertEqual(code, 413)

    def test_syntax_error_does_not_expose_server_exception(self):
        code, item = self._post("/api/preflight", {
            "prompt": "PRIVATE", "backend": "openai_api",
        }, csrf=self.csrf)
        self.assertEqual(code, 400)
        self.assertNotIn("PRIVATE", json.dumps(item))

    def test_live_disabled_overrides_correct_approval(self):
        self.server.ui_service.allow_live = False
        item = self._preflight()
        code, _ = self._post("/api/run", {
            "pending_id": item["pending_id"], "scope_sha256": item["scope_sha256"],
            "confirmation": "SEND",
        }, csrf=self.csrf)
        self.assertEqual(code, 400)
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
