import importlib.util
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_harness as existing
h = existing.h
loader = importlib.util.spec_from_file_location('installer', existing.MODULE.parent/'install.py')
installer = importlib.util.module_from_spec(loader)
loader.loader.exec_module(installer)


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.HarnessTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.s = self.fixture.s
        cfg = json.loads((existing.MODULE.parent/'templates/web-no-db.json').read_text())
        cfg.update(needsConfiguration=False, requiredTools=[], setup=[], build=[])
        cfg['checks']=[{'command':['npm','run','test:ci']}]
        self.write_config(cfg)
        self.s.pop('containerId')
        self.s.update(database=None, snapshot=None, snapshotSha256=None, ports={'web':8888})

    def tearDown(self):
        self.fixture.tearDown()

    def write_config(self, cfg):
        (self.root/'harness/config.json').write_text(json.dumps(cfg))

    def test_templates_are_blocked_until_configured(self):
        cfg=h.config();cfg['needsConfiguration']=True;self.write_config(cfg)
        with self.assertRaises(h.Blocked): h.validate_config()

    def test_no_database_needs_no_snapshot_or_docker(self):
        h.validate_config()
        with patch.object(h, 'run') as run:
            h.create_clone(self.s)
            self.assertIsNone(h.db_info(self.s))
            self.assertEqual(h.migration_history(self.s), 'no-database')
            run.assert_not_called()
        with self.assertRaises(h.Blocked): h.reset_db(self.s)

    def test_config_path_escape_is_rejected(self):
        cfg=h.config();cfg['services'][0]['cwd']='../outside';self.write_config(cfg)
        with self.assertRaises(h.Blocked): h.validate_config()
        cfg['services'][0]['cwd']='.';cfg['adapter']='../../outside.py';self.write_config(cfg)
        with self.assertRaises(h.Blocked): h.validate_config()

    def test_service_templates_and_multiple_ports(self):
        cfg=h.config()
        cfg['services']=[{'name':'api','command':['python3','server.py','${PORT_API}'], 'healthPath':'/health'},
                         {'name':'web','command':['node','server.js','${PORT_WEB}'], 'env':{'API_URL':'${URL_API}'}, 'healthPath':'/'}]
        cfg['previewService']='web';self.write_config(cfg)
        self.s['ports']={'api':8123,'web':8888}
        services=h.adapter_call('services',self.s)
        self.assertEqual(services[0]['command'][-1],'8123')
        self.assertEqual(services[1]['env']['API_URL'],'http://localhost:8123')
        self.assertNotIn('DATABASE_URL',services[1]['env'])

    def test_unknown_template_variable_fails(self):
        cfg=h.config();cfg['env']={'URL':'${MISSING}'};self.write_config(cfg)
        with self.assertRaises(h.Blocked): h.app_env(self.s)

    def test_launcher_child_server_is_recognized_and_stopped(self):
        child = 'import socket,time; s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)'
        parent = ('import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",' + repr(child) +
                  '],stdout=subprocess.PIPE,text=True); print(p.stdout.readline(),flush=True); time.sleep(30)')
        p = subprocess.Popen([sys.executable, '-c', parent], stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            port = int(p.stdout.readline())
            self.assertFalse(h.owns_listener(p.pid, port))
            self.assertTrue(h.group_owns_listener(p.pid, port))
            self.s['previews'] = {'web': {'pid': p.pid, 'start': h.process_identity(p.pid)}}
            h.stop_preview(self.s)
            p.wait(timeout=5)
            self.assertNotIn('previews', self.s)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            p.stdout.close()

    def test_generic_postgres_environment_only_uses_clone(self):
        cfg=h.config();cfg['database']={'provider':'postgres','schema':'public','image':'postgres:17',
                    'appRole':'test_app','migratorRole':'test_migrate','historySql':'SELECT 1'}
        self.write_config(cfg)
        h.atomic(h.jobdir(self.s['id'])/'secrets.json',{'password':'local-secret'})
        with patch.object(h,'db_info'), patch.dict(os.environ,{'DATABASE_URL':'production'}):
            runtime=h.app_env(self.s)
            migration=h.app_env(self.s,True)
        self.assertEqual(runtime['DB_RESOURCE_KEY'], self.s['resourceKey'])
        self.assertEqual(runtime['E2E_CLONE_ID'], self.s['resourceKey'])
        self.assertNotEqual(runtime['E2E_CLONE_ID'], self.s['id'])
        self.assertIn('test_app:local-secret@127.0.0.1:5555',runtime['DATABASE_URL'])
        self.assertIn('test_migrate:local-secret@127.0.0.1:5555',migration['DATABASE_URL'])

    def test_junit_testsuites_nested_without_double_counting(self):
        path=self.root/'.harness/reports/junit.xml';path.parent.mkdir(parents=True);path.write_text(
            '<testsuites><testsuite tests="2"><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuite></testsuites>')
        self.assertEqual(h.test_summary(self.root)['tests']['tests'],2)
        path.write_text('<testsuites><testsuite tests="2" skipped="1"/></testsuites>')
        with self.assertRaises(h.Blocked):h.test_summary(self.root)

    def test_stale_reports_removed_before_generic_checks(self):
        path=self.root/'.harness/reports/junit.xml';path.parent.mkdir(parents=True);path.write_text('<testsuite tests="5"/>')
        cfg=h.config();cfg['checks']=[{'command':[sys.executable,'-c','pass']}];self.write_config(cfg)
        with self.assertRaises(h.Blocked):h.adapter_call('checks',self.s)
        self.assertFalse(path.exists())

    def test_artefact_directory_and_symlink_escape(self):
        path=self.root/'dist';path.mkdir();(path/'index.html').write_text('old')
        before=h.artifact_version(self.root,['dist'])
        (path/'index.html').write_text('new')
        self.assertNotEqual(before,h.artifact_version(self.root,['dist']))
        (path/'outside').symlink_to('/etc/passwd')
        with self.assertRaises(h.Blocked):h.artifact_version(self.root,['dist'])

    def test_changed_job_config_cannot_restart_environment(self):
        self.s['configVersion']='different'
        with self.assertRaises(h.Blocked):h.verify(self.s)

    def test_installer_preserves_instructions_and_excludes_state(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        (target/'CLAUDE.md').write_text('Keep my rules\n')
        (target/'.gitignore').write_text('node_modules/\n')
        (target/'LICENSE').write_text('Project-specific license\n')
        installer.install(target,'web-no-db')
        installer.install(target,'web-no-db')
        self.assertTrue((target/'CLAUDE.md').read_text().startswith('Keep my rules'))
        self.assertEqual((target/'.gitignore').read_text().count('.harness/'),1)
        self.assertFalse((target/'.harness').exists())
        self.assertFalse((target/'harness/__pycache__').exists())
        self.assertEqual((target/'LICENSE').read_text(),'Project-specific license\n')
        self.assertEqual((target/'harness/LICENSE').read_bytes(),(installer.BUNDLE/'LICENSE').read_bytes())
        self.assertEqual(json.loads((target/'harness/config.json').read_text())['database']['provider'],'none')

    def test_install_conflict_aborts_before_writing(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        (target/'harness').mkdir();(target/'harness/config.json').write_text('existing')
        with self.assertRaises(ValueError):installer.install(target,'web-no-db')
        self.assertFalse((target/'harness/harness.py').exists())
        self.assertEqual((target/'harness/config.json').read_text(),'existing')

    def test_cycle_limit_is_five_and_update_changes_only_on_explicit_request(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        installer.install(target,'web-no-db')
        path=target/'harness/config.json';cfg=json.loads(path.read_text())
        self.assertEqual(cfg['maxCorrectionCycles'],5)
        cfg['maxCorrectionCycles']=3;path.write_text(json.dumps(cfg))
        installer.install(target,'auto',update=True)
        self.assertEqual(json.loads(path.read_text())['maxCorrectionCycles'],3)
        installer.install(target,'auto',update=True,max_cycles=5)
        cfg['maxCorrectionCycles']=5
        self.assertEqual(json.loads(path.read_text()),cfg)

    def test_update_blocks_active_jobs_before_changing_files(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        installer.install(target,'auto')
        code=target/'harness/harness.py';code.write_text('old controller')
        state=target/'.harness/runs/active/state.json';state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'stage':'qa'}))
        template=target/'specs/plantilla-spec.md';template.write_text('Local active template\n')
        with self.assertRaises(ValueError):installer.install(target,'auto',dry_run=True,update=True)
        with self.assertRaises(ValueError):installer.install(target,'auto',update=True)
        self.assertEqual(code.read_text(),'old controller')
        self.assertEqual(template.read_text(),'Local active template\n')
        self.assertFalse((target/'.harness/install-backups').exists())

    def test_update_preserves_local_config_and_backs_up_controller(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        installer.install(target,'auto')
        cfg=target/'harness/config.json';cfg.write_text('custom configuration')
        permissions=target/'harness/claude-settings.json';permissions.write_text('custom permissions')
        code=target/'harness/harness.py';code.write_text('old controller')
        installer.install(target,'auto',dry_run=True,update=True)
        self.assertEqual(code.read_text(),'old controller')
        self.assertFalse((target/'.harness').exists())
        installer.install(target,'auto',update=True)
        self.assertEqual(cfg.read_text(),'custom configuration')
        self.assertEqual(permissions.read_text(),'custom permissions')
        self.assertEqual(code.read_bytes(),existing.MODULE.read_bytes())
        backups=list((target/'.harness/install-backups').glob('*/harness/harness.py'))
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_text(),'old controller')

    def test_update_isolates_legacy_report_only_after_guard_and_with_backup(self):
        target=self.root/'destination';target.mkdir()
        subprocess.run(['git','init','-q',str(target)],check=True)
        installer.install(target,'auto')
        path=target/'harness/config.json';cfg=json.loads(path.read_text())
        cfg['checks'][0]['command']=['npm','run','test','--','--outputFile.junit=test-results/harness-unit.xml']
        cfg['testReports']['tests']='test-results/harness-unit.xml'
        path.write_text(json.dumps(cfg));before=path.read_bytes()
        installer.install(target,'auto',dry_run=True,update=True)
        self.assertEqual(path.read_bytes(),before)
        installer.install(target,'auto',update=True)
        after=json.loads(path.read_text());self.assertEqual(after['testReports']['tests'],'.harness/reports/harness-unit.xml')
        self.assertEqual(after['database'],cfg['database'])
        backups=list((target/'.harness/install-backups').glob('*/harness/config.json'))
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),before)
        installer.install(target,'auto',update=True)
        self.assertEqual(len(list((target/'.harness/install-backups').glob('*/harness/config.json'))),1)


if __name__=='__main__':unittest.main()
