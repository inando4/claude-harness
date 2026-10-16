import importlib.util
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_verification as verification
import test_harness as core
h = core.h


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture=verification.VerificationTests(); self.fixture.setUp()
        self.s=self.fixture.s

    def tearDown(self): self.fixture.tearDown()

    def test_approval_from_different_terminal_reuses_receipts_and_requires_confirmation(self):
        with patch.dict(os.environ, {'LANG':'C','LC_ALL':'C','ANTHROPIC_API_KEY':'not-recorded'}):
            h.capture_environment(self.s)
            with h.job_environment(self.s): self.fixture.checks()
        self.assertNotIn('ANTHROPIC_API_KEY',self.s['executionEnvironment']['values'])
        self.s.update(stage='awaiting_human_approval')
        self.s['approvedCandidate']=h.version(self.s)
        receipts=json.dumps(self.s['receipts'],sort_keys=True)
        def validate(s): h.verification_module().validate_receipts(h.CoreContext(),s)
        with patch.dict(os.environ, {'PATH':'/nonexistent','LANG':'es_PE.UTF-8','LC_ALL':'different'}), \
             patch.object(h,'preview_valid',side_effect=validate), \
             patch('sys.stdin.isatty',return_value=True), patch('builtins.input',return_value='wrong'):
            with self.assertRaises(h.Blocked): h.approve(self.s,self.s['approvedCandidate'])
            self.assertEqual(os.environ['PATH'],'/nonexistent')
        self.assertNotIn('approval',self.s)
        with patch.dict(os.environ, {'PATH':'/nonexistent','LANG':'other'}), \
             patch.object(h,'preview_valid',side_effect=validate), \
             patch('sys.stdin.isatty',return_value=True), patch('builtins.input',return_value='APROBAR '+self.s['id']):
            h.approve(self.s,self.s['approvedCandidate'])
        self.assertEqual(self.s['stage'],'documenting')
        self.assertEqual(receipts,json.dumps(self.s['receipts'],sort_keys=True))
        self.assertEqual(self.fixture.counts(),[1,1])

    def test_recorded_environment_tamper_blocks_and_context_restores_on_error(self):
        h.capture_environment(self.s)
        original=h.clean_env()
        with self.assertRaises(RuntimeError),h.job_environment(self.s): raise RuntimeError('interrupted')
        self.assertEqual(h.clean_env(),original)
        self.s['executionEnvironment']['values']['LANG']='tampered'
        with self.assertRaises(h.Blocked),h.job_environment(self.s): pass

    def test_real_input_or_evidence_changes_still_block_in_saved_environment(self):
        h.capture_environment(self.s)
        with h.job_environment(self.s): self.fixture.checks()
        (self.fixture.front/'src/app.js').write_text('changed')
        with h.job_environment(self.s):
            with self.assertRaisesRegex(h.Blocked,'inputs'):
                h.verification_module().validate_receipts(h.CoreContext(),self.s)


class ReportAndMetricsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=core.HarnessTests();self.fixture.setUp();self.s=self.fixture.s;self.root=self.fixture.root
        shutil.copy2(core.MODULE.parent/'qa-report.example.json',self.root/'harness/qa-report.example.json')
        spec=importlib.util.spec_from_file_location('metrics',core.MODULE.parent/'metrics.py')
        self.metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.metrics)

    def tearDown(self): self.fixture.tearDown()

    def test_report_template_has_current_version_and_does_not_overwrite_evidence(self):
        self.s.update(stage='qa',qaVersion='a'*64)
        with patch('builtins.print'): h.qa_template(self.s)
        p=h.jobdir(self.s['id'])/'artifacts'/('qa-report-'+'a'*64+'.json')
        report=json.loads(p.read_text());self.assertEqual(report['version'],self.s['qaVersion']);self.assertEqual(report['status'],'blocked')
        self.assertEqual(report['criteria'],[{'id':'AC-1','status':'pending','evidence':''}])
        p.write_text('existing evidence')
        with patch('builtins.print'): h.qa_template(self.s)
        self.assertEqual(p.read_text(),'existing evidence')

    def test_report_check_collects_errors_without_mutating_state(self):
        self.s.update(qaVersion='new')
        original=json.dumps(self.s,sort_keys=True)
        errors=h.report_errors({'version':'old','status':'passed'},self.s)
        self.assertGreaterEqual(len(errors),3)
        for malformed in [None,[],{'status':'pass','exploration':None,'repeatableE2E':[]}]:
            self.assertTrue(h.report_errors(malformed,self.s))
        self.assertEqual(original,json.dumps(self.s,sort_keys=True))

    def test_usage_deduplicates_streamed_messages_and_ignores_other_sessions(self):
        p=self.root/'session.jsonl';rows=[]
        for output in [1,50,50]:
            rows.append({'sessionId':'session','type':'assistant','effort':'max','message':{'id':'same','model':'opus',
                'usage':{'output_tokens':output,'cache_read_input_tokens':100,'iterations':[{'output_tokens':1000}]},
                'content':[{'type':'tool_use','id':'call','name':'Read'}]}})
        rows.append({'sessionId':'other','type':'assistant','message':{'id':'other','usage':{'output_tokens':999}}})
        p.write_text('\n'.join(json.dumps(v) for v in rows)+'\n{"incomplete":')
        result=self.metrics.transcript_counts(p,'session')
        self.assertEqual(result['messages'],1);self.assertEqual(result['tokens']['output_tokens'],50)
        self.assertEqual(result['tokens']['cache_read_input_tokens'],100);self.assertEqual(result['tools'],{'Read':1})

    def test_qa_findings_with_added_tests_return_directly_to_implementation(self):
        self.s.update(stage='qa',qaVersion='reviewed')
        report={'version':'reviewed','status':'fail','findings':[{'id':'QA-01','title':'Lost date'}]}
        p=self.root/'.harness/report.json';p.write_text(json.dumps(report))
        (self.root/'src/test').mkdir();(self.root/'src/test/regression.txt').write_text('new test')
        with patch.object(h,'version',return_value='changed') as version, patch.object(h,'preview_valid') as preview, \
             patch.object(h,'release_browser'),patch('builtins.print'):
            h.qa_result(self.s,p)
        version.assert_not_called();preview.assert_not_called()
        self.assertEqual(self.s['stage'],'implementing');self.assertEqual(self.s['cycles'],1)
        self.assertEqual(json.loads((h.jobdir(self.s['id'])/'qa.json').read_text()),report)
        self.assertNotIn('approvedCandidate',self.s)

    def test_changed_revision_cannot_pass_or_submit_another_reviews_findings(self):
        self.s.update(stage='qa',qaVersion='reviewed')
        p=self.root/'.harness/report.json'
        p.write_text(json.dumps({'version':'wrong','status':'fail','findings':[{'id':'QA-01'}]}))
        with self.assertRaises(h.Blocked):h.qa_result(self.s,p)
        p.write_text(json.dumps({'version':'reviewed','status':'pass','findings':[]}))
        with patch.object(h,'validate_report'),patch.object(h,'version',return_value='changed'),self.assertRaises(h.Blocked):
            h.qa_result(self.s,p)
        self.assertEqual(self.s['stage'],'qa');self.assertEqual(self.s['cycles'],0)

    def test_saved_ci_state_does_not_claim_worker_is_running(self):
        self.s.update(stage='awaiting_ci',publication={'phase':'awaiting_ci','repos':{},
                       'worker':{'pid':os.getpid(),'start':h.process_identity(os.getpid())}})
        self.assertEqual(h.diagnostic(self.s)['publication']['workerStatus'],'running')
        self.s['publication']['worker']['start']='different-process'
        report=h.diagnostic(self.s)['publication']
        self.assertEqual(report['workerStatus'],'stopped');self.assertIn('finalize',report['recovery'])
        self.s['publication'].pop('worker')
        self.assertEqual(h.diagnostic(self.s)['publication']['workerStatus'],'unknown')


class ReportIsolationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=verification.VerificationTests();self.fixture.setUp()
        self.root=self.fixture.root;self.front=self.fixture.front
        self.paths=h.runtime_module('report_paths')

    def tearDown(self):self.fixture.tearDown()

    def legacy(self):
        cfg=h.config();step=cfg['checks'][1]
        step['command']=['npm','run','test','--','--outputFile.junit=test-results/harness-unit.xml']
        cfg['testReports']['frontend']={'repo':'front','glob':'test-results/harness-unit.xml'}
        cfg['publication']={'repositories':{'front':{'disposablePaths':['node_modules']}}}
        cfg['e2e']={'repo':'front','runner':'playwright-test','command':['npm','run','test:e2e']}
        return cfg

    def test_upgrade_only_known_pair_is_idempotent_and_does_not_mutate_input(self):
        cfg=self.legacy();before=json.dumps(cfg)
        upgraded=self.paths.upgrade_legacy_vitest(cfg)
        self.assertEqual(json.dumps(cfg),before)
        self.assertEqual(self.paths.upgrade_legacy_vitest(upgraded),upgraded)
        self.assertEqual(upgraded['checks'][1]['command'][-1],'--outputFile.junit=.harness/reports/harness-unit.xml')
        self.assertEqual(upgraded['testReports']['frontend']['glob'],'.harness/reports/harness-unit.xml')
        self.assertEqual(upgraded['publication']['repositories']['front']['disposablePaths'],['node_modules','.harness/reports'])
        cfg['testReports']['frontend']['glob']='custom/report.xml'
        self.assertEqual(self.paths.upgrade_legacy_vitest(cfg),cfg)

    def test_preflight_detects_default_and_custom_output_and_separates_repos(self):
        cfg=self.legacy();self.fixture.fixture.write_config(cfg)
        with self.assertRaisesRegex(h.Blocked,'comparte salida'):self.paths.validate(h.CoreContext())
        upgraded=self.paths.upgrade_legacy_vitest(cfg);self.fixture.fixture.write_config(upgraded)
        self.paths.validate(h.CoreContext())
        (self.front/'playwright.config.ts').write_text("export default {outputDir: 'target'}")
        # target/junit.xml belongs to the other repo, and must not be rejected.
        self.paths.validate(h.CoreContext())
        upgraded['testReports']['frontend']['glob']='target/junit.xml';self.fixture.fixture.write_config(upgraded)
        with self.assertRaises(h.Blocked):self.paths.validate(h.CoreContext())

    def test_playwright_cleanup_preserves_check_receipt_after_isolation(self):
        cfg=h.config();step=cfg['checks'][1]
        (self.front/'.gitignore').write_text('target/\n.harness/\ntest-results/\n')
        step['command'][-1]=step['command'][-1].replace("(p/'junit.xml').write_text", "q=Path('.harness/reports');q.mkdir(parents=True,exist_ok=True)\n(q/'unit.xml').write_text")
        cfg['testReports']['frontend']['glob']='.harness/reports/unit.xml';self.fixture.fixture.write_config(cfg)
        self.fixture.checks()
        output=self.front/'test-results';output.mkdir();(output/'e2e.xml').write_text('playwright output')
        shutil.rmtree(output)
        h.verification_module().validate_receipts(h.CoreContext(),self.fixture.s)
        self.fixture.checks();self.assertEqual(self.fixture.counts(),[1,1])
