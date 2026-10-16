"""Real commands and repositories exercise invalidation rather than mocking the cache."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
import test_portability as portability
import test_setup as setup_tests
h=portability.h


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=portability.PortabilityTests();self.fixture.setUp()
        self.root=self.fixture.root;self.s=self.fixture.s
        self.front=self.root/'.harness/frontend';self.front.mkdir(parents=True)
        subprocess.run(['git','init','-q',str(self.front)],check=True)
        (self.front/'.gitignore').write_text('target/\n')
        (self.front/'src').mkdir();(self.front/'src/app.js').write_text('hello')
        (self.front/'e2e').mkdir();(self.front/'e2e/test.js').write_text('test')
        cfg=h.config()
        cfg.update(repositories={'main':{'path':'.'},'front':{'path':str(self.front)}},primaryRepository='main')
        self.s['worktrees']={'main':str(self.root),'front':str(self.front)}
        cfg['checks']=[];cfg['testReports']={}
        for repo,name in [('main','backend'),('front','frontend')]:
            script="""from pathlib import Path
p=Path('target');p.mkdir(exist_ok=True)
c=p/'count';c.write_text(str(int(c.read_text())+1 if c.exists() else 1))
(p/'junit.xml').write_text('<testsuite tests="1"/>')
"""
            cfg['checks'].append({'id':name,'repo':repo,'command':[sys.executable,'-c',script],
                                  'cache':True,'usesDatabase':False,'reports':[name],
                                  'inputs':[{'repo':repo,'exclude':['e2e/**']}]})
            cfg['testReports'][name]={'repo':repo,'glob':'target/junit.xml'}
        self.fixture.write_config(cfg)
        h.validate_config()

    def tearDown(self):
        h._execution=None;h._tool_versions={}
        self.fixture.tearDown()

    def checks(self,force=False):
        self.s['forceVerification']=force
        h.begin_attempt(self.s,'test')
        try:return h.adapter_call('checks',self.s)
        finally:h.end_attempt(self.s)

    def counts(self):return [int((p/'target/count').read_text()) for p in (self.root,self.front)]

    def test_reuses_only_unchanged_component_and_e2e_exclusions(self):
        self.checks();self.checks();self.assertEqual(self.counts(),[1,1])
        (self.front/'e2e/test.js').write_text('new test')
        self.checks();self.assertEqual(self.counts(),[1,1])
        (self.front/'src/app.js').write_text('new product')
        self.checks();self.assertEqual(self.counts(),[1,2])
        (self.root/'src/main/app.txt').write_text('backend')
        self.checks();self.assertEqual(self.counts(),[2,2])
        self.checks(force=True);self.assertEqual(self.counts(),[3,3])

    def test_new_deleted_and_mode_changed_inputs_invalidate(self):
        self.checks();p=self.front/'src/unknown';p.write_text('new')
        self.checks();self.assertEqual(self.counts(),[1,2])
        p.unlink();self.checks();self.assertEqual(self.counts(),[1,3])
        p=self.front/'src/app.js';p.chmod(0o755)
        self.checks();self.assertEqual(self.counts(),[1,4])

    def test_reports_logs_and_archives_must_be_intact(self):
        self.checks()
        for kind in ['report','log','archive']:
            old=self.s['receipts']['frontend']
            p={'report':self.front/'target/junit.xml','log':Path(old['log']),
               'archive':Path(next(iter(old['archives'])))}[kind]
            p.unlink()
            with self.assertRaises(h.Blocked):h.verification_module().validate_receipts(h.CoreContext(),self.s)
            self.checks()
        self.assertEqual(self.counts(),[1,4])
        logs=list((h.jobdir(self.s['id'])/'attempts').glob('*/backend.log'))
        self.assertEqual(len(logs),1)

    def test_failure_has_no_receipt_and_does_not_reuse_old_report(self):
        self.checks();cfg=h.config();cfg['checks'][1]['command']=[sys.executable,'-c','raise SystemExit(2)']
        self.fixture.write_config(cfg)
        with self.assertRaises(h.Blocked):self.checks()
        self.assertNotIn('frontend',self.s['receipts'])
        self.assertFalse((self.front/'target/junit.xml').exists())

    def test_changing_inputs_during_command_fails(self):
        cfg=h.config();cfg['checks'][1]['command'][-1]+="\nPath('src/app.js').write_text('mutated')"
        self.fixture.write_config(cfg)
        with self.assertRaises(h.Blocked):self.checks()
        self.assertNotIn('frontend',self.s['receipts'])

    def test_receipts_cannot_cross_jobs_or_tool_configuration(self):
        self.checks();self.s['id']='test-two';h.save(self.s,'second')
        self.checks();self.assertEqual(self.counts(),[2,2])
        cfg=h.config();cfg['checks'][1]['tools']=['git'];self.fixture.write_config(cfg)
        self.checks();self.assertEqual(self.counts(),[2,3])

    def test_declared_dependency_invalidates_dependent_group(self):
        cfg=h.config();cfg['checks'][1]['dependsOn']=['backend'];self.fixture.write_config(cfg)
        self.checks();(self.root/'src/main/app.txt').write_text('new API contract')
        self.checks();self.assertEqual(self.counts(),[2,2])
        cfg['checks'][0]['dependsOn']=['frontend'];self.fixture.write_config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()

    def test_default_inputs_are_conservative(self):
        cfg=h.config();cfg['checks'][1].pop('inputs');self.fixture.write_config(cfg)
        self.checks();(self.front/'e2e/test.js').write_text('modified')
        self.checks();self.assertEqual(self.counts(),[1,2])

    def test_live_database_checks_never_reuse(self):
        cfg=h.config();cfg['checks'][0]['usesDatabase']=True;self.fixture.write_config(cfg)
        module=h.verification_module();step=cfg['checks'][0]
        # Execute through cache helper with an isolated database policy; no DB access in fixture command.
        for _ in range(2):
            h.begin_attempt(self.s,'live')
            with patch.object(h,'has_database',return_value=True):
                def execute():
                    p=h.attempt_path(self.s,'live.log');p.write_text('passed');return p
                module.group(h.CoreContext(),self.s,step,'backend',{},execute)
            h.end_attempt(self.s)
        self.assertIsNone(self.s['receipts']['backend']['key'])
        self.assertEqual(self.s['steps'][-1]['status'],'passed')

    def test_outputs_missing_or_modified_invalidate_installation(self):
        cfg=h.config();step=cfg['checks'][1]
        step['outputs']=['target/installed']
        step['command'][-1]+="\n(p/'installed').write_text('coherent installation')"
        self.fixture.write_config(cfg);self.checks()
        (self.front/'target/installed').write_text('tampered')
        self.checks();(self.front/'target/installed').unlink();self.checks()
        self.assertEqual(self.counts(),[1,3])

    def test_timeout_records_duration_and_never_caches(self):
        h.begin_attempt(self.s,'timeout')
        with self.assertRaises(h.Blocked):
            h.run([sys.executable,'-c','import time;time.sleep(5)'],log=h.attempt_path(self.s,'timeout.log'),timeout=.05)
        rows=[json.loads(v) for v in h.attempt_path(self.s,'commands.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['exitCode'],124)
        self.assertGreater(rows[-1]['durationSeconds'],0)
        self.assertNotIn('currentCommand',self.s)

    def test_qa_infrastructure_block_does_not_consume_cycle(self):
        self.s.update(stage='qa',qaVersion='v')
        p=self.root/'.harness/report.json';p.write_text(json.dumps({'status':'blocked','findings':[]}))
        with patch.object(h,'version',return_value='v'),patch.object(h,'validate_report'),patch.object(h,'release_browser'):
            h.qa_result(self.s,p)
        self.assertEqual(self.s['cycles'],0);self.assertEqual(self.s['stage'],'blocked')

    def test_verify_timeout_is_infrastructure_not_product_failure(self):
        with patch.object(h,'db_info'),patch.object(h,'adapter_call',side_effect=h.InfrastructureBlocked('timeout')):
            with self.assertRaises(h.Blocked):h.verify(self.s)
        self.assertEqual(self.s['cycles'],0);self.assertEqual(self.s['blockedPhase'],'infrastructure')

    def test_real_tool_version_change_invalidates_group(self):
        tool=self.front/'tool';tool.write_text('#!/bin/sh\necho version-one\n');tool.chmod(0o755)
        cfg=h.config();cfg['checks'][1]['tools']=['./tool'];self.fixture.write_config(cfg)
        self.checks();tool.write_text('#!/bin/sh\necho version-two\n');self.checks()
        self.assertEqual(self.counts(),[1,2])

    def test_input_and_report_metadata_rejects_ambiguous_ownership(self):
        cfg=h.config();cfg['checks'][1]['reports']=['backend'];self.fixture.write_config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()
        cfg['checks'][1]['reports']=['frontend'];cfg['checks'][1]['inputs']=[{'repo':'front'},{'repo':'front'}]
        self.fixture.write_config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()

    def test_generated_dependency_cache_does_not_invalidate_installation(self):
        cfg=h.config()
        cfg['setup']=[{'id':'install','repo':'front','command':[sys.executable,'-c',
            "from pathlib import Path;p=Path('target/modules');p.mkdir(parents=True,exist_ok=True);(p/'package').write_text('installed')"],
            'cache':True,'usesDatabase':False,'outputs':['target/modules'],
            'outputExcludes':['target/modules/.cache/**']}]
        cfg['checks'][1]['dependsOn']=['install'];self.fixture.write_config(cfg)
        self.checks();receipt=self.s['receipts']['install']
        cache=self.front/'target/modules/.cache';cache.mkdir();(cache/'generated').write_text('runtime')
        self.checks();self.assertEqual(receipt,self.s['receipts']['install'])
        self.assertEqual(self.counts(),[1,1])
        (self.front/'target/modules/package').write_text('tampered')
        self.checks();self.assertEqual(self.counts(),[1,2])

    def test_install_failure_is_infrastructure(self):
        cfg=h.config();cfg['setup']=[{'id':'install','command':[sys.executable,'-c','raise SystemExit(1)']}]
        self.fixture.write_config(cfg)
        with self.assertRaises(h.InfrastructureBlocked):self.checks()
        self.assertNotIn('install',self.s.get('receipts',{}))


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=setup_tests.SetupTests();self.fixture.setUp();self.fixture.no_db()
        self.root=self.fixture.root
        (self.root/'package.json').write_text(json.dumps({'devDependencies':{'@playwright/test':'1'},'scripts':{'test:e2e':'playwright test'}}))
        (self.root/'playwright.config.ts').write_text("export default {reporter:'junit'}")
        (self.root/'e2e').mkdir();self.helper=self.root/'e2e/helper.ts'
        (self.root/'e2e/example.spec.ts').write_text("import { test, expect } from '@playwright/test'; test('example', () => expect(1).toBe(1))")
        self.helper.write_text("execFileSync('docker', ['exec', process.env.DB_CONTAINER, 'psql', '-U', process.env.PGUSER])")
        cfg=h.config();cfg['e2e']={'runner':'playwright-test','command':['npm','run','test:e2e'],
                                 'report':'target/e2e.xml','testDir':'e2e'}
        (self.root/'harness/config.json').write_text(json.dumps(cfg))

    def tearDown(self):self.fixture.tearDown()

    def test_parameterized_helper_passes_static_check(self):
        self.assertEqual(self.fixture.validate()['testPreflight']['status'],'static_passed')

    def test_excel_xml_namespace_is_not_a_database_destination(self):
        self.helper.write_text(self.helper.read_text()+"\nns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}")
        self.assertEqual(self.fixture.validate()['testPreflight']['status'],'static_passed')

    def test_namespace_exception_does_not_hide_real_connections(self):
        base=self.helper.read_text()
        ns="\nns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}"
        for extra in ["\nurl='postgresql://prod/database'", "\nurl='https://api.example.com/data'",
                      "\nurl='http://schemas.openxmlformats.org/spreadsheetml/2006/main'"]:
            with self.subTest(extra=extra):
                self.helper.write_text(base+ns+extra)
                with self.assertRaisesRegex(h.Blocked,'No edites|no edites'):self.fixture.validate()
        for url in ['http://schemas.openxmlformats.org.evil/main',
                    'http://schemas.openxmlformats.org/spreadsheetml/2006/main?db=prod']:
            self.helper.write_text(base+"\nns = {'s': '"+url+"'}")
            with self.assertRaises(h.Blocked):self.fixture.validate()

    def test_fixed_database_role_blocks_before_migrations(self):
        self.fixture.validate()
        self.helper.write_text("execFileSync('docker', ['exec', process.env.DB_CONTAINER, 'psql', '-U', 'postgres','-d','main_db'])")
        with self.assertRaises(h.Blocked):self.fixture.validate()
        self.assertEqual(json.loads((h.STORE/'setup.json').read_text())['status'],'blocked')

    def test_missing_runner_script_is_blocked(self):
        (self.root/'package.json').write_text('{}')
        with self.assertRaises(h.Blocked):self.fixture.validate()

    def test_fixed_environment_fallback_is_blocked(self):
        self.helper.write_text("const pg = process.env.E2E_PG ?? 'shared-db'; execFileSync('docker', ['exec',pg,'psql'])")
        with self.assertRaises(h.Blocked):self.fixture.validate()

    def api_suite(self,enabled=None):
        root=self.root/'api-tests';(root/'pruebas').mkdir(parents=True)
        (root/'package.json').write_text(json.dumps({'devDependencies':{'@playwright/test':'1'}}))
        (root/'playwright.config.ts').write_text("export default {reporter:'junit'}")
        cfg=h.config();cfg['apiE2E']={'runner':'playwright-test','cwd':'api-tests','command':['npx','playwright','test'],
            'testDir':'api-tests/pruebas','report':'api-tests/test-results/junit.xml'}
        if enabled is not None:cfg['apiE2E']['enabled']=enabled
        (self.root/'harness/config.json').write_text(json.dumps(cfg));return root/'pruebas'

    def test_disabled_api_suite_is_not_inspected_or_executed(self):
        self.api_suite(False)
        cfg=h.config();cfg['apiE2E']['cwd']='missing';cfg['pendientes']={'apiE2E':'not in use'}
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        result=self.fixture.validate()['testPreflight']
        self.assertEqual(result['status'],'static_passed')
        self.assertEqual(result['suites']['apiE2E'],{'status':'disabled'})
        self.assertEqual(result['suites']['e2e']['status'],'static_passed')

    def test_enabled_empty_api_suite_blocks_before_database_preflight(self):
        self.api_suite(True)
        probe=Mock(database=Mock())
        with patch.object(h,'doctor',return_value=0),patch.object(h,'preflight_module',return_value=probe):
            with self.assertRaisesRegex(h.Blocked,'apiE2E:.*sin archivos'):setup_tests.setup.validate(h.CoreContext(),self.fixture.args)
        probe.database.assert_not_called()

    def test_legacy_api_suite_without_enabled_flag_is_validated(self):
        tests=self.api_suite()
        with self.assertRaisesRegex(h.Blocked,'apiE2E'):self.fixture.validate()
        (tests/'smoke.spec.ts').write_text("import { test } from '@playwright/test'; test('smoke', () => {})")
        result=self.fixture.validate()['testPreflight']
        self.assertEqual(result['suites']['apiE2E']['status'],'static_passed')
        (tests/'helper.ts').write_text("const db = process.env.PGDATABASE ?? 'shared_db';")
        with self.assertRaisesRegex(h.Blocked,'apiE2E:.*Destino fijo'):self.fixture.validate()

    def test_api_only_suite_is_validated_and_boolean_is_strict(self):
        tests=self.api_suite(True)
        (tests/'smoke.spec.ts').write_text("import { test } from '@playwright/test'; test('smoke', () => {})")
        cfg=h.config();cfg.pop('e2e');(self.root/'harness/config.json').write_text(json.dumps(cfg))
        self.assertEqual(self.fixture.validate()['testPreflight']['suites']['apiE2E']['status'],'static_passed')
        cfg['apiE2E']['enabled']='false';(self.root/'harness/config.json').write_text(json.dumps(cfg))
        with self.assertRaisesRegex(h.Blocked,'booleano'):self.fixture.validate()

    def test_enabled_frontend_requires_nonempty_test_files(self):
        p=self.root/'e2e/example.spec.ts';p.write_text('')
        with self.assertRaisesRegex(h.Blocked,'vacíos'):self.fixture.validate()
        p.unlink()
        with self.assertRaisesRegex(h.Blocked,'sin archivos'):self.fixture.validate()
