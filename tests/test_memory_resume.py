"""待機再開の合成検証。実AWS/DynamoDBの動作証明とは分ける。"""
from __future__ import annotations

import copy
import io
import json
import threading
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import botocore.session
from botocore.validate import validate_parameters
from copilot_learning.core import digest, json_bytes
from copilot_learning.review_safety import ExternalExecutionBlocked
from runtime_service.contracts import Rejected, decode
from runtime_service.pipeline import Pipeline
from runtime_service.resume import (EVENT, INDEX, QUEUE, ResumeLedger, ResumePolicy, canonical_request,
                                    context_sha, dispatch_due, retry_for, wait_update)
from runtime_service.server import JobService
from runtime_service.trigger import lambda_handler
from runtime_support import fixture, Ledger


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class MemoryLedger(Ledger):
    """本体CASテストとは別に、配線・ワーカー・checkpointをメモリ内で検証する。"""
    def __init__(self, settings, policy=None):
        super().__init__()
        self.settings, self.policy = settings, policy or ResumePolicy()
        self.now = 100000
        self.clock = lambda: self.now
        self.strong_reads = 0
        self.stale_index = None

    def get(self, jid):
        self.strong_reads += 1
        return super().get(jid)

    def claim(self, jid, expected_revision=None):
        before = self.get(jid) or {}
        row, won = super().claim(jid, expected_revision)
        if won:
            with self.lock:
                self.rows[jid].update({k: v for k, v in before.items() if k.startswith('resume_')})
                row = copy.deepcopy(self.rows[jid])
        return row, won

    def bind_request(self, row, request):
        value = canonical_request(request, self.settings)
        with self.lock:
            current = self.rows[row['job_id']]
            sha = digest(json_bytes(value))
            ctx = context_sha(self.settings, self.policy)
            if current.get('resume_request_sha256', sha) != sha or current.get('resume_context_sha256', ctx) != ctx:
                raise Rejected('RESUME_BINDING_CHANGED')
            current.update(resume_request=value, resume_request_sha256=sha, resume_context_sha256=ctx)
            current.setdefault('resume_started_at', self.now)
            current.setdefault('resume_wait_count', 0)
            current.setdefault('resume_dispatch_count', 0)
            for k in ('resume_queue', 'resume_due_at', 'resume_dispatch_token'):
                current.pop(k, None)
            return copy.deepcopy(current)

    def finish(self, row, status, code='NONE'):
        super().finish(row, status, code)
        with self.lock:
            current = self.rows[row['job_id']]
            if status == 'WAITING_MEMORY':
                current.update(wait_update(current, self.now, self.policy))
            if current['status'] != 'WAITING_MEMORY':
                current.pop('resume_queue', None)
                current.pop('resume_due_at', None)
            current.pop('resume_dispatch_token', None)

    def due(self):
        with self.lock:
            rows = copy.deepcopy(self.stale_index) if self.stale_index is not None else [copy.deepcopy(r) for r in self.rows.values()
                if r.get('resume_queue') == QUEUE and r['resume_due_at'] <= self.now]
        rows.sort(key=lambda r: r.get('resume_due_at', 0))
        return {'Items': rows[:self.policy.batch_size],
                'LastEvaluatedKey': {'job_id': 'more'} if len(rows) > self.policy.batch_size else None}

    def reserve(self, row):
        with self.lock:
            current = self.rows[row['job_id']]
            if current['revision'] != row['revision'] or current['status'] != 'WAITING_MEMORY' or current.get('resume_due_at', self.now+1)>self.now:
                return None
            current['resume_dispatch_count'] += 1
            current['resume_due_at'] = self.now + self.policy.dispatch_lease_seconds
            return copy.deepcopy(current)

    def hold(self, row, code):
        with self.lock:
            current = self.rows[row['job_id']]
            if current['revision'] != row['revision'] or current['status'] != 'WAITING_MEMORY' or current['resume_due_at']>self.now:
                return False
            current.update(status='RECOVERY_REQUIRED', error_code=code, revision=current['revision']+1)
            current.pop('resume_queue', None);current.pop('resume_due_at', None)
            return True


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.settings,self.store,_,self.models,self.memory,self.spec,self.request = fixture('memory_wiki')
        self.ledger = MemoryLedger(self.settings)
        self.memory.empty = True
        self.pipeline = Pipeline(self.settings,self.store,self.models,self.memory,sleep=lambda _:None)
        self.service = JobService(self.settings,self.pipeline,self.ledger)
        self.addCleanup(self.service.close)
        self.runtime = Mock()
        self.runtime.invoke_agent_runtime.side_effect = self.invoke
        self.calls=[]

    def invoke(self, **kwargs):
        payload=decode(kwargs['payload']);self.calls.append(kwargs)
        status, result = self.service.invoke(payload)
        return {'statusCode':status, 'response':io.BytesIO(json_bytes(result))}

    def settle(self):
        until=time.monotonic()+5
        while self.service.health()!='Healthy' and time.monotonic()<until:
            time.sleep(.002)
        self.assertEqual(self.service.health(),'Healthy')
        return self.ledger.get(self.request['job_id'])

    def waiting(self):
        self.service.invoke(self.request);row=self.settle()
        self.assertEqual(row['status'],'WAITING_MEMORY')
        self.ledger.now=row['resume_due_at']
        return self.ledger.get(self.request['job_id'])

    def tick(self, **kwargs):
        return dispatch_due(EVENT,self.settings,self.ledger,self.runtime,runtime_arn='synthetic-runtime',**kwargs)

    def test_wait_then_resume_finishes_without_reingesting_events(self):
        row=self.waiting();self.assertEqual(row['resume_wait_count'],1)
        self.memory.empty=False
        with patch('socket.socket.connect') as network:
            self.assertEqual(self.tick()['dispatched'],1)
            result=self.settle();network.assert_not_called()
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(len(self.memory.sent),3)
        self.assertEqual(len(self.models.calls),1)
        self.assertNotIn('resume_queue',result)
        report=decode(self.store.result('results/'+row['job_id']+'/complete.json'))
        self.assertFalse(report['memory_extraction_complete'])
        self.assertFalse(report['approved_for_copilot'])

    def test_before_due_no_invocation(self):
        row=self.waiting();self.ledger.now-=1
        self.assertEqual(self.tick()['dispatched'],0);self.runtime.invoke_agent_runtime.assert_not_called()

    def test_repeated_empty_results_use_backoff_and_keep_checkpoints(self):
        row=self.waiting()
        self.tick();row=self.settle()
        self.assertEqual(row['resume_wait_count'],2)
        self.assertEqual(row['resume_due_at'],self.ledger.now+120)
        self.assertEqual(len(self.memory.sent),3)
        self.assertEqual(self.models.calls,[])

    def test_wait_budget_reaches_manual_hold(self):
        self.ledger.policy=ResumePolicy(max_wait_cycles=1)
        self.waiting();self.tick();row=self.settle()
        self.assertEqual(row['status'],'RECOVERY_REQUIRED')
        self.assertEqual(row['error_code'],'MEMORY_WAIT_LIMIT')
        self.assertNotIn('resume_queue',row)

    def test_deadline_holds_before_runtime_call(self):
        row=self.waiting();self.ledger.now=row['resume_started_at']+7200
        self.assertEqual(self.tick()['held'],1);self.runtime.invoke_agent_runtime.assert_not_called()
        self.assertEqual(self.ledger.get(row['job_id'])['error_code'],'MEMORY_WAIT_DEADLINE')

    def test_failed_running_success_and_stale_index_are_not_restarted(self):
        row=self.waiting()
        for status in ('FAILED','RUNNING','SUCCEEDED','RECOVERY_REQUIRED'):
            self.ledger.rows[row['job_id']]['status']=status
            self.ledger.stale_index=[row]
            with self.subTest(status=status):self.assertEqual(self.tick()['dispatched'],0)
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_setting_change_holds_without_model_or_runtime(self):
        row=self.waiting();self.settings=replace(self.settings,code_sha256='f'*64)
        self.assertEqual(self.tick()['held'],1)
        self.runtime.invoke_agent_runtime.assert_not_called()
        self.assertEqual(self.ledger.rows[row['job_id']]['error_code'],'RESUME_BINDING_CHANGED')

    def test_request_tamper_holds(self):
        row=self.waiting()
        self.ledger.rows[row['job_id']]['resume_request']['job_ref']['sha256']='f'*64
        self.assertEqual(self.tick()['held'],1)
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_duplicate_schedule_only_one_reservation(self):
        self.waiting()
        entered=threading.Event();release=threading.Event()
        def blocked(**kw):
            entered.set();release.wait(3);return self.invoke(**kw)
        self.runtime.invoke_agent_runtime.side_effect=blocked
        thread=threading.Thread(target=self.tick);thread.start()
        try:
            self.assertTrue(entered.wait(2));self.assertEqual(self.tick()['dispatched'],0)
        finally:release.set();thread.join(3)
        self.settle();self.assertEqual(self.runtime.invoke_agent_runtime.call_count,1)

    def test_lost_ack_after_acceptance_never_rolls_back(self):
        self.waiting();self.memory.empty=False
        def lost(**kw):
            self.invoke(**kw);raise TimeoutError('private response')
        self.runtime.invoke_agent_runtime.side_effect=lost
        result=self.tick();self.assertEqual(result['errors'],1)
        self.assertEqual(self.settle()['status'],'SUCCEEDED')
        self.ledger.now+=121;self.tick()
        self.assertEqual(len(self.memory.sent),3);self.assertEqual(len(self.models.calls),1)

    def test_lost_ack_before_acceptance_retries_same_revision_session(self):
        row=self.waiting()
        self.runtime.invoke_agent_runtime.side_effect=TimeoutError('private')
        first=self.tick();self.assertEqual(first['errors'],1)
        old=self.runtime.invoke_agent_runtime.call_args.kwargs
        self.assertEqual(self.ledger.get(row['job_id'])['status'],'WAITING_MEMORY')
        self.assertEqual(self.tick()['dispatched'],0)
        self.ledger.now+=121;self.runtime.invoke_agent_runtime.side_effect=self.invoke
        self.tick();self.settle()
        new=self.calls[-1]
        self.assertEqual(old['runtimeSessionId'],new['runtimeSessionId'])
        self.assertEqual(old['payload'],new['payload'])

    def test_delivery_errors_are_bounded(self):
        self.ledger.policy=ResumePolicy(max_dispatches=1)
        row=self.waiting();self.runtime.invoke_agent_runtime.side_effect=TimeoutError()
        self.tick();self.ledger.now+=121
        self.assertEqual(self.tick()['held'],1)
        self.assertEqual(self.ledger.get(row['job_id'])['error_code'],'RESUME_DISPATCH_LIMIT')

    def test_capacity_and_bad_response_do_not_erase_wait_state(self):
        row=self.waiting()
        for response in ({'statusCode':429,'response':io.BytesIO(b'{}')},
                         {'statusCode':202,'response':io.BytesIO(b'not json')},
                         {'statusCode':202,'response':io.BytesIO(b'x'*32769)},
                         {'statusCode':202,'response':io.BytesIO(json_bytes({'job_id':'wrong','status':'ACCEPTED'}))}):
            self.runtime.invoke_agent_runtime.side_effect=None
            self.runtime.invoke_agent_runtime.return_value=response
            with self.subTest(response=response['statusCode']):
                self.assertEqual(self.tick()['errors'],1)
                self.assertTrue(response['response'].closed)
                self.assertEqual(self.ledger.get(row['job_id'])['status'],'WAITING_MEMORY')
            self.ledger.now+=121

    def test_event_payload_cannot_override_input_or_destination(self):
        self.waiting()
        for event in ({}, {**EVENT,'job_ref':{}}, {**EVENT,'runtime_arn':'evil'}):
            with self.assertRaises(Rejected):dispatch_due(event,self.settings,self.ledger,self.runtime,runtime_arn='fixed')
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_lambda_time_budget_is_respected(self):
        self.waiting();self.assertTrue(self.tick(remaining_ms=lambda:34000)['time_budget_deferred'])
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_batch_limit_and_more_pending_are_visible(self):
        row=self.waiting()
        self.ledger.stale_index=[row] * (self.ledger.policy.batch_size+1)
        self.assertTrue(self.tick()['more_pending'])
        self.settle()
        self.assertEqual(self.runtime.invoke_agent_runtime.call_count,1)

    def test_near_timeout_before_dispatch_does_not_reserve(self):
        row=self.waiting()
        budgets=iter([120000,64000])
        result=self.tick(remaining_ms=lambda:next(budgets))
        self.assertTrue(result['time_budget_deferred'])
        self.assertEqual(self.ledger.get(row['job_id'])['resume_dispatch_count'],0)
        self.runtime.invoke_agent_runtime.assert_not_called()

    def test_binder_failure_does_not_start_worker_or_leak_slot(self):
        row=self.waiting()
        self.ledger.rows[row['job_id']]['resume_context_sha256']='f'*64
        retry={**self.request,'action':'retry','expected_revision':row['revision']}
        with self.assertRaises(Rejected):self.service.invoke(retry)
        self.assertEqual(self.service.health(),'Healthy')
        self.assertEqual(self.ledger.get(row['job_id'])['status'],'FAILED')
        self.assertEqual(self.ledger.get(row['job_id'])['error_code'],'RESUME_BINDING_REJECTED')
        self.assertEqual(len(self.memory.sent),3)

    def test_runtime_invoke_sdk_shape(self):
        self.waiting();self.tick();self.settle()
        args=self.calls[0]
        validate_parameters(args,botocore.session.get_session().get_service_model('bedrock-agentcore').operation_model('InvokeAgentRuntime').input_shape)

    def test_guard_still_blocks_scheduled_entrypoint_before_sdk(self):
        with patch('boto3.Session') as sdk:
            with self.assertRaises(ExternalExecutionBlocked):lambda_handler(EVENT,Mock())
            sdk.assert_not_called()

    def test_summary_does_not_expose_context(self):
        self.waiting();self.runtime.invoke_agent_runtime.side_effect=ValueError('private source content')
        result=self.tick()
        encoded=json.dumps(result)
        for term in ('private','inputs/','sha256','namespace','project'):
            self.assertNotIn(term,encoded)


class ResumeContractTests(unittest.TestCase):
    def setUp(self):
        self.settings,*others=fixture('memory_wiki');self.request=others[-1]
        self.table=Mock();self.ledger=ResumeLedger(self.table,self.settings,guard=lambda:None,clock=lambda:1000)
        self.row={'job_id':self.request['job_id'],'token':'token','revision':2,'status':'WAITING_MEMORY',
                  'resume_queue':QUEUE,'resume_due_at':900,'resume_wait_count':1,
                  'resume_started_at':800,'resume_dispatch_count':0}

    def test_invalid_policy(self):
        for kwargs in ({'batch_size':True},{'max_wait_cycles':0},{'max_dispatches':65},
                       {'initial_seconds':100,'maximum_seconds':99}):
            with self.subTest(kwargs=kwargs),self.assertRaises(Rejected):ResumePolicy(**kwargs)

    def test_backoff_caps(self):
        p=ResumePolicy();self.assertEqual([p.delay(i) for i in range(1,9)],[60,120,240,480,900,900,900,900])

    def test_decimal_fields_and_missing_context(self):
        row={**self.row,'resume_wait_count':Decimal(1),'resume_started_at':Decimal(800),'resume_dispatch_count':Decimal(0)}
        self.assertEqual(wait_update(row,1000,ResumePolicy())['resume_due_at'],1120)
        for value in (True,'1',1.5,Decimal('1.5'),-1):
            with self.subTest(value=value),self.assertRaises(Rejected):wait_update({**row,'resume_wait_count':value},1000,ResumePolicy())

    def test_bind_condition_pins_request_settings_and_token(self):
        self.table.update_item.return_value={'Attributes':self.row}
        self.ledger.bind_request(self.row,self.request)
        args=self.table.update_item.call_args.kwargs
        self.assertIn('#tok=:token',args['ConditionExpression'])
        self.assertIn('#sha=:sha',args['ConditionExpression']);self.assertIn('#ctx=:ctx',args['ConditionExpression'])
        self.assertEqual(args['ExpressionAttributeValues'][':request']['action'],'run')
        self.assertNotIn('sensitive_literals',args['ExpressionAttributeValues'][':request'])

    def test_query_gsi_only_and_strong_get(self):
        self.ledger.due();args=self.table.query.call_args.kwargs
        self.assertEqual(args['IndexName'],INDEX);self.assertNotIn('ConsistentRead',args)
        self.table.scan.assert_not_called()
        self.ledger.get(self.row['job_id']);self.assertTrue(self.table.get_item.call_args.kwargs['ConsistentRead'])

    def test_reserve_keeps_revision_and_is_conditioned_on_due(self):
        self.table.update_item.return_value={'Attributes':self.row}
        self.ledger.reserve(self.row);args=self.table.update_item.call_args.kwargs
        self.assertIn('#rev=:rev',args['ConditionExpression']);self.assertIn('resume_due_at<=:now',args['ConditionExpression'])
        self.assertNotIn('#rev=',args['UpdateExpression'])
        self.assertEqual(args['ExpressionAttributeValues'][':later'],1120)

    def test_reservation_conflict_is_not_an_error(self):
        self.table.update_item.side_effect=AwsError('ConditionalCheckFailedException')
        self.assertIsNone(self.ledger.reserve(self.row));self.assertFalse(self.ledger.hold(self.row,'MEMORY_WAIT_LIMIT'))

    def test_access_denied_is_not_a_conflict(self):
        self.table.update_item.side_effect=AwsError('AccessDeniedException')
        with self.assertRaises(AwsError):self.ledger.reserve(self.row)
        with self.assertRaises(AwsError):self.ledger.hold(self.row,'MEMORY_WAIT_LIMIT')

    def test_finish_wait_schedules_but_final_state_removes_index_keys(self):
        self.ledger.finish(self.row,'WAITING_MEMORY')
        args=self.table.update_item.call_args.kwargs
        self.assertIn('resume_queue',args['ExpressionAttributeNames'].values())
        for status in ('SUCCEEDED','FAILED'):
            self.ledger.finish(self.row,status);args=self.table.update_item.call_args.kwargs
            self.assertIn('resume_queue, resume_due_at',args['UpdateExpression'])
            self.assertIn('#tok=:token',args['ConditionExpression'])

    def test_bad_final_status_or_hold_code_blocked(self):
        with self.assertRaises(Rejected):self.ledger.finish(self.row,'APPROVED')
        with self.assertRaises(Rejected):self.ledger.hold(self.row,'secret error')
        self.table.update_item.assert_not_called()

    def test_dynamo_sdk_request_shapes(self):
        from boto3.dynamodb.types import TypeSerializer
        serializer=TypeSerializer()
        model=botocore.session.get_session().get_service_model('dynamodb')
        self.table.update_item.return_value={'Attributes':self.row}
        for call in (lambda:self.ledger.bind_request(self.row,self.request),lambda:self.ledger.reserve(self.row),
                     lambda:self.ledger.finish(self.row,'WAITING_MEMORY'),lambda:self.ledger.hold(self.row,'MEMORY_WAIT_LIMIT')):
            call();args=copy.deepcopy(self.table.update_item.call_args.kwargs)
            args['TableName']='test-jobs'
            for key in ('Key','ExpressionAttributeValues'):
                args[key]={k:serializer.serialize(v) for k,v in args[key].items()}
            validate_parameters(args,model.operation_model('UpdateItem').input_shape)

    def test_default_guard_blocks_all_added_dynamo_operations(self):
        ledger=ResumeLedger(self.table,self.settings)
        for call in (lambda:ledger.bind_request(self.row,self.request),ledger.due,
                     lambda:ledger.reserve(self.row),lambda:ledger.hold(self.row,'MEMORY_WAIT_LIMIT'),
                     lambda:ledger.finish(self.row,'WAITING_MEMORY')):
            with self.assertRaises(ExternalExecutionBlocked):call()
        self.assertEqual(self.table.mock_calls,[])

    def test_cloudformation_one_runtime_one_lambda_schedule_disabled(self):
        import yaml
        class Loader(yaml.SafeLoader):pass
        def tag(loader,suffix,node):
            value=(loader.construct_scalar(node) if isinstance(node,yaml.ScalarNode)
                   else loader.construct_sequence(node) if isinstance(node,yaml.SequenceNode)
                   else loader.construct_mapping(node))
            return {suffix:value}
        Loader.add_multi_constructor('!',tag)
        root=Path(__file__).resolve().parents[1]
        doc=yaml.load((root/'infra/single-runtime.yaml').read_text(),Loader=Loader)
        types=[r['Type'] for r in doc['Resources'].values()]
        self.assertEqual(types.count('AWS::Lambda::Function'),1)
        self.assertEqual(types.count('AWS::BedrockAgentCore::Runtime'),1)
        self.assertEqual(types.count('AWS::Scheduler::Schedule'),1)
        self.assertFalse(any('Harness' in t for t in types))
        self.assertEqual(doc['Parameters']['EnableMemoryResume']['Default'],'false')
        schedule=doc['Resources']['MemoryResumeSchedule']['Properties']
        self.assertEqual(schedule['FlexibleTimeWindow']['Mode'],'OFF')
        self.assertEqual(json.loads(schedule['Target']['Input']),EVENT)
        self.assertEqual(schedule['State']['If'][-1],'DISABLED')
        self.assertEqual(doc['Resources']['Jobs']['Properties']['GlobalSecondaryIndexes'][0]['IndexName'],INDEX)
        variables=doc['Resources']['Trigger']['Properties']['Environment']['Variables']
        self.assertIn('SENSITIVE_LITERALS_JSON',variables)


if __name__=='__main__':unittest.main()
