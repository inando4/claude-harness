"""GitHub adapter using the installed gh API interface (no model calls)."""
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote


def api(h,endpoint):
    for attempt in range(2):
        try:return json.loads(h.run(['gh','api','--method','GET',endpoint],timeout=30))
        except subprocess.TimeoutExpired:
            if attempt:raise
            time.sleep(1)
    raise AssertionError('unreachable')


def pages(h,endpoint,key):
    rows=[];page=1
    while True:
        value=api(h,endpoint+('&' if '?' in endpoint else '?')+f'per_page=100&page={page}')
        batch=value[key] if key else value
        rows.extend(batch)
        if len(batch)<100:return rows
        page+=1


def required_checks(h,repo,policy):
    branch=api(h,f'repos/{repo}/branches/'+quote(policy['base'],safe=''))
    protection=branch.get('protection',{}).get('required_status_checks',{}) or {}
    required=set(policy.get('requiredChecks',[])) | set(protection.get('contexts',[]))
    required.update(v['context'] for v in protection.get('checks',[]))
    # Rulesets may protect a branch without classic branch protection.
    rules=pages(h,f'repos/{repo}/rules/branches/'+quote(policy['base'],safe=''),None)
    for rule in rules:
        if rule['type']=='required_status_checks':
            required.update(v['context'] for v in rule['parameters']['required_status_checks'])
    return required


def preflight(h,tree,policy):
    url=h.run(['git','remote','get-url','--all','--push',policy['remote']],cwd=tree)
    match=re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?',url)
    h.require(match,'El remoto de publicación debe ser GitHub sin credenciales en la URL.')
    h.require(h.run(['git','remote','get-url','--all',policy['remote']],cwd=tree)==url,
              'Usa un único destino idéntico para lectura y push; se conservan los remotos existentes.')
    repo=match.group(1)
    data=api(h,'repos/'+repo)
    h.require(data.get('permissions',{}).get('push') and not data.get('archived'),'Sin permisos de escritura o repositorio archivado.')
    required_checks(h,repo,policy)
    for workflow in policy['workflows']:
        wf=api(h,'repos/'+repo+'/actions/workflows/'+quote(workflow,safe=''))
        h.require(wf.get('state')=='active','Workflow CI inactivo: '+workflow)
    for key in ['user.name','user.email']:
        h.require(h.run(['git','config','--get',key],cwd=tree),'Falta identidad Git: '+key)
    return {'repository':repo,'remoteUrl':url,'base':policy['base']}


def setup(h):
    cfg=h.publication_module().settings(h)
    if not cfg.get('enabled'):return {'status':'disabled'}
    h.publication_module().validate(h)
    targets={name:preflight(h,h.repository_root(None,name),item) for name,item in cfg['repositories'].items()}
    return {'status':'passed','targets':targets}


def validate_pr(h,item,policy,pr):
    """Validate the exact published revision, including squash/rebase merges."""
    repo=item['github']['repository']
    h.require(pr.get('head',{}).get('sha')==item['commit'],
              'El head del PR cambió después de publicar: esperado '+item['commit']+
              ', actual '+str(pr.get('head',{}).get('sha'))+
              '. No es el SHA del squash/merge. Se conserva el trabajo; revisa los commits posteriores.')
    h.require(pr.get('number')==item['pr']['number'] and
              pr.get('head',{}).get('sha')==item['commit'] and
              pr.get('base',{}).get('ref')==policy['base'] and
              pr.get('base',{}).get('repo',{}).get('full_name','').lower()==repo.lower(),
              'Cambió la identidad, revisión o destino del PR; se conserva el trabajo.')
    if pr.get('state')=='open':
        h.require(not pr.get('merged'), 'Estado del PR inconsistente.')
        return 'open'
    h.require(pr.get('state')=='closed' and pr.get('merged') is True and pr.get('merged_at'),
              'PR cerrado sin fusionar; se conserva el trabajo.')
    merge=pr.get('merge_commit_sha')
    h.require(merge, 'Falta identidad de la fusión; se conserva el trabajo.')
    # GitHub returns the resulting commit for merge, squash and rebase. Check that
    # result against the current destination, not ancestry of a pre-squash head.
    comparison=api(h,f'repos/{repo}/compare/{quote(merge,safe="")}...{quote(policy["base"],safe="")}')
    h.require(comparison.get('status') in {'ahead','identical'} and
              comparison.get('merge_base_commit',{}).get('sha')==merge,
              'La fusión no está respaldada en el destino remoto; se conserva el trabajo.')
    return 'merged'


def cleanup_pr(h,item,policy):
    pr=api(h,f'repos/{item["github"]["repository"]}/pulls/{item["pr"]["number"]}')
    state=validate_pr(h,item,policy,pr)
    if state=='open':
        remote=h.publication_module().out(h,item['source'],'ls-remote',policy['remote'],'refs/heads/'+item['branch'])
        h.require(remote and remote.split()[0]==item['commit'],'Respaldo remoto cambió; no se limpia.')
    return state


def run_belongs_to_pr(run,pr,item,workflow):
    """GitHub drops pull_requests associations after merge; retain exact-run proof."""
    if run.get('head_sha')!=item['commit']:return False
    associated=run.get('pull_requests',[])
    if associated:return any(p.get('number')==pr['number'] for p in associated)
    if not pr.get('merged') or run.get('event')!='pull_request':return False
    prior=item.get('ci',{})
    if prior.get('commit')==item['commit']:
        expected=f'https://github.com/{item["github"]["repository"]}/actions/runs/{run.get("id")}'
        for proof in prior.get('details',[]):
            if proof.get('workflow')==workflow and (proof.get('runId')==run.get('id') or proof.get('url')==expected):
                return True
    # Merge before the first poll: match the branch, source repository and PR's
    # lifetime too. Missing metadata is not evidence and leaves CI pending.
    created=run.get('created_at');opened=pr.get('created_at');merged=pr.get('merged_at')
    source=pr.get('head',{}).get('repo') or {}
    return bool(created and opened and merged and opened<=created<=merged and
                source.get('id') and source.get('id')==(run.get('head_repository') or {}).get('id') and
                pr['head'].get('ref') and pr['head']['ref']==run.get('head_branch'))


def ensure_pr(h,s,name):
    item=s['publication']['repos'][name];target=item['github'];repo=target['repository']
    owner=repo.split('/')[0]
    endpoint='repos/'+repo+'/pulls?state=all&head='+quote(owner+':'+item['branch'],safe='')+'&base='+quote(target['base'],safe='')
    candidates=pages(h,endpoint,None)
    h.require(len(candidates)<=1,'Varios PR para la rama; requiere revisión.')
    if not candidates:
        directory=h.jobdir(s['id'])/'publication'/name
        body=directory/'pr.md'
        # Specs and prior verified summaries are already available; no extra agent to draft a PR.
        titles=[]
        for relative in s.get('specs',[]):
            text=(Path(s['worktree'])/relative).read_text()
            titles.append(next((v.lstrip('# ').strip() for v in text.splitlines() if v.startswith('# ')),Path(relative).stem))
        title=(' / '.join(titles) or s['id'])[:180]
        counts={k:v.get('tests') for k,v in s.get('tests',{}).items()}
        qa=s.get('qa',{})
        criteria='\n'.join('- '+str(v.get('id','criterio'))+': '+str(v.get('status','')) for v in qa.get('criteria',[]))
        body.write_text('Implementa '+title+'.\n\nValidación local aprobada por QA y revisión humana; documentación actualizada.\n\n'
                        +'Pruebas: '+json.dumps(counts,ensure_ascii=False)+'.\n\n'+criteria+'\n')
        h.run(['gh','pr','create','--repo',repo,'--base',target['base'],'--head',item['branch'],
               '--title',title,'--body-file',str(body)],cwd=item['worktree'],timeout=60)
        candidates=pages(h,endpoint,None)
    h.require(len(candidates)==1,'No se pudo confirmar el PR creado.')
    pr=api(h,f'repos/{repo}/pulls/{candidates[0]["number"]}')
    validate_pr(h,dict(item,pr={'number':pr['number']}),s['publication']['policy']['repositories'][name],pr)
    return {'number':pr['number'],'url':pr['html_url']}


def ci(h,item,policy):
    """Require observed, successful configured workflows plus required status/check contexts."""
    repo=item['github']['repository'];number=item['pr']['number'];commit=item['commit']
    pr=api(h,f'repos/{repo}/pulls/{number}')
    validate_pr(h,item,policy,pr)
    state='passed';details=[]
    tested_merge=pr.get('merge_commit_sha') if not pr.get('merged') else (
        item.get('ci',{}).get('testedMergeSha') if item.get('ci',{}).get('commit')==commit else None)
    for workflow in policy['workflows']:
        endpoint=f'repos/{repo}/actions/workflows/{quote(workflow,safe="")}/runs?event=pull_request&head_sha={commit}'
        runs=[r for r in pages(h,endpoint,'workflow_runs') if run_belongs_to_pr(r,pr,item,workflow)]
        latest=max(runs,key=lambda r:(r['id'],r.get('run_attempt',1))) if runs else None
        if not latest or latest.get('status')!='completed':result='pending'
        elif latest.get('conclusion')=='success':result='passed'
        else:result='failed'
        details.append({'workflow':workflow,'status':result,'url':latest.get('html_url') if latest else None,
                        'runId':latest.get('id') if latest else None,'runAttempt':latest.get('run_attempt') if latest else None})
        if result=='failed':state='failed'
        elif result=='pending' and state!='failed':state='pending'
    # Protected-branch requirements are inspected at runtime so changes cannot silently weaken the gate.
    required=required_checks(h,repo,policy)
    if required:
        # GitHub may run checks against the PR's test-merge commit; both must belong to this exact PR head.
        refs=[commit]
        if tested_merge:refs.append(tested_merge)
        values={}
        for ref in refs:
            current={}
            for v in pages(h,f'repos/{repo}/commits/{ref}/check-runs','check_runs'):
                key=v['name'];stamp=v.get('completed_at') or v.get('started_at') or ''
                if key not in current or stamp>=current[key][0]:
                    current[key]=(stamp,'pending' if v['status']!='completed' else 'passed' if v['conclusion']=='success' else 'failed')
            for v in pages(h,f'repos/{repo}/commits/{ref}/statuses',None):
                key=v['context'];stamp=v.get('updated_at','')
                if key not in current or stamp>=current[key][0]:current[key]=(stamp,'passed' if v['state']=='success' else 'pending' if v['state']=='pending' else 'failed')
            # A check present on the current test-merge revision takes precedence over head.
            values.update(current)
        for key in sorted(required):
            result=values.get(key,('', 'pending'))[1];details.append({'check':key,'status':result})
            if result=='failed':state='failed'
            elif result=='pending' and state!='failed':state='pending'
    return {'status':state,'details':details,'commit':commit,'testedMergeSha':tested_merge}
