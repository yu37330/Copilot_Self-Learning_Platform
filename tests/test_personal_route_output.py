"""個人用モデル経路の保存先予約と検証済みJSONの抽出を確認する。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from scripts import run_personal_model_route as route


class PersonalRouteOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repository = Path(__file__).resolve().parents[1]
        self.plan = self.root / "plan.json"
        self.request = self.root / "request.json"
        self.policy = self.root / "policy.json"
        self.preflight = self.root / "preflight"
        self.out = self.root / "result"
        self.plan.write_bytes((repository / "examples/personal_local_model_plan.synthetic.json").read_bytes())
        self.request.write_bytes((repository / "examples/personal_local_model_request.synthetic.json").read_bytes())
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["機密データの合成例"],
        }))
        self.common = ["--plan", str(self.plan), "--request", str(self.request),
                       "--policy", str(self.policy)]
        status, result, _ = self.invoke(["preflight", *self.common, "--out", str(self.preflight)])
        self.assertEqual(status, 0)
        self.scope_sha = result["scope_sha256"]

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = route.main(args)
        result = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
        return status, result, stderr.getvalue()

    def run_args(self, out=None):
        return ["run", *self.common, "--preflight", str(self.preflight),
                "--approved-scope-sha256", self.scope_sha, "--acknowledge-external-send",
                "--out", str(out if out is not None else self.out)]

    def provider(self, plan, request, policy, *, preflight, **kwargs):
        # 外部通信は行わず、保存・検証に必要な合成の応答だけを返す。
        scope = preflight["scope"]
        response = {"candidates": [], "synthetic_verified_snapshot": True}
        return {
            "schema_version": "1.0", "record_type": "untrusted-personal-model-response",
            "scope_sha256": preflight["scope_sha256"],
            "request_sha256": scope["request_sha256"],
            "model_plan_sha256": scope["model_plan_sha256"],
            "policy_sha256": scope["policy_sha256"],
            "output_sha256": digest(json_bytes(response)), "response": response,
            "external_model_invoked": True, "approved_for_copilot": False,
            "backend": scope["target"]["backend"], "role": request["role"],
        }, preflight

    def test_existing_result_never_calls_provider_or_changes_existing_data(self):
        self.out.mkdir()
        keep = self.out / "keep.txt"
        keep.write_text("existing data", encoding="utf-8")
        with patch.object(route, "run_with_approved_scope") as provider:
            status, _, _ = self.invoke(self.run_args())
        self.assertEqual(status, 2)
        provider.assert_not_called()
        self.assertEqual(keep.read_text("utf-8"), "existing data")
        self.assertEqual({p.name for p in self.out.iterdir()}, {"keep.txt"})

    def test_invalid_result_paths_fail_before_provider(self):
        git = self.root / "repository"
        (git / ".git").mkdir(parents=True)
        parent_file = self.root / "ordinary-file"
        parent_file.write_bytes(b"keep")
        paths = [git / "out", self.root / ".codex" / "out",
                 self.root / ".agents" / "out", parent_file, parent_file / "out"]
        for destination in paths:
            with self.subTest(path=destination), patch.object(route, "run_with_approved_scope") as provider:
                status, _, _ = self.invoke(self.run_args(destination))
                self.assertEqual(status, 2)
                provider.assert_not_called()
        self.assertEqual(parent_file.read_bytes(), b"keep")

    def test_unwritable_result_file_fails_before_provider_and_cleans_reservation(self):
        original_open = Path.open

        def deny_response(path, mode="r", *args, **kwargs):
            if path == self.out / route.RESPONSE and mode == "xb":
                raise PermissionError("synthetic write denied")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", deny_response), patch.object(route, "run_with_approved_scope") as provider:
            status, _, _ = self.invoke(self.run_args())
        self.assertEqual(status, 2)
        provider.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_result_is_reserved_before_provider_and_second_run_is_blocked(self):
        def invoke_once(*args, **kwargs):
            self.assertEqual({p.name for p in self.out.iterdir()}, {route.RESPONSE, route.MANIFEST})
            self.assertTrue(all(p.stat().st_size == 0 for p in self.out.iterdir()))
            status, _, _ = self.invoke(self.run_args())
            self.assertEqual(status, 2)
            return self.provider(*args, **kwargs)

        with patch.object(route, "run_with_approved_scope", side_effect=invoke_once) as provider:
            status, _, _ = self.invoke(self.run_args())
        self.assertEqual(status, 0)
        provider.assert_called_once()

    def test_provider_failure_cleans_reservation_and_allows_retry(self):
        with patch.object(route, "run_with_approved_scope", side_effect=InvalidData("private failure")) as provider:
            status, _, error = self.invoke(self.run_args())
        self.assertEqual(status, 2)
        provider.assert_called_once()
        self.assertNotIn("private failure", error)
        self.assertFalse(self.out.exists())
        with patch.object(route, "run_with_approved_scope", side_effect=self.provider):
            status, _, _ = self.invoke(self.run_args())
        self.assertEqual(status, 0)

    def test_reserved_result_still_verifies_and_exports_normally(self):
        with patch.object(route, "run_with_approved_scope", side_effect=self.provider):
            status, result, _ = self.invoke(self.run_args())
        self.assertEqual(status, 0)
        verified_args = [*self.common, "--preflight", str(self.preflight), "--out", str(self.out),
                         "--expected-manifest-sha256", result["manifest_sha256"]]
        with patch.object(route, "run_with_approved_scope") as provider:
            status, summary, _ = self.invoke(["verify", *verified_args])
            self.assertEqual(status, 0)
            self.assertEqual(summary["network_calls"], 0)
            exported = self.root / "export.json"
            status, _, _ = self.invoke(["export-json", *verified_args, "--output-json", str(exported)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(exported.read_text("utf-8")),
                         {"candidates": [], "synthetic_verified_snapshot": True})
        provider.assert_not_called()

    def test_export_uses_same_verified_object_after_report_changes(self):
        with patch.object(route, "run_with_approved_scope", side_effect=self.provider):
            status, result, _ = self.invoke(self.run_args())
        self.assertEqual(status, 0)
        original_verify = route._verify

        def verify_then_replace(*args, **kwargs):
            verified = original_verify(*args, **kwargs)
            report = self.out / route.RESPONSE
            report.chmod(0o600)
            report.write_bytes(json_bytes({"response": {"unverified_replacement": True}}))
            return verified

        exported = self.root / "export.json"
        with patch.object(route, "_verify", side_effect=verify_then_replace):
            status, _, _ = self.invoke([
                "export-json", *self.common, "--preflight", str(self.preflight),
                "--out", str(self.out), "--expected-manifest-sha256", result["manifest_sha256"],
                "--output-json", str(exported),
            ])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(exported.read_text("utf-8")),
                         {"candidates": [], "synthetic_verified_snapshot": True})


if __name__ == "__main__":
    unittest.main()
