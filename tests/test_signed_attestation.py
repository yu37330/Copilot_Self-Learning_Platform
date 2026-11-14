"""署名検証は合成鍵を使い、会社認証・AWS通信は行わない。"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from copilot_learning.approval_attestation import verify_attestation
from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.frozen_bundle import freeze_memory_bundle
from copilot_learning.privacy_scan import scan_frozen_bundle
from copilot_learning.review_safety import ExternalExecutionBlocked, require_online_release
from scripts.prepare_sessions import prepare


class SignedApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "session-1"
        self.session.mkdir()
        event = self.session / "events.jsonl"
        event.write_text(json.dumps({"type":"message", "role":"user", "text":"合成データのテスト",
                                     "timestamp":"2026-11-01T10:00:00Z"}, ensure_ascii=False)+"\n", encoding="utf-8")
        os.utime(event, (1_700_000_000, 1_700_000_000))
        self.staging = self.root / "staging"
        archive = prepare(self.session, self.staging, 30)
        self.fp = archive.stem.removeprefix("session-")
        self.bundle = self.root / "frozen"
        self.params = {"memory_id":"TestMemory-1234567890", "actor_id":"test_actor",
                       "account_id":"123456789012", "region":"ap-northeast-1"}
        self.receipt = freeze_memory_bundle(self.staging, self.fp, self.bundle, **self.params)
        self.receipt_hash = digest((self.bundle / "receipt.json").read_bytes())
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_bytes(json_bytes({"schema_version":"1.0","record_type":"offline-sensitive-literals","sensitive_literals":["禁止登録語"]}))
        scan = scan_frozen_bundle(self.bundle, self.policy_path, expected_receipt_sha256=self.receipt_hash, **self.params)
        self.assertEqual(scan["findings_total"],0)
        self.scan_path = self.root / "scan.json"
        self.scan_path.write_bytes(json_bytes(scan))
        self.signer = Ed25519PrivateKey.generate()
        public = self.signer.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        self.trust_path = self.root / "trust.json"
        self.trust = {"schema_version":"1.0", "record_type":"offline-trust-store", "policy_sha256":scan["policy_sha256"],
                      "keys":[{"key_id":"test-key", "principal_id":"test-reviewer", "public_key_base64":base64.b64encode(public).decode(),"operations":["memory-send"]}]}
        self.write_trust()
        self.att_path = self.root / "attestation.json"
        start = datetime(2026,11,15,6,0,tzinfo=timezone.utc)
        self.now = start + timedelta(hours=1)
        self.att = {"schema_version":"1.0", "record_type":"offline-scope-attestation", "key_id":"test-key",
                    "principal_id":"test-reviewer", "decision":"approved", "operation":"memory-send",
                    "receipt_sha256":self.receipt_hash,"privacy_report_sha256":digest(self.scan_path.read_bytes()),
                    "policy_sha256":scan["policy_sha256"], "scope_sha256":digest(json_bytes(self.receipt["scope"])),
                    "issued_at_utc":start.isoformat(), "expires_at_utc":(start+timedelta(days=1)).isoformat()}
        self.sign()

    def write_trust(self):
        self.trust_path.write_bytes(json_bytes(self.trust))
        self.trust_hash=digest(self.trust_path.read_bytes())

    def sign(self):
        raw = json_bytes(self.att)
        signed = {**self.att,"signature_base64":base64.b64encode(self.signer.sign(raw)).decode()}
        self.att_path.write_bytes(json_bytes(signed))

    def verify(self, **change):
        return verify_attestation(bundle=self.bundle, receipt_sha256=self.receipt_hash,
                                  privacy_report=self.scan_path, attestation_file=self.att_path,
                                  trust_store=self.trust_path, pinned_trust_sha256=self.trust_hash,
                                  now=self.now, **{**self.params,**change})

    def test_valid_signature_still_cannot_send(self):
        with patch("copilot_learning.aws_bridge.aws_command") as aws:
            result=self.verify()
            aws.assert_not_called()
        self.assertTrue(result["signature_verified"])
        self.assertFalse(result["organization_identity_verified"])
        self.assertFalse(result["external_transfer_authorized"])
        with self.assertRaises(ExternalExecutionBlocked):
            require_online_release()

    def test_tampered_attestation_field_rejected(self):
        att=json.loads(self.att_path.read_text("utf-8"))
        att["expires_at_utc"]="2026-11-15T22:00:00+00:00"
        self.att_path.write_bytes(json_bytes(att))
        with self.assertRaises(InvalidData):
            self.verify()

    def test_wrong_signing_key_rejected(self):
        self.signer = Ed25519PrivateKey.generate()
        self.sign()
        with self.assertRaisesRegex(InvalidData,"署名"):
            self.verify()

    def test_approval_scope_change_rejected(self):
        for key,value in [("actor_id","different"),("region","us-east-1"),("memory_id","OtherMemory-1234567890")]:
            with self.subTest(key=key), self.assertRaises(InvalidData):
                self.verify(**{key:value})

    def test_swap_report_rejected(self):
        changed=json.loads(self.scan_path.read_text("utf-8"))
        changed["findings_total"]=1
        self.scan_path.write_bytes(json_bytes(changed))
        with self.assertRaises(InvalidData): self.verify()

    def test_report_change_with_valid_shape_detected_by_signature(self):
        changed=json.loads(self.scan_path.read_text("utf-8"))
        changed["limitations"]="changed"
        self.scan_path.write_bytes(json_bytes(changed))
        with self.assertRaisesRegex(InvalidData,"変更"):
            self.verify()

    def test_expired_and_future_rejected(self):
        for current in [self.now+timedelta(days=2),self.now-timedelta(days=2)]:
            with self.subTest(current=current), self.assertRaises(InvalidData):
                verify_attestation(bundle=self.bundle, receipt_sha256=self.receipt_hash,
                                   privacy_report=self.scan_path, attestation_file=self.att_path,
                                   trust_store=self.trust_path, pinned_trust_sha256=self.trust_hash,
                                   now=current, **self.params)

    def test_untrusted_store_change_rejected(self):
        self.trust["keys"][0]["principal_id"]="other"
        self.trust_path.write_bytes(json_bytes(self.trust))
        with self.assertRaisesRegex(InvalidData,"SHA-256"):
            self.verify()

    def test_policy_hash_pinned_by_trust(self):
        self.trust["policy_sha256"]="a"*64
        self.write_trust()
        with self.assertRaisesRegex(InvalidData,"ポリシー"):
            self.verify()

    def test_duplicate_trust_keys_rejected(self):
        self.trust["keys"].append(dict(self.trust["keys"][0]))
        self.write_trust()
        with self.assertRaisesRegex(InvalidData,"重複"):
            self.verify()

    def test_invalid_signature_encoding_rejected(self):
        data=json.loads(self.att_path.read_text("utf-8"))
        data["signature_base64"]="!"
        self.att_path.write_bytes(json_bytes(data))
        with self.assertRaises(InvalidData):self.verify()


if __name__ == "__main__": unittest.main()
