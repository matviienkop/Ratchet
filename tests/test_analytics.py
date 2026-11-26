"""Analysis exports preserve provenance, mark unknown data and expire without touching acceptance evidence."""
from contextlib import closing
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_dev import analytics, history
from ai_dev.store import Card, record

NOW = 1800000000
DAY = 86400


class Analytics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"AI_DEV_HOME": str(self.root), "AI_DEV_POLICY": str(self.root / 'policy.json'),
                                     "HERMES_KANBAN_RUN_ID": "run-1", "AI_DEV_ROLE": "coder"})
        env.start()
        self.addCleanup(env.stop)
        self.card = Card('project', 't_1')
        with self.card.locked() as state:
            state['log'] = [{'at': NOW - DAY, 'event': 'review.accept', 'round': 1},
                            {'at': NOW - 11*DAY, 'event': 'old', 'reason': 'expired-private-text'}]
        self.before = self.card.file.read_bytes()

    def data(self):
        return json.loads((self.root / 'analytics/dataset.json').read_text())

    def test_expiration_dedup_and_no_reimport_of_old_source(self):
        for _ in range(2):
            result, code = analytics.collect(native=False, clock=NOW)
            self.assertEqual(code, 0)
        rows = self.data()['observations']
        self.assertEqual(len([r for r in rows if r['source']=='event']), 1)
        self.assertNotIn('expired-private-text', json.dumps(self.data()))
        analytics.collect(native=False, clock=NOW+11*DAY)
        self.assertEqual(self.data()['observations'], [])
        self.assertEqual(self.card.file.read_bytes(), self.before)
        self.assertEqual((self.root/'analytics/dataset.json').stat().st_mode & 0o777, 0o600)
        with closing(analytics.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)

    def test_request_hook_excludes_content_and_marks_unknown_cost(self):
        meta = dict(session_id='session', api_request_id='request', model='model', provider='provider',
                    started_at=NOW-3, ended_at=NOW, api_duration=3,
                    usage={'input_tokens':10,'output_tokens':4,'total_tokens':14,'api_key':'secret'},
                    request={'messages':['PRIVATE PROMPT']}, response={'assistant_message':'PRIVATE OUTPUT'})
        with patch('ai_dev.analytics.time.time', return_value=NOW):
            analytics.observe_api(self.card, 'ok', **meta)
            analytics.observe_api(self.card, 'ok', **meta)
            analytics.observe_api(self.card, 'error', session_id='s',api_request_id='failed',ended_at=NOW,
                                  error={'message':'PRIVATE EXCEPTION'},reason='timeout')
        analytics.collect(native=False,clock=NOW)
        rows=[r['data'] for r in self.data()['observations'] if r['source']=='api_request']
        self.assertEqual(len(rows),2)
        passed=next(r for r in rows if r['status']=='ok')
        self.assertEqual(passed['usage']['total_tokens'],14)
        self.assertIsNone(passed['cost_usd'])
        self.assertEqual(passed['cost_status'],'not_reported')
        for value in ('PRIVATE PROMPT','PRIVATE OUTPUT','PRIVATE EXCEPTION','secret'):
            self.assertNotIn(value,json.dumps(rows))

    def test_native_runs_relationships_and_coverage(self):
        detail={'task':{'id':'t_1','status':'done','completed_at':NOW,'title':'Task',
                        'body':'private body\nTracker ticket: OWNER/REPO#7'},
                'parents':['t_parent'],'children':['t_child'],
                'events':[{'kind':'blocked','created_at':NOW-12,'run_id':1,
                           'payload':{'reason':'password=supersecret','source_status':'review','command':'secret command'}}],
                'runs':[{'id':1,'started_at':NOW-20,'ended_at':NOW-10,'profile':'coder','outcome':'review_requested',
                         'metadata':{'session_id':'session','token':'do not export'}}]}
        with patch.object(analytics,'native_show',return_value=detail):
            analytics.collect(clock=NOW)
        rows=self.data()['observations']
        context=next(r['data'] for r in rows if r['source']=='card')
        self.assertEqual(context['coverage']['native'],'available')
        self.assertEqual(context['parents'],['t_parent'])
        self.assertEqual(context['tracker'],['Tracker ticket: OWNER/REPO#7'])
        run=next(r['data'] for r in rows if r['source']=='native_run')
        self.assertEqual(run['duration_seconds'],10)
        for private in ('private body','supersecret','secret command','do not export'):
            self.assertNotIn(private,json.dumps(rows))
        from ai_dev.store import CardError
        with patch.object(analytics,'native_show',side_effect=CardError('unavailable')):
            analytics.collect(clock=NOW+1)
        context=next(r['data'] for r in self.data()['observations'] if r['source']=='card')
        self.assertEqual(context['coverage']['native'],'unavailable')
        self.assertTrue(any(r['source']=='native_run' for r in self.data()['observations']))

    def test_retention_boundary_and_invalid_timestamps(self):
        with closing(analytics.connect()) as db,db:
            for identity,at in [('boundary',NOW-10*DAY),('expired',NOW-10*DAY-1),('bad','broken'),('future',NOW+1)]:
                analytics.put(db,self.card,'example',identity,at,{'id':identity},NOW)
            analytics.export(db,NOW)
        self.assertEqual([r['id'] for r in self.data()['observations']],['boundary'])
        self.assertEqual(analytics.timestamp('2026-11-26T10:00:00+02:00'),analytics.timestamp('2026-11-26T08:00:00Z'))

    def test_missing_tools_and_native_data_are_not_reported_as_zero_usage(self):
        with patch('ai_dev.analytics.time.time',return_value=NOW):
            analytics.label(self.card,'bugfix','high','Changes persistent state')
        analytics.collect(native=False,clock=NOW)
        rows=self.data()['observations']
        label=next(r['data'] for r in rows if r['source']=='classification')
        self.assertEqual(label['risk'],'high')
        context=next(r['data'] for r in rows if r['source']=='card')
        self.assertEqual(context['coverage']['native'],'not_requested')
        self.assertFalse(any(r['source']=='api_request' for r in rows))

    def test_tool_activity_import_omits_arbitrary_payloads(self):
        with patch('ai_dev.history.now',return_value='2027-01-15T08:00:00+00:00'):
            history.observe(self.card,'terminal',{'command':'PRIVATE COMMAND'},status='error',duration_ms=12)
        event_time=analytics.timestamp('2027-01-15T08:00:00+00:00')
        analytics.collect(native=False,clock=event_time)
        tools=[r for r in self.data()['observations'] if r['source']=='tool']
        self.assertEqual(tools[0]['data']['duration_ms'],12)
        self.assertNotIn('PRIVATE COMMAND',json.dumps(self.data()))

    def test_hook_storage_failure_does_not_change_gate_decision(self):
        path=Path(__file__).resolve().parents[1]/'hermes/plugin/ai-dev-gate/__init__.py'
        spec=importlib.util.spec_from_file_location('analytics_gate',path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ,{'HERMES_KANBAN_TASK':'t_1','HERMES_KANBAN_BOARD':'project'}), \
                patch.object(module,'_gate') as gate, patch.object(analytics,'observe_gate',side_effect=OSError('disk full')):
            gate.return_value.decide.return_value='Expected refusal'
            with self.assertLogs('analytics_gate',level='WARNING'):
                result=module.check(tool_name='kanban_complete',args={})
            self.assertEqual(result,{'action':'block','message':'Expected refusal'})

    def test_nested_evidence_is_excluded_and_history_expires(self):
        with self.card.locked() as state:
            state['rounds'] = [{'n': 1, 'started': NOW, 'rechecks': [
                {'key': 'f1', 'fixed': True, 'klass': 'pass', 'argv': ['PRIVATE COMMAND']}]}]
            state['ledger'] = {'f1': {'status': 'resolved', 'history': [
                {'at': NOW-11*DAY, 'round': 1, 'reason': 'EXPIRED HISTORY'},
                {'at': NOW, 'round': 1, 'status': 'resolved', 'evidence': {'log': 'PRIVATE LOG'}}],
                'disposition': {'at': NOW, 'outcome': 'deferred', 'ticket': 'https://example.com/issues/9',
                                'evidence': {'argv': ['PRIVATE COMMAND']}}}}
        analytics.collect(native=False, clock=NOW)
        data = json.dumps(self.data())
        for private in ('PRIVATE COMMAND', 'PRIVATE LOG', 'EXPIRED HISTORY'):
            self.assertNotIn(private, data)
        self.assertIn('https://example.com/issues/9', data)

    def test_collection_errors_are_exported_without_raw_error_text(self):
        self.card.file.write_text('invalid private content')
        result, code = analytics.collect(native=False, clock=NOW)
        self.assertEqual(code, 1)
        self.assertEqual(self.data()['collection']['status'], 'partial')
        self.assertEqual(result['errors'][0]['error'], 'JSONDecodeError')
        self.assertNotIn('invalid private content', json.dumps(self.data()))

    def test_hook_only_card_is_discovered_without_creating_card_state(self):
        other = Card('project', 't_hook')
        with patch('ai_dev.analytics.time.time', return_value=NOW):
            analytics.observe_api(other, 'ok', api_request_id='r', session_id='s')
        with patch.object(analytics, 'native_show', return_value={'task': {'status': 'in_progress'}}) as native:
            analytics.collect(clock=NOW)
        self.assertIn(unittest.mock.call('project', 't_hook'), native.call_args_list)
        self.assertFalse(other.exists)

    def test_request_retries_are_distinct_but_repeated_delivery_is_not(self):
        with patch('ai_dev.analytics.time.time', return_value=NOW):
            for retry in (1, 1, 2):
                analytics.observe_api(self.card, 'error', api_request_id='r', session_id='s',
                                      started_at=NOW-5, ended_at=NOW, retry_count=retry)
        analytics.collect(native=False, clock=NOW)
        rows = [r for r in self.data()['observations'] if r['source'] == 'api_request']
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['data']['retry_count'] for r in rows}, {1, 2})

    def test_preflight_projection_keeps_discovery_and_excludes_commands(self):
        with self.card.locked() as state:
            state['preflight_history'] = [{'id': 1, 'at': NOW, 'ready': False, 'base': 'base',
                'before_candidate_changes': True, 'checks': [{'name': 'tests', 'klass': 'environment',
                    'test_count': 0, 'argv': ['PRIVATE COMMAND']}]}]
        analytics.collect(native=False, clock=NOW)
        rows = [r for r in self.data()['observations'] if r['source'] == 'preflight']
        self.assertEqual(rows[0]['data']['checks'][0]['test_count'], 0)
        self.assertFalse(rows[0]['data']['ready'])
        self.assertNotIn('PRIVATE COMMAND', json.dumps(rows))

    def test_analysis_projection_excludes_original_chat_and_tracks_revision(self):
        with self.card.locked() as state:
            state['analyses'] = [{'revision':1,'at':NOW,'data':{
                'source':{'kind':'chat','text':'PRIVATE ORIGINAL REQUEST'},'readiness':'ready',
                'budget':{'minutes':15,'used_minutes':4},'evidence':[], 'unknowns':[], 'risks':[]}}]
            state['plans'] = [{'revision':1,'at':NOW,'data':{'analysis_revision':1,'steps':['Implement scoped fix']}}]
        analytics.collect(native=False,clock=NOW)
        rows=self.data()['observations']
        analysis=next(r['data'] for r in rows if r['source']=='analysis')
        self.assertEqual(analysis['source_kind'],'chat')
        self.assertEqual(analysis['budget']['used_minutes'],4)
        self.assertNotIn('PRIVATE ORIGINAL REQUEST',json.dumps(rows))
        self.assertEqual(next(r['data']['analysis_revision'] for r in rows if r['source']=='plan'),1)
