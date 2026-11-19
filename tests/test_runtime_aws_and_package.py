"""AWS呼出契約・永続化・CodeZip構造を模擬検証。実AWSへは接続しない。"""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import botocore.session
from botocore.validate import validate_parameters
from copilot_learning.core import digest, json_bytes, InvalidData
from copilot_learning.review_safety import ExternalExecutionBlocked
from runtime_service.aws_adapters import S3Store, Memory, Models, DynamoLedger, production
from runtime_service.contracts import Rejected, decode
from runtime_support import fixture
from scripts.build_runtime_zip import build, ROOT


class AwsError(Exception):
    def __init__(self, code):
        self.response={"Error":{"Code":code}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.settings,*_=fixture()
        self.client=Mock()
        self.store=S3Store(self.client,self.settings,guard=lambda:None)
        self.data=b'fixed immutable bytes'
        self.ref={"bucket":self.settings.bucket,"key":"inputs/objects/a.json","version_id":"v-001","sha256":digest(self.data)}
        self.checksum=base64.b64encode(bytes.fromhex(digest(self.data))).decode()

    def response(self,data=None,**updates):
        data=self.data if data is None else data
        return {"Body":io.BytesIO(data),"ContentLength":len(data),"VersionId":"v-001",
                "ServerSideEncryption":"aws:kms","SSEKMSKeyId":self.settings.kms_key,
                "ChecksumSHA256":base64.b64encode(bytes.fromhex(digest(data))).decode(),**updates}

    def test_read_pins_version_owner_and_checks_bytes(self):
        self.client.get_object.return_value=self.response()
        self.assertEqual(self.store.read(self.ref),self.data)
        args=self.client.get_object.call_args.kwargs
        self.assertEqual(args["VersionId"],"v-001")
        self.assertEqual(args["ExpectedBucketOwner"],self.settings.owner)
        validate_parameters(args,botocore.session.get_session().get_service_model("s3").operation_model("GetObject").input_shape)

    def test_read_rejects_other_version_and_kms(self):
        for change in ({"VersionId":"v-002"},{"SSEKMSKeyId":"wrong"},{"ServerSideEncryption":"AES256"}):
            self.client.get_object.return_value=self.response(**change)
            with self.subTest(change=change),self.assertRaises(Rejected):self.store.read(self.ref)

    def test_size_and_sha_are_verified_not_metadata(self):
        for data in (b'wrong',b'x'*30):
            self.client.get_object.return_value=self.response(data)
            with self.assertRaises(Rejected):self.store.read(self.ref,limit=25)

    def test_put_uses_service_checksum_and_conditional_write(self):
        self.client.put_object.return_value={"ChecksumSHA256":self.checksum,"VersionId":"version-1"}
        self.store.put("results/one/data.json",self.data)
        args=self.client.put_object.call_args.kwargs
        self.assertEqual(args["IfNoneMatch"],"*")
        self.assertEqual(args["ChecksumSHA256"],self.checksum)
        self.assertEqual(args["Body"],self.data)
        validate_parameters(args,botocore.session.get_session().get_service_model("s3").operation_model("PutObject").input_shape)

    def test_same_conditional_conflict_is_idempotent_only_after_content_check(self):
        self.client.put_object.side_effect=AwsError("PreconditionFailed")
        self.client.get_object.return_value=self.response()
        self.assertEqual(self.store.put("results/a.json",self.data)["sha256"],digest(self.data))
        self.client.get_object.return_value=self.response(b'other',ChecksumSHA256=self.checksum)
        with self.assertRaises(Rejected):self.store.put("results/a.json",self.data)

    def test_checksum_absent_is_not_success(self):
        self.client.put_object.return_value={"VersionId":"version-1"}
        with self.assertRaises(Rejected):self.store.put("results/a.json",self.data)

    def test_access_denied_does_not_become_missing_cache(self):
        self.client.get_object.side_effect=AwsError("AccessDenied")
        with self.assertRaises(AwsError):self.store.result("results/a.json")
        self.client.get_object.side_effect=AwsError("NoSuchKey")
        self.assertIsNone(self.store.result("results/a.json"))

    def test_default_guard_blocks_even_injected_client(self):
        live=S3Store(self.client,self.settings)
        with self.assertRaises(ExternalExecutionBlocked):live.read(self.ref)
        self.client.get_object.assert_not_called()


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.settings,*_=fixture()
        self.profile=self.settings.memory("test","demo")
        self.client,self.control=Mock(),Mock()
        self.mem=Memory(self.client,self.control,guard=lambda:None)
        self.record={"memoryRecordId":"record-1","memoryStrategyId":"Episodic-1",
                     "namespaces":[self.profile["namespace"]+"reflections/"],"content":{"text":"合成の経験"}}

    def test_memory_and_strategies_must_be_active(self):
        self.control.get_memory.return_value={"memory":{"status":"ACTIVE","strategies":[{"strategyId":"Episodic-1","status":"ACTIVE"}]}}
        self.mem.ready(self.profile)
        self.control.get_memory.return_value["memory"]["strategies"][0]["status"]="CREATING"
        with self.assertRaises(Rejected):self.mem.ready(self.profile)

    def test_create_event_uses_datetime_and_stable_token(self):
        self.client.create_event.return_value={"event":{"eventId":"event-123"}}
        request={"memoryId":self.profile["memory_id"],"actorId":"actor","sessionId":"session-1",
                 "eventTimestamp":"2026-11-01T10:00:00Z","clientToken":"a"*64,
                 "payload":[{"conversational":{"content":{"text":"合成"},"role":"USER"}}]}
        self.mem.send(request)
        params=self.client.create_event.call_args.kwargs
        self.assertIsInstance(params["eventTimestamp"],datetime)
        self.assertEqual(params["clientToken"],request["clientToken"])
        validate_parameters(params,botocore.session.get_session().get_service_model("bedrock-agentcore").operation_model("CreateEvent").input_shape)

    def test_all_pages_are_collected_with_strategy_filter(self):
        second={**self.record,"memoryRecordId":"record-2","createdAt":datetime(2026,1,1,tzinfo=timezone.utc)}
        self.client.list_memory_records.side_effect=[{"memoryRecordSummaries":[self.record],"nextToken":"page2"},
                                                     {"memoryRecordSummaries":[second]}]
        result=self.mem.collect(self.profile)
        self.assertEqual(len(result["response"]["memoryRecordSummaries"]),2)
        for call in self.client.list_memory_records.call_args_list:
            validate_parameters(call.kwargs,botocore.session.get_session().get_service_model("bedrock-agentcore").operation_model("ListMemoryRecords").input_shape)
            self.assertEqual(call.kwargs["memoryStrategyId"],"Episodic-1")
        self.assertNotIn("nextToken",result["response"])

    def test_infinite_pagination_and_duplicate_ids_rejected(self):
        self.client.list_memory_records.return_value={"memoryRecordSummaries":[],"nextToken":"same"}
        with self.assertRaises(Rejected):self.mem.collect(self.profile)
        self.client.list_memory_records.side_effect=[{"memoryRecordSummaries":[self.record],"nextToken":"p2"},
                                                     {"memoryRecordSummaries":[self.record]}]
        with self.assertRaises(Rejected):self.mem.collect(self.profile)

    def test_wrong_strategy_or_namespace_is_rejected(self):
        for field,value in (("memoryStrategyId","Other"),("namespaces",["/other/"])):
            self.client.list_memory_records.return_value={"memoryRecordSummaries":[{**self.record,field:value}]}
            with self.subTest(field=field),self.assertRaises(Rejected):self.mem.collect(self.profile)

    def test_default_guard_blocks_memory(self):
        with self.assertRaises(ExternalExecutionBlocked):Memory(self.client,self.control).collect(self.profile)
        self.client.list_memory_records.assert_not_called()


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.settings,*_=fixture(max_output_tokens=1000)
        self.client=Mock()
        self.client.converse.return_value={"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1},
                                           "output":{"message":{"content":[{"text":"{\"candidates\":[]}"}]}}}
        self.models=Models(self.client,self.settings,guard=lambda:None)

    def test_system_is_separate_and_output_cap_applies(self):
        result,meta=self.models.generate("synthetic-extractor","fixed instruction","{\"messages\":[]}")
        self.assertEqual(result,{"candidates":[]})
        args=self.client.converse.call_args.kwargs
        self.assertEqual(args["inferenceConfig"]["maxTokens"],1000)
        self.assertEqual(args["system"],[{"text":"fixed instruction"}])
        validate_parameters(args,botocore.session.get_session().get_service_model("bedrock-runtime").operation_model("Converse").input_shape)

    def test_truncation_and_wrong_model_are_rejected(self):
        self.client.converse.return_value["stopReason"]="max_tokens"
        with self.assertRaises(InvalidData):self.models.generate("synthetic-extractor","s","data")
        with self.assertRaises(Rejected):self.models.generate("unauthorized","s","data")

    def test_production_factory_does_not_create_boto_session(self):
        with patch("boto3.Session") as boto:
            with self.assertRaises(ExternalExecutionBlocked):production(self.settings)
            boto.assert_not_called()


class DynamoTests(unittest.TestCase):
    def setUp(self):
        self.settings,*_=fixture()
        self.table=Mock()
        self.ledger=DynamoLedger(self.table,self.settings,guard=lambda:None)

    def test_claim_is_atomic_and_duplicate_uses_strong_read(self):
        row,fresh=self.ledger.claim("a"*64)
        self.assertTrue(fresh)
        self.assertEqual(self.table.put_item.call_args.kwargs["ConditionExpression"],"attribute_not_exists(job_id)")
        self.table.put_item.side_effect=AwsError("ConditionalCheckFailedException")
        self.table.get_item.return_value={"Item":row}
        existing,fresh=self.ledger.claim("a"*64)
        self.assertFalse(fresh);self.assertEqual(existing,row)
        self.assertTrue(self.table.get_item.call_args.kwargs["ConsistentRead"])

    def test_retry_and_finish_require_matching_revision_and_token(self):
        self.table.update_item.return_value={"Attributes":{"status":"RUNNING","revision":3}}
        self.ledger.claim("a"*64,2)
        req=self.table.update_item.call_args.kwargs
        self.assertIn("#rev=:rev",req["ConditionExpression"])
        self.assertIn(":waiting",req["ConditionExpression"])
        self.ledger.finish({"job_id":"a"*64,"token":"one"},"SUCCEEDED")
        self.assertIn("#tok=:token",self.table.update_item.call_args.kwargs["ConditionExpression"])

    def test_retry_conflict_is_not_reclaimed(self):
        self.table.update_item.side_effect=AwsError("ConditionalCheckFailedException")
        with self.assertRaises(Rejected):self.ledger.claim("a"*64,2)


class PackageTests(unittest.TestCase):
    def test_zip_is_reproducible_self_contained_and_has_no_native_binary(self):
        with tempfile.TemporaryDirectory() as td:
            a,b=Path(td)/"a.zip",Path(td)/"b.zip"
            first,second=build(a),build(b)
            self.assertEqual(first["sha256"],second["sha256"])
            unpacked=Path(td)/"expanded"
            with zipfile.ZipFile(a) as z:
                self.assertIn("main.py",z.namelist());self.assertIn("lambda_function.py",z.namelist())
                self.assertIn("boto3/__init__.py",z.namelist())
                self.assertIn("copilot_learning/model_review_contract.py", z.namelist())
                # CodeZipを再配布する場合にも、プロジェクト本体のMIT表示が必要。
                self.assertIn("LICENSE",z.namelist())
                self.assertEqual(z.read("LICENSE"),(ROOT/"LICENSE").read_bytes())
                self.assertIn(b"MIT License",z.read("LICENSE"))
                embedded_manifest=json.loads(z.read("BUILD-MANIFEST.json"))
                self.assertEqual(embedded_manifest["files"]["LICENSE"],digest(z.read("LICENSE")))
                self.assertFalse(any(n.endswith((".so",".pyd",".pyc")) for n in z.namelist()))
                self.assertFalse(any(n.startswith(("tests/","examples/")) for n in z.namelist()))
                self.assertTrue(all((i.external_attr>>16)&0o777 == (0o755 if i.is_dir() else 0o644) for i in z.infolist()))
                z.extractall(unpacked)
            # -I -Sでsite-packagesを使用せず、ZIPに入れた依存だけで入口をimportする。
            code="import sys;sys.path.insert(0,sys.argv[1]);import main,lambda_function,boto3,yaml;assert not yaml.__with_libyaml__;print('ZIP_IMPORT_OK')"
            result=subprocess.run([sys.executable,"-I","-S","-c",code,str(unpacked)],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn("ZIP_IMPORT_OK",result.stdout)
            with self.assertRaises(InvalidData):build(a)

    def test_cloudformation_has_one_runtime_no_harness_and_disabled_trigger(self):
        import yaml
        class Loader(yaml.SafeLoader):pass
        def tag(loader,suffix,node):
            value=loader.construct_scalar(node) if isinstance(node,yaml.ScalarNode) else loader.construct_sequence(node) if isinstance(node,yaml.SequenceNode) else loader.construct_mapping(node)
            return {suffix:value}
        Loader.add_multi_constructor("!",tag)
        template=yaml.load((ROOT/"infra/single-runtime.yaml").read_text(),Loader=Loader)
        resources=template["Resources"]
        self.assertEqual(sum(x["Type"]=="AWS::BedrockAgentCore::Runtime" for x in resources.values()),1)
        self.assertFalse(any("Harness" in x["Type"] for x in resources.values()))
        self.assertEqual(template["Parameters"]["EnableS3Trigger"]["Default"],"false")
        self.assertEqual(resources["LearningRuntime"]["Properties"]["AgentRuntimeArtifact"]["CodeConfiguration"]["EntryPoint"],["main.py"])
        self.assertEqual(resources["DataBucket"]["Properties"]["VersioningConfiguration"]["Status"],"Enabled")
        self.assertEqual(resources["Jobs"]["DeletionPolicy"],"Retain")


if __name__=="__main__":unittest.main()
