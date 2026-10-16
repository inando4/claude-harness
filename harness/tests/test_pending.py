import argparse
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_conversation as conversation
import test_delivery as delivery
import test_verification as verification
import test_setup as setup_tests
import test_repositories as repositories

h = conversation.h
chat = conversation.chat
setup = setup_tests.setup


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = conversation.ConversationTests(); self.fixture.setUp()
        self.ctx, self.s = self.fixture.ctx, self.fixture.s
        chat.prompt(self.ctx, self.fixture.event('/harness recover '+self.s['id'], conversation.OTHER))
        self.inventory = patch.object(h, 'run', return_value=json.dumps([
            {'sessionId':conversation.SID, 'state':'stopped'}]))
        self.inventory.start()

    def tearDown(self):
        self.inventory.stop(); self.fixture.tearDown()

    def request(self): return chat.recover_request(self.ctx, self.s, conversation.OTHER)

    def test_transfer_requires_real_prompt_invalidates_approval_and_preserves_evidence(self):
        old = self.fixture.request()
        self.s = h.load(self.s['id'])
        self.s['qa'] = {'status':'pass', 'version':'version'}; h.save(self.s, 'qa_saved')
        req = self.request()
        with self.assertRaises(h.Blocked): chat.accept_recovery(self.ctx, self.s, conversation.OTHER)
        chat.prompt(self.ctx, self.fixture.event(req['reply'], conversation.OTHER))
        current = h.load(self.s['id'])
        with patch.object(h, 'verify') as verify:
            chat.accept_recovery(self.ctx, current, conversation.OTHER)
        verify.assert_not_called()
        self.assertEqual(current['qa']['status'], 'pass')
        self.assertEqual(current['stage'], 'awaiting_human_approval')
        self.assertEqual(current['sessionId'], conversation.OTHER)
        self.assertNotIn('reviewRequest', current); self.assertNotIn('userConsent', current)
        for sid in (conversation.SID, conversation.OTHER):
            with self.assertRaises(h.Blocked): chat.prompt(self.ctx, self.fixture.event(old['reply'], sid))
        new = chat.request(self.ctx, current, conversation.OTHER)
        self.assertNotEqual(new['reply'], old['reply'])
        with self.assertRaises(h.Blocked): chat.accept_recovery(self.ctx, current, conversation.OTHER)

    def test_active_unknown_and_restarted_previous_session_block(self):
        for sessions in ([], [{'sessionId':conversation.SID, 'state':'working'}],
                         [{'sessionId':conversation.SID, 'state':'unknown'}]):
            with patch.object(h, 'run', return_value=json.dumps(sessions)), self.assertRaises(h.Blocked): self.request()
        req = self.request()
        chat.prompt(self.ctx, self.fixture.event(req['reply'], conversation.OTHER))
        with patch.object(h, 'run', return_value=json.dumps([{'sessionId':conversation.SID, 'state':'working'}])), self.assertRaises(h.Blocked):
            chat.accept_recovery(self.ctx, h.load(self.s['id']), conversation.OTHER)
        self.assertEqual(h.load(self.s['id'])['sessionId'], conversation.SID)

    def test_recorded_live_process_blocks_even_if_inventory_says_stopped(self):
        h.atomic(h.STORE/'conversations'/conversation.SID/'hook.json',
                 {'ownerPid':123, 'ownerStart':'identity'})
        with patch.object(h, 'process_identity', return_value='identity'), self.assertRaises(h.Blocked): self.request()
        with patch.object(h, 'process_identity', return_value=None): self.request()

    def test_new_session_cannot_take_two_jobs_or_confirm_wrong_token(self):
        req = self.request()
        with self.assertRaises(h.Blocked): chat.prompt(self.ctx, self.fixture.event(req['reply']))
        with self.assertRaises(h.Blocked): chat.prompt(self.ctx, self.fixture.event('RECUPERAR '+self.s['id']+' 00000000', conversation.OTHER))
        another = dict(self.s, id='another', conversation={'sessionId':conversation.OTHER})
        h.save(another, 'another_job')
        with self.assertRaises(h.Blocked): self.request()

    def test_changed_version_or_unhealthy_preview_cannot_be_approved_after_recovery(self):
        req = self.request(); chat.prompt(self.ctx, self.fixture.event(req['reply'], conversation.OTHER))
        current = h.load(self.s['id']); chat.accept_recovery(self.ctx, current, conversation.OTHER)
        with patch.object(h, 'version', return_value='changed'), self.assertRaises(h.Blocked):
            chat.request(self.ctx, current, conversation.OTHER)
        with patch.object(h, 'preview_valid', side_effect=h.Blocked('down')), self.assertRaises(h.Blocked):
            chat.request(self.ctx, current, conversation.OTHER)
        self.assertNotIn('approval', current)

    def test_review_diagnoses_session_mismatch_before_offering_token(self):
        with self.assertRaisesRegex(h.Blocked, '/harness recover'):
            chat.request(self.ctx, self.s, conversation.OTHER)
        self.assertNotIn('reviewRequest', self.s)


class E2EVigencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = verification.ReadinessTests(); self.fixture.setUp()
        self.root, self.ctx = self.fixture.root, h.CoreContext()
        self.record = self.fixture.fixture.validate()
        self.fixture.fixture.fixture.authorize_spec()
        self.args = argparse.Namespace(spec=['specs/feature.md'], job=None, branch=None, base='HEAD', launch=False)

    def tearDown(self): self.fixture.tearDown()

    def start(self):
        with patch.object(h, 'prepare') as prepare, patch('builtins.print'):
            setup.start(self.ctx, self.args)
        return prepare.call_args.args[0]

    def test_added_modified_removed_tests_with_another_job_do_not_rewrite_setup(self):
        other = self.fixture.fixture.fixture.s; other['stage']='implementing'; h.save(other, 'active')
        state = (h.jobdir(other['id'])/'state.json').read_bytes()
        setup_path = h.STORE/'setup.json'; before = setup_path.read_bytes()
        test = self.root/'e2e/second.spec.ts'; test.write_text('test("another", () => {})')
        first = self.start()
        self.assertEqual(first.test_preflight['infrastructure'], setup.infrastructure(self.record['testPreflight']))
        test.write_text('test("changed", () => {})'); self.start()
        test.unlink(); self.start()
        self.assertEqual(before, setup_path.read_bytes())
        self.assertEqual(state, (h.jobdir(other['id'])/'state.json').read_bytes())

    def test_runner_config_changes_still_require_setup(self):
        (self.root/'playwright.config.ts').write_text("export default { reporter:'junit', workers:2 }")
        with self.assertRaisesRegex(h.Blocked, 'runner/configuración'): self.start()

    def test_external_relative_helper_checked_and_config_imports_are_infrastructure(self):
        shared = self.root/'shared'; shared.mkdir()
        helper = shared/'helper.ts'; helper.write_text('export const db = process.env.PGDATABASE;')
        self.fixture.helper.write_text("export {db} from '../shared/helper';")
        self.start()
        helper.write_text("export const db = process.env.PGDATABASE || 'main';")
        with self.assertRaisesRegex(h.Blocked, 'Destino fijo'): self.start()
        helper.write_text('export const workers = 2;')
        (self.root/'playwright.config.ts').write_text("import {workers} from './shared/helper'; export default {reporter:'junit',workers}")
        self.record = self.fixture.fixture.validate()
        helper.write_text('export const workers = 3;')
        with self.assertRaisesRegex(h.Blocked, 'runner/configuración'): self.start()

    def test_missing_or_outside_import_and_new_unsafe_helper_block(self):
        for source, error in [("import './missing';", 'inexistente'),
                              ("import '../../outside';", 'fuera del repo'),
                              ("import '/tmp/outside';", 'fuera del repo'),
                              ("import '@/shared/db';", 'Alias local'),
                              ("const db = process.env.PGDATABASE ?? 'main';", 'Destino fijo')]:
            self.fixture.helper.write_text(source)
            with self.assertRaisesRegex(h.Blocked, error): self.start()

    def test_prepared_tree_has_own_receipt_and_racing_inputs_block(self):
        state = self.fixture.fixture.fixture.s
        state['base'] = h.run(['git','rev-parse','HEAD'])
        state['configVersion'] = hashlib.sha256(json.dumps(h.config(),sort_keys=True).encode()).hexdigest()
        expected = self.start().test_preflight
        setup.capture_job_preflight(self.ctx, state, expected)
        self.assertEqual(state['testPreflight']['readiness'], setup.test_readiness(self.ctx))
        current = setup.test_readiness(self.ctx)
        with patch.object(setup, 'test_readiness', side_effect=[current, dict(current, changed=True)]), self.assertRaisesRegex(h.Blocked, 'cambiaron'):
            setup.capture_job_preflight(self.ctx, state, expected)

    def test_effective_worktree_is_checked_instead_of_controller_checkout(self):
        tree = self.root/'isolated'; tree.mkdir()
        for name in ['e2e', 'package.json', 'playwright.config.ts']:
            src = self.root/name
            if src.is_dir(): shutil.copytree(src, tree/name)
            else: shutil.copy2(src, tree/name)
        (tree/'e2e/helper.ts').write_text("const db = process.env.PGDATABASE || 'unsafe';")
        state = dict(self.fixture.fixture.fixture.s, worktree=str(tree))
        with self.assertRaisesRegex(h.Blocked, 'Destino fijo'): setup.test_readiness(self.ctx, state)
        self.assertEqual(setup.test_readiness(self.ctx)['status'], 'static_passed')

    def test_static_upgrade_preserves_database_and_runtime_evidence(self):
        self.record['testPreflight'].pop('format')
        h.atomic(h.STORE/'setup.json', self.record)
        with self.assertRaisesRegex(h.Blocked, 'refresh-tests'): self.start()
        with patch.object(h, 'create_clone') as clone, patch.object(h, 'verify') as verify:
            result = setup.refresh_tests(self.ctx)
        clone.assert_not_called(); verify.assert_not_called()
        for key in ('databasePreflight','runtimeValidation','snapshot'):
            self.assertEqual(self.record.get(key), result.get(key))
        self.start()


class QAContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = delivery.ReportAndMetricsTests(); self.fixture.setUp()
        self.s = self.fixture.s; self.s.update(qaVersion='one', cycles=0)
        self.module = conversation.module('qa_context')

    def tearDown(self): self.fixture.tearDown()

    def test_context_detects_added_deleted_changed_files_without_claiming_live_evidence(self):
        self.module.capture(h.CoreContext(), self.s)
        file = self.fixture.root/'src/new.py'; file.write_text('hello')
        self.s['qaVersion']='two'; self.s['cycles']=1
        self.module.capture(h.CoreContext(), self.s)
        context = self.s['qaContext']
        self.assertEqual(context['mode'], 'delta')
        self.assertEqual(context['previousVersion'], 'one')
        self.assertTrue(context['liveEvidenceRequired'])
        self.assertIn('src/new.py', context['changedFiles']['main'])
        file.unlink(); self.s['qaVersion']='three'; self.module.capture(h.CoreContext(), self.s)
        self.assertIn('src/new.py', self.s['qaContext']['changedFiles']['main'])

    def test_metrics_does_not_turn_overnight_wait_into_active_execution(self):
        directory = h.jobdir(self.s['id']); folder = directory/'attempts/00001'; folder.mkdir(parents=True)
        (folder/'steps.jsonl').write_text(json.dumps({'id':'backend','status':'running','at':0})+'\n')
        (folder/'commands.jsonl').write_text(json.dumps({'durationSeconds':60})+'\n{"partial":')
        (directory/'events.jsonl').write_text('\n'.join(json.dumps(v) for v in [
            {'at':0,'stage':'qa'},{'at':36000,'stage':'awaiting_human_approval'},{'at':36120,'stage':'completed'}]))
        self.s['stage']='completed'
        result = self.fixture.metrics.summarize(h.CoreContext(), self.s)
        self.assertEqual(result['elapsedWallSeconds'],36120)
        self.assertEqual(result['humanReviewWallSeconds'],120)
        self.assertEqual(result['recordedCommandSeconds'],60)
        self.assertIsNone(result['activeAgentSeconds'])
        self.assertEqual(result['attempts'][0]['unfinished'],['backend'])


class PairedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = repositories.RepositoryTests()
        self.fixture.setUp()

    def tearDown(self): self.fixture.tearDown()

    def test_receipt_is_bound_to_actual_frontend_and_backend_bases(self):
        front = self.fixture.front
        (front/'package.json').write_text(json.dumps({'devDependencies':{'@playwright/test':'1'}}))
        (front/'playwright.config.ts').write_text("export default {reporter:'junit'}")
        (front/'e2e').mkdir(); (front/'e2e/a.spec.ts').write_text("test('one', () => {})")
        self.fixture.git(front,'add','.'); self.fixture.git(front,'commit','-qm','E2E fixture')
        cfg = h.config(); cfg['e2e'] = {'repo':'frontend','runner':'playwright-test',
            'testDir':'e2e','report':'test-results/e2e.xml','command':['npx','playwright','test']}
        cfg['testReports']['frontend']['glob'] = '.harness/reports/unit.xml'
        self.fixture.config(cfg)
        plan = h.repositories_module().plan(h.CoreContext(), 'feat/static-preflight', 'HEAD')
        trees = {name:str(h.STORE/'worktrees/paired'/name) for name in plan}
        state = dict(self.fixture.s, repositories=plan, worktrees=trees, worktree=trees['backend'],
                     configVersion=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest())
        h.repositories_module().create(h.CoreContext(), state)
        expected = {'manifest':setup.signature(h.CoreContext()),
                    'infrastructure':setup.infrastructure(setup.test_readiness(h.CoreContext()))}
        setup.capture_job_preflight(h.CoreContext(), state, expected)
        receipt = state['testPreflight']
        self.assertEqual(receipt['bases'],{k:v['base'] for k,v in plan.items()})
        (front/'e2e/a.spec.ts').write_text("const db = process.env.PGDATABASE || 'principal';")
        # Controller dirt is not confused with the isolated tree.
        setup.capture_job_preflight(h.CoreContext(), state, expected)
        self.assertEqual(receipt['readiness'],state['testPreflight']['readiness'])
        (Path(trees['frontend'])/'e2e/a.spec.ts').write_text("const db = process.env.PGDATABASE || 'principal';")
        with self.assertRaisesRegex(h.Blocked, 'Destino fijo'):
            setup.capture_job_preflight(h.CoreContext(), state, expected)
