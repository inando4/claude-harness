"""Publish an approved worktree through a private index, with durable checkpoints."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SHARED = ('.gitignore', 'CLAUDE.md', 'CLAUDE.MD', 'AGENTS.md')
INTERNAL = ('harness/', '.harness/', '.worktrees/', '.claude/skills/harness/', 'graphify-out/')
LOCAL = ('.harness-parent.json', 'docs/harness-claude-spec.md', 'docs/harness-publication-plan.md', 'docs/harness-optimization-plan.md')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def internal(name):
    return name in LOCAL or name.startswith(INTERNAL) or name.startswith('.claude/agents/harness-')


def forbidden(name):
    p=Path(name)
    return internal(name) or any(v.startswith('.env') or v in {'node_modules','__pycache__','target','dist','test-results','playwright-report'} for v in p.parts) or p.suffix in {'.pem','.key','.dump','.backup','.pyc'}


def git(h,tree,*args,data=None,env=None,check=True):
    p=subprocess.run(['git',*args],cwd=tree,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                     env=env,timeout=h.config()['timeoutSeconds'])
    if check:h.require(p.returncode==0,'Git: '+p.stderr.decode(errors='replace')[:350])
    return p


def out(h,tree,*args,**kwargs):
    return git(h,tree,*args,**kwargs).stdout.decode().strip()


def settings(h):
    # Derive one concrete cleanup policy from declared npm installation steps.
    # Arbitrary build outputs are never automatically disposable.
    cfg=h.config(); policy=json.loads(json.dumps(cfg.get('publication',{'enabled':False})))
    if not policy.get('enabled'):return policy
    for step in cfg.get('setup',[]):
        command=step.get('command',[])
        if len(command)<2 or Path(command[0]).name!='npm' or command[1]!='ci':continue
        path=str(Path(step.get('cwd','.'))/'node_modules')
        if path not in step.get('outputs',[]):continue
        name=step.get('repo') or cfg.get('primaryRepository','main')
        if name not in policy.get('repositories',{}):continue
        h.safe_path(h.repository_root(None,name),path)
        paths=policy['repositories'][name].setdefault('disposablePaths',[])
        if path not in paths:paths.append(path)
    return policy


def validate(h):
    cfg=settings(h)
    h.require(type(cfg.get('enabled',False)) is bool,'publication.enabled debe ser booleano.')
    if not cfg.get('enabled'):return
    roots=h.repository_roots()
    h.require(set(cfg.get('repositories',{}))==set(roots),'Configura publicación para cada repositorio.')
    h.require(5<=cfg.get('pollSeconds',30)<=60 and 30<=cfg.get('timeoutSeconds',3600)<=3600,'Intervalo/timeout de publicación inválido.')
    for name,item in cfg['repositories'].items():
        h.require(re.fullmatch(r'[A-Za-z0-9_.-]+',item.get('remote','')),'Remoto inválido.')
        git(h,roots[name],'check-ref-format','refs/heads/'+item.get('base',''))
        workflows=item.get('workflows',[])
        h.require(workflows and all(re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml',v) for v in workflows),'Declara workflows CI esperados.')
        h.require(isinstance(item.get('requiredChecks',[]),list),'requiredChecks debe ser lista.')
        for value in item.get('disposablePaths',[]):
            h.require(value not in {'','.','..'} and not Path(value).is_absolute(),'Ruta desechable inválida.')
            h.safe_path(roots[name],value)


def repository_plan(h,s):
    if s.get('repositories'):return s['repositories']
    name=h.config().get('primaryRepository','main')
    return {name:{'source':str(h.ROOT),'base':s['base'],'branch':s['branch'],'created':True}}


def capture(h,s):
    """Called after controller overlays, before any product implementation."""
    if not settings(h).get('enabled'):return
    result={'version':1,'executor':h.controller_version(),'policy':settings(h),'repos':{},'phase':'prepared'}
    for name,item in repository_plan(h,s).items():
        tree=h.repository_root(s,name);overlays={}
        for path in SHARED:
            original=git(h,tree,'show',item['base']+':'+path,check=False)
            prepared=tree/path
            current=prepared.read_bytes() if prepared.is_file() else None
            base=original.stdout if original.returncode==0 else None
            if base!=current:
                overlays[path]={'base':base64.b64encode(base).decode() if base is not None else None,
                                'prepared':base64.b64encode(current).decode() if current is not None else None}
        owned=git(h,tree,'ls-files','-z','--cached','--others','--exclude-standard').stdout.decode().split('\0')
        owned+=git(h,tree,'ls-files','-z','--others','--ignored','--exclude-standard').stdout.decode().split('\0')
        result['repos'][name]={'ownedFiles':[v for v in owned if internal(v)],'overlays':overlays,'base':item['base'],'source':item['source'],
                               'branch':item['branch'],'worktree':str(tree),
                               'commonDir':str((tree/out(h,tree,'rev-parse','--git-common-dir')).resolve())}
        # Only start's primary worktree receives the input specs. Keep an exact
        # backup outside it; an ignored directory is never blanket-disposable.
        copies={}
        if tree == Path(s['worktree']):
            for relative in s.get('specInputFiles', s.get('specs', [])):
                source=h.safe_path(h.ROOT,relative);copied=h.safe_path(tree,relative)
                if git(h,tree,'ls-files','--error-unmatch','--',relative,check=False).returncode==0:continue
                h.require(copied.is_file() and not (tree/relative).is_symlink() and copied.read_bytes()==source.read_bytes(),
                          'La copia de la spec no coincide con su origen: '+relative)
                archive=h.safe_path(h.jobdir(s['id']),'artifacts/input-specs/'+relative)
                archive.parent.mkdir(parents=True,exist_ok=True)
                archive.write_bytes(copied.read_bytes())
                copies[relative]={'sha256':h.sha(copied),'archive':str(archive.relative_to(h.jobdir(s['id'])))}
        result['repos'][name]['copiedSpecs']=copies
    s['publication']=result
    h.save(s,'publication_preparation_recorded')


def normalized(h,tree,name,overlays):
    p=tree/name
    current=p.read_bytes() if p.is_file() else None
    record=overlays.get(name)
    if not record:return current
    decode=lambda v:base64.b64decode(v) if v is not None else None
    original,prepared=decode(record['base']),decode(record['prepared'])
    if current==prepared:return original
    # Remove only the recorded preparation delta. Overlapping edits need human resolution.
    with tempfile.TemporaryDirectory(prefix='harness-overlay-') as tmp:
        paths=[Path(tmp)/v for v in ['original','prepared','current']]
        for path,data in zip(paths,[original,prepared,current]):path.write_bytes(data or b'')
        merged=git(h,tree,'merge-file','-p',*[str(p) for p in paths],check=False)
        h.require(merged.returncode==0,'No se puede separar el overlay de '+name+'. Conserva el worktree y resuelve el cambio.')
        return merged.stdout or (None if original is None else b'')


def assert_history(h,tree,base,disposable=()):
    h.require(git(h,tree,'merge-base','--is-ancestor',base,'HEAD',check=False).returncode==0,'La rama ya no desciende de su base.')
    for commit in out(h,tree,'rev-list',base+'..HEAD').splitlines():
        paths=out(h,tree,'diff-tree','--root','-m','--no-commit-id','--name-only','-r',commit).splitlines()
        h.require(not any(forbidden(p) or any(p==v or p.startswith(v+'/') for v in disposable) for p in paths),
                  'Historial nuevo contiene archivos internos/sensibles; no se publicará.')


def inventory(h,tree):
    # Include tracked deletions and new versionable files, never silently drop ignored tracked files.
    names=git(h,tree,'ls-files','-z','--cached','--others','--exclude-standard').stdout.decode().split('\0')
    result={}
    for name in sorted(set(names)):
        if not name or internal(name):continue
        p=tree/name
        if p.is_symlink():result[name]=['link',os.readlink(p)]
        elif p.is_file():result[name]=['file',h.sha(p),p.stat().st_mode&0o777]
        else:result[name]=['deleted']
    return result


def index_ready(h,s):
    for name,tree in h.repository_roots(s).items():
        conflicts=out(h,tree,'diff','--name-only','--diff-filter=U').splitlines()
        partial=set(out(h,tree,'diff','--cached','--name-only').splitlines()) & set(out(h,tree,'diff','--name-only').splitlines())
        h.require(not conflicts and not partial,'Índice Git requiere revisión en '+name+': '+', '.join(sorted(set(conflicts)|partial))+'. Conserva contenido y resuelve el índice; no exige repetir QA por sí solo.')


def evidence(h,s):
    report=s.get('qa',{})
    h.require(report.get('status')=='pass' and s.get('approval',{}).get('version')==s.get('approvedCandidate'),'Aprobación o QA no vigentes.')
    paths={name:h.sha(h.safe_path(h.jobdir(s['id'])/'artifacts',name)) for name in report.get('artifacts',[])}
    for name,receipt in s.get('receipts',{}).items():
        log=Path(receipt.get('log',''))
        h.require(receipt.get('status')=='passed' and log.resolve().is_relative_to(h.jobdir(s['id']).resolve()) and
                  log.is_file() and h.sha(log)==receipt.get('logSha256') and h.verification_module().archives_valid(h,s,receipt),
                  'Evidencia de checks alterada o ausente: '+name)
    qa=h.jobdir(s['id'])/'qa.json'
    h.require(qa.is_file(),'Falta reporte QA aprobado.')
    return digest({'qa':h.sha(qa),'report':report,'artifacts':paths,'receipts':s.get('receipts',{}),
                   'approval':s['approval'],'config':s.get('configVersion'),'controller':h.controller_version()})


def retry(h,s):
    h.require(s.get('approvalEvidence') and evidence(h,s)==s['approvalEvidence'],'Evidencia de aprobación ausente o modificada: revalidar.')
    pub=s['publication']
    # Never discard a commit checkpoint or reconcile a crash by rewriting HEAD.
    started=any(v.get('commit') or v.get('pushed') or
                (v.get('headBefore') and out(h,v['worktree'],'rev-parse','HEAD')!=v['headBefore']) for v in pub['repos'].values())
    if started:unchanged(h,s)
    else:
        for item in pub['repos'].values():
            for key in ('tree','inventory','headBefore'):item.pop(key,None)
    h.save(s,'publication_retry_validated')


def archive_final_specs(h,s):
    for name,item in s['publication']['repos'].items():
        for relative,original in item.get('copiedSpecs',{}).items():
            copied=h.safe_path(Path(item['worktree']),relative)
            h.require(copied.is_file() and not (Path(item['worktree'])/relative).is_symlink(),'Spec de entrada ausente o enlazada: '+relative)
            if h.sha(copied)==original['sha256']:continue
            if any(v.get('commit') or (v.get('headBefore') and out(h,v['worktree'],'rev-parse','HEAD')!=v['headBefore']) for v in s['publication']['repos'].values()):
                h.require(h.finalization_module().known_generated(h,s,name,relative),'Spec modificada después del checkpoint de publicación; conserva el trabajo y registra feedback.')
                continue
            archive=h.safe_path(h.jobdir(s['id']),'artifacts/final-specs/'+name+'/'+relative)
            archive.parent.mkdir(parents=True,exist_ok=True)
            archive.write_bytes(copied.read_bytes());archive.chmod(0o600)
            item.setdefault('finalSpecs',{})[relative]={'sha256':h.sha(copied),'archive':str(archive.relative_to(h.jobdir(s['id'])))}
    h.save(s,'final_specs_archived')


def build_index(h,s,name,freeze=True):
    pub=s['publication'];item=pub['repos'][name];tree=Path(item['worktree'])
    branch=out(h,tree,'symbolic-ref','--short','HEAD')
    h.require(branch==item['branch'],'Cambió la rama del worktree.')
    disposable=pub['policy']['repositories'][name].get('disposablePaths',[])
    assert_history(h,tree,item['base'],disposable)
    # The private index must never discard staging-only work (including partial staging).
    staged=set(out(h,tree,'diff','--cached','--name-only').splitlines())
    unstaged=set(out(h,tree,'diff','--name-only').splitlines())
    h.require(not (staged & unstaged), 'Hay contenido parcialmente preparado en Git; revisa el índice antes de publicar.')
    for path,overlay in item['overlays'].items():
        head=git(h,tree,'show','HEAD:'+path,check=False)
        base=base64.b64decode(overlay['base']) if overlay['base'] is not None else None
        if item.get('overlayHead') and out(h,tree,'rev-parse','HEAD')==item['overlayHead']:
            prior=git(h,tree,'show',item['overlayHead']+':'+path,check=False)
            base=prior.stdout if prior.returncode==0 else None
        elif item.get('publishedCommit'):
            prior=git(h,tree,'show',item['publishedCommit']+':'+path,check=False)
            base=prior.stdout if prior.returncode==0 else None
        if not item.get('commit') and not item.get('tree'):
            h.require((head.stdout if head.returncode==0 else None)==base,'Commit previo contiene cambios en un archivo con overlays: '+path)
    root=h.jobdir(s['id'])/'publication'/name;root.mkdir(parents=True,exist_ok=True,mode=0o700)
    index=root/'index';index.unlink(missing_ok=True)
    env=dict(os.environ,GIT_INDEX_FILE=str(index),GRAPHIFY_SKIP_HOOK='1')
    git(h,tree,'read-tree','HEAD',env=env)
    # Hash only changed/new files, not every source file on every publication retry.
    names=set(git(h,tree,'diff','HEAD','--name-only','-z').stdout.decode().split('\0'))
    names.update(git(h,tree,'ls-files','-z','--others','--exclude-standard').stdout.decode().split('\0'))
    names.update(item['overlays'])
    for path in sorted(names- {''}):
        if internal(path):continue
        h.require(not forbidden(path),'Archivo sensible/generado en selección publicable: '+path)
        h.require(not any(path==v or path.startswith(v+'/') for v in disposable),'Artefacto desechable en selección publicable: '+path)
        p=tree/path
        h.require(not p.is_symlink(),'Enlace no publicable automáticamente: '+path)
        data=normalized(h,tree,path,item['overlays'])
        if data is None:
            git(h,tree,'update-index','--force-remove','--',path,env=env)
        else:
            h.require(not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data),'Clave privada en archivo publicable: '+path)
            oid=out(h,tree,'hash-object','-w','--stdin',data=data)
            mode='100755' if p.exists() and p.stat().st_mode&0o111 else '100644'
            git(h,tree,'update-index','--add','--cacheinfo',mode,oid,path,env=env)
    expected=out(h,tree,'write-tree',env=env)
    if not freeze:return env
    if item.get('tree'):h.require(expected==item['tree'],'Cambió la selección congelada; requiere nueva revisión.')
    else:
        item.update(tree=expected,headBefore=out(h,tree,'rev-parse','HEAD'),inventory=inventory(h,tree))
        h.save(s,'publication_manifest_'+name)
    return env


def freeze(h,s):
    pub=s.get('publication')
    h.require(pub and pub.get('version')==1,'Trabajo anterior sin inventario de publicación; no se migra automáticamente.')
    h.require(pub['policy']==settings(h) and pub['executor']==h.controller_version(),
              'Cambió el controlador/política del trabajo. Se conserva la entrega; no repitas verify/QA para resolver esta huella. '+
              'Restaura la versión exacta desde el respaldo de instalación, o prepara una recuperación independiente. '+
              'No edites state.json ni hashes; actualizar el controlador requiere cerrar los trabajos activos.')
    h.require(s.get('approval') and h.job_fingerprint(s,True)==s['productBaseline'],'Falta aprobación vigente del producto.')
    pub['phase']='publishing'
    if h.runtime_module('synchronization').enabled(s):
        for name,item in pub['repos'].items():item['validatedBase']=s['synchronization']['bases'][name]
    for name in pub['repos']:build_index(h,s,name)
    s['stage']='publishing';pub['handoff']=True
    h.save(s,'publication_frozen')


def unchanged(h,s):
    for item in s['publication']['repos'].values():
        if item.get('worktreeRemoved'):continue
        tree=Path(item['worktree'])
        if not tree.exists() and item.get('removalStarted'):continue
        current=inventory(h,tree)
        # After committing a deletion Git no longer enumerates that path in its index.
        for path,value in item.get('inventory',{}).items():
            if value==['deleted'] and path not in current:current[path]=['deleted']
        h.require(current==item.get('inventory'),'Cambió contenido desde publicación; conserva el trabajo y registra feedback.')


def commit(h,s,name):
    item=s['publication']['repos'][name];tree=Path(item['worktree'])
    env=build_index(h,s,name)
    current=out(h,tree,'rev-parse','HEAD')
    if item.get('commit'):
        h.require(current==item['commit'],'La rama local cambió después de publicarse.');return
    # A crash after Git commit but before saving is reconciled by the exact expected tree/parent.
    if current!=item['headBefore']:
        h.require(out(h,tree,'rev-parse','HEAD^{tree}')==item['tree'] and
                  out(h,tree,'rev-parse','HEAD^')==item['headBefore'],'Commit inesperado durante recuperación.')
    elif out(h,tree,'rev-parse','HEAD^{tree}')!=item['tree']:
        msg=h.jobdir(s['id'])/'publication'/name/'commit.txt'
        msg.write_text('feat: '+s['id']+'\n')
        git(h,tree,'commit','-F',str(msg),env=env)
        h.require(out(h,tree,'rev-parse','HEAD^{tree}')==item['tree'],'Un hook cambió el commit; no se publicará.')
    unchanged(h,s)
    item['commit']=out(h,tree,'rev-parse','HEAD')
    item['changed']=out(h,tree,'rev-parse',item['base']+'^{tree}')!=item['tree']
    h.require(item['changed'] or item['commit']==item['base'],'Diff vacío pero hay commits locales sin PR; conserva y revisa el historial.')
    # Synchronize the normal index with HEAD; keep controller overlays in the working tree.
    git(h,tree,'read-tree','HEAD')
    h.save(s,'publication_commit_'+name)


def publish(h,s):
    h.require_job_config(s)
    pub=s['publication'];h.require(s.get('approval') and pub.get('handoff'),'Falta entrega autorizada a publicación.')
    h.require(pub['policy']==settings(h),'Cambió política de publicación.')
    unchanged(h,s)
    if h.runtime_module('synchronization').enabled(s):
        states=[h.github_module().cleanup_pr(h,v,pub['policy']['repositories'][n]) for n,v in pub['repos'].items() if v.get('pr') and v.get('pushed')]
        if 'merged' not in states:h.runtime_module('synchronization').guard(h,s)
    github=h.github_module()
    # Validate every repository before the first remote mutation.
    for name,item in pub['repos'].items():
        policy=pub['policy']['repositories'][name]
        if item.get('pushed'):
            h.require(out(h,item['worktree'],'remote','get-url','--all','--push',policy['remote'])==item['github']['remoteUrl'] and
                      out(h,item['worktree'],'remote','get-url','--all',policy['remote'])==item['github']['remoteUrl'],
                      'Cambió el remoto de publicación.')
            continue
        remote=github.preflight(h,Path(item['worktree']),policy)
        if item.get('github'):h.require(remote==item['github'],'Cambió destino de publicación.')
        item['github']=remote
        if h.runtime_module('synchronization').enabled(s):
            upstream=s['synchronization']['bases'][name]
        else:
            git(h,item['worktree'],'fetch',policy['remote'],policy['base']);upstream='FETCH_HEAD'
        h.require(git(h,item['worktree'],'merge-base','--is-ancestor',item['base'],upstream,check=False).returncode==0,
                  'La base local contiene commits ajenos al destino del PR.')
    for name in pub['repos']:commit(h,s,name)
    for name,item in pub['repos'].items():
        if not item['changed']:continue
        policy=pub['policy']['repositories'][name]
        if item.get('pr') and item.get('publishedCommit')==item['commit']:
            github.cleanup_pr(h,item,policy)
            continue
        tree=Path(item['worktree']);ref='refs/heads/'+item['branch']
        remote=out(h,tree,'ls-remote',policy['remote'],ref)
        if remote:
            oid=remote.split()[0]
            h.require(oid==item['commit'] or oid==item.get('publishedCommit'),
                      'La rama remota contiene otro trabajo; no se sobrescribe.')
        if not remote or remote.split()[0]!=item['commit']:
            git(h,tree,'push','--set-upstream',policy['remote'],item['commit']+':'+ref)
        h.require(out(h,tree,'ls-remote',policy['remote'],ref).split()[0]==item['commit'],'No se confirmó respaldo remoto.')
        item['pushed']=True;item['publishedCommit']=item['commit'];h.save(s,'publication_pushed_'+name)
        item['pr']=github.ensure_pr(h,s,name)
        h.save(s,'publication_pr_'+name)
    pub['phase']='awaiting_ci';s['stage']='awaiting_ci'
    h.save(s,'publication_all_prs_created')
    print(json.dumps({name:v.get('pr') for name,v in pub['repos'].items() if v['changed']},ensure_ascii=False))
