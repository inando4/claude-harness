import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_portability as fixture
import test_publication as publication_fixture
h=fixture.h


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.PortabilityTests();self.fixture.setUp()
        self.s=self.fixture.s;self.mod=h.runtime_module('execution');self.ctx=h.CoreContext()
        cfg=h.config();cfg['resources']={'heavySlots':1,'waitSeconds':1,'minAvailableMiB':0};self.fixture.write_config(cfg)

    def tearDown(self):
        h._execution=None;self.fixture.tearDown()

    def test_identity_alive_dead_reused_and_reboot(self):
        live=self.mod.identity(os.getpid())
        self.assertEqual(self.mod.status(live),'alive')
        self.assertEqual(self.mod.status(dict(live,start='invalid')),'unknown')
        self.assertEqual(self.mod.status(dict(live,boot='prior-boot')),'dead')
        p=subprocess.Popen([sys.executable,'-c','pass']);receipt=self.mod.identity(p.pid);p.wait()
        self.assertEqual(self.mod.status(receipt),'dead')

    def test_orphaned_descendant_prevents_recovery(self):
        child='import time; time.sleep(20)'
        script='import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); print("ready",flush=True); time.sleep(20)'
        p=subprocess.Popen([sys.executable,'-c',script],start_new_session=True,stdout=subprocess.PIPE,text=True)
        try:
            p.stdout.readline();receipt=dict(self.mod.identity(p.pid),group=True)
            p.kill();p.wait()
            self.assertEqual(self.mod.status(receipt),'alive')
        finally:
            os.killpg(p.pid,signal.SIGKILL);p.stdout.close()

    def test_recovery_no_cycle_and_unknown_migration_blocks(self):
        self.s.update(stage='checking',operationStartedAt=time.time(),executionOwner={'boot':'previous'},cycles=1)
        self.mod.recover(self.ctx,self.s)
        self.assertEqual((self.s['stage'],self.s['cycles']),('blocked',1))
        self.s.update(stage='checking',migrationFailed=True);self.s.pop('operationFinishedAt')
        with self.assertRaisesRegex(h.Blocked,'migración'):self.mod.recover(self.ctx,self.s)
        self.assertTrue(self.s['migrationFailed']);self.assertEqual(self.s['cycles'],1)

    def test_alive_or_unknown_owner_cannot_recover(self):
        for owner in [self.mod.identity(os.getpid()),None]:
            self.s.update(stage='checking',operationStartedAt=time.time(),executionOwner=owner)
            with self.assertRaises(h.Blocked):self.mod.recover(self.ctx,self.s)
            self.assertEqual(self.s['stage'],'checking')

    def test_tool_waits_for_durable_identity_and_failure_does_not_launch(self):
        marker=self.fixture.root/'executed'
        command=[sys.executable,'-c','from pathlib import Path;Path('+repr(str(marker))+').touch()']
        with open(os.devnull,'wb') as out:
            def fail(_):
                self.assertFalse(marker.exists());raise RuntimeError('save failed')
            with self.assertRaises(RuntimeError):self.mod.launch(command,cwd=self.fixture.root,env=None,stdin=None,stdout=out,record=fail)
            time.sleep(.1);self.assertFalse(marker.exists())
            receipts=[]
            p=self.mod.launch(command,cwd=self.fixture.root,env=None,stdin=None,stdout=out,record=receipts.append)
            p.wait(timeout=5)
        self.assertTrue(marker.exists());self.assertEqual(receipts[0]['pid'],p.pid)

    def test_heavy_slot_blocks_other_job_but_not_light(self):
        other=dict(self.s,id='test-two');h.save(other,'fixture')
        with self.mod.reservation(self.ctx,self.s,{'command':['mvn','verify']}) as fd:
            self.assertIsInstance(fd,int)
            with self.mod.reservation(self.ctx,other,{'command':['python3','light.py']}) as light:self.assertIsNone(light)
            with self.assertRaises(h.InfrastructureBlocked):
                with self.mod.reservation(self.ctx,other,{'command':['mvn','verify']}):self.fail('double heavy execution')
        with self.mod.reservation(self.ctx,other,{'command':['mvn','verify']}):pass
        self.assertEqual(other['cycles'],0);self.assertGreater(other['resourceWaits'][0]['seconds'],.9)

    def test_low_memory_blocks_and_does_not_spend_cycle(self):
        cfg=h.config();cfg['resources']['minAvailableMiB']=1;self.fixture.write_config(cfg)
        with patch.object(self.mod,'available_memory',return_value=0):
            with self.assertRaises(h.InfrastructureBlocked):
                with self.mod.reservation(self.ctx,self.s,{'command':['mvn']}):self.fail()
        self.assertEqual(self.s['resourceWaits'][0]['reason'],'memory');self.assertEqual(self.s['cycles'],0)

    def test_slot_survives_owner_controller_exit(self):
        # Independent controller exits after launching; supervisor retains slot.
        ready=self.fixture.root/'receipt.json'
        script='''import sys,importlib.util,json,os
from pathlib import Path
spec=importlib.util.spec_from_file_location('execution',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
slot=open(sys.argv[2],'a+');import fcntl;fcntl.flock(slot,fcntl.LOCK_EX)
out=open(os.devnull,'wb')
m.launch([sys.executable,'-c','import time;time.sleep(20)'],cwd='/',env=None,stdin=None,stdout=out,slot=slot.fileno(),record=lambda r:Path(sys.argv[3]).write_text(json.dumps(r)))
os._exit(0)
'''
        root=h.STORE/'resources';root.mkdir(parents=True,exist_ok=True)
        subprocess.run([sys.executable,'-c',script,str(h.ROOT/'harness/execution.py'),str(root/'heavy-0.lock'),str(ready)],check=True)
        receipt=json.loads(ready.read_text())
        try:
            with self.assertRaises(h.InfrastructureBlocked):
                with self.mod.reservation(self.ctx,self.s,{'command':['mvn']}):self.fail()
        finally:os.killpg(receipt['pid'],signal.SIGKILL)
        for _ in range(50):
            if self.mod.status(receipt)=='dead':break
            time.sleep(.02)
        with self.mod.reservation(self.ctx,self.s,{'command':['mvn']}):pass

    def test_show_does_not_wait_for_the_mutating_job_lock(self):
        h.save(self.s,'fixture')
        with patch.object(sys,'argv',['harness.py','show',self.s['id']]),patch.object(h,'lock',side_effect=AssertionError('read blocked')),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(h.main(),0)
        self.assertEqual(json.loads(out.getvalue())['id'],self.s['id'])

    def test_timeout_kills_child_ignoring_term_without_spending_product_cycle(self):
        child='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(20)'
        parent='import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'+repr(child)+'],stdout=subprocess.PIPE);p.stdout.readline();p.wait()'
        h.begin_attempt(self.s,'timeout')
        with self.assertRaises(h.InfrastructureBlocked):h.run([sys.executable,'-c',parent],log=h.attempt_path(self.s,'timeout.log'),timeout=.3)
        residue=self.s.get('commandResidue')
        if residue:
            for _ in range(50):
                if self.mod.status(residue)=='dead':break
                time.sleep(.02)
            self.assertEqual(self.mod.status(residue),'dead')
        self.assertEqual(self.s['cycles'],0)

    def test_log_command_has_identity_and_success_receipt(self):
        h.begin_attempt(self.s,'test')
        h.run([sys.executable,'-c','print("passed")'],log=h.attempt_path(self.s,'tiny.log'))
        self.assertNotIn('currentCommand',self.s)
        self.assertEqual(json.loads(h.attempt_path(self.s,'commands.jsonl').read_text())['exitCode'],0)
        h.end_attempt(self.s)
        self.assertEqual(self.mod.observed(self.s)['status'],'idle')


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx=publication_fixture.PublicationTests();self.fx.setUp()
        self.s=self.fx.s;self.pub=self.fx.pub;self.ctx=h.CoreContext()
        self.s['approvedCandidate']='approved';self.s['qa']={'status':'pass','artifacts':['proof.txt']}
        artifacts=h.jobdir(self.s['id'])/'artifacts';artifacts.mkdir(exist_ok=True)
        (artifacts/'proof.txt').write_text('qa evidence');h.atomic(h.jobdir(self.s['id'])/'qa.json',self.s['qa'])
        self.s['approvalEvidence']=self.pub.evidence(self.ctx,self.s)

    def tearDown(self):self.fx.tearDown()

    def test_staging_only_repair_retries_without_qa_or_new_approval(self):
        self.s['stage']='documenting'
        for item in self.s['publication']['repos'].values():
            for key in ('tree','inventory','headBefore'):item.pop(key,None)
        doc=self.fx.back/'docs/readme.md';doc.write_text('first doc');self.fx.git(self.fx.back,'add','docs/readme.md');doc.write_text('final doc')
        with self.assertRaisesRegex(h.Blocked,'Índice Git'):h.finish(self.s)
        self.assertEqual(self.s['blockedPhase'],'publishing')
        approval=dict(self.s['approval']);self.fx.git(self.fx.back,'add','docs/readme.md')
        with patch.object(h,'verify',side_effect=AssertionError('redundant verify')),patch.object(h,'qa_start',side_effect=AssertionError('redundant QA')),patch.object(h,'finalization_module',return_value=self.fx.clean),patch.object(self.fx.clean,'start'):
            h.finish(self.s)
        self.assertEqual(self.s['approval'],approval);self.assertEqual(self.s['stage'],'awaiting_ci');self.assertEqual(self.fx.fake.calls,2)

    def test_retry_rejects_changed_product_or_evidence(self):
        self.s.update(stage='blocked',blockedPhase='publishing')
        proof=h.jobdir(self.s['id'])/'artifacts/proof.txt';proof.write_text('changed')
        with self.assertRaisesRegex(h.Blocked,'Evidencia'):h.finish(self.s)
        (self.fx.back/'src/main/app.txt').write_text('changed product')
        with self.assertRaisesRegex(h.Blocked,'producto'):h.finish(self.s)
        self.assertEqual(self.fx.fake.calls,0)

    def test_receipt_archive_corruption_rejects_publication_retry(self):
        log=h.jobdir(self.s['id'])/'check.log';log.write_text('pass')
        xml=h.jobdir(self.s['id'])/'check.xml';xml.write_text('<testsuite tests="1"/>')
        self.s['receipts']={'checks':{'status':'passed','log':str(log),'logSha256':h.sha(log),'archives':{str(xml):h.sha(xml)}}}
        self.s['approvalEvidence']=self.pub.evidence(self.ctx,self.s)
        xml.write_text('changed')
        with self.assertRaisesRegex(h.Blocked,'Evidencia de checks'):self.pub.retry(self.ctx,self.s)

    def test_partial_remote_publication_retry_keeps_commits(self):
        self.fx.fake.error_repo='front'
        with self.assertRaises(h.Blocked):self.fx.publish()
        commits={k:v['commit'] for k,v in self.s['publication']['repos'].items()}
        self.s.update(stage='blocked',blockedPhase='publishing');self.fx.fake.error_repo=None
        self.pub.retry(self.ctx,self.s);self.fx.publish()
        self.assertEqual(commits,{k:v['commit'] for k,v in self.s['publication']['repos'].items()});self.assertEqual(self.fx.fake.calls,2)

    def test_updated_ignored_input_is_archived_without_overwriting_original(self):
        relative='specs/input.md';self.s['specs']=[relative]
        (self.fx.root/'.git/info/exclude').write_text('specs/\n')
        for tree in (self.fx.root,self.fx.back):(tree/relative).write_text('original')
        self.pub.capture(self.ctx,self.s)
        copied=self.fx.back/relative;copied.write_text('final decisions')
        self.pub.archive_final_specs(self.ctx,self.s)
        self.assertTrue(self.fx.clean.known_generated(self.ctx,self.s,'back',relative))
        self.assertEqual((self.fx.root/relative).read_text(),'original')
        item=self.s['publication']['repos']['back']
        self.assertEqual((h.jobdir(self.s['id'])/item['copiedSpecs'][relative]['archive']).read_text(),'original')
        self.assertEqual((h.jobdir(self.s['id'])/item['finalSpecs'][relative]['archive']).read_text(),'final decisions')
        copied.write_text('post approval change')
        self.assertFalse(self.fx.clean.known_generated(self.ctx,self.s,'back',relative))

    def test_generated_gradle_only_new_unchanged_files(self):
        mod=h.runtime_module('generated');tree=self.fx.web
        (tree/'android/.gradle').mkdir(parents=True);old=tree/'android/.gradle/old';old.write_text('IDE data')
        baseline=mod.before(self.ctx,self.s,{'repo':'front','cwd':'android','command':['./gradlew','test']})
        new=tree/'android/.gradle/new';new.write_text('created by command')
        escaped=tree/'android/.gradle/link';escaped.symlink_to('/etc/hosts')
        mod.after(self.ctx,self.s,baseline)
        self.assertTrue(self.fx.clean.known_generated(self.ctx,self.s,'front','android/.gradle/new'))
        for name in ('old','link'):self.assertFalse(self.fx.clean.known_generated(self.ctx,self.s,'front','android/.gradle/'+name))
        new.write_text('modified later');self.assertFalse(self.fx.clean.known_generated(self.ctx,self.s,'front','android/.gradle/new'))

    def test_unknown_ignored_outputs_block_before_any_remote_publication(self):
        self.s['stage']='documenting'
        (self.fx.back/'unknown').mkdir();(self.fx.back/'unknown/file').write_text('private')
        (self.fx.root/'.git/info/exclude').write_text('unknown/\n')
        with self.assertRaisesRegex(h.Blocked,'ignorados desconocidos'):h.finish(self.s)
        self.assertEqual(self.fx.fake.calls,0)


class ReadRetryTests(unittest.TestCase):
    def test_github_get_retries_timeout_once_but_not_denial(self):
        spec=importlib.util.spec_from_file_location('ghretry',fixture.existing.MODULE.parent/'github.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with patch.object(h,'run',side_effect=[subprocess.TimeoutExpired('gh',30),'{}']) as run,patch.object(mod.time,'sleep'):
            self.assertEqual(mod.api(h.CoreContext(),'repos/example/repo'),{});self.assertEqual(run.call_count,2)
        with patch.object(h,'run',side_effect=h.Blocked('denied')) as run:
            with self.assertRaises(h.Blocked):mod.api(h.CoreContext(),'repos/example/repo')
            self.assertEqual(run.call_count,1)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.existing.HarnessTests();self.fx.setUp();self.s=self.fx.s
        self.info=self.fx.docker_info()[0]
        self.info['HostConfig']={'PortBindings':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'5555'}]}}
        self.info['Mounts']=[{'Name':'vol-1','Destination':'/var/lib/postgresql/data'}]
        self.info['State']['Running']=False

    def tearDown(self):self.fx.tearDown()

    def run_mock(self,argv,**kwargs):
        if argv[:3]==['docker','volume','inspect']:return json.dumps([{'Labels':{h.LABEL:'key-1'}}])
        if argv[:2]==['docker','inspect']:return json.dumps([self.info])
        if argv[:2]==['docker','start']:self.info['State']['Running']=True;return 'started'
        raise AssertionError(argv)

    def test_resume_starts_only_owned_clone_without_restore_or_cycle(self):
        with patch.object(h,'run',side_effect=self.run_mock) as run,patch.object(h,'stop_preview'),patch.object(h,'release_browser'):
            h.resume_runtime(self.s)
        self.assertEqual(self.s['stage'],'blocked');self.assertEqual(self.s['cycles'],0)
        self.assertFalse(any('pg_restore' in c.args[0] for c in run.call_args_list))

    def test_resume_rejects_foreign_and_legacy_dynamic_clones(self):
        for mutation in ('id','port','volume'):
            info=json.loads(json.dumps(self.info))
            if mutation=='id':self.info['Id']='foreign'
            if mutation=='port':self.info['HostConfig']['PortBindings']['5432/tcp'][0]['HostPort']=''
            if mutation=='volume':self.info['Mounts']=[]
            with patch.object(h,'run',side_effect=self.run_mock) as run:
                with self.assertRaises(h.Blocked):h.resume_runtime(self.s)
                self.assertFalse(any(c.args[0][:2]==['docker','start'] for c in run.call_args_list))
            self.info=info

    def test_stopped_clone_diagnostic_is_not_a_loopback_error(self):
        self.info['NetworkSettings']['Ports']={}
        with patch.object(h,'run',side_effect=self.run_mock) as run:
            with self.assertRaisesRegex(h.RuntimeStopped,'detenido'):h.db_info(self.s)
            self.assertFalse(any(c.args[0][:2]==['docker','start'] for c in run.call_args_list))

    def test_verify_recovers_interrupted_owned_clone_and_preserves_receipts(self):
        self.s.update(stage='checking',executionOwner={'boot':'previous'},operationStartedAt=time.time()-30,
                      receipts={'prior':{'status':'passed'}},qa={'status':'pass'},cycles=1)
        with patch.object(h,'run',side_effect=self.run_mock) as run,patch.object(h,'stop_preview'),patch.object(h,'release_browser'),patch.object(h,'migrations_compatible',return_value=False):
            # Stop immediately after runtime recovery, before any new checks.
            with self.assertRaisesRegex(h.Blocked,'Cambió una migración'):h.verify(self.s)
        self.assertEqual(self.s['cycles'],1)
        self.assertEqual(self.s['receipts'],{'prior':{'status':'passed'}})
        self.assertNotIn('qa',self.s)
        self.assertEqual(len(self.s['interruptions']),1)
        self.assertEqual(sum(c.args[0][:2]==['docker','start'] for c in run.call_args_list),1)
        self.assertFalse(any('pg_restore' in c.args[0] for c in run.call_args_list))

    def test_uncertain_migration_never_starts_clone(self):
        self.s['migrationFailed']=True
        with patch.object(h,'run',side_effect=AssertionError('must not start')),patch.object(h,'release_browser'):
            with self.assertRaisesRegex(h.Blocked,'Migración'):h.verify(self.s)
            with self.assertRaisesRegex(h.Blocked,'Migración'):h.resume_runtime(self.s)


class SelectorTests(unittest.TestCase):
    def test_nested_upgrade_preserves_exclusions_custom_selection_and_is_idempotent(self):
        spec=importlib.util.spec_from_file_location('test_selectors',fixture.existing.MODULE.parent/'test_selectors.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        cfg={'checks':[{'command':['./mvnw','verify','-Dtest=*Test,*Tests,!RemoteTest#paid','-Dit.test=*IT,!MailIT']}],
             'integrationChecks':[{'command':['mvn','test','-Dtest=SpecificTest#method']}]}
        upgraded=mod.upgrade(cfg);command=upgraded['checks'][0]['command']
        self.assertEqual(command[2],'-Dtest=*Test,*Tests,!RemoteTest#paid,*Test$*,*Tests$*')
        self.assertEqual(command[3],'-Dit.test=*IT,!MailIT,*IT$*,!MailIT$*')
        self.assertEqual(upgraded['integrationChecks'],cfg['integrationChecks'])
        self.assertEqual(mod.upgrade(upgraded),upgraded)
        self.assertNotEqual(upgraded,cfg)


class RecoveredMetricsTests(unittest.TestCase):
    def test_transferred_sessions_are_linked_deduplicated_and_missing_reported(self):
        fx=fixture.PortabilityTests();fx.setUp()
        try:
            spec=importlib.util.spec_from_file_location('metrics_test',fixture.existing.MODULE.parent/'metrics.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            s=fx.s;s['sessionId']='current';s['metricsWindow']={'startedAt':1,'endedAt':100}
            s['conversationHistory']=[{'source':'UserPromptSubmit','from':'missing','to':'old'},{'source':'UserPromptSubmit','from':'old','to':'current'}]
            def row(sid,message):return json.dumps({'timestamp':'1970-01-01T00:00:10Z','type':'assistant','sessionId':sid,'message':{'id':message,'usage':{'input_tokens':5},'content':[]}})+'\n'
            (fx.root/'current.jsonl').write_text(row('current','shared'))
            (fx.root/'old.jsonl').write_text(row('old','shared')+row('old','prior'))
            result=mod.summarize(h.CoreContext(),s,str(fx.root/'current.jsonl'))
            self.assertEqual(result['tokens']['total']['input_tokens'],10)
            self.assertEqual(result['tokens']['missingSessions'],['missing']);self.assertTrue(result['tokens']['partial'])
        finally:fx.tearDown()

    def test_window_excludes_previous_followup_and_other_session_messages(self):
        fx=fixture.PortabilityTests();fx.setUp()
        try:
            spec=importlib.util.spec_from_file_location('metrics_window_test',fixture.existing.MODULE.parent/'metrics.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
            s=fx.s;s['sessionId']='current'
            s['stage']='completed';s['metricsWindow']={'startedAt':10,'endedAt':50}
            s['conversationHistory']=[{'source':'UserPromptSubmit','from':'old','to':'current','at':30}]
            def row(sid,mid,at,tokens=5):
                return json.dumps({'timestamp':at,'type':'assistant','sessionId':sid,
                  'message':{'id':mid,'usage':{'input_tokens':tokens,'output_tokens':3},'content':[]}})+'\n'
            (fx.root/'current.jsonl').write_text(row('current','before','1970-01-01T00:00:20Z',1000)+
                row('current','ok','1970-01-01T00:00:40Z')+row('current','ok','1970-01-01T00:00:41Z',7)+
                row('current','after','1970-01-01T00:01:00Z',1000)+row('current','undated',None,1000))
            (fx.root/'old.jsonl').write_text(row('old','previous','1970-01-01T00:00:01Z',1000)+
                row('old','prior','1970-01-01T00:00:20Z')+row('old','unrelated','1970-01-01T00:00:40Z',1000))
            result=mod.summarize(h.CoreContext(),s,str(fx.root/'current.jsonl'))['tokens']
            self.assertEqual(result['total']['input_tokens'],12)
            self.assertEqual(result['total']['output_tokens'],6)
            self.assertTrue(result['partial'])
            self.assertEqual(result['agents'][0]['inputContextTokens']['max'],7)
            self.assertEqual(result['agents'][0]['undatedAssistantRows'],1)
        finally:fx.tearDown()

    def test_terminal_boundary_does_not_expand_when_closed_job_is_saved_again(self):
        fx=fixture.PortabilityTests();fx.setUp()
        try:
            s=fx.s;s['stage']='completed'
            h.save(s,'completed');end=s['metricsWindow']['endedAt']
            h.save(s,'read_followup')
            self.assertEqual(s['metricsWindow']['endedAt'],end)
        finally:fx.tearDown()
