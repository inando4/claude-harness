import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
import test_harness as core
import test_portability as portable
import test_setup as setup_tests

h = core.h
SID = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


def module(name):
    spec = importlib.util.spec_from_file_location(name, core.MODULE.parent/(name+'.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


chat = module('conversation')
finalization = module('finalization')


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.HarnessTests(); self.fixture.setUp()
        self.s = self.fixture.s; self.ctx = h.CoreContext()
        self.s.update(conversation={'sessionId': SID, 'mode': 'inline', 'owned': False},
                      sessionId=SID, stage='awaiting_human_approval', approvedCandidate='version', specs=['specs/feature.md'])
        h.save(self.s, 'prepared')
        self.version = patch.object(h, 'version', return_value='version'); self.version.start()
        self.preview = patch.object(h, 'preview_valid'); self.preview.start()
        shutil.copy2(core.MODULE.parent/'conversation.py', self.fixture.root/'harness/conversation.py')

    def tearDown(self):
        self.preview.stop(); self.version.stop(); self.fixture.tearDown()

    def event(self, text, sid=SID, kind='UserPromptSubmit'):
        return {'hook_event_name': kind, 'session_id': sid, 'prompt': text}

    def request(self):
        return chat.request(self.ctx, self.s)

    def test_explicit_chat_approval_runs_same_validation_without_tty(self):
        req = self.request()
        chat.prompt(self.ctx, self.event(req['reply']))
        s = h.load(self.s['id'])
        self.assertEqual(s['stage'], 'awaiting_human_approval')
        with patch('sys.stdin.isatty', return_value=False): chat.accept(self.ctx, s)
        self.assertEqual(s['stage'], 'documenting')
        self.assertEqual(s['approval']['version'], 'version')

    def test_ok_question_answer_and_tool_event_never_approve(self):
        req = self.request()
        for text in ['ok', 'sí', 'APROBAR', req['reply']+' pero corrige algo', 'Texto '+req['reply']]:
            self.assertIsNone(chat.prompt(self.ctx, self.event(text)))
        chat.prompt(self.ctx, self.event(req['reply'], kind='PostToolUse'))
        with self.assertRaises(h.Blocked): chat.accept(self.ctx, h.load(self.s['id']))

    def test_other_session_and_wrong_token_rejected(self):
        req = self.request()
        for event in [self.event(req['reply'], OTHER), self.event('APROBAR 00000000')]:
            with self.assertRaises(h.Blocked): chat.prompt(self.ctx, event)
        self.assertNotIn('userConsent', h.load(self.s['id']))

    def test_request_is_idempotent_and_changed_version_invalidates_consent(self):
        req = self.request(); self.assertEqual(req, self.request())
        chat.prompt(self.ctx, self.event(req['reply']))
        s = h.load(self.s['id'])
        with patch.object(h, 'version', return_value='changed'):
            with self.assertRaises(h.Blocked): chat.accept(self.ctx, s)
        self.assertNotIn('approval', s)
        s['approvedCandidate'] = 'changed'
        with patch.object(h, 'version', return_value='changed'): new = chat.request(self.ctx, s)
        self.assertNotEqual(req['reply'], new['reply'])
        self.assertNotIn('userConsent', s)

    def test_invalid_preview_still_blocks_chat_approval(self):
        req = self.request(); chat.prompt(self.ctx, self.event(req['reply']))
        s = h.load(self.s['id'])
        with patch.object(h, 'preview_valid', side_effect=h.Blocked('invalidated')):
            with self.assertRaises(h.Blocked): chat.accept(self.ctx, s)
        self.assertNotIn('approval', s)

    def test_feedback_clears_old_consent_even_if_version_is_unchanged(self):
        req = self.request(); chat.prompt(self.ctx, self.event(req['reply']))
        s = h.load(self.s['id']); h.feedback(s, 'corregir')
        self.assertNotIn('userConsent', s); self.assertNotIn('reviewRequest', s)
        with self.assertRaises(h.Blocked): chat.prompt(self.ctx, self.event(req['reply']))

    def test_consumed_confirmation_cannot_be_replayed(self):
        req = self.request(); chat.prompt(self.ctx, self.event(req['reply']))
        chat.accept(self.ctx, h.load(self.s['id']))
        with self.assertRaises(h.Blocked): chat.prompt(self.ctx, self.event(req['reply']))

    def test_hook_readiness_is_session_specific(self):
        with self.assertRaises(h.Blocked): chat.ready(self.ctx, SID)
        chat.prompt(self.ctx, self.event('/harness specs/feature.md'))
        chat.ready(self.ctx, SID)
        with self.assertRaises(h.Blocked): chat.ready(self.ctx, OTHER)

    def test_browser_lease_requires_explicit_current_qa_job(self):
        browser = module('browser_mcp'); lease=browser.Lease(self.ctx)
        with self.assertRaises(h.Blocked):lease.acquire(self.s['id'],'v1')
        self.s.update(stage='qa',qaVersion='v1');h.save(self.s,'qa_started')
        with patch.object(h,'require_job_config'):
            artifacts=lease.acquire(self.s['id'],'v1')
        self.assertEqual(artifacts,h.jobdir(self.s['id'])/'artifacts')
        lease.release()
        self.assertFalse((h.STORE/'browser.json').exists())

    def test_inline_cleanup_keeps_user_session_and_requires_handoff(self):
        self.s['publication'] = {'handoff': True}
        with patch.object(h, 'run') as run:
            finalization.session_stop(self.ctx, self.s)
            finalization.session_stop(self.ctx, self.s)
        run.assert_not_called()
        self.assertTrue(self.s['publication']['sessionPreserved'])
        self.s['publication']['handoff'] = False
        with self.assertRaises(h.Blocked): finalization.session_stop(self.ctx, self.s)

    def test_inline_job_cannot_be_launched_or_kill_another_session(self):
        with patch.object(h, 'run') as run:
            with self.assertRaises(h.Blocked): h.launch(self.s, True)
            self.s.update(backgroundId='another', publication={'handoff': True})
            with self.assertRaises(h.Blocked): finalization.session_stop(self.ctx, self.s)
        run.assert_not_called()

    def test_repeated_start_recovers_same_job_without_prepare_or_launch(self):
        chat.prompt(self.ctx, self.event('/harness specs/feature.md'))
        args = argparse.Namespace(session_id=SID, spec=['specs/feature.md'], launch=False)
        with patch.object(h, 'prepare') as prepare, patch.object(h, 'launch') as launch, patch.object(h, 'brief', return_value={}), patch('builtins.print'):
            setup_tests.setup.start(self.ctx, args)
        prepare.assert_not_called(); launch.assert_not_called()
        args.spec = ['specs/another.md']
        with self.assertRaises(h.Blocked): setup_tests.setup.start(self.ctx, args)
        args.spec = ['specs/feature.md', 'specs/another.md']
        with self.assertRaises(h.Blocked): setup_tests.setup.start(self.ctx, args)


class InstallConversationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.HarnessTests(); self.fixture.setUp()
        self.target = self.fixture.root/'project with spaces'; self.target.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.target)], check=True)

    def tearDown(self): self.fixture.tearDown()

    def test_install_merges_hook_once_and_preserves_user_settings(self):
        path = self.target/'.claude/settings.local.json'; path.parent.mkdir()
        original = {'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'echo existing'}]}]},
                    'permissions': {'allow': ['Bash(npm test)']}, 'model': 'user-choice'}
        path.write_text(json.dumps(original))
        portable.installer.install(self.target, 'auto', dry_run=True)
        self.assertEqual(json.loads(path.read_text()), original)
        portable.installer.install(self.target, 'auto')
        portable.installer.install(self.target, 'auto', update=True)
        value = json.loads(path.read_text())
        self.assertEqual(value['model'], 'user-choice')
        self.assertEqual(value['permissions']['allow'], original['permissions']['allow'])
        self.assertEqual(len(value['hooks']['UserPromptSubmit']), 2)
        agent = (self.target/'.claude/agents/harness-qa.md').read_text()
        self.assertIn(json.dumps(str(self.target/'harness/browser_mcp.py')), agent)
        command = value['hooks']['UserPromptSubmit'][1]['hooks'][0]['command']
        # Run the installed hook as Claude does, with a real pipe and spaced path.
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': SID, 'prompt': '/harness specs/one.md'}
        result = subprocess.run(command, shell=True, input=json.dumps(event), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target/'.harness/conversations'/SID/'hook.json').is_file())

    def test_browser_is_pinned_isolated_and_headed(self):
        browser = module('browser_mcp')
        args = browser.command({'playwrightMcpVersion': '0.0.80'})
        self.assertIn('@playwright/mcp@0.0.80', args)
        self.assertIn('--isolated', args); self.assertIn('chrome', args)
        self.assertNotIn('--headless', args)
        with self.assertRaises(ValueError): browser.command({'playwrightMcpVersion': 'latest'})


if __name__ == '__main__': unittest.main()
