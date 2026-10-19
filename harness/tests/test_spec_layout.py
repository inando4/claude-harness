from pathlib import Path
import unittest
from unittest.mock import patch

import test_spec_readiness as readiness
import test_publication as publication

h = readiness.h


class SpecLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fx = readiness.SpecReadinessTests(); self.fx.setUp()
        self.root, self.ctx, self.mod = self.fx.root, self.fx.ctx, self.fx.mod
        self.layout = h.runtime_module('spec_layout')

    def tearDown(self): self.fx.tearDown()

    def write(self, path, text):
        file = self.root / path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(text)
        return file

    def approve(self, path):
        req = self.mod.request(self.ctx, path, readiness.SID)
        self.fx.chat.prompt(self.ctx, self.fx.event(req['reply']))
        return self.mod.accept(self.ctx, req['spec'], readiness.SID)

    def test_bundle_moves_with_assets_and_both_ends_of_links(self):
        self.write('specs/topic/spec.md', '# Topic\nEstado: borrador\n\n![Image](assets/pic.svg)\n[Doc](../../docs/readme.md#intro)\n')
        image = self.write('specs/topic/assets/pic.svg', '<svg>original image</svg>')
        self.write('specs/topic/notes.md', '[Main](spec.md)\n[Ref]: ../../docs/readme.md\n')
        self.write('README.md', '[Topic](specs/topic/spec.md)\n![Asset](specs/topic/assets/pic.svg)\n')
        approved = self.approve('specs/topic/spec.md')
        self.assertEqual(approved['spec'], 'specs/Ready/topic/spec.md')
        self.assertFalse(image.exists())
        self.assertEqual((self.root / 'specs/Ready/topic/assets/pic.svg').read_text(), '<svg>original image</svg>')
        self.assertIn('(assets/pic.svg)', (self.root / approved['spec']).read_text())
        self.assertIn('../../../docs/readme.md#intro', (self.root / approved['spec']).read_text())
        self.assertIn('specs/Ready/topic/assets/pic.svg', (self.root / 'README.md').read_text())
        self.assertIn('[Main](spec.md)', (self.root / 'specs/Ready/topic/notes.md').read_text())
        draft = self.mod.draft(self.ctx, 'specs/topic/spec.md')
        self.assertEqual(draft['spec'], 'specs/Draft/topic/spec.md'); self.assertFalse(draft['authorized'])
        self.assertEqual(self.layout.input_files(self.ctx, [draft['spec']]),
                         ['specs/Draft/topic/assets/pic.svg', 'specs/Draft/topic/notes.md', draft['spec']])

    def test_incoming_rewrite_preserves_real_approval_but_not_user_edits(self):
        self.write('specs/other.md', '# Other\nEstado: borrador\n\n[Feature](feature.md)\n')
        self.approve('specs/other.md')
        before = self.mod.inspect(self.ctx, 'specs/other.md')['approval']
        self.fx.approve()
        after = self.mod.inspect(self.ctx, 'specs/other.md')
        self.assertTrue(after['authorized']); self.assertEqual(before['at'], after['approval']['at'])
        self.assertIn('(feature.md)', (self.root / after['spec']).read_text())
        self.assertTrue(self.mod.accept(self.ctx, after['spec'], readiness.SID)['authorized'])
        file = self.root / after['spec']; file.write_text(file.read_text() + '\nNew requirement.\n')
        self.mod.draft(self.ctx, 'specs/feature.md')
        self.assertFalse(self.mod.inspect(self.ctx, after['spec'])['authorized'])

    def test_migration_is_previewed_then_idempotent_and_does_not_invent_permission(self):
        self.write('specs/manual.md', '# Manual\nEstado: lista para implementar\n')
        self.write('specs/completed.md', '# Finished\nEstado: TERMINADO\n')
        self.write('specs/plantilla-spec.md', '# Template\nEstado: borrador\n')
        self.write('specs/Done/old.md', '# Historical\n')
        self.fx.approve()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / 'specs').rglob('*') if p.is_file()}
        preview = self.mod.organize(self.ctx)
        self.assertFalse(preview['applied']); self.assertIsNone(preview['backup'])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / 'specs').rglob('*') if p.is_file()})
        result = self.mod.organize(self.ctx, apply=True)
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertIn('Estado: borrador', (self.root / 'specs/Draft/manual.md').read_text())
        self.assertTrue((self.root / 'specs/Done/completed.md').exists())
        self.assertEqual((self.root / 'specs/Done/old.md').read_text(), '# Historical\n')
        self.assertTrue(self.mod.inspect(self.ctx, 'specs/Ready/feature.md')['authorized'])
        self.assertEqual(self.mod.organize(self.ctx, apply=True)['changedFiles'], 0)

    def test_legacy_receipt_moves_to_stable_identity(self):
        approved = self.fx.approve()
        doc = self.mod.document(self.ctx, approved['spec']); saved = self.mod.record(self.ctx, doc)
        legacy_path = self.root / 'specs/feature.md'
        (self.root / approved['spec']).rename(legacy_path)
        saved['spec'] = 'specs/feature.md'
        self.mod.record_path(self.ctx, doc['key']).unlink()
        h.atomic(self.mod.record_path(self.ctx, self.mod.digest(saved['spec'].encode())), saved)
        self.mod.organize(self.ctx, apply=True)
        self.assertTrue(self.mod.inspect(self.ctx, approved['spec'])['authorized'])
        self.assertEqual(self.mod.inspect(self.ctx, 'specs/feature.md')['spec'], approved['spec'])

    def test_collisions_symlinks_and_active_jobs_leave_sources_untouched(self):
        source = self.root / 'specs/feature.md'; before = source.read_bytes()
        collision = self.write('specs/Draft/feature.md', 'Other spec')
        with self.assertRaisesRegex(h.Blocked, 'destino'): self.fx.request()
        self.assertEqual(source.read_bytes(), before); self.assertEqual(collision.read_text(), 'Other spec')
        collision.unlink()
        self.write('specs/topic/spec.md', '# Topic\nEstado: borrador\n')
        link = self.root / 'specs/topic/link.md'; link.symlink_to(source)
        with self.assertRaisesRegex(h.Blocked, 'enlazados'): self.mod.draft(self.ctx, 'specs/topic/spec.md')
        link.unlink()
        job = self.fx.fixture.s; job.update(specs=['specs/topic/spec.md'], stage='implementing'); h.save(job, 'active')
        with self.assertRaisesRegex(h.Blocked, 'activos'): self.mod.organize(self.ctx, apply=True)
        with self.assertRaisesRegex(h.Blocked, 'trabajo abierto'): self.mod.draft(self.ctx, 'specs/topic/spec.md')
        self.assertEqual(source.read_bytes(), before)

    def test_failed_receipt_write_rolls_back_bundle_links_and_status(self):
        self.write('README.md', '[Feature](specs/feature.md)\n')
        before = (self.root / 'specs/feature.md').read_bytes(); original = h.atomic
        def fail_receipt(path, value):
            if 'spec-readiness' in Path(path).parts: raise OSError('simulated disk error')
            return original(path, value)
        with patch.object(h, 'atomic', side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, 'simulated'): self.mod.draft(self.ctx, 'specs/feature.md')
        self.assertEqual((self.root / 'specs/feature.md').read_bytes(), before)
        self.assertFalse((self.root / 'specs/Draft/feature.md').exists())
        self.assertEqual((self.root / 'README.md').read_text(), '[Feature](specs/feature.md)\n')

    def test_changed_source_after_preflight_is_preserved(self):
        operation = self.layout.plan(self.ctx, {'specs/feature.md': 'Draft'})
        file = self.root / 'specs/feature.md'; file.write_text('Concurrent edit')
        with self.assertRaisesRegex(h.Blocked, 'modificado'): self.layout.apply(self.ctx, operation)
        self.assertEqual(file.read_text(), 'Concurrent edit')

    def test_ambiguous_alias_never_picks_a_document(self):
        (self.root / 'specs/feature.md').unlink()
        self.write('specs/Ready/feature.md', '# Ready\n')
        self.write('specs/Draft/feature.md', '# Draft\n')
        with self.assertRaisesRegex(h.Blocked, 'ambigua'): self.mod.inspect(self.ctx, 'specs/feature.md')

    def test_alias_does_not_follow_a_symlink_in_another_stage(self):
        (self.root / 'specs/feature.md').unlink()
        target = self.write('docs/feature.md', '# Linked\nEstado: borrador\n')
        link = self.root / 'specs/Ready/feature.md'; link.parent.mkdir(); link.symlink_to(target)
        with self.assertRaisesRegex(h.Blocked, 'enlazados'): self.mod.inspect(self.ctx, 'specs/feature.md')

    def test_spec_named_spec_outside_specs_is_not_a_directory_bundle(self):
        self.assertEqual(self.layout.unit('docs/example/spec.md'), Path('docs/example/spec.md'))


class BundleCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fx = publication.PublicationTests(); self.fx.setUp()
        self.ctx, self.s = h.CoreContext(), self.fx.s
        self.mod = h.runtime_module('spec_status')
        self.relative = 'specs/Ready/topic/spec.md'
        for tree in (self.fx.root, self.fx.back):
            path = tree / self.relative; path.parent.mkdir(parents=True)
            path.write_text('# Topic\nEstado: lista para implementar\n\n![Image](pic.svg)\n[Doc](../../../docs/readme.md)\n')
            (path.parent / 'pic.svg').write_text('<svg>resource</svg>')
        self.s['specs'] = [self.relative]

    def tearDown(self): self.fx.tearDown()

    def test_uncommitted_readiness_move_does_not_duplicate_base_spec(self):
        layout = h.runtime_module('spec_layout')
        ready = self.fx.root / 'specs/Ready/feature.md'
        (self.fx.root / 'specs/feature.md').rename(ready)
        self.s['specs'] = ['specs/Ready/feature.md']
        before_index = self.fx.git(self.fx.root, 'ls-files', '--stage')
        layout.prepare_worktree(self.ctx, self.s)
        self.assertFalse((self.fx.back / 'specs/feature.md').exists())
        self.assertTrue((self.fx.back / 'specs/Ready/feature.md').exists())
        self.assertEqual(before_index, self.fx.git(self.fx.root, 'ls-files', '--stage'))
        self.mod.capture(self.ctx, self.s)
        self.assertTrue(self.s['specStatusInputs']['specs/Ready/feature.md']['tracked'])
        self.mod.stage_tracked(self.ctx, self.s)
        self.assertTrue((self.fx.back / 'specs/Done/feature.md').exists())
        self.assertFalse((self.fx.back / 'specs/Ready/feature.md').exists())

    def test_tracked_bundle_and_ignored_companion_move_inside_worktree(self):
        for tree in (self.fx.root, self.fx.back):
            self.fx.git(tree, 'add', '--', 'specs/Ready/topic')
        companion = self.fx.back / 'specs/Ready/topic/local.md'; companion.write_text('[Doc](../../../docs/readme.md)\n')
        with (self.fx.root / '.git/info/exclude').open('a') as stream: stream.write('specs/**/topic/local.md\n')
        archived = h.jobdir(self.s['id']) / 'artifacts/local-input.md'
        archived.parent.mkdir(parents=True, exist_ok=True); archived.write_bytes(companion.read_bytes())
        self.s['publication']['repos']['back']['copiedSpecs'] = {'specs/Ready/topic/local.md':
            {'sha256': h.sha(companion), 'archive': str(archived.relative_to(h.jobdir(self.s['id'])))}}
        (self.fx.back / 'README.md').write_text('[Topic](specs/Ready/topic/spec.md)\n')
        self.mod.capture(self.ctx, self.s); self.mod.stage_tracked(self.ctx, self.s)
        dest = self.fx.back / 'specs/Done/topic'
        self.assertFalse(companion.parent.exists()); self.assertTrue((dest / 'pic.svg').exists())
        self.assertIn('Estado: TERMINADO', (dest / 'spec.md').read_text()); self.assertIn('(pic.svg)', (dest / 'spec.md').read_text())
        self.assertIn('specs/Done/topic/spec.md', (self.fx.back / 'README.md').read_text())
        self.assertTrue((self.fx.root / self.relative).exists())
        self.fx.pub.archive_final_specs(self.ctx, self.s)
        self.assertIn('specs/Done/topic/local.md', self.s['publication']['repos']['back']['copiedSpecs'])
        self.mod.prepare(self.ctx, self.s); self.mod.complete(self.ctx, self.s)
        self.assertEqual(self.s['specCompletion'][self.relative]['source'], 'published_in_pr')
        self.assertTrue((h.jobdir(self.s['id']) / 'artifacts/completed-specs/specs/Done/topic/pic.svg').exists())

    def test_unversioned_spec_reaches_done_in_pr_and_retires_after_merge(self):
        relative = 'specs/Ready/new.md'
        for tree in (self.fx.root, self.fx.back): (tree / relative).write_text('# New\nEstado: lista para implementar\n')
        self.s['specs'] = [relative]
        self.mod.capture(self.ctx, self.s); self.mod.stage_tracked(self.ctx, self.s)
        env = self.fx.pub.build_index(self.ctx, self.s, 'back', freeze=False)
        published = self.fx.pub.git(self.ctx, self.fx.back, 'ls-files', '--', 'specs', env=env).stdout.decode().split()
        self.assertIn('specs/Done/new.md', published); self.assertNotIn(relative, published)
        self.assertIn('Estado: TERMINADO', (self.fx.back / 'specs/Done/new.md').read_text())
        self.mod.prepare(self.ctx, self.s); self.mod.complete(self.ctx, self.s)
        self.assertTrue((self.fx.root / relative).exists())
        self.assertNotIn('local', self.s['specCompletion'][relative])
        self.s.pop('specCompletion'); self.s['publication']['phase'] = 'cleaning'
        self.mod.complete(self.ctx, self.s)
        self.assertFalse((self.fx.root / relative).exists()); self.assertFalse((self.fx.root / 'specs/Done/new.md').exists())
        self.assertEqual(self.s['specCompletion'][relative]['local'], 'removed_after_merge')

    def test_unversioned_bundle_retires_only_unchanged_untracked_copies(self):
        self.mod.capture(self.ctx, self.s); self.mod.stage_tracked(self.ctx, self.s)
        self.assertTrue((self.fx.back / 'specs/Done/topic/pic.svg').exists())
        self.assertFalse((self.fx.back / 'specs/Ready/topic').exists())
        self.mod.prepare(self.ctx, self.s); self.s['publication']['phase'] = 'cleaning'
        asset = self.fx.root / 'specs/Ready/topic/pic.svg'; asset.write_text('New user image')
        self.mod.complete(self.ctx, self.s)
        self.assertEqual(asset.read_text(), 'New user image')
        self.assertEqual(self.s['specCompletion'][self.relative]['local'], 'preserved_user_changes')
        asset.write_text('<svg>resource</svg>'); self.fx.git(self.fx.root, 'add', '--', 'specs/Ready/topic')
        self.mod.complete(self.ctx, self.s)
        self.assertTrue((self.fx.root / self.relative).exists())
        self.assertEqual(self.s['specCompletion'][self.relative]['local'], 'tracked_pull_required')
        self.fx.git(self.fx.root, 'rm', '-q', '--cached', '-r', '--', 'specs/Ready/topic')
        self.mod.complete(self.ctx, self.s)
        self.assertFalse((self.fx.root / 'specs/Ready/topic').exists()); self.assertTrue((self.fx.root / 'specs/Ready').exists())
        self.assertEqual(self.s['specCompletion'][self.relative]['local'], 'removed_after_merge')

    def test_ignored_bundle_moves_together_and_retries(self):
        with (self.fx.root / '.git/info/exclude').open('a') as stream: stream.write('specs/Ready/topic/\nspecs/Done/topic/\n')
        self.mod.capture(self.ctx, self.s); self.mod.prepare(self.ctx, self.s)
        self.mod.complete(self.ctx, self.s); self.mod.complete(self.ctx, self.s)
        self.assertFalse((self.fx.root / 'specs/Ready/topic').exists())
        self.assertTrue((self.fx.root / 'specs/Done/topic/pic.svg').exists())
        self.assertEqual(self.s['specCompletion'][self.relative]['source'], 'moved_local')

    def test_ignored_bundle_with_changed_resource_is_preserved(self):
        with (self.fx.root / '.git/info/exclude').open('a') as stream: stream.write('specs/Ready/topic/\nspecs/Done/topic/\n')
        self.mod.capture(self.ctx, self.s); self.mod.prepare(self.ctx, self.s)
        asset = self.fx.root / 'specs/Ready/topic/pic.svg'; asset.write_text('New user image')
        self.mod.complete(self.ctx, self.s)
        self.assertEqual(asset.read_text(), 'New user image')
        self.assertTrue((self.fx.root / self.relative).exists())
        self.assertEqual(self.s['specCompletion'][self.relative]['source'], 'preserved_user_changes')


if __name__ == '__main__': unittest.main()
