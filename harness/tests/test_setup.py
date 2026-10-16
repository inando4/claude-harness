import argparse
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch, Mock
import unittest
import test_harness as existing

h=existing.h
spec=importlib.util.spec_from_file_location('setup_test',existing.MODULE.parent/'setup_project.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.HarnessTests();self.fixture.setUp();self.root=self.fixture.root
        # The fixture's simulated job must not count as an active real setup job.
        self.fixture.s['stage']='completed';h.save(self.fixture.s,'fixture_completed')
        self.ctx=h.CoreContext()
        self.args=argparse.Namespace(snapshot=None,safe_snapshot=False,test_account=None)

    def tearDown(self):self.fixture.tearDown()

    def no_db(self):
        cfg=json.loads((existing.MODULE.parent/'templates/web-no-db.json').read_text())
        cfg['needsConfiguration']=False
        (self.root/'harness/config.json').write_text(json.dumps(cfg))

    def validate(self):
        with patch.object(h,'doctor',return_value=0), patch.object(h,'preflight_module',return_value=Mock(database=Mock(return_value={'status':'passed'}))):return setup.validate(self.ctx,self.args)

    def test_discovery_does_not_execute_scripts_or_show_their_contents(self):
        (self.root/'package.json').write_text(json.dumps({'scripts':{'test':'echo secret-value'},'dependencies':{'vite':'1'}}))
        result=setup.inspect(self.ctx)
        self.assertEqual(result['manifests'][0]['scripts'],['test'])
        self.assertNotIn('secret-value',json.dumps(result))
        self.assertFalse((h.STORE/'setup.json').exists())

    def test_no_db_setup_is_ready_but_runtime_unverified(self):
        self.no_db();result=self.validate()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['runtimeValidation'],'pending')
        self.assertIsNone(result['snapshot'])

    def test_database_setup_without_backup_remains_pending(self):
        result=self.validate()
        self.assertEqual(result['status'],'needs_snapshot')

    def test_backup_needs_authorization_and_is_reused_by_hash(self):
        backup=self.root/'snapshot.dump';backup.write_bytes(b'PGDMPsynthetic')
        self.args.snapshot=str(backup);self.args.test_account='test@example.invalid'
        with self.assertRaises(h.Blocked):self.validate()
        self.args.safe_snapshot=True
        result=self.validate();self.assertEqual(result['status'],'ready')
        self.args.snapshot=None;self.args.safe_snapshot=False
        self.assertEqual(self.validate()['snapshot']['sha256'],result['snapshot']['sha256'])
        backup.write_bytes(b'PGDMPchanged')
        with self.assertRaises(h.Blocked):self.validate()
        self.args.snapshot=str(backup);self.args.safe_snapshot=True
        renewed=self.validate()
        self.assertNotEqual(renewed['snapshot']['sha256'], result['snapshot']['sha256'])

    def test_configuring_active_job_or_job_worktree_is_rejected(self):
        self.fixture.s['stage']='implementing';h.save(self.fixture.s,'active')
        with self.assertRaises(h.Blocked):self.validate()
        (self.root/'.harness-parent.json').write_text('{}')
        with self.assertRaises(h.Blocked):setup.inspect(self.ctx)

    def test_start_derives_id_branch_and_can_launch_background(self):
        self.no_db();self.validate()
        self.fixture.authorize_spec()
        args=argparse.Namespace(spec=['specs/feature.md'],job=None,branch=None,base='HEAD',launch=True)
        with patch.object(h,'prepare') as prepare,patch.object(h,'load',return_value={'id':'job'}),patch.object(h,'launch') as launch:
            setup.start(self.ctx,args)
        passed=prepare.call_args.args[0]
        self.assertTrue(passed.job.startswith('feature-'))
        self.assertEqual(passed.branch,'feat/'+passed.job)
        self.assertIsNone(passed.snapshot)
        launch.assert_called_once_with({'id':'job'},background=True)

    def test_start_inline_binds_session_without_launching_model(self):
        import shutil
        shutil.copy2(existing.MODULE.parent/'conversation.py',self.root/'harness/conversation.py')
        self.no_db();self.validate()
        self.fixture.authorize_spec()
        sid='22222222-2222-4222-8222-222222222222'
        h.atomic(h.STORE/'conversations'/sid/'hook.json',{'sessionId':sid})
        args=argparse.Namespace(spec=['specs/feature.md'],job=None,branch=None,base='HEAD',launch=False,session_id=sid)
        with patch.object(h,'prepare') as prepare,patch.object(h,'load',return_value={'id':'job'}),patch.object(h,'brief',return_value={}),patch.object(h,'launch') as launch:
            setup.start(self.ctx,args)
        self.assertEqual(prepare.call_args.args[0].session_id,sid)
        launch.assert_not_called()

    def test_changed_manifests_require_new_setup(self):
        self.no_db();self.validate()
        self.fixture.authorize_spec()
        (self.root/'package.json').write_text('{}')
        args=argparse.Namespace(spec=['specs/feature.md'],job=None,branch=None,base='HEAD',launch=False)
        with patch.object(h,'prepare') as prepare,self.assertRaises(h.Blocked):setup.start(self.ctx,args)
        prepare.assert_not_called()

    def test_failed_doctor_does_not_save_ready_setup(self):
        self.no_db()
        with patch.object(h,'doctor',return_value=1),self.assertRaises(h.Blocked):setup.validate(self.ctx,self.args)
        self.assertFalse((h.STORE/'setup.json').exists())

    def test_preflight_failure_invalidates_previously_ready_setup(self):
        backup=self.root/'snapshot.dump';backup.write_bytes(b'PGDMPsynthetic')
        self.args.snapshot=str(backup);self.args.test_account='test';self.args.safe_snapshot=True
        self.validate()
        probe=Mock(database=Mock(side_effect=h.Blocked('connection refused')))
        with patch.object(h,'doctor',return_value=0),patch.object(h,'preflight_module',return_value=probe):
            with self.assertRaises(h.Blocked):setup.validate(self.ctx,self.args)
        self.assertEqual(json.loads((h.STORE/'setup.json').read_text())['status'],'blocked')
        probe.database.assert_called_once()

    def test_old_database_setup_cannot_start_without_real_preflight(self):
        backup=self.root/'snapshot.dump';backup.write_bytes(b'PGDMPsynthetic')
        self.args.snapshot=str(backup);self.args.test_account='test';self.args.safe_snapshot=True
        record=self.validate();record.pop('databasePreflight');h.atomic(h.STORE/'setup.json',record)
        self.fixture.authorize_spec()
        args=argparse.Namespace(spec=['specs/feature.md'],job=None,branch=None,base='HEAD',launch=False)
        with patch.object(h,'prepare') as prepare,self.assertRaises(h.Blocked):setup.start(self.ctx,args)
        prepare.assert_not_called()

    def test_record_runtime_does_not_claim_qa_or_approval(self):
        self.no_db();self.validate();setup.record_runtime(self.ctx,{'id':'real-check'})
        result=json.loads((h.STORE/'setup.json').read_text())['runtimeValidation']
        self.assertEqual(result['result'],'checks_and_preview_passed')
        self.assertEqual(result['qa'],'pending')
        self.assertEqual(result['humanApproval'],'pending')

    def test_failed_doctor_invalidates_previous_ready_setup(self):
        self.no_db();self.validate()
        with patch.object(h,'doctor',return_value=1),self.assertRaises(h.Blocked):setup.validate(self.ctx,self.args)
        self.assertEqual(json.loads((h.STORE/'setup.json').read_text())['status'],'blocked')


if __name__=='__main__':unittest.main()
