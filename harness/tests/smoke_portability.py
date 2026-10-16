#!/usr/bin/env python3
"""Opt-in: install into disposable Python/Node repositories and run the real controller."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

BUNDLE=Path(__file__).resolve().parents[1]


def module(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def git(root,*args):
    subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true')
    if not parser.parse_args().run:parser.error('Explicit --run required')
    install=module(BUNDLE/'install.py','install').install
    with tempfile.TemporaryDirectory(prefix='harness-portability-') as tmp:
        for stack in ('python','node','separate-repos'):
            root=Path(tmp)/stack;root.mkdir();git(root,'init','-q')
            git(root,'config','user.name','Harness Test');git(root,'config','user.email','harness@example.invalid')
            install(root,'auto')
            (root/'specs').mkdir(exist_ok=True);(root/'specs/feature.md').write_text('Estado: borrador\n\nAC-1: localhost responds with synthetic fixture content.')
            with (root/'.gitignore').open('a') as f:f.write('.harness/reports/\ngraphify-out/\n')
            if stack=='python':
                (root/'server.py').write_text('import http.server,sys\nhttp.server.ThreadingHTTPServer(("127.0.0.1",int(sys.argv[1])),http.server.SimpleHTTPRequestHandler).serve_forever()\n')
                command=[sys.executable,'server.py','${PORT_WEB}']
            else:
                (root/'server.cjs').write_text('require("http").createServer((req,res)=>res.end("synthetic fixture")).listen(Number(process.argv[2]),"127.0.0.1");\n')
                command=['node','server.cjs','${PORT_WEB}']
            (root/'checks.py').write_text('''from pathlib import Path
assert 'AC-1: localhost responds' in Path('specs/feature.md').read_text()
Path('.harness/reports').mkdir(parents=True,exist_ok=True)
c=Path('.harness/reports/count');c.write_text(str(int(c.read_text())+1 if c.exists() else 1))
Path('.harness/reports/junit.xml').write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
''')
            cfg=json.loads((root/'harness/config.json').read_text())
            cfg.update(needsConfiguration=False,requiredTools=['node' if stack!='python' else 'python3'],
                       setup=[],build=[],checks=[{'command':[sys.executable,'checks.py']}],
                       services=[{'name':'web','command':command,'healthPath':'/','artifacts':[]}])
            if stack=='node':
                cfg['services'].append({'name':'api','command':['node','server.cjs','${PORT_API}'],'healthPath':'/','artifacts':[]})
            if stack=='separate-repos':
                front=Path(tmp)/'frontend';front.mkdir();git(front,'init','-q')
                git(front,'config','user.name','Harness Test');git(front,'config','user.email','harness@example.invalid')
                (front/'.gitignore').write_text('graphify-out/\n.harness/reports/\n')
                (front/'server.cjs').write_text('const http=require("http"); http.createServer((req,res)=>http.get(process.env.API_URL,r=>r.pipe(res))).listen(Number(process.argv[2]),"127.0.0.1");\n')
                (front/'checks.py').write_text("from pathlib import Path\nassert 'process.env.API_URL' in Path('server.cjs').read_text()\nPath('.harness/reports').mkdir(parents=True,exist_ok=True)\nPath('.harness/reports/junit.xml').write_text('<testsuite tests=\"1\"/>')\n")
                git(front,'add','.');git(front,'commit','-qm','Synthetic frontend')
                cfg.update(primaryRepository='backend',repositories={'backend':{'path':'.'},'frontend':{'path':str(front)}})
                cfg['checks'].append({'repo':'frontend','command':[sys.executable,'checks.py']})
                cfg['testReports']['frontend']={'repo':'frontend','glob':'.harness/reports/junit.xml'}
                cfg['services']=[{'name':'api','command':['node','server.cjs','${PORT_API}'],'healthPath':'/'},
                                 {'name':'web','repo':'frontend','command':['node','server.cjs','${PORT_WEB}'],
                                  'healthPath':'/','env':{'API_URL':'${URL_API}'}}]
                cfg['previewService']='web'
            for i,step in enumerate(cfg['checks']):
                repo=step.get('repo',cfg.get('primaryRepository','main'))
                reports=[name for name,value in cfg['testReports'].items()
                         if (value.get('repo',cfg.get('primaryRepository','main')) if isinstance(value,dict) else cfg.get('primaryRepository','main'))==repo]
                step.update(id='tests-'+repo,cache=True,usesDatabase=False,reports=reports,
                            inputs=[{'repo':repo,'exclude':['e2e/**']}])
            for service in cfg['services']:
                service['inputs']=[{'repo':service.get('repo'),'exclude':['e2e/**']}]
            (root/'harness/config.json').write_text(json.dumps(cfg))
            git(root,'add','.');git(root,'commit','-qm','Synthetic project')
            h=module(root/'harness/harness.py','harness_'+stack)
            jobs=[]
            setup=h.setup_module()
            result=setup.validate(h.CoreContext(),argparse.Namespace(snapshot=None,safe_snapshot=False,test_account=None))
            assert result['status']=='ready' and result['runtimeValidation']=='pending'
            # Synthetic user consent is separate from starting either job.
            readiness=h.runtime_module('spec_readiness');author_sid=str(h.uuid.uuid4())
            req=readiness.request(h.CoreContext(),'specs/feature.md',author_sid)
            h.runtime_module('conversation').prompt(h.CoreContext(),{
                'hook_event_name':'UserPromptSubmit','session_id':author_sid,'prompt':req['reply']})
            readiness.accept(h.CoreContext(),'specs/feature.md',author_sid)
            try:
                for name in ('first','second'):
                    sid = str(h.uuid.uuid4())
                    chat = h.runtime_module('conversation')
                    chat.prompt(h.CoreContext(), {'hook_event_name':'UserPromptSubmit', 'session_id':sid,
                                                 'prompt':'/harness specs/feature.md'})
                    args=argparse.Namespace(job=name,branch=None,base='HEAD',spec=['specs/feature.md'],launch=False,session_id=sid)
                    setup.start(h.CoreContext(),args);state=h.load(name);jobs.append(state)
                    assert state['conversation']=={'sessionId':sid,'mode':'inline','owned':False}
                    assert not state.get('launched') and not state.get('backgroundId')
                    setup.start(h.CoreContext(),args)  # Same conversation recovers the same worktree.
                assert len(list((h.STORE/'runs').glob('*/state.json')))==2
                assert jobs[0]['worktree']!=jobs[1]['worktree']
                assert not (set(jobs[0]['ports'].values()) & set(jobs[1]['ports'].values()))
                if stack=='separate-repos':
                    import urllib.request
                    for job in jobs:
                        h.verify(job);h.preview_valid(job)
                        assert urllib.request.urlopen(job['url']).read()==b'synthetic fixture'
                    assert len(set(jobs[0]['worktrees'].values())|set(jobs[1]['worktrees'].values()))==4
                    # Changes in frontend invalidate the ready-for-QA version.
                    front_file=Path(jobs[1]['worktrees']['frontend'])/'server.cjs'
                    original=front_file.read_text();front_file.write_text(original+'// changed\n')
                    try:h.qa_start(jobs[1]);raise AssertionError('stale frontend accepted')
                    except h.Blocked:pass
                    front_file.write_text(original)
                state=jobs[0]
                if stack!='separate-repos':h.verify(state)
                h.preview_valid(state)
                assert state['stage']=='ready_for_qa'
                assert json.loads((h.STORE/'setup.json').read_text())['runtimeValidation']['result']=='checks_and_preview_passed'
                assert len(state['previews'])==len(cfg['services'])
                first_pids={k:v['pid'] for k,v in state['previews'].items()}
                before_receipts=json.loads(json.dumps(state['receipts']))
                h.verify(state);h.preview_valid(state)
                assert {k:v['pid'] for k,v in state['previews'].items()}==first_pids
                assert state['receipts']==before_receipts
                assert all(row['status']=='reused' for row in state['steps'])
                # E2E-only addition keeps app checks/preview but refreshes that repository's graph.
                tree=Path(state['worktrees']['frontend'] if stack=='separate-repos' else state['worktree'])
                (tree/'e2e').mkdir();(tree/'e2e/new_test.py').write_text('assert True\n')
                h.verify(state);h.preview_valid(state)
                assert {k:v['pid'] for k,v in state['previews'].items()}==first_pids
                assert state['receipts']==before_receipts
                assert any(row['id'].startswith('graph-') and row['status']=='passed' for row in state['steps'])
                h.verify(state,force=True);h.preview_valid(state)
                assert state['receipts']!=before_receipts
                assert int((Path(state['worktree'])/'.harness/reports/count').read_text())==2
                assert {k:v['pid'] for k,v in state['previews'].items()}==first_pids
                product=tree/('server.py' if stack=='python' else 'server.cjs')
                product.write_text(product.read_text()+('\n# product edit\n' if stack=='python' else '\n// product edit\n'))
                h.verify(state);h.preview_valid(state)
                assert state['previews']['web']['pid']!=first_pids['web']
                if stack=='separate-repos':assert state['previews']['api']['pid']==first_pids['api']
                h.qa_start(state)
                # Deliberately no synthetic QA pass or human approval.
                h.release_browser(state)
                other=h.load('second');assert other['stage']==('ready_for_qa' if stack=='separate-repos' else 'implementing')
                print('PASS:',stack,'installation, setup, start, isolated worktrees/ports, checks, Graphify,',len(cfg['services']),'live service(s), cached checks, graph refresh, stable PIDs, force, QA gate')
            finally:
                for state in jobs:h.stop_preview(state)
    print('Disposable repositories removed. No Claude/model calls, original repository or database changes.')


if __name__=='__main__':main()
