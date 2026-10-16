import argparse
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


class SpecReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = portable.PortabilityTests(); self.fixture.setUp()
        self.root = self.fixture.root
        self.ctx = h.CoreContext()
        self.mod = h.runtime_module('spec_readiness')
        self.chat = h.runtime_module('conversation')
        self.path = self.root / 'specs/feature.md'
        self.path.write_text('# Feature\n\nEstado: borrador\n\n## Criterios\nAC-1: initial behavior.\n')

    def tearDown(self):
        self.fixture.tearDown()

    def event(self, text, sid=SID, kind='UserPromptSubmit'):
        return {'hook_event_name': kind, 'session_id': sid, 'prompt': text}

    def request(self, sid=SID):
        return self.mod.request(self.ctx, 'specs/feature.md', sid)

    def approve(self):
        req = self.request()
        self.chat.prompt(self.ctx, self.event(req['reply']))
        return self.mod.accept(self.ctx, 'specs/feature.md', SID)

    def args(self):
        return argparse.Namespace(job='new-spec', branch='feat/new-spec', base='HEAD',
                                  spec=['specs/feature.md'], launch=False, session_id=None,
                                  safe_snapshot=False, snapshot=None, schema=None, test_account=None)

    def test_draft_blocks_both_start_and_prepare_before_provisioning(self):
        for invoke in (lambda: setup_tests.setup.start(self.ctx, self.args()), lambda: h.prepare(self.args())):
            with patch.object(h, 'run') as run, patch.object(h, 'create_clone') as clone, patch.object(h, 'graph') as graph:
                with self.assertRaisesRegex(h.Blocked, 'borrador'): invoke()
                run.assert_not_called(); clone.assert_not_called(); graph.assert_not_called()
        self.assertFalse(h.jobdir('new-spec').exists())

    def test_manual_ready_text_never_authorizes_a_spec(self):
        self.path.write_text(self.path.read_text().replace('Estado: borrador', 'Estado: lista para implementar'))
        status = self.mod.inspect(self.ctx, 'specs/feature.md')
        self.assertFalse(status['authorized']); self.assertEqual(status['status'], 'borrador')
        with self.assertRaisesRegex(h.Blocked, 'autorización vigente'):
            self.mod.require_all(self.ctx, ['specs/feature.md'])

    def test_explicit_hook_consent_marks_ready_without_starting_a_job(self):
        before = sorted((h.STORE / 'runs').glob('*/state.json'))
        req = self.request()
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)
        with patch.object(h, 'prepare') as prepare, patch.object(h, 'launch') as launch:
            self.chat.prompt(self.ctx, self.event(req['reply']))
            self.assertIn('Estado: borrador', self.path.read_text())
            result = self.mod.accept(self.ctx, 'specs/feature.md', SID)
            prepare.assert_not_called(); launch.assert_not_called()
        self.assertTrue(result['authorized'])
        self.assertIn('Estado: lista para implementar', self.path.read_text())
        self.assertEqual(self.mod.accept(self.ctx, 'specs/feature.md', SID), result)
        self.assertEqual(before, sorted((h.STORE / 'runs').glob('*/state.json')))
        proof = self.mod.require_all(self.ctx, [str(self.path)])['specs/feature.md']
        self.assertEqual(proof['sessionId'], SID)
        self.assertEqual(proof['documentSha256'], h.sha(self.path))
        # An approved document is usable by another implementation session.
        self.assertIsNone(self.request(OTHER)['reply'])

    def test_questions_other_events_wrong_tokens_and_sessions_do_not_authorize(self):
        req = self.request()
        for text in ('ok', 'sí', 'LISTA PARA IMPLEMENTAR', req['reply'] + ' pero cambia algo', 'Texto ' + req['reply']):
            self.assertIsNone(self.chat.prompt(self.ctx, self.event(text)))
        self.assertIsNone(self.chat.prompt(self.ctx, self.event(req['reply'], kind='PostToolUse')))
        with self.assertRaises(h.Blocked): self.chat.prompt(self.ctx, self.event(req['reply'], sid=OTHER))
        with self.assertRaises(h.Blocked): self.chat.prompt(self.ctx, self.event('LISTA PARA IMPLEMENTAR 00000000'))
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)

    def test_edits_before_confirmation_and_after_consent_require_new_review(self):
        req = self.request()
        self.path.write_text(self.path.read_text() + 'AC-2: changed before confirmation.\n')
        with self.assertRaisesRegex(h.Blocked, 'cambió'): self.chat.prompt(self.ctx, self.event(req['reply']))
        new = self.request(); self.assertNotEqual(req['reply'], new['reply'])
        self.chat.prompt(self.ctx, self.event(new['reply']))
        self.path.write_text(self.path.read_text() + 'AC-3: changed after confirmation.\n')
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)
        self.assertIn('Estado: borrador', self.path.read_text())

    def test_edit_after_approval_invalidates_receipt_and_new_review_resets_draft(self):
        old = self.approve()
        self.path.write_text(self.path.read_text() + '\nNew requirement.\n')
        self.assertFalse(self.mod.inspect(self.ctx, 'specs/feature.md')['authorized'])
        with self.assertRaises(h.Blocked): self.mod.require_all(self.ctx, ['specs/feature.md'])
        req = self.request()
        self.assertIn('Estado: borrador', self.path.read_text())
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)
        self.chat.prompt(self.ctx, self.event(req['reply']))
        new = self.mod.accept(self.ctx, 'specs/feature.md', SID)
        self.assertNotEqual(old['version'], new['version'])

    def test_explicit_draft_revokes_previous_consent_and_never_reuses_it(self):
        req = self.request()
        self.chat.prompt(self.ctx, self.event(req['reply']))
        self.mod.accept(self.ctx, 'specs/feature.md', SID)
        self.mod.draft(self.ctx, 'specs/feature.md')
        with self.assertRaises(h.Blocked): self.chat.prompt(self.ctx, self.event(req['reply']))
        fresh = self.request(); self.assertNotEqual(req['reply'], fresh['reply'])
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)

    def test_manual_return_to_draft_also_requires_fresh_confirmation(self):
        old = self.request()
        self.chat.prompt(self.ctx, self.event(old['reply']))
        self.mod.accept(self.ctx, 'specs/feature.md', SID)
        self.path.write_text(self.path.read_text().replace('Estado: lista para implementar', 'Estado: borrador'))
        self.assertNotEqual(self.request()['reply'], old['reply'])
        with self.assertRaises(h.Blocked): self.mod.accept(self.ctx, 'specs/feature.md', SID)

    def test_requests_are_idempotent_but_new_session_invalidates_old_request(self):
        req = self.request(); self.assertEqual(req, self.request())
        new = self.request(OTHER); self.assertNotEqual(req['reply'], new['reply'])
        with self.assertRaises(h.Blocked): self.chat.prompt(self.ctx, self.event(req['reply']))
        self.chat.prompt(self.ctx, self.event(new['reply'], sid=OTHER))
        self.assertTrue(self.mod.accept(self.ctx, 'specs/feature.md', OTHER)['authorized'])

    def test_active_job_prevents_changing_spec_readiness(self):
        self.approve()
        job = self.fixture.s; job.update(specs=['specs/feature.md'], stage='blocked')
        h.save(job, 'active')
        before = self.path.read_bytes()
        for action in (self.request, lambda: self.mod.accept(self.ctx, 'specs/feature.md', SID),
                       lambda: self.mod.draft(self.ctx, 'specs/feature.md')):
            with self.assertRaisesRegex(h.Blocked, 'trabajo abierto'): action()
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_status_defaults_to_draft_and_markdown_body_examples_are_not_metadata(self):
        self.path.write_text('# Example\n\n## Code\n```text\nEstado: lista para implementar\n```\n')
        self.assertEqual(self.mod.inspect(self.ctx, 'specs/feature.md')['status'], 'borrador')
        self.assertTrue(self.approve()['authorized'])
        self.assertIn('```text\nEstado: lista para implementar\n```', self.path.read_text())
        self.assertEqual(self.path.read_text().count('Estado: lista para implementar'), 2)

    def test_bold_status_is_supported_but_duplicate_and_finished_headers_are_rejected(self):
        self.path.write_text('# Example\n\n**Estado**: **BORRADOR**\n\n## AC\nAC-1: example.\n')
        self.assertTrue(self.approve()['authorized'])
        self.path.write_text('# Example\nEstado: borrador\nEstado: lista para implementar\n')
        with self.assertRaises(h.Blocked): self.request()
        self.path.write_text('# Example\nEstado: TERMINADO\n')
        with self.assertRaises(h.Blocked): self.request()

    def test_non_markdown_outside_and_linked_specs_are_rejected(self):
        other = self.root / 'specs/linked.md'; other.symlink_to(self.path)
        for value in ('../outside.md', str(self.root.parent / 'outside.md'), 'specs/linked.md'):
            with self.assertRaises(h.Blocked): self.mod.request(self.ctx, value, SID)

    def test_worktree_copy_must_match_authorized_version(self):
        self.approve()
        proof = self.mod.require_all(self.ctx, ['specs/feature.md'])
        tree = self.root / 'prepared'; (tree / 'specs').mkdir(parents=True)
        copied = tree / 'specs/feature.md'; shutil.copy2(self.path, copied)
        job = {'worktree': str(tree), 'specReadiness': proof}
        self.mod.verify_copy(self.ctx, job)
        copied.write_text(copied.read_text() + 'Late source edit.\n')
        with self.assertRaisesRegex(h.Blocked, 'versión autorizada'): self.mod.verify_copy(self.ctx, job)


class SpecInstallationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.HarnessTests(); self.fixture.setUp()
        self.target = self.fixture.root / 'installed project'; self.target.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.target)], check=True)

    def tearDown(self): self.fixture.tearDown()

    def test_template_policy_and_hook_are_installed_without_overwriting_local_template(self):
        path = self.target / 'specs/plantilla-spec.md'; path.parent.mkdir()
        original = '# Custom template\nEstado: lista para implementar | borrador\n\n## Local\nKeep this section.\n'
        path.write_text(original)
        portable.installer.install(self.target, 'auto', dry_run=True)
        self.assertEqual(path.read_text(), original)
        portable.installer.install(self.target, 'auto')
        expected = original.replace('lista para implementar | borrador', 'borrador')
        self.assertEqual(path.read_text(), expected)
        portable.installer.install(self.target, 'auto', update=True)
        self.assertEqual(path.read_text(), expected)
        self.assertEqual((self.target / 'CLAUDE.md').read_text().count('## Autorización de specs'), 1)
        spec = self.target / 'specs/new feature.md'; spec.write_text(expected)
        cli = ['python3', '-B', str(self.target / 'harness/harness.py')]
        request = subprocess.run(cli + ['spec-review', str(spec), '--session-id', SID], text=True, capture_output=True)
        self.assertEqual(request.returncode, 0, request.stderr)
        req = json.loads(request.stdout)
        denied = subprocess.run(cli + ['spec-accept', str(spec), '--session-id', SID], text=True, capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        hook = subprocess.run(['python3', '-B', str(self.target / 'harness/conversation.py')],
                              input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': SID, 'prompt': req['reply']}),
                              text=True, capture_output=True)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        accepted = subprocess.run(cli + ['spec-accept', str(spec), '--session-id', SID], text=True, capture_output=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(json.loads(accepted.stdout)['authorized'])
        self.assertFalse((self.target / '.harness/runs').exists())


if __name__ == '__main__': unittest.main()
