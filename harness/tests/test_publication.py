import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch, Mock
import test_portability as fixture
h=fixture.h


class FakeGithub:
    def __init__(self):self.prs={};self.calls=0;self.ci_state='passed';self.error_repo=None;self.pr_states={}
    def preflight(self,h,tree,policy):return {'repository':tree.name+'/repo','remoteUrl':h.run(['git','remote','get-url','origin'],cwd=tree),'base':'main'}
    def ensure_pr(self,h,s,name):
        if name==self.error_repo:raise h.Blocked('GitHub unavailable')
        item=s['publication']['repos'][name]
        if name not in self.prs:self.calls+=1
        self.prs[name]={'number':self.prs.get(name,{}).get('number',len(self.prs)+1),'url':'https://example.invalid/'+name,'sha':item['commit']}
        return {k:self.prs[name][k] for k in ['number','url']}
    def ci(self,h,item,policy):return {'status':self.ci_state,'commit':item['commit'],'details':[]}
    def cleanup_pr(self,h,item,policy):
        remote=h.publication_module().out(h,item['source'],'ls-remote',policy['remote'],'refs/heads/'+item['branch'])
        h.require(remote and remote.split()[0]==item['commit'],'remote changed')
        state=self.pr_states.get(item['source'],'merged')
        h.require(state!='closed','PR cerrado sin fusionar')
        return state
    def api(self,h,endpoint):
        name=endpoint.split('/')[1]
        return {'state':'open','head':{'sha':self.prs[name]['sha']}}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.PortabilityTests();self.fixture.setUp()
        self.root=self.fixture.root;self.s=self.fixture.s
        for name in ['publication','github','finalization']:
            shutil.copy2(Path(fixture.existing.MODULE).parent/(name+'.py'),self.root/'harness'/(name+'.py'))
        self.pub=h.publication_module();self.clean=h.finalization_module()
        self.fake=FakeGithub();self.patch=patch.object(h,'github_module',return_value=self.fake);self.patch.start()
        self.front=self.root/'.harness/front';self.front.mkdir()
        self.git(self.front,'init','-q','-b','main');self.git(self.front,'config','user.email','test@example.invalid');self.git(self.front,'config','user.name','Test')
        (self.front/'app.txt').write_text('front initial')
        (self.front/'.gitignore').write_text('node_modules/\ngraphify-out/\n')
        self.git(self.front,'add','.');self.git(self.front,'commit','-qm','Initial')
        self.git(self.root,'branch','-M','main')
        cfg=h.config();cfg.update(primaryRepository='back',repositories={'back':{'path':'.'},'front':{'path':str(self.front)}})
        cfg['publication']={'enabled':True,'repositories':{n:{'remote':'origin','base':'main','workflows':['ci.yml'],
            'disposablePaths':['node_modules','target','dist','test-results','graphify-out']} for n in ['back','front']}}
        self.fixture.write_config(cfg)
        self.s.update(branch='feat/test-one',repositories={},worktrees={})
        for name,source in [('back',self.root),('front',self.front)]:
            remote=self.root/'.harness'/('remote-'+name+'.git')
            self.git(self.root,'init','--bare','-q',str(remote))
            self.git(source,'remote','add','origin',str(remote));self.git(source,'push','-q','origin','main')
            base=self.git(source,'rev-parse','HEAD')
            tree=h.STORE/'worktrees'/self.s['id']/name
            self.git(source,'worktree','add','-qb','feat/test-one',str(tree),'main')
            self.s['repositories'][name]={'source':str(source),'base':base,'branch':'feat/test-one','created':True}
            self.s['worktrees'][name]=str(tree)
            with (tree/'.gitignore').open('a') as f:f.write('\n.harness-parent.json\n')
            h.atomic(tree/'.harness-parent.json',{'job':self.s['id'],'controller':str(self.root)})
            (tree/'harness').mkdir(exist_ok=True);(tree/'harness/local.json').write_text('local harness data')
        self.s['worktree']=self.s['worktrees']['back'];self.s['specs']=[]
        self.pub.capture(h.CoreContext(),self.s)
        self.back=Path(self.s['worktrees']['back']);self.web=Path(self.s['worktrees']['front'])
        (self.back/'src/main/app.txt').write_text('approved backend')
        (self.web/'app.txt').write_text('approved frontend')
        (self.back/'e2e').mkdir();(self.back/'e2e/test.js').write_text('useful new test')
        self.s.update(stage='documenting',approval={'version':'approved'},productBaseline=h.job_fingerprint(self.s,True),
                      docsBaseline=h.job_inventory(self.s),qa={'criteria':[]})
        self.pub.freeze(h.CoreContext(),self.s)

    def tearDown(self):
        self.patch.stop();h._execution=None;self.fixture.tearDown()

    def git(self,root,*args):
        return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True,text=True).stdout.strip()

    def publish(self):self.pub.publish(h.CoreContext(),self.s)

    def test_waits_for_both_merges_without_repeating_ci_or_stopping_resources(self):
        self.publish();self.fake.pr_states[str(self.front)]='open'
        with patch.object(h,'stop_preview') as stop:
            self.assertFalse(self.clean.tick(h.CoreContext(),self.s))
            self.assertEqual(self.s['stage'],'awaiting_merge')
            self.assertTrue(self.back.exists() and self.web.exists());stop.assert_not_called()
            with patch.object(self.fake,'ci',side_effect=AssertionError('redundant CI poll')):
                self.assertFalse(self.clean.tick(h.CoreContext(),self.s))
        self.fake.pr_states[str(self.front)]='merged'
        self.assertTrue(self.clean.tick(h.CoreContext(),self.s))

    def test_cleanup_cannot_bypass_merge_gate(self):
        self.publish();self.fake.pr_states[str(self.front)]='open'
        self.s['publication']['phase']='cleaning'
        for item in self.s['publication']['repos'].values():item['ci']={'status':'passed'}
        with patch.object(h,'stop_preview') as stop:
            with self.assertRaises(h.Blocked):self.clean.cleanup(h.CoreContext(),self.s)
            stop.assert_not_called()
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_closed_unmerged_pr_preserves_both_worktrees(self):
        self.publish();self.fake.pr_states[str(self.front)]='closed'
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_merge_wait_does_not_expire_with_ci_timeout(self):
        self.publish();self.fake.pr_states[str(self.front)]='open'
        def merged_after_wait(seconds):self.fake.pr_states[str(self.front)]='merged'
        with patch.object(self.clean.time,'monotonic',side_effect=[0,7200]),patch.object(self.clean.time,'sleep',side_effect=merged_after_wait):
            self.clean.worker(h.CoreContext(),self.s['id'])
        self.assertEqual(h.load(self.s['id'])['stage'],'completed')

    def copied_spec(self):
        relative='specs/input-new.md';self.s['specs']=[relative]
        (self.root/'.git/info/exclude').write_text('specs/\n')
        for tree in [self.root,self.back]:
            (tree/'specs').mkdir(exist_ok=True);(tree/relative).write_text('The approved input spec')
        self.pub.capture(h.CoreContext(),self.s);self.refreeze();self.publish()
        return self.back/relative

    def test_ignored_input_spec_is_archived_and_cleaned(self):
        copied=self.copied_spec()
        record=self.s['publication']['repos']['back']['copiedSpecs']['specs/input-new.md']
        archive=h.jobdir(self.s['id'])/record['archive']
        self.assertEqual(archive.read_bytes(),copied.read_bytes())
        self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed');self.assertTrue(archive.exists())
        self.assertTrue((self.root/'specs/input-new.md').exists())

    def test_changed_ignored_spec_is_preserved(self):
        self.copied_spec().write_text('A new local decision')
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_unknown_ignored_spec_is_preserved(self):
        self.copied_spec();(self.back/'specs/other.md').write_text('Not an input copy')
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_missing_input_spec_archive_blocks_cleanup(self):
        self.copied_spec()
        record=self.s['publication']['repos']['back']['copiedSpecs']['specs/input-new.md']
        (h.jobdir(self.s['id'])/record['archive']).unlink()
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_nested_installed_dependencies_are_disposable_but_other_outputs_are_not(self):
        cfg=h.config()
        cfg['setup']=[{'id':'install-api','repo':'back','cwd':'e2e','command':['npm','ci'],
                       'outputs':['e2e/node_modules','important-data']}]
        self.fixture.write_config(cfg)
        policy=self.pub.settings(h.CoreContext())
        self.assertIn('e2e/node_modules',policy['repositories']['back']['disposablePaths'])
        self.assertNotIn('important-data',policy['repositories']['back']['disposablePaths'])
        self.s['publication']['policy']=policy
        ignore=self.back/'.gitignore'
        ignore.write_text('e2e/node_modules/\n'+ignore.read_text())
        self.refreeze();self.publish()
        target=self.back/'e2e/node_modules';target.mkdir(parents=True);(target/'dependency').write_text('installed')
        self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed')

    def test_external_symlink_in_disposable_root_blocks_cleanup(self):
        self.publish()
        outside=self.root/'.harness/other-data';outside.mkdir();(outside/'keep').write_text('other work')
        (self.back/'node_modules').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(h.Blocked): self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue((outside/'keep').exists());self.assertTrue(self.web.exists())

    def test_merged_pr_and_deleted_remote_branch_can_resume_without_push(self):
        self.publish()
        self.git(self.root,'push','origin','--delete',self.s['branch'])
        # GitHub proof is covered independently below; exercise real local cleanup/checkpoints.
        original=self.fake.cleanup_pr
        def checked(ctx,item,policy):
            if item['source']==str(self.root): return 'merged'
            return original(ctx,item,policy)
        with patch.object(self.fake,'cleanup_pr',side_effect=checked):
            self.publish()
            self.assertEqual(self.git(self.root,'ls-remote','origin','refs/heads/'+self.s['branch']),'')
            self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed');self.assertEqual(self.fake.calls,2)

    def test_publish_and_cleanup_two_repos_no_checks_or_models(self):
        with patch.object(h,'verify',side_effect=AssertionError('redundant verify')),patch.object(h,'graph',side_effect=AssertionError('redundant graph')):
            self.publish();commits={n:v['commit'] for n,v in self.s['publication']['repos'].items()}
            self.publish();self.assertEqual(self.fake.calls,2)
            self.assertEqual(commits,{n:v['commit'] for n,v in self.s['publication']['repos'].items()})
            files=self.git(self.back,'diff','--name-only',self.s['repositories']['back']['base'],'HEAD').splitlines()
            self.assertEqual(set(files),{'src/main/app.txt','e2e/test.js'})
            self.assertEqual(self.git(self.back,'show','HEAD:.gitignore'),self.git(self.root,'show','main:.gitignore'))
            self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed')
        for name,item in self.s['publication']['repos'].items():
            self.assertFalse(Path(item['worktree']).exists())
            self.assertEqual(self.git(Path(item['source']),'branch','--list',item['branch']),'')
            self.assertIn(item['commit'],self.git(Path(item['source']),'ls-remote','origin','refs/heads/'+item['branch']))
        self.assertTrue((h.jobdir(self.s['id'])/'state.json').is_file())

    def test_inline_publication_cleans_resources_without_stopping_user_session(self):
        self.s['conversation']={'sessionId':'user-session','mode':'inline','owned':False}
        original=h.run
        def command(argv,**kwargs):
            if argv[0]=='claude': raise AssertionError('User session must not be managed')
            return original(argv,**kwargs)
        with patch.object(h,'run',side_effect=command):
            self.publish();self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed')
        self.assertTrue(self.s['publication']['sessionPreserved'])
        self.assertFalse(self.back.exists());self.assertFalse(self.web.exists())

    def test_partial_pr_failure_reuses_commits_and_existing_pr(self):
        self.fake.error_repo='front'
        with self.assertRaises(h.Blocked):self.publish()
        commits={n:v['commit'] for n,v in self.s['publication']['repos'].items()}
        self.assertTrue(self.back.exists() and self.web.exists())
        self.fake.error_repo=None;self.publish()
        self.assertEqual(self.fake.calls,2);self.assertEqual(commits,{n:v['commit'] for n,v in self.s['publication']['repos'].items()})

    def refreeze(self):
        for item in self.s['publication']['repos'].values():
            for key in ('tree','inventory','headBefore','commit','changed','ci','pushed'):item.pop(key,None)
        self.s['productBaseline']=h.job_fingerprint(self.s,True)
        self.pub.freeze(h.CoreContext(),self.s)

    def test_unchanged_repository_gets_no_pr(self):
        (self.web/'app.txt').write_text('front initial');self.refreeze()
        self.publish();self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(set(self.fake.prs),{'back'})
        self.assertFalse(self.web.exists())

    def test_approved_deletion_survives_commit_and_cleanup(self):
        (self.web/'app.txt').unlink();self.refreeze();self.publish();self.publish()
        self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed')

    def test_product_and_shared_doc_changes_survive_overlay_removal(self):
        # Put a genuine documentation edit away from the recorded harness append.
        original=self.git(self.root,'show','main:.gitignore')+'\n'
        (self.back/'.gitignore').write_text('logs/\n'+original+'\n.harness-parent.json\n')
        self.refreeze();self.publish()
        self.assertEqual(self.git(self.back,'show','HEAD:.gitignore'),'logs/\n'+original.strip())
        # Correcting a published job preserves the earlier legitimate shared-file commit.
        (self.back/'src/main/app.txt').write_text('second approved backend')
        self.refreeze();self.publish();self.assertEqual(self.fake.calls,2)

    def test_partial_staging_is_not_discarded(self):
        self.git(self.web,'add','app.txt');(self.web/'app.txt').write_text('additional unstaged edit')
        with self.assertRaises(h.Blocked):self.refreeze()
        self.assertEqual(self.git(self.web,'show',':app.txt'),'approved frontend')

    def test_commit_hook_mutation_prevents_push(self):
        hook=self.root/'.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf mutated > src/main/app.txt\ngit add src/main/app.txt\n');hook.chmod(0o755)
        with self.assertRaises(h.Blocked):self.publish()
        self.assertEqual(self.fake.calls,0)
        self.assertEqual(self.git(self.root,'ls-remote','origin','refs/heads/feat/test-one'),'')

    def test_remote_url_changed_after_push_prevents_cleanup(self):
        self.publish();self.git(self.front,'remote','set-url','origin',str(self.root/'.harness/remote-back.git'))
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_cleanup_resume_requires_current_ci(self):
        self.publish();self.s['publication']['phase']='cleaning';self.fake.ci_state='pending'
        self.assertFalse(self.clean.tick(h.CoreContext(),self.s))
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_session_identity_mismatch_preserves_environment(self):
        self.publish();self.s.update(launched=True,backgroundId='ours',sessionId='ours-session')
        def command(argv,**kwargs):
            if argv[:2]==['claude','agents']:return json.dumps([{'id':'ours','sessionId':'another','cwd':str(self.back),'state':'running'}])
            raise AssertionError('Unexpected command: '+str(argv))
        with patch.object(h,'run',side_effect=command):
            with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_worker_failure_preserves_cycles_and_can_resume(self):
        self.publish();cycles=self.s['cycles'];self.fake.ci_state='failed'
        self.clean.worker(h.CoreContext(),self.s['id']);self.s=h.load(self.s['id'])
        self.assertEqual(self.s['stage'],'blocked');self.assertEqual(self.s['cycles'],cycles)
        self.fake.ci_state='passed';self.clean.worker(h.CoreContext(),self.s['id'])
        self.assertEqual(h.load(self.s['id'])['stage'],'completed')

    def test_finish_hands_approved_documentation_to_detached_worker(self):
        self.s['stage']='documenting'
        for item in self.s['publication']['repos'].values():
            for key in ('tree','inventory','headBefore'):item.pop(key,None)
        (self.back/'docs/readme.md').write_text('Updated approved documentation')
        with patch.object(h,'finalization_module',return_value=self.clean),patch.object(self.clean,'start') as start:
            h.finish(self.s)
        start.assert_called_once();self.assertEqual(self.s['stage'],'awaiting_ci')
        self.assertEqual(self.git(self.back,'show','HEAD:docs/readme.md'),'Updated approved documentation')

    def test_worker_timeout_and_lock_do_not_repeat_product_checks(self):
        self.publish();self.fake.ci_state='pending';cycles=self.s['cycles']
        with h.lock('finalize-'+self.s['id']):
            with self.assertRaises(h.Blocked):self.clean.worker(h.CoreContext(),self.s['id'])
        with patch.object(self.clean.time,'monotonic',side_effect=[0,3601]):
            self.clean.worker(h.CoreContext(),self.s['id'])
        state=h.load(self.s['id']);self.assertEqual(state['stage'],'blocked');self.assertEqual(state['cycles'],cycles)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_database_removal_rejects_replacement_and_checks_volume_label(self):
        self.s['containerId']='owned-id';commands=[];wrong_label=True
        def run(argv,**kwargs):
            commands.append(argv)
            if argv[1:3]==['volume','ls']:return self.s['volume']
            if argv[1:3]==['volume','inspect']:
                return json.dumps([{'Labels':{h.LABEL:'another-job' if wrong_label else self.s['resourceKey']}}])
            if argv[1:3]==['volume','rm']:return self.s['volume']
            raise AssertionError(str(argv))
        rows=Mock(returncode=0,stdout=json.dumps({'ID':'replacement','Names':self.s['container']}))
        with patch.object(h,'has_database',return_value=True),patch.object(h,'run',side_effect=run),patch.object(self.clean.subprocess,'run',return_value=rows):
            with self.assertRaises(h.Blocked):self.clean.remove_database(h.CoreContext(),self.s)
            self.assertFalse(commands)
            rows.stdout=''  # Container already removed by an interrupted cleanup.
            with self.assertRaises(h.Blocked):self.clean.remove_database(h.CoreContext(),self.s)
            self.assertFalse(any(v[1:3]==['volume','rm'] for v in commands))
            wrong_label=False;self.clean.remove_database(h.CoreContext(),self.s)
            self.assertTrue(self.s['publication']['database']['removed'])

    def test_missing_commit_checkpoint_is_reconciled(self):
        self.pub.commit(h.CoreContext(),self.s,'back');item=self.s['publication']['repos']['back'];expected=item.pop('commit')
        self.pub.commit(h.CoreContext(),self.s,'back')
        self.assertEqual(item['commit'],expected)

    def test_ci_pending_and_failure_preserve_everything(self):
        self.publish();self.fake.ci_state='pending'
        self.assertFalse(self.clean.tick(h.CoreContext(),self.s));self.assertTrue(self.back.exists())
        self.fake.ci_state='failed'
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists() and self.web.exists())

    def test_late_local_edit_blocks_cleanup(self):
        self.publish();(self.web/'app.txt').write_text('not uploaded')
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists())

    def test_unknown_ignored_file_blocks_cleanup(self):
        self.publish()
        with (Path(self.s['publication']['repos']['front']['commonDir'])/'info/exclude').open('a') as f:f.write('\nprivate-notes\n')
        (self.web/'private-notes').write_text('keep this')
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.web.exists())

    def test_generated_node_modules_can_be_removed(self):
        self.publish();p=self.web/'node_modules/cache';p.mkdir(parents=True);(p/'generated').write_text('cache')
        self.clean.tick(h.CoreContext(),self.s);self.assertFalse(self.web.exists())

    def test_changed_remote_prevents_any_cleanup(self):
        self.publish();item=self.s['publication']['repos']['front']
        self.git(self.front,'push','--force','origin',item['base']+':refs/heads/'+item['branch'])
        with self.assertRaises(h.Blocked):self.clean.tick(h.CoreContext(),self.s)
        self.assertTrue(self.back.exists())

    def test_internal_files_in_intermediate_commit_block_publication(self):
        self.git(self.back,'add','harness/local.json');self.git(self.back,'commit','-qm','bad commit')
        self.git(self.back,'rm','harness/local.json');self.git(self.back,'commit','-qm','remove from final tree')
        with self.assertRaises(h.Blocked):self.publish()
        self.assertEqual(self.fake.calls,0)

    def test_cleaning_can_resume_after_first_worktree_removed(self):
        self.publish()
        original=self.pub.git
        # Simulate a process crash right after worktree removal, before checkpoint.
        def fail_save(s,event):
            if event=='cleanup_worktree_removed_back':raise OSError('crash')
            return real_save(s,event)
        real_save=h.save
        with patch.object(h,'save',side_effect=fail_save):
            with self.assertRaises(OSError):self.clean.tick(h.CoreContext(),self.s)
        self.s=h.load(self.s['id']);self.clean.tick(h.CoreContext(),self.s)
        self.assertEqual(self.s['stage'],'completed')


class GithubTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        p=Path(fixture.existing.MODULE).parent/'github.py';spec=importlib.util.spec_from_file_location('test_github_adapter',p)
        self.g=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.g)
        self.item={'github':{'repository':'owner/repo'},'pr':{'number':1},'commit':'sha'}
        self.policy={'base':'main','workflows':['ci.yml'],'requiredChecks':[]}
        self.run={'id':1,'run_attempt':1,'status':'completed','conclusion':'success','head_sha':'sha','pull_requests':[{'number':1}]}
    def api(self,h,endpoint):
        if '/pulls/' in endpoint:return {'number':1,'state':'open','head':{'sha':'sha'},'base':{'ref':'main','repo':{'full_name':'owner/repo'}}}
        if '/runs?' in endpoint:return {'workflow_runs':[self.run] if self.run else []}
        if '/rules/branches/' in endpoint:return []
        return {'protected':False}
    def test_success_and_missing_or_wrong_revision_runs(self):
        with patch.object(self.g,'api',side_effect=self.api):
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'passed')
            self.run['head_sha']='older';self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'pending')
            self.run=None;self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'pending')
    def test_cancelled_and_skipped_workflows_are_not_success(self):
        with patch.object(self.g,'api',side_effect=self.api):
            for result in ['failure','cancelled','skipped','neutral','timed_out']:
                self.run['conclusion']=result
                self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'failed')

    def test_required_ruleset_check_and_merge_revision_take_precedence(self):
        merge='pending'
        def api(ctx,endpoint):
            if '/pulls/' in endpoint:return {'number':1,'state':'open','head':{'sha':'sha'},'base':{'ref':'main','repo':{'full_name':'owner/repo'}},'merge_commit_sha':'merge'}
            if '/rules/branches/' in endpoint:return [{'type':'required_status_checks','parameters':{'required_status_checks':[{'context':'security'}]}}]
            if '/check-runs' in endpoint:return {'check_runs':[]}
            if '/statuses' in endpoint:
                return [{'context':'security','updated_at':'2026-01-02' if '/sha/' in endpoint else '2026-01-01',
                         'state':'success' if '/sha/' in endpoint else merge}]
            return self.api(ctx,endpoint)
        with patch.object(self.g,'api',side_effect=api):
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'pending')
            merge='failure';self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'failed')
            merge='success';self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'passed')

    def test_merged_pr_validates_result_in_destination_for_each_merge_strategy(self):
        for result_sha in ['normal-merge','squashed-commit','rebased-tip']:
            with self.subTest(strategy=result_sha):
                pr=self.api(None,'/pulls/1')
                pr.update(state='closed',merged=True,merged_at='2026-09-18',merge_commit_sha=result_sha)
                def api(ctx,endpoint):
                    if '/pulls/' in endpoint:return pr
                    if '/compare/' in endpoint:return {'status':'ahead','merge_base_commit':{'sha':result_sha}}
                    return self.api(ctx,endpoint)
                with patch.object(self.g,'api',side_effect=api):
                    self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'passed')
                    self.assertEqual(self.g.cleanup_pr(h.CoreContext(),self.item,self.policy),'merged')

    def test_closed_unmerged_wrong_head_or_missing_remote_merge_never_pass(self):
        original=self.api(None,'/pulls/1')
        invalid=[dict(original,state='closed'),dict(original,number=2),dict(original,head={'sha':'other'}),
                 dict(original,base={'ref':'other','repo':{'full_name':'owner/repo'}})]
        for pr in invalid:
            with patch.object(self.g,'api',return_value=pr), self.assertRaises(h.Blocked):
                self.g.ci(h.CoreContext(),self.item,self.policy)
        pr=dict(original,state='closed',merged=True,merged_at='today',merge_commit_sha='merge')
        with patch.object(self.g,'api',return_value={'status':'diverged','merge_base_commit':{'sha':'old'}}),self.assertRaises(h.Blocked):
            self.g.validate_pr(h.CoreContext(),self.item,self.policy,pr)

    def test_merged_pr_still_requires_successful_ci(self):
        pr=self.api(None,'/pulls/1');pr.update(state='closed',merged=True,merged_at='today',merge_commit_sha='merge')
        def api(ctx,endpoint):
            if '/pulls/' in endpoint:return pr
            if '/compare/' in endpoint:return {'status':'identical','merge_base_commit':{'sha':'merge'}}
            return self.api(ctx,endpoint)
        self.run['conclusion']='failure'
        with patch.object(self.g,'api',side_effect=api):
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'failed')

    def test_merged_github_empty_pr_associations_reuse_identity_not_old_success(self):
        pr=self.api(None,'/pulls/1');pr.update(state='closed',merged=True,merged_at='2026-09-18T20:00:00Z',merge_commit_sha='merge')
        self.run.update(pull_requests=[],event='pull_request')
        self.item['ci']={'commit':'sha','details':[{'workflow':'ci.yml','runId':1,'status':'passed'}]}
        def api(ctx,endpoint):
            if '/pulls/' in endpoint:return pr
            if '/compare/' in endpoint:return {'status':'identical','merge_base_commit':{'sha':'merge'}}
            return self.api(ctx,endpoint)
        with patch.object(self.g,'api',side_effect=api):
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'passed')
            self.run.update(run_attempt=2,conclusion='failure')
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'failed')
            self.run['id']=2
            self.assertEqual(self.g.ci(h.CoreContext(),self.item,self.policy)['status'],'pending')

    def test_merge_before_first_ci_poll_requires_complete_run_identity(self):
        pr=self.api(None,'/pulls/1');pr.update(merged=True,created_at='2026-09-18T10:00:00Z',merged_at='2026-09-18T11:00:00Z')
        pr['head'].update(ref='feature',repo={'id':42})
        self.run.update(pull_requests=[],event='pull_request',head_repository={'id':42},head_branch='feature',created_at='2026-09-18T10:30:00Z')
        self.assertTrue(self.g.run_belongs_to_pr(self.run,pr,self.item,'ci.yml'))
        for key,value in [('head_sha','other'),('head_repository',{'id':99}),('head_branch','other'),('created_at','2026-09-17T10:30:00Z'),('pull_requests',[{'number':2}])]:
            run=dict(self.run);run[key]=value
            self.assertFalse(self.g.run_belongs_to_pr(run,pr,self.item,'ci.yml'))
