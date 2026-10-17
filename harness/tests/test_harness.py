import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import socket
import shutil
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'harness.py'
spec = importlib.util.spec_from_file_location('harness', MODULE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = h.ROOT, h.STORE
        h.ROOT, h.STORE = self.root, self.root / '.harness'
        (self.root / 'harness').mkdir()
        shutil.copytree(MODULE.parent/'adapters', self.root/'harness/adapters')
        for name in ('browser_mcp.py','spec_status.py','spec_layout.py','qa_context.py','spec_readiness.py','conversation.py'):
            shutil.copy2(MODULE.parent/name,self.root/'harness'/name)
        shutil.copy2(MODULE.parent/'repositories.py', self.root/'harness/repositories.py')
        shutil.copy2(MODULE.parent/'preflight.py', self.root/'harness/preflight.py')
        shutil.copy2(MODULE.parent/'verification.py', self.root/'harness/verification.py')
        shutil.copy2(MODULE.parent/'synchronization.py', self.root/'harness/synchronization.py')
        shutil.copy2(MODULE.parent/'setup_project.py', self.root/'harness/setup_project.py')
        shutil.copy2(MODULE.parent/'test_selectors.py', self.root/'harness/test_selectors.py')
        shutil.copy2(MODULE.parent/'generated.py', self.root/'harness/generated.py')
        shutil.copy2(MODULE.parent/'execution.py', self.root/'harness/execution.py')
        shutil.copy2(MODULE.parent/'report_paths.py', self.root/'harness/report_paths.py')
        cfg = json.loads((MODULE.parent/'templates/sistema-juridico.json').read_text())
        cfg.update(timeoutSeconds=10, jar='target/app.jar')
        (self.root / 'harness/config.json').write_text(json.dumps(cfg))
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Harness Test')
        (self.root / '.gitignore').write_text('.harness/\ntarget/\nharness/**/__pycache__/\n')
        (self.root / 'src/main').mkdir(parents=True)
        (self.root / 'src/main/app.txt').write_text('initial')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/readme.md').write_text('Doc')
        (self.root / 'specs').mkdir()
        (self.root / 'specs/feature.md').write_text('AC-1: Example')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.s = {'id': 'test-one', 'stage': 'implementing', 'worktree': str(self.root),
                  'cycles': 0, 'container': 'local-only', 'containerId': 'id-1',
                  'resourceKey': 'key-1', 'volume': 'vol-1', 'database': 'test_db',
                  'schema': 'sistema_juridico', 'dbPort': 5555, 'httpPort': 8888,
                  'snapshotSha256': 'snapshot', 'sessionId': 'session', 'url': 'http://localhost:8888'}
        h.save(self.s, 'fixture')

    def tearDown(self):
        h.ROOT, h.STORE = self.old
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True).stdout.decode().strip()

    def authorize_spec(self, relative='specs/feature.md', sid='22222222-2222-4222-8222-222222222222'):
        readiness = h.runtime_module('spec_readiness')
        request = readiness.request(h.CoreContext(), relative, sid)
        h.runtime_module('conversation').prompt(h.CoreContext(), {
            'hook_event_name': 'UserPromptSubmit', 'session_id': sid, 'prompt': request['reply']})
        return readiness.accept(h.CoreContext(), relative, sid)

    def test_new_untracked_product_file_invalidates_version(self):
        before = h.fingerprint(self.root)
        (self.root / 'src/main/new.txt').write_text('new')
        self.assertNotEqual(before, h.fingerprint(self.root))

    def test_deleted_tracked_file_invalidates_version(self):
        before = h.fingerprint(self.root)
        (self.root / 'src/main/app.txt').unlink()
        self.assertNotEqual(before, h.fingerprint(self.root))

    def test_docs_changes_do_not_invalidate_product_but_do_change_full_version(self):
        product = h.fingerprint(self.root, True)
        full = h.fingerprint(self.root)
        (self.root / 'docs/readme.md').write_text('Updated')
        self.assertEqual(product, h.fingerprint(self.root, True))
        self.assertNotEqual(full, h.fingerprint(self.root))

    def test_symlink_change_detected_without_reading_external_target(self):
        target = self.root / 'src/main/link'
        target.symlink_to('/etc/passwd')
        before = h.fingerprint(self.root)
        target.unlink()
        target.symlink_to('/etc/hosts')
        self.assertNotEqual(before, h.fingerprint(self.root))

    def test_id_traversal_rejected(self):
        for value in ['../other', '/tmp', 'X', 'a/b', '']:
            with self.assertRaises(h.Blocked): h.jobdir(value)

    def test_atomic_state_has_private_permissions(self):
        path = h.jobdir(self.s['id']) / 'state.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(h.load(self.s['id'])['stage'], 'implementing')

    def test_original_database_env_not_inherited(self):
        with patch.dict(os.environ, {'SPRING_DATASOURCE_URL': 'jdbc:postgresql://production/real',
                                     'DB_MIGRATION_PASSWORD': 'secret', 'JAVA_TOOL_OPTIONS': 'bad'}):
            env = h.clean_env()
        self.assertNotIn('SPRING_DATASOURCE_URL', env)
        self.assertNotIn('DB_MIGRATION_PASSWORD', env)
        self.assertNotIn('JAVA_TOOL_OPTIONS', env)

    def docker_info(self):
        return [{'Id': 'id-1', 'State': {'Running': True}, 'Config': {'Labels': {h.LABEL: 'key-1'}},
                 'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5555'}]}},
                 'Mounts': [{'Name': 'vol-1'}]}]

    def test_remote_or_replaced_clone_rejected(self):
        for mutate in ('remote', 'replacement', 'label', 'port'):
            info = self.docker_info()
            if mutate == 'remote': info[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp'] = '0.0.0.0'
            if mutate == 'replacement': info[0]['Id'] = 'other'
            if mutate == 'label': info[0]['Config']['Labels'] = {}
            if mutate == 'port': info[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'] = '5432'
            with patch.object(h, 'run', return_value=json.dumps(info)):
                with self.assertRaises(h.Blocked): h.db_info(self.s)

    def test_sql_command_can_only_target_labelled_local_container(self):
        with patch.object(h, 'db_info'), patch.object(h, 'run', return_value='ok') as run:
            self.assertEqual(h.sql(self.s, 'SELECT 1'), 'ok')
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ['docker', 'exec', 'local-only'])
            self.assertNotIn('-h', command)

    def test_app_env_overrides_profile_with_loopback(self):
        h.atomic(h.jobdir(self.s['id']) / 'secrets.json', {'password': 'temporary', 'rateLimitKey': 'local'})
        with patch.object(h, 'db_info'):
            props = json.loads(h.app_env(self.s)['SPRING_APPLICATION_JSON'])
        self.assertEqual(props['spring.datasource.url'], 'jdbc:postgresql://127.0.0.1:5555/test_db?currentSchema=sistema_juridico')
        self.assertEqual(props['server.address'], '127.0.0.1')
        self.assertFalse(props['spring.flyway.enabled'])
        self.assertEqual(props['spring.flyway.password'], '')

    def test_listener_must_belong_to_preview_process(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(h.owns_listener(os.getpid(), port))
            self.assertFalse(h.owns_listener(99999999, port))

    def test_config_change_invalidates_qa_version(self):
        before = h.version(self.s)
        cfg = h.config(); cfg['healthPath'] = '/different'
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        self.assertNotEqual(before, h.version(self.s))

    def test_check_failure_consumes_limit_without_running_migrations(self):
        self.s['cycles'] = h.config()['maxCorrectionCycles']-1
        cfg = h.config(); cfg['checkCommand'] = ['false']
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        with patch.object(h, 'db_info'), patch.object(h, 'fingerprint', return_value='same'), \
             patch.object(h, 'run', side_effect=h.Blocked('tests failed')) as run:
            with self.assertRaises(h.Blocked): h.verify(self.s)
        self.assertEqual(self.s['stage'], 'paused')
        self.assertEqual(run.call_args.args[0], ['false'])
        self.assertNotIn('migrationAttempt', self.s)

    def test_active_session_prevents_duplicate_launch_or_cleanup(self):
        self.s['launched'] = True
        sessions = [{'sessionId': 'session', 'state': 'running'}]
        with patch.object(h, 'run', return_value=json.dumps(sessions)), \
             patch.object(h, 'launch_native') as launch:
            with self.assertRaises(h.Blocked): h.launch(self.s, True)
            with self.assertRaises(h.Blocked): h.require_no_active_session(self.s)
            launch.assert_not_called()

    def test_qa_failure_persists_findings_for_resumed_implementation(self):
        self.s.update(stage='qa', qaVersion=h.version(self.s))
        report = {'version': self.s['qaVersion'], 'status': 'fail', 'findings': [{'id': 'BUG-1'}]}
        path = h.jobdir(self.s['id'])/'report.json'
        h.atomic(path, report)
        with patch('builtins.print'): h.qa_result(self.s, path)
        saved = h.load(self.s['id'])
        self.assertEqual(saved['stage'], 'implementing')
        self.assertEqual(saved['qa']['findings'][0]['id'], 'BUG-1')

    def test_pass_then_human_approval_then_documentation(self):
        report = self.valid_report()
        self.s.update(stage='qa', qaVersion=h.version(self.s))
        report['version'] = self.s['qaVersion']
        path = h.jobdir(self.s['id'])/'report.json'
        h.atomic(path, report)
        with patch.object(h, 'preview_valid'), patch('builtins.print'):
            h.qa_result(self.s, path)
            with self.assertRaises(h.Blocked): h.finish(self.s)
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('builtins.input', return_value='APROBAR test-one'):
                h.approve(self.s, self.s['approvedCandidate'])
        (self.root/'docs/readme.md').write_text('Documentation updated')
        h.finish(self.s)
        self.assertEqual(self.s['stage'], 'completed')

    def valid_report(self):
        artifact = h.jobdir(self.s['id']) / 'artifacts/screen.png'
        artifact.parent.mkdir()
        artifact.write_bytes(b'example')
        (artifact.parent/'e2e.xml').write_text('<testsuite tests="1" failures="0"/>')
        self.s['qaVersion'] = 'v1'
        return {'version': 'v1', 'status': 'pass', 'findings': [], 'browser': 'chrome', 'headed': True,
                'visualInspection': True, 'migrationVerified': True,
                'criteria': [{'id': 'AC-1', 'status': 'pass', 'evidence': 'screen.png'}],
                'integration': 'checks.log', 'e2e': 'Playwright tests', 'artifacts': ['screen.png', 'e2e.xml'],
                'exploration': {'tool': 'playwright-mcp', 'scenarios': ['Invalid input'], 'evidence': ['screen.png']},
                'repeatableE2E': {'framework': 'playwright', 'runner': 'playwright-test',
                                 'command': ['npx', 'playwright', 'test'], 'testFiles': ['tests/form.spec.ts'],
                                 'exitCode': 0, 'passed': 1, 'report': 'e2e.xml'}}

    def test_qa_requires_both_exploration_and_repeatable_tests(self):
        report = self.valid_report()
        for field in ('exploration', 'repeatableE2E'):
            missing = dict(report); missing.pop(field)
            with self.subTest(field=field), self.assertRaises(h.Blocked): h.validate_report(missing, self.s)
        for update in ({'exitCode': 1}, {'passed': 0}, {'report': 'missing.xml'}):
            invalid = dict(report, repeatableE2E=dict(report['repeatableE2E'], **update))
            with self.subTest(update=update), self.assertRaises(h.Blocked): h.validate_report(invalid, self.s)

    def test_pass_requires_visual_evidence_and_no_findings(self):
        report = self.valid_report()
        h.validate_report(report, self.s)
        for field, value in [('headed', False), ('browser', 'chromium'), ('findings', ['bug']),
                             ('visualInspection', False), ('version', 'old'), ('criteria', [])]:
            with self.subTest(field=field), self.assertRaises(h.Blocked):
                h.validate_report(dict(report, **{field: value}), self.s)

    def test_report_evidence_cannot_escape_artifacts(self):
        report = self.valid_report()
        report['artifacts'] = ['../state.json']
        with self.assertRaises(h.Blocked): h.validate_report(report, self.s)

    def test_qa_start_does_not_reserve_shared_chrome(self):
        self.s.update(stage='ready_for_qa', checkedVersion=h.fingerprint(self.root))
        h.atomic(h.STORE / 'browser.json', {'job': 'other-job'})
        with patch.object(h, 'preview_valid'): h.qa_start(self.s)
        self.assertEqual(self.s['stage'],'qa')
        self.assertEqual(json.loads((h.STORE/'browser.json').read_text())['job'], 'other-job')

    def test_browser_release_does_not_release_other_job(self):
        h.atomic(h.STORE / 'browser.json', {'job': 'other-job'})
        h.release_browser(self.s)
        self.assertTrue((h.STORE/'browser.json').exists())

    def test_approval_requires_latest_exact_version(self):
        self.s.update(stage='awaiting_human_approval', approvedCandidate=h.version(self.s))
        (self.root/'src/main/app.txt').write_text('edited after QA')
        with patch.object(h, 'preview_valid'), self.assertRaises(h.Blocked):
            h.approve(self.s, self.s['approvedCandidate'], confirm=False)

    def test_approval_requires_human_terminal(self):
        self.s.update(stage='awaiting_human_approval', approvedCandidate=h.version(self.s))
        with patch.object(h, 'preview_valid'), patch('sys.stdin.isatty', return_value=False), self.assertRaises(h.Blocked):
            h.approve(self.s, self.s['approvedCandidate'])

    def test_approval_advances_only_after_explicit_confirmation(self):
        self.s.update(stage='awaiting_human_approval', approvedCandidate=h.version(self.s))
        with patch.object(h, 'preview_valid'), patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='APROBAR test-one'):
            h.approve(self.s, self.s['approvedCandidate'])
        self.assertEqual(h.load('test-one')['stage'], 'documenting')

    def test_question_answer_or_wrong_confirmation_does_not_approve(self):
        self.s.update(stage='awaiting_human_approval', approvedCandidate=h.version(self.s))
        with patch.object(h, 'preview_valid'), patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='opcion 1'), self.assertRaises(h.Blocked):
            h.approve(self.s, self.s['approvedCandidate'])

    def test_finish_refuses_product_edits(self):
        self.s.update(stage='documenting', approval={'version': 'v1'}, docsBaseline=h.inventory(self.root),
                      productBaseline=h.fingerprint(self.root, True))
        (self.root/'src/main/app.txt').write_text('unapproved')
        with self.assertRaises(h.Blocked): h.finish(self.s)

    def test_finish_refuses_harness_edits_even_when_fingerprint_ignores_them(self):
        self.s.update(stage='documenting', approval={'version': 'v1'}, docsBaseline=h.inventory(self.root),
                      productBaseline=h.fingerprint(self.root, True))
        cfg = json.loads((self.root/'harness/config.json').read_text()); cfg['maxJobs'] = 99
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        with self.assertRaises(h.Blocked): h.finish(self.s)

    def test_docs_finish_checks_links_and_keeps_services(self):
        self.s.update(stage='documenting', approval={'version': 'v1'}, docsBaseline=h.inventory(self.root),
                      productBaseline=h.fingerprint(self.root, True), preview={'pid': 123, 'start': '100'})
        (self.root/'docs/readme.md').write_text('[missing](absent.md)')
        with self.assertRaises(h.Blocked): h.finish(self.s)
        (self.root/'docs/readme.md').write_text('[spec](../specs/feature.md)')
        h.finish(self.s)
        self.assertEqual(self.s['stage'], 'completed')
        self.assertIn('preview', self.s)

    def test_feedback_invalidates_approval_and_pauses_at_limit(self):
        self.s.update(cycles=h.config()['maxCorrectionCycles']-1, stage='awaiting_human_approval', approval={'version': 'v1'})
        h.feedback(self.s, 'correct this')
        self.assertEqual(self.s['stage'], 'paused')
        self.assertNotIn('approval', self.s)

    def test_missing_backup_never_touches_docker_or_git(self):
        a = argparse.Namespace(safe_snapshot=True, snapshot=str(self.root/'missing.dump'))
        with patch.object(h, 'run') as run, self.assertRaises(h.Blocked): h.prepare(a)
        run.assert_not_called()

    def test_cleanup_refuses_replaced_container(self):
        info = self.docker_info(); info[0]['Id'] = 'someone-else'
        with patch.object(h, 'run', return_value=json.dumps(info)) as run, self.assertRaises(h.Blocked):
            h.remove_clone(self.s)
        self.assertEqual(run.call_count, 1)

    def test_old_test_reports_cannot_substitute_missing_integration(self):
        path = self.root/'target/surefire-reports/TEST-one.xml'
        path.parent.mkdir(parents=True)
        path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
        with self.assertRaises(h.Blocked): h.test_summary(self.root)
        path = self.root/'target/failsafe-reports/TEST-one.xml'
        path.parent.mkdir()
        path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="1"/>')
        with self.assertRaises(h.Blocked): h.test_summary(self.root)
        path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
        self.assertEqual(h.test_summary(self.root)['failsafe']['tests'], 1)

    def test_active_session_in_worktree_blocks_relaunch_despite_wrong_id(self):
        self.s['launched']=True
        active=[{'id':'actual','sessionId':'different','cwd':self.s['worktree'],'state':'blocked'}]
        with patch.object(h,'run',return_value=json.dumps(active)):
            with self.assertRaises(h.Blocked):h.require_no_active_session(self.s)

    def test_missing_background_id_blocks_without_claiming_success(self):
        result=subprocess.CompletedProcess([],0,'unexpected output','')
        with patch('subprocess.run',return_value=result):
            with self.assertRaises(h.Blocked):h.launch_native(self.s,True)
        self.assertNotIn('backgroundId',self.s)

    def test_launch_uses_native_claude_and_excludes_paid_api_environment(self):
        cfg = json.loads((self.root/'harness/config.json').read_text()); cfg['playwrightMcpVersion'] = '0.0.80'
        (self.root/'harness/config.json').write_text(json.dumps(cfg))
        session={'id':'abcd1234','sessionId':'real-session-id','cwd':str(self.root),'state':'blocked'}
        result=subprocess.CompletedProcess([],0,'backgrounded · abcd1234\n  claude attach abcd1234\n','')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'paid-secret'}), patch.object(h, 'run', return_value=json.dumps([session])), patch('subprocess.run', return_value=result) as call:
            h.launch(self.s, True)
        argv = call.call_args.args[0]
        self.assertIn('--bg', argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-opus-5')
        self.assertEqual(argv[argv.index('--effort') + 1], 'xhigh')
        self.assertEqual(call.call_args.kwargs['env']['CLAUDE_CODE_EFFORT_LEVEL'], 'xhigh')
        self.assertEqual(call.call_args.kwargs['env']['CLAUDE_CODE_SUBAGENT_MODEL'], 'claude-opus-5')
        self.assertNotIn('--session-id', argv)
        self.assertNotIn('-p', argv)
        self.assertFalse(argv[-1].startswith('/'))
        self.assertEqual(argv[-2], '--')
        self.assertEqual(self.s['sessionId'],'real-session-id')
        self.assertEqual(self.s['backgroundId'],'abcd1234')
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'project')
        self.assertNotIn('ANTHROPIC_API_KEY', call.call_args.kwargs['env'])
        mcp = json.loads((h.jobdir('test-one')/'mcp.json').read_text())
        args = mcp['mcpServers']['playwright']['args']
        self.assertEqual(args,[str(self.root/'harness/browser_mcp.py')])
        self.assertNotIn('--headless', args)

    def test_prompt_is_separated_from_variadic_options_in_all_launch_modes(self):
        frontend = self.root / 'frontend con espacios'
        session = {'id': 'abcd1234', 'sessionId': 'real-session-id', 'cwd': str(self.root)}
        result = subprocess.CompletedProcess([], 0, 'claude attach abcd1234\n', '')
        for background in (True, False):
            for resumed in (False, True):
                for two_repos in (False, True):
                    with self.subTest(background=background, resumed=resumed, two_repos=two_repos):
                        state = dict(self.s, launched=resumed, sessionId='original-session')
                        roots = {'backend': self.root}
                        if two_repos: roots['frontend'] = frontend
                        with patch.object(h, 'repository_roots', return_value=roots), \
                             patch.object(h, 'run', return_value=json.dumps([session])), \
                             patch('subprocess.run', return_value=result) as bg, \
                             patch('subprocess.call', return_value=0) as fg:
                            h.launch_native(state, background)
                        argv = (bg if background else fg).call_args.args[0]
                        # After --bg is removed, variadic options must still end before the prompt.
                        child_argv = [arg for arg in argv if arg != '--bg']
                        boundary = child_argv.index('--')
                        self.assertEqual(child_argv[boundary + 1:], [argv[-1]])
                        self.assertIn(state['id'], argv[-1])
                        if two_repos:
                            start = child_argv.index('--add-dir') + 1
                            directories = []
                            for arg in child_argv[start:]:
                                if arg.startswith('--'): break
                                directories.append(arg)
                            self.assertEqual(directories, [str(frontend)])
                        if resumed:
                            self.assertEqual(argv[argv.index('--resume') + 1], 'original-session')

    def test_idle_background_launch_keeps_session_for_recovery_and_does_not_retry(self):
        for ansi in (False, True):
            with self.subTest(ansi=ansi):
                notice = 'idle — send a prompt to start'
                if ansi: notice = '\x1b[2m' + notice + '\x1b[0m'
                output = f'backgrounded · abcd1234 ({notice})\n  claude attach abcd1234\n'
                result = subprocess.CompletedProcess([], 0, output, '')
                session = {'id': 'abcd1234', 'sessionId': 'real-session-id',
                           'cwd': str(self.root), 'state': 'blocked'}
                with patch.object(h, 'run', return_value=json.dumps([session])), \
                     patch('subprocess.run', return_value=result) as call, patch('builtins.print') as printed:
                    with self.assertRaises(h.Blocked) as caught:
                        h.launch_native(self.s, True)
                self.assertIn('claude attach abcd1234', str(caught.exception))
                self.assertIn(f'/harness test-one {self.root}', str(caught.exception))
                call.assert_called_once()
                printed.assert_not_called()
                saved = h.load('test-one')
                self.assertTrue(saved['launched'])
                self.assertEqual(saved['sessionId'], 'real-session-id')
                self.assertEqual(saved['backgroundId'], 'abcd1234')
                self.assertEqual(saved['cycles'], 0)
                self.assertEqual((h.jobdir('test-one')/'launch.log').read_text(), output)
                with patch.object(h, 'run', return_value=json.dumps([session])), \
                     patch('subprocess.run') as relaunch:
                    with self.assertRaises(h.Blocked): h.launch(saved, True)
                relaunch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
