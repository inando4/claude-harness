import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_harness as existing
import test_setup

h=existing.h


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.HarnessTests();self.fixture.setUp();self.root=self.fixture.root
        self.extra=tempfile.TemporaryDirectory();self.front=Path(self.extra.name)
        self.git(self.front,'init','-q');self.git(self.front,'config','user.name','Harness Test')
        self.git(self.front,'config','user.email','harness@example.invalid')
        (self.front/'app.js').write_text('original')
        (self.front/'package.json').write_text('{"scripts": {"test":"false"}}')
        (self.front/'.gitignore').write_text('test-results/\nnode_modules/\n.env\n')
        (self.front/'docs').mkdir();(self.front/'docs/readme.md').write_text('Original')
        self.git(self.front,'add','.');self.git(self.front,'commit','-qm','Frontend initial')
        cfg=json.loads((existing.MODULE.parent/'templates/web-no-db.json').read_text())
        cfg.update(needsConfiguration=False,primaryRepository='backend',repositories={
            'backend':{'path':'.'},'frontend':{'path':str(self.front)}})
        cfg['services'][0]['repo']='frontend';cfg['setup']=[];cfg['build']=[]
        cfg['checks']=[{'repo':'frontend','command':['npm','run','test:ci']}]
        cfg['testReports']={'frontend':{'repo':'frontend','glob':'test-results/junit.xml'}}
        self.config(cfg)
        self.s=dict(self.fixture.s,worktrees={'backend':str(self.root),'frontend':str(self.front)})
        self.s['repositories']={name:{'base':self.git(Path(tree),'rev-parse','HEAD')} for name,tree in self.s['worktrees'].items()}

    def tearDown(self):
        self.fixture.tearDown();self.extra.cleanup()

    def git(self,root,*args):
        return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True,text=True).stdout.strip()

    def config(self,cfg): (self.root/'harness/config.json').write_text(json.dumps(cfg))

    def test_steps_and_reports_resolve_their_repository(self):
        h.validate_config()
        self.s['ports']={'web':8888}
        cfg=h.config();cfg['services'][0]['env']={'SELECTED':'${WORKTREE}','OTHER':'${WORKTREE_BACKEND}'};self.config(cfg)
        service=h.adapter_call('services',self.s)[0]
        self.assertEqual(service['repo'],'frontend');self.assertEqual(service['env']['SELECTED'],str(self.front))
        self.assertEqual(service['env']['OTHER'],str(self.root))
        p=self.front/'test-results/junit.xml';p.parent.mkdir();p.write_text('<testsuite tests="2"/>')
        self.assertEqual(h.adapter_call('test_summary',self.s)['frontend']['tests'],2)
        p.write_text('<testsuite tests="2" failures="1"/>')
        with self.assertRaises(h.Blocked):h.adapter_call('test_summary',self.s)

    def test_bad_repository_and_cwd_escape_rejected(self):
        cfg=h.config();cfg['services'][0]['repo']='missing';self.config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()
        cfg['services'][0]['repo']='frontend';cfg['services'][0]['cwd']='../';self.config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()

    def test_duplicate_repository_and_wrong_primary_rejected(self):
        cfg=h.config();cfg['repositories']['frontend']['path']='.';self.config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()
        cfg['repositories']['frontend']['path']=str(self.front);cfg['primaryRepository']='frontend';self.config(cfg)
        with self.assertRaises(h.Blocked):h.validate_config()

    def test_frontend_change_invalidates_combined_review(self):
        before=h.job_fingerprint(self.s);product=h.job_fingerprint(self.s,True)
        (self.front/'app.js').write_text('changed')
        self.assertNotEqual(before,h.job_fingerprint(self.s));self.assertNotEqual(product,h.job_fingerprint(self.s,True))
        self.s.update(stage='documenting',approval={'version':'x'},productBaseline=product,docsBaseline=h.job_inventory(self.s))
        with self.assertRaises(h.Blocked):h.finish(self.s)

    def test_documentation_allowed_in_both_repositories_only(self):
        self.s.update(stage='documenting',approval={'version':'x'},productBaseline=h.job_fingerprint(self.s,True),docsBaseline=h.job_inventory(self.s))
        (self.front/'docs/readme.md').write_text('[app](../app.js)')
        (self.root/'docs/readme.md').write_text('Updated backend')
        h.finish(self.s);self.assertEqual(self.s['stage'],'completed')

    def test_frontend_manifest_invalidates_setup(self):
        before=test_setup.setup.signature(h.CoreContext())
        (self.front/'package.json').write_text('{}')
        self.assertNotEqual(before,test_setup.setup.signature(h.CoreContext()))
        self.assertEqual({v['repo'] for v in test_setup.setup.inspect(h.CoreContext())['manifests']},{'frontend'})

    def test_branch_conflict_preflight_creates_no_worktrees(self):
        self.git(self.front,'branch','feat/conflict')
        with self.assertRaises(h.Blocked):h.repositories_module().plan(h.CoreContext(),'feat/conflict','HEAD')
        self.assertFalse((h.STORE/'worktrees').exists())
        self.assertFalse(self.git(self.root,'branch','--list','feat/conflict'))

    def test_two_worktrees_keep_original_dirt_and_independent_bases(self):
        plan=h.repositories_module().plan(h.CoreContext(),'feat/example','HEAD')
        (self.front/'app.js').write_text('uncommitted original')
        state=dict(self.s,repositories=plan,worktrees={name:str(h.STORE/'worktrees/pair'/name) for name in plan})
        state['worktree']=state['worktrees']['backend']
        h.repositories_module().create(h.CoreContext(),state)
        for name,tree in state['worktrees'].items():
            self.assertEqual(self.git(Path(tree),'rev-parse','HEAD'),plan[name]['base'])
            self.assertEqual(self.git(Path(tree),'branch','--show-current'),'feat/example')
            self.assertTrue((Path(tree)/'.harness-parent.json').exists())
        self.assertEqual((Path(state['worktrees']['frontend'])/'app.js').read_text(),'original')
        self.assertEqual((self.front/'app.js').read_text(),'uncommitted original')

    def test_supplemental_paths_skip_ignored_and_reject_secrets(self):
        (self.front/'helpers').mkdir();(self.front/'helpers/tool.js').write_text('test helper')
        (self.front/'helpers/node_modules').mkdir();(self.front/'helpers/node_modules/foo').write_text('ignored')
        cfg=h.config();cfg['repositories']['frontend']['copyPaths']=['helpers'];self.config(cfg)
        paths=h.repositories_module().supplemental(h.CoreContext(),h.repositories_module().sources(h.CoreContext())['frontend'])
        self.assertEqual([p.name for p in paths],['tool.js'])
        (self.front/'helpers/private.key').write_text('not copied')
        with self.assertRaises(h.Blocked):h.repositories_module().plan(h.CoreContext(),'feat/no-secret','HEAD')

    def test_launch_adds_frontend_worktree(self):
        self.s['ports']={'web':8888}
        with patch.object(h.subprocess,'call',return_value=0) as call:
            h.launch_native(self.s,False)
        argv=call.call_args.args[0]
        self.assertEqual(argv[argv.index('--add-dir')+1],str(self.front))


if __name__=='__main__':unittest.main()
