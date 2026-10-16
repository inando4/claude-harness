"""Regression checks for merge races, independent browsers and resumable QA."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_conversation as chat
import test_synchronization as sync_tests
import test_pending as pending
import test_publication as publication
h=chat.h


class MergeRaceTests(unittest.TestCase):
    def setUp(self):
        self.fx=sync_tests.SynchronizationTests();self.fx.setUp()
        self.pub=self.fx.fx;self.s=self.fx.s;self.ctx=self.fx.ctx
        self.s.update(stage='documenting',approval={'version':'approved'},productBaseline=h.job_fingerprint(self.s,True))
        self.pub.pub.freeze(self.ctx,self.s);self.pub.publish()
        self.s['publication']['phase']='awaiting_merge';self.s['stage']='awaiting_merge'
    def tearDown(self):self.fx.tearDown()

    def test_own_merge_between_pr_and_remote_reads_reconciles_without_blocking(self):
        self.fx.advance('front','new.txt','upstream')
        fake=self.pub.fake
        with patch.object(fake,'cleanup_pr',side_effect=['open','open','merged','merged']):
            self.assertFalse(self.pub.clean.tick(self.ctx,self.s))
        self.assertNotEqual(self.s['stage'],'blocked')
        self.assertTrue(self.pub.clean.tick(self.ctx,self.s))
        self.assertEqual(self.s['stage'],'completed')

    def test_partial_merge_waits_and_foreign_only_still_blocks(self):
        self.fx.advance('front','new.txt','upstream')
        with patch.object(self.pub.fake,'cleanup_pr',side_effect=['open','open','merged','open']):
            self.assertFalse(self.pub.clean.tick(self.ctx,self.s))
        self.assertEqual(self.s['stage'],'awaiting_merge')
        with patch.object(self.pub.fake,'cleanup_pr',return_value='open'):
            with self.assertRaises(h.InfrastructureBlocked):self.pub.clean.tick(self.ctx,self.s)

    def test_merge_race_while_awaiting_ci_keeps_ci_validation(self):
        self.s['publication']['phase']='awaiting_ci';self.s['stage']='awaiting_ci'
        self.fx.advance('front','new.txt','upstream')
        with patch.object(self.pub.fake,'cleanup_pr',side_effect=['open','open','merged','merged']):
            self.assertFalse(self.pub.clean.tick(self.ctx,self.s))
        self.pub.fake.ci_state='pending'
        self.assertFalse(self.pub.clean.tick(self.ctx,self.s))
        self.assertTrue(self.pub.back.exists())


class BrowserLeaseTests(unittest.TestCase):
    def setUp(self):
        self.fx=chat.ConversationTests();self.fx.setUp();self.ctx=self.fx.ctx;self.s=self.fx.s
        self.mod=chat.module('browser_mcp');self.s.update(stage='qa',qaVersion='v1');h.save(self.s,'qa')
        self.guard=patch.object(h,'require_job_config');self.guard.start();self.leases=[]
    def tearDown(self):
        for l in self.leases:l.release()
        self.guard.stop();self.fx.tearDown()
    def lease(self):
        l=self.mod.Lease(self.ctx);self.leases.append(l);return l
    def test_two_jobs_can_explore_but_same_job_cannot(self):
        a=self.lease();a.acquire(self.s['id'],'v1')
        b=self.lease()
        with self.assertRaises(h.Blocked):b.acquire(self.s['id'],'v1')
        other=dict(self.s,id='other');h.jobdir('other').mkdir();h.save(other,'qa')
        b.acquire('other','v1');a.validate();b.validate()
        with self.assertRaises(h.Blocked):self.mod.ensure_released(self.ctx,self.s)
        a.release();self.mod.ensure_released(self.ctx,self.s);b.validate()
    def test_stale_version_and_serial_fallback(self):
        with self.assertRaises(h.Blocked):self.lease().acquire(self.s['id'],'old')
        cfg=h.config();cfg['browser']={'maxExplorations':1}
        with patch.object(h,'config',return_value=cfg):
            self.lease().acquire(self.s['id'],'v1')
            other=dict(self.s,id='other');h.jobdir('other').mkdir();h.save(other,'qa')
            with self.assertRaises(h.Blocked):self.lease().acquire('other','v1')
    def test_no_binding_or_changed_stage_rejects_browser_calls(self):
        a=self.lease()
        with self.assertRaises(h.Blocked):a.validate()
        a.acquire(self.s['id'],'v1');self.s['stage']='implementing';h.save(self.s,'changed')
        with self.assertRaises(h.Blocked):a.validate()


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.fx=pending.QAContextTests();self.fx.setUp();self.s=self.fx.s;self.mod=self.fx.module;self.ctx=h.CoreContext()
        (h.jobdir(self.s['id'])/'artifacts').mkdir(exist_ok=True)
        self.s['stage']='qa';self.path=Path(h.repository_root(self.s))/'tests';self.path.mkdir(exist_ok=True)
    def tearDown(self):self.fx.tearDown()
    def test_untracked_tests_have_exact_delta_and_partial_checkpoint(self):
        file=self.path/'form.spec.ts';file.write_text('expect(old).toBe(true);\n')
        self.mod.capture(self.ctx,self.s)
        report=h.jobdir(self.s['id'])/'artifacts/progress.json';report.write_text(json.dumps({'version':'one','criteria':[{'id':'AC-1','status':'pending'}]}))
        self.mod.checkpoint(self.ctx,self.s,str(report))
        file.write_text('expect(newValue).toBe(true);\n');self.s['qaVersion']='two'
        self.mod.capture(self.ctx,self.s)
        delta=Path(self.s['qaContext']['testDelta']).read_text()
        self.assertIn('-expect(old)',delta);self.assertIn('+expect(newValue)',delta)
        self.assertEqual(self.s['qaContext']['checkpoint']['version'],'one')
        self.assertEqual(self.s['stage'],'qa')
    def test_checkpoint_rejects_foreign_report_and_wrong_version(self):
        report=h.jobdir(self.s['id'])/'artifacts/progress.json';report.write_text('{"version":"other"}')
        with self.assertRaises(h.Blocked):self.mod.checkpoint(self.ctx,self.s,str(report))
        with self.assertRaises(h.Blocked):self.mod.checkpoint(self.ctx,self.s,'/tmp/nope.json')


class SpecCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fx=publication.PublicationTests();self.fx.setUp();self.s=self.fx.s;self.ctx=h.CoreContext();self.mod=chat.module('spec_status')
        self.path=self.fx.root/'specs/local.md';self.path.write_text('# Feature\n\nEstado: PENDIENTE\n\n[Doc](../docs/readme.md)\n')
        with (self.fx.root/'.git/info/exclude').open('a') as f:f.write('specs/local.md\nspecs/Done/\n')
        self.s['specs']=['specs/local.md'];self.mod.capture(self.ctx,self.s)
        target=self.fx.back/'specs/local.md';target.parent.mkdir(exist_ok=True);target.write_bytes(self.path.read_bytes())
        self.mod.prepare(self.ctx,self.s)
    def tearDown(self):self.fx.tearDown()
    def test_local_ignored_move_done_and_retry_keep_links(self):
        self.mod.complete(self.ctx,self.s);self.mod.complete(self.ctx,self.s)
        target=self.fx.root/'specs/Done/local.md'
        self.assertFalse(self.path.exists());self.assertIn('Estado: TERMINADO',target.read_text())
        self.assertIn('../../docs/readme.md',target.read_text())
        self.assertEqual(self.s['specCompletion']['specs/local.md']['source'],'moved_local')
    def test_modified_source_or_destination_collision_is_preserved(self):
        self.path.write_text('User changes')
        self.mod.complete(self.ctx,self.s)
        self.assertEqual(self.path.read_text(),'User changes')
        self.assertEqual(self.s['specCompletion']['specs/local.md']['source'],'preserved_user_changes')
    def test_existing_done_file_is_never_overwritten(self):
        target=self.fx.root/'specs/Done/local.md';target.parent.mkdir();target.write_text('Different spec')
        self.mod.complete(self.ctx,self.s)
        self.assertEqual(target.read_text(),'Different spec');self.assertTrue(self.path.exists())
    def test_tracked_spec_moves_in_worktree_not_source(self):
        self.s['specs']=['specs/feature.md'];self.s.pop('specCompletionPlan');self.mod.capture(self.ctx,self.s)
        source=self.fx.back/'specs/feature.md';self.mod.stage_tracked(self.ctx,self.s)
        self.assertFalse(source.exists());self.assertTrue((self.fx.root/'specs/feature.md').exists())
        self.assertIn('TERMINADO',(self.fx.back/'specs/Done/feature.md').read_text())
        env=self.fx.pub.build_index(self.ctx,self.s,'back',freeze=False)
        recorded=self.fx.pub.git(self.ctx,self.fx.back,'show',':specs/Done/feature.md',env=env).stdout.decode()
        self.assertIn('TERMINADO',recorded)
        self.mod.prepare(self.ctx,self.s);self.mod.complete(self.ctx,self.s)
        self.assertEqual(self.s['specCompletion']['specs/feature.md']['source'],'published_in_pr')


class QALeaseTransitionTests(unittest.TestCase):
    def test_recheck_does_not_invalidate_approval_when_browser_is_busy(self):
        fx=chat.ConversationTests();fx.setUp()
        try:
            fx.s.update(stage='qa',approval={'version':'old'})
            with patch.object(h,'release_browser',side_effect=h.Blocked('busy')):
                with self.assertRaises(h.Blocked):h.recheck(fx.s)
            self.assertEqual(fx.s['stage'],'qa');self.assertEqual(fx.s['approval'],{'version':'old'})
        finally:fx.tearDown()
