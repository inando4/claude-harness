import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch, Mock
import test_publication as fixture
h=fixture.h


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.PublicationTests();self.fx.setUp()
        self.s=self.fx.s;self.ctx=h.CoreContext();self.mod=h.runtime_module('synchronization')
        self.s['stage']='implementing'
        self.s.pop('approval');self.s.pop('qa')
        self.s['publication']['phase']='prepared';self.s['publication']['handoff']=False
        for item in self.s['publication']['repos'].values():
            for field in self.mod.FIELDS:item.pop(field,None)
        self.mod.initialize(self.ctx,self.s)

    def tearDown(self):self.fx.tearDown()

    def advance(self,name,path,content):
        root=self.fx.root if name=='back' else self.fx.front
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
        self.fx.git(root,'add','--',path);self.fx.git(root,'commit','-qm','Other feature')
        self.fx.git(root,'push','-q','origin','main')
        return self.fx.git(root,'rev-parse','HEAD')

    def test_integrates_two_repos_preserving_feature_overlays_and_original_sources(self):
        back=self.advance('back','src/main/remote.txt','remote backend')
        front=self.advance('front','remote.txt','remote frontend')
        cycles=self.s['cycles'];result=self.mod.sync(self.ctx,self.s)
        self.assertEqual(result['status'],'integrated');self.assertTrue(result['productChanged'])
        self.assertEqual(self.s['cycles'],cycles);self.assertEqual(self.s['stage'],'implementing')
        self.assertEqual((self.fx.back/'src/main/app.txt').read_text(),'approved backend')
        self.assertEqual((self.fx.web/'app.txt').read_text(),'approved frontend')
        self.assertEqual((self.fx.back/'src/main/remote.txt').read_text(),'remote backend')
        self.assertEqual((self.fx.web/'remote.txt').read_text(),'remote frontend')
        self.assertIn('.harness-parent.json',(self.fx.back/'.gitignore').read_text())
        self.assertEqual(self.fx.git(self.fx.root,'rev-parse','HEAD'),back)
        self.assertEqual(self.fx.git(self.fx.front,'rev-parse','HEAD'),front)
        self.assertFalse(any((h.jobdir(self.s['id'])/'integration/1').glob('*/.git')))
        for name in ('back','front'):
            self.fx.pub.build_index(self.ctx,self.s,name)
            tree=self.s['publication']['repos'][name]['worktree']
            history=self.fx.git(Path(tree),'log','--format=%s',self.s['publication']['repos'][name]['base']+'..HEAD')
            self.assertIn('local harness integration checkpoint',history)
        self.assertEqual(self.mod.sync(self.ctx,self.s)['status'],'current')

    def test_upstream_rename_removes_the_old_path(self):
        self.fx.git(self.fx.root,'mv','--','docs/readme.md','docs/guide.md');self.fx.git(self.fx.root,'commit','-qm','Rename guide')
        self.fx.git(self.fx.root,'push','-q','origin','main')
        self.assertEqual(self.mod.sync(self.ctx,self.s)['status'],'integrated')
        self.assertTrue((self.fx.back/'docs/guide.md').is_file());self.assertFalse((self.fx.back/'docs/readme.md').exists())

    def test_same_base_does_not_create_commit_or_invoke_qa(self):
        before=self.fx.git(self.fx.back,'rev-parse','HEAD')
        with patch.object(h,'qa_start',side_effect=AssertionError('unneeded QA')):
            self.assertEqual(self.mod.sync(self.ctx,self.s)['status'],'current')
        self.assertEqual(self.fx.git(self.fx.back,'rev-parse','HEAD'),before)
        self.assertFalse(self.s['synchronization']['history'])

    def test_conflicts_are_isolated_and_resume_with_both_repositories(self):
        self.advance('back','src/main/app.txt','remote conflicting backend')
        self.advance('front','remote.txt','remote frontend')
        original=self.fx.git(self.fx.back,'rev-parse','HEAD')
        with self.assertRaisesRegex(h.InfrastructureBlocked,'Integración pendiente'):self.mod.sync(self.ctx,self.s)
        self.assertEqual(self.fx.git(self.fx.back,'rev-parse','HEAD'),original)
        self.assertEqual((self.fx.back/'src/main/app.txt').read_text(),'approved backend')
        shadow=Path(self.s['synchronization']['transaction']['repos']['back']['shadow'])
        (shadow/'src/main/app.txt').write_text('resolved both changes');self.fx.git(shadow,'add','src/main/app.txt')
        self.mod.sync(self.ctx,self.s)
        self.assertEqual((self.fx.back/'src/main/app.txt').read_text(),'resolved both changes')
        self.assertEqual((self.fx.web/'remote.txt').read_text(),'remote frontend');self.assertEqual(self.s['cycles'],0)

    def test_concurrent_edit_to_product_blocks_application(self):
        self.advance('back','src/main/app.txt','remote conflict')
        with self.assertRaises(h.InfrastructureBlocked):self.mod.sync(self.ctx,self.s)
        row=self.s['synchronization']['transaction']['repos']['back'];shadow=Path(row['shadow'])
        (shadow/'src/main/app.txt').write_text('resolved');self.fx.git(shadow,'add','src/main/app.txt')
        (self.fx.back/'src/main/app.txt').write_text('another agent edit')
        with self.assertRaisesRegex(h.Blocked,'Cambió el worktree'):self.mod.sync(self.ctx,self.s)
        self.assertEqual((self.fx.back/'src/main/app.txt').read_text(),'another agent edit')

    def test_unsafe_incoming_e2e_blocks_before_product_worktree_mutation(self):
        self.advance('front','remote.txt','unsafe incoming helper')
        head=self.fx.git(self.fx.web,'rev-parse','HEAD')
        readiness=Mock(side_effect=h.Blocked('Destino fijo/externo en auxiliar E2E'))
        with patch.object(h,'setup_module',return_value=Mock(test_readiness=readiness)):
            with self.assertRaisesRegex(h.Blocked,'Destino fijo'):self.mod.sync(self.ctx,self.s)
        candidate=readiness.call_args.args[1]
        self.assertIn('/integration/',str(candidate['worktrees']['front']))
        self.assertEqual(self.fx.git(self.fx.web,'rev-parse','HEAD'),head)
        self.assertFalse((self.fx.web/'remote.txt').exists());self.assertEqual(self.s['cycles'],0)

    def test_partial_apply_crash_recovers_without_duplicate_commits(self):
        self.advance('back','src/main/remote.txt','remote backend');self.advance('front','remote.txt','remote front')
        apply=self.mod.apply
        def interrupted(ctx,s,name,row):
            apply(ctx,s,name,row)
            if name=='back':raise RuntimeError('controller interrupted')
        with patch.object(self.mod,'apply',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.mod.sync(self.ctx,self.s)
        self.s=h.load(self.s['id']);checkpoint=self.fx.git(self.fx.back,'rev-parse','HEAD')
        self.mod.sync(self.ctx,self.s)
        self.assertEqual(self.fx.git(self.fx.back,'rev-parse','HEAD'),checkpoint)
        self.assertEqual((self.fx.web/'remote.txt').read_text(),'remote front');self.assertEqual(len(self.s['synchronization']['history']),1)

    def test_remote_advance_guard_blocks_without_spending_cycle_or_modifying_tree(self):
        self.s['stage']='ready_for_qa';head=self.fx.git(self.fx.back,'rev-parse','HEAD')
        self.advance('front','remote.txt','remote')
        with self.assertRaisesRegex(h.InfrastructureBlocked,'Cambió la base'):self.mod.guard(self.ctx,self.s)
        self.assertEqual(self.s['blockedPhase'],'upstream');self.assertEqual(self.s['cycles'],0)
        self.assertEqual(self.fx.git(self.fx.back,'rev-parse','HEAD'),head)

    def test_peer_partial_merge_blocks_until_delivery_is_complete(self):
        peer=json.loads(json.dumps(self.s));peer['id']='peer';peer['stage']='awaiting_merge'
        for n,item in peer['publication']['repos'].items():item.update(changed=True,pr={'number':1})
        h.save(peer,'fixture');self.advance('back','src/main/remote.txt','remote')
        with patch.object(self.fx.fake,'cleanup_pr',side_effect=['merged','open']):
            with self.assertRaisesRegex(h.InfrastructureBlocked,'fusión completa'):self.mod.sync(self.ctx,self.s)
        self.assertFalse(self.s['synchronization'].get('transaction'))
        with patch.object(self.fx.fake,'cleanup_pr',return_value='merged'):self.mod.sync(self.ctx,self.s)
        self.assertEqual((self.fx.back/'src/main/remote.txt').read_text(),'remote')

    def test_explicit_dependency_waits_without_polling(self):
        peer=json.loads(json.dumps(self.s));peer['id']='peer';h.save(peer,'fixture')
        with self.assertRaisesRegex(h.InfrastructureBlocked,'dependencia'):self.mod.sync(self.ctx,self.s,after='peer')
        self.assertEqual(self.s['cycles'],0)

    def test_job_started_on_partial_delivery_still_waits_before_checks(self):
        peer=json.loads(json.dumps(self.s));peer['id']='peer';peer['stage']='awaiting_merge'
        for item in peer['publication']['repos'].values():item.update(changed=True,pr={'number':1})
        h.save(peer,'fixture')
        with patch.object(self.fx.fake,'cleanup_pr',side_effect=['merged','open']):
            with self.assertRaisesRegex(h.InfrastructureBlocked,'fusión completa'):self.mod.sync(self.ctx,self.s)
        self.assertEqual(self.s['cycles'],0)

    def test_ci_worker_stops_on_remote_advance_without_cleaning_or_rerunning_tests(self):
        self.s.update(stage='documenting',approval={'version':'approved'},productBaseline=h.job_fingerprint(self.s,True))
        self.fx.pub.freeze(self.ctx,self.s);self.fx.publish()
        self.fx.fake.pr_states={str(self.fx.root):'open',str(self.fx.front):'open'}
        self.advance('front','remote.txt','new upstream')
        with patch.object(self.fx.fake,'ci',side_effect=AssertionError('outdated CI')),patch.object(h,'stop_preview') as stop:
            with self.assertRaisesRegex(h.InfrastructureBlocked,'Cambió la base'):self.fx.clean.tick(self.ctx,self.s)
            stop.assert_not_called()
        self.assertEqual(self.s['blockedPhase'],'upstream');self.assertEqual(self.s['cycles'],0)
        self.assertTrue(self.fx.back.exists() and self.fx.web.exists())

    def test_ignored_local_collision_is_preserved(self):
        (self.fx.root/'.git/info/exclude').write_text('remote-secret.txt\n')
        (self.fx.back/'remote-secret.txt').write_text('private local data')
        # Explicit add in the other checkout simulates a legitimate new upstream tracked file.
        target=self.fx.root/'remote-secret.txt';target.write_text('upstream file');self.fx.git(self.fx.root,'add','-f','remote-secret.txt')
        self.fx.git(self.fx.root,'commit','-qm','upstream new file');self.fx.git(self.fx.root,'push','-q','origin','main')
        with self.assertRaisesRegex(h.Blocked,'colisiona'):self.mod.sync(self.ctx,self.s)
        self.assertEqual((self.fx.back/'remote-secret.txt').read_text(),'private local data')

    def test_docs_only_preserves_approved_product_without_new_qa(self):
        self.s.update(stage='documenting',approval={'version':'approved'},approvedCandidate='approved',qa={'status':'pass'},approvalEvidence='proof')
        self.advance('back','docs/remote.md','Remote documentation')
        with patch.object(h,'preview_valid'),patch.object(h,'validate_report'),patch.object(h,'publication_module',return_value=self.fx.pub),patch.object(self.fx.pub,'evidence',return_value='proof'),patch.object(h,'qa_start',side_effect=AssertionError('redundant QA')):
            result=self.mod.sync(self.ctx,self.s)
        self.assertTrue(result['qaReused']);self.assertEqual(result['next'],'finish')
        self.assertEqual(self.s['approval'],{'version':'approved'});self.assertEqual(self.s['stage'],'documenting')

    def test_product_change_revokes_approval_and_reuses_no_browser_pass(self):
        self.s.update(stage='documenting',approval={'version':'approved'},qa={'status':'pass'},approvalEvidence='proof')
        self.advance('front','remote.txt','new runtime code')
        with patch.object(h,'preview_valid'),patch.object(h,'validate_report'),patch.object(h,'publication_module',return_value=self.fx.pub),patch.object(self.fx.pub,'evidence',return_value='proof'):
            result=self.mod.sync(self.ctx,self.s)
        self.assertFalse(result['qaReused']);self.assertNotIn('approval',self.s);self.assertEqual(result['next'],'verify')

    def test_frontend_only_integration_preserves_backend_verification_inputs(self):
        before=h.fingerprint(self.fx.back,True)
        self.advance('front','remote.txt','new frontend')
        self.mod.sync(self.ctx,self.s)
        self.assertEqual(h.fingerprint(self.fx.back,True),before)

    def test_unchanged_repository_fast_forwards_without_an_empty_pr(self):
        (self.fx.back/'src/main/app.txt').write_text(self.fx.git(self.fx.back,'show','HEAD:src/main/app.txt'))
        shutil.rmtree(self.fx.back/'e2e')
        head=self.advance('back','src/main/remote.txt','remote')
        self.mod.sync(self.ctx,self.s)
        self.assertEqual(self.fx.git(self.fx.back,'rev-parse','HEAD'),head)
        self.s.update(stage='documenting',approval={'version':'new'},productBaseline=h.job_fingerprint(self.s,True))
        self.fx.pub.freeze(self.ctx,self.s);self.fx.publish()
        self.assertFalse(self.s['publication']['repos']['back']['changed'])
        self.assertEqual(self.fx.fake.calls,1)

    def test_republishes_existing_pr_fast_forward_with_updated_overlay(self):
        self.s.update(stage='documenting',approval={'version':'approved'},productBaseline=h.job_fingerprint(self.s,True))
        self.fx.pub.freeze(self.ctx,self.s);self.fx.publish()
        old=self.s['publication']['repos']['back']['commit']
        self.fx.fake.pr_states={str(self.fx.root):'open',str(self.fx.front):'open'}
        self.advance('back','.gitignore',(self.fx.root/'.gitignore').read_text()+'remote-output/\n')
        self.mod.sync(self.ctx,self.s)
        self.s.update(stage='documenting',approval={'version':'new'},productBaseline=h.job_fingerprint(self.s,True))
        self.fx.pub.freeze(self.ctx,self.s);self.fx.publish()
        item=self.s['publication']['repos']['back']
        self.assertNotEqual(item['commit'],old);self.assertEqual(self.fx.fake.calls,2)
        self.fx.git(self.fx.back,'merge-base','--is-ancestor',old,item['commit'])
        self.fx.git(self.fx.back,'merge-base','--is-ancestor',item['validatedBase'],item['commit'])
        content=self.fx.git(self.fx.back,'show',item['commit']+':.gitignore')
        self.assertIn('remote-output/',content);self.assertNotIn('.harness-parent.json',content)

    def test_exact_temporary_write_is_resumed_after_interruption(self):
        self.advance('back','src/main/remote.txt','remote')
        apply=self.mod.apply
        def interrupted(ctx,s,name,row):
            target=self.fx.back/'src/main/remote.txt'
            target.with_name(target.name+'.harness-sync-'+s['resourceKey']).write_text('remote')
            raise RuntimeError('crash before rename')
        with patch.object(self.mod,'apply',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.mod.sync(self.ctx,self.s)
        self.mod.sync(self.ctx,self.s)
        self.assertEqual((self.fx.back/'src/main/remote.txt').read_text(),'remote')

    def test_appended_migrations_allowed_but_edits_and_deletions_require_reset(self):
        cfg=h.config();cfg['migrationFiles']=[{'repo':'back','glob':'migrations/*.sql'}]
        self.fx.fixture.write_config(cfg)
        directory=self.fx.back/'migrations';directory.mkdir()
        old=directory/'V1__initial.sql';old.write_text('select 1;')
        self.s['migrationAttempt']=h.migrations_version(self.s)
        self.s['appliedMigrationFiles']=h.adapter_call('migration_files',self.s)
        (directory/'V2__remote.sql').write_text('select 2;')
        self.assertTrue(h.migrations_compatible(self.s))
        old.write_text('select 3;');self.assertFalse(h.migrations_compatible(self.s))
        old.unlink();self.assertFalse(h.migrations_compatible(self.s))
        self.s.pop('appliedMigrationFiles');self.assertFalse(h.migrations_compatible(self.s))

    def test_rewritten_remote_history_is_not_automatically_adopted(self):
        self.advance('front','remote.txt','new')
        self.mod.sync(self.ctx,self.s)
        self.fx.git(self.fx.front,'reset','--hard','HEAD^');self.fx.git(self.fx.front,'push','-q','--force','origin','main')
        with self.assertRaisesRegex(h.Blocked,'reescribió'):self.mod.sync(self.ctx,self.s)
