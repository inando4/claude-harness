"""Ready specs, tracked in the base or new, close through one clean PR commit."""
import sys
import unittest
from unittest.mock import patch

import test_publication as publication

h = publication.h
SPEC = 'specs/Ready/closure.md'
DONE = 'specs/Done/closure.md'
TEXT = '# Cierre de la spec\nEstado: lista para implementar\n\n' + ''.join(
    f'- AC-{n}: criterio observable número {n}\n' for n in range(1, 9))


class SpecClosureTests(unittest.TestCase):
    def setUp(self):
        self.fx = publication.PublicationTests(); self.fx.setUp()
        self.ctx, self.s = h.CoreContext(), self.fx.s
        self.mod = h.runtime_module('spec_status')

    def tearDown(self): self.fx.tearDown()

    def stage(self, tracked, move=True):
        """A tracked spec is committed in the published base; a new one only exists on disk."""
        root, back = self.fx.root, self.fx.back
        (root / SPEC).parent.mkdir(parents=True, exist_ok=True); (root / SPEC).write_text(TEXT)
        if tracked:
            self.fx.git(root, 'add', '--', SPEC); self.fx.git(root, 'commit', '-qm', 'Authorize spec')
            self.fx.git(root, 'push', '-q', 'origin', 'main'); self.fx.git(back, 'merge', '-q', '--ff-only', 'main')
            base = self.fx.git(root, 'rev-parse', 'HEAD')
            self.s['repositories']['back']['base'] = self.s['publication']['repos']['back']['base'] = base
        else:
            (back / SPEC).parent.mkdir(parents=True, exist_ok=True); (back / SPEC).write_text(TEXT)
        self.s['specs'] = [SPEC]; self.mod.capture(self.ctx, self.s)
        if move: self.mod.stage_tracked(self.ctx, self.s)

    def close(self, tracked, local):
        self.stage(tracked); self.fx.refreeze(); self.fx.publish()
        files = self.fx.git(self.fx.back, 'ls-tree', '-r', '--name-only', 'HEAD', '--', 'specs').splitlines()
        self.assertIn(DONE, files); self.assertNotIn(SPEC, files)
        self.assertIn('Estado: TERMINADO', self.fx.git(self.fx.back, 'show', 'HEAD:' + DONE))
        self.assertTrue(self.fx.clean.tick(self.ctx, self.s))
        self.assertEqual(self.s['specCompletion'][SPEC]['local'], local)

    def test_tracked_spec_leaves_ready_in_the_commit_and_cleanup_completes(self):
        self.close(True, 'tracked_pull_required')

    def test_new_spec_leaves_ready_in_the_commit_and_cleanup_completes(self):
        self.close(False, 'removed_after_merge')

    def pr_title(self, tracked):
        self.stage(tracked)
        github = h.runtime_module('github'); commands = []
        self.s['publication']['repos']['back']['github'] = {'repository': 'owner/repo', 'base': 'main'}
        listed = lambda ctx, endpoint, key: [{'number': 7}] if commands else []
        with patch.object(github, 'pages', side_effect=listed), patch.object(github, 'validate_pr'), \
                patch.object(github, 'api', return_value={'number': 7, 'html_url': 'https://example.invalid/7'}), \
                patch.object(h, 'run', side_effect=lambda argv, **kwargs: commands.append(argv)):
            self.assertEqual(github.ensure_pr(self.ctx, self.s, 'back')['number'], 7)
        self.assertEqual(commands[0][commands[0].index('--title') + 1], 'Cierre de la spec')

    def test_pr_reads_tracked_spec_from_done(self): self.pr_title(True)

    def test_pr_reads_new_spec_from_done(self): self.pr_title(False)

    def fingerprint(self, tracked):
        self.stage(tracked, move=False)
        module = h.verification_module()
        step = {'id': 'unit', 'command': [sys.executable], 'tools': [sys.executable], 'usesDatabase': False}
        before = module.components(self.ctx, self.s, step)['specs']
        self.mod.stage_tracked(self.ctx, self.s)
        self.assertEqual(module.components(self.ctx, self.s, step)['specs'], before)
        (self.fx.back / DONE).write_text(TEXT + '- AC-9: criterio nuevo\n')
        self.assertNotEqual(module.components(self.ctx, self.s, step)['specs'], before)

    def test_verify_keeps_receipts_after_moving_tracked_spec(self): self.fingerprint(True)

    def test_verify_keeps_receipts_after_moving_new_spec(self): self.fingerprint(False)

    def cut_front_commit(self):
        pub, front = self.fx.pub, self.s['publication']['repos']['front']
        original = pub.unchanged
        def cut(ctx, s):
            if self.fx.git(self.fx.web, 'rev-parse', 'HEAD') != front['headBefore']: raise h.Blocked('Corte simulado')
            return original(ctx, s)
        with patch.object(pub, 'unchanged', side_effect=cut), self.assertRaises(h.Blocked): self.fx.publish()
        return front

    def test_cut_between_repository_commits_keeps_index_publishable(self):
        front = self.cut_front_commit()
        self.assertEqual(self.fx.git(self.fx.web, 'diff', '--cached', '--name-only', 'HEAD'), '')
        self.fx.publish(); self.assertEqual(front['commit'], self.fx.git(self.fx.web, 'rev-parse', 'HEAD'))

    def test_stale_index_after_older_cut_is_realigned_only_when_lossless(self):
        front = self.cut_front_commit()
        self.fx.git(self.fx.web, 'read-tree', front['headBefore'])
        (self.fx.web / 'app.txt').write_text('edited after the commit')
        with self.assertRaises(h.Blocked): self.fx.publish()
        self.assertEqual(self.fx.git(self.fx.web, 'diff', '--cached', '--name-only', 'HEAD'), 'app.txt')
        (self.fx.web / 'app.txt').write_text('approved frontend')
        self.fx.publish(); self.assertEqual(front['commit'], self.fx.git(self.fx.web, 'rev-parse', 'HEAD'))

    def finish(self):
        self.s['stage'] = 'documenting'
        for item in self.s['publication']['repos'].values():
            for key in ('tree', 'inventory', 'headBefore'): item.pop(key, None)
        with patch.object(h, 'finalization_module', return_value=self.fx.clean), patch.object(self.fx.clean, 'start'):
            h.finish(self.s)

    def test_successful_retry_clears_previous_publication_error(self):
        self.s.update(qa={'status': 'pass', 'criteria': [], 'artifacts': []}, approvedCandidate='approved')
        h.atomic(h.jobdir(self.s['id']) / 'qa.json', self.s['qa'])
        self.s['approvalEvidence'] = self.fx.pub.evidence(self.ctx, self.s)
        self.fx.fake.error_repo = 'front'
        with self.assertRaises(h.Blocked): self.finish()
        self.assertEqual(self.s['blockedPhase'], 'publishing'); self.assertTrue(h.diagnostic(self.s)['publication']['error'])
        self.fx.fake.error_repo = None
        with patch.object(h, 'finalization_module', return_value=self.fx.clean), patch.object(self.fx.clean, 'start'):
            h.finish(self.s)
        for shown in (h.diagnostic(self.s), h.diagnostic(h.load(self.s['id']))):
            self.assertIsNone(shown['blockedPhase']); self.assertIsNone(shown['publication']['error'])

    def test_finish_resolves_sibling_repository_links_from_the_checkout(self):
        (self.fx.back / 'docs/readme.md').write_text(f'[CRM](../../{self.fx.root.name}/.harness/front/app.txt)\n')
        self.finish(); self.assertEqual(self.s['stage'], 'awaiting_ci')

    def test_finish_still_blocks_missing_sibling_repository_links(self):
        (self.fx.back / 'docs/readme.md').write_text(f'[CRM](../../{self.fx.root.name}/.harness/front/missing.txt)\n')
        with self.assertRaises(h.Blocked): self.finish()
        self.assertEqual(self.s['stage'], 'documenting')


if __name__ == '__main__': unittest.main()
