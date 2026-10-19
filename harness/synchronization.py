"""Boundary-triggered upstream integration. No model calls, force pushes or poll loop."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

FIELDS=('tree','inventory','headBefore','commit','changed','ci','pushed')


def ref(job,name):return 'refs/harness-sync/'+job+'/'+name


def fetch(h,tree,policy,job,name):
    pub=h.publication_module();target=ref(job,name)
    pub.git(h,tree,'fetch','--no-write-fetch-head',policy['remote'],
            '+refs/heads/'+policy['base']+':'+target)
    return pub.out(h,tree,'rev-parse',target+'^{commit}')


def initialize(h,s):
    if not s.get('publication'):return
    s['synchronization']={'version':1,'bases':{n:v['base'] for n,v in s['publication']['repos'].items()},'history':[]}
    h.save(s,'upstream_tracking_initialized')


def enabled(s):return s.get('synchronization',{}).get('version')==1


def observe(h,s):
    pub=h.publication_module();result={}
    for name,item in s['publication']['repos'].items():
        policy=s['publication']['policy']['repositories'][name]
        if item.get('github'):
            h.require(pub.out(h,item['source'],'remote','get-url','--all',policy['remote'])==item['github']['remoteUrl'], 'Cambió el remoto de integración.')
        result[name]=fetch(h,item['source'],policy,s['id'],name)
    return result


def blocked(h,s,reason):
    record=s['synchronization']
    record.setdefault('previousStage',s['stage']);record['reason']=reason
    s['stage']='blocked';s['blockedPhase']='upstream'
    h.save(s,'upstream_sync_required')
    raise h.InfrastructureBlocked(reason+' Continúa con sync '+s['id']+'; no uses feedback ni edites el estado.')


def guard(h,s,observed=None):
    if not enabled(s):return
    if s['synchronization'].get('transaction'):
        blocked(h,s,'Hay una integración pendiente de completar.')
    current=observed if observed is not None else observe(h,s)
    if current!=s['synchronization']['bases']:
        s['synchronization']['observed']=current
        blocked(h,s,'Cambió la base remota desde la última integración.')


def dependencies(h,s,current):
    """A known cross-repository delivery cannot be imported halfway through merge."""
    requested=s['synchronization'].get('after')
    if requested:
        peer=h.load(requested)
        h.require(peer['stage']!='cancelled','La dependencia fue cancelada; requiere una decisión de alcance.')
    for path in (h.STORE/'runs').glob('*/state.json'):
        peer=json.loads(path.read_text())
        if peer['id']==s['id'] or peer.get('stage') in h.DONE:continue
        rows=peer.get('publication',{}).get('repos',{})
        changed=[v for v in rows.values() if v.get('changed')]
        if not changed or not all(v.get('pr') for v in changed):
            if peer['id']==requested:blocked(h,s,'La dependencia '+requested+' aún no tiene una entrega fusionable.')
            continue
        same={n:v for n,v in rows.items() if n in s['publication']['repos'] and
              v.get('source')==s['publication']['repos'][n].get('source') and v.get('changed')}
        if not same:continue
        states=[h.github_module().cleanup_pr(h,v,peer['publication']['policy']['repositories'][n]) for n,v in same.items()]
        if ('merged' in states and any(v!='merged' for v in states)) or (peer['id']==requested and any(v!='merged' for v in states)):
            blocked(h,s,'Espera la fusión completa de '+peer['id']+' en sus repositorios.')


def blob(h,tree,commit,path):
    pub=h.publication_module();row=pub.git(h,tree,'ls-tree','-z',commit,'--',path).stdout
    if not row:return None,0o644
    meta,actual=row.rstrip(b'\0').split(b'\t',1)
    mode,kind,oid=meta.decode().split()
    h.require(kind=='blob' and mode in {'100644','100755'},'Integración automática no admite enlaces/submódulos: '+path)
    return pub.git(h,tree,'cat-file','blob',oid).stdout,int(mode,8)&0o777


def digest(data,mode=0o644):
    return None if data is None else [hashlib.sha256(data).hexdigest(),mode]


def disk(path):
    if path.is_symlink():return ['symlink',os.readlink(path)]
    if not path.exists():return None
    if not path.is_file():return ['directory']
    return digest(path.read_bytes(),path.stat().st_mode&0o777)


def restore_overlay(h,tree,record,content):
    decode=lambda value:base64.b64decode(value) if value is not None else b''
    original=decode(record['base']);prepared=decode(record['prepared'])
    if prepared.startswith(original):
        # Controller instructions/ignores are appended. Preserve the known suffix
        # without a false conflict when upstream also appended to the product file.
        return (content or b'')+prepared[len(original):]
    with tempfile.TemporaryDirectory(prefix='harness-sync-overlay-') as directory:
        paths=[Path(directory)/name for name in ('prepared','base','merged')]
        for path,data in zip(paths,[decode(record['prepared']),decode(record['base']),content or b'']):path.write_bytes(data)
        result=h.publication_module().git(h,tree,'merge-file','-p',*[str(p) for p in paths],check=False)
        h.require(result.returncode==0,'El cambio remoto entra en conflicto con instrucciones locales; conserva la integración y revisa el overlay.')
        return result.stdout


def begin(h,s,targets):
    pub=h.publication_module();pub.index_ready(h,s)
    record=s['synchronization'];previous=record.get('previousStage',s['stage'])
    h.require(previous not in {'preparing','paused','completed','cancelled','cleaning'},'No se sincroniza en esta etapa.')
    h.require(not any(v.get('removalStarted') for v in s['publication']['repos'].values()),'La limpieza ya comenzó; no se modifica el trabajo.')
    approved=bool(s.get('approval'));passed=s.get('qa',{}).get('status')=='pass'
    reuse=False
    if passed:
        try:
            h.preview_valid(s)
            if approved:h.require(pub.evidence(h,s)==s.get('approvalEvidence'),'Evidencia modificada.')
            h.validate_report(s['qa'],s);reuse=True
        except (h.Blocked,OSError):pass
    transaction={'id':str(len(record['history'])+1),'targets':targets,'previousStage':previous,
                 'product':h.job_fingerprint(s,True),'qaReusable':reuse,'approved':approved,'repos':{},'at':time.time()}
    for name,item in s['publication']['repos'].items():
        if record['bases'][name]==targets[name]:continue
        tree=Path(item['worktree']);base=record['bases'][name]
        h.require(pub.out(h,tree,'symbolic-ref','--short','HEAD')==item['branch'],'Cambió rama del trabajo.')
        h.require(pub.git(h,tree,'merge-base','--is-ancestor',base,targets[name],check=False).returncode==0,
                  'El remoto reescribió su historial; no se integra automáticamente.')
        incoming=pub.git(h,tree,'diff','--no-renames','--name-only','-z',base,targets[name]).stdout.decode().split('\0')
        h.require(not any(pub.internal(p) for p in incoming if p),'El remoto cambió código del harness; requiere actualizar fuera de trabajos activos.')
        # The publication filter removes overlays/secrets before making a local checkpoint.
        env=pub.build_index(h,s,name,freeze=False)
        checkpoint_tree=pub.out(h,tree,'write-tree',env=env)
        head=pub.out(h,tree,'rev-parse','HEAD')
        checkpoint=head if checkpoint_tree==pub.out(h,tree,'rev-parse','HEAD^{tree}') else pub.out(h,tree,'commit-tree',checkpoint_tree,'-p',head,data=b'chore: local harness integration checkpoint\n')
        shadow=h.jobdir(s['id'])/'integration'/transaction['id']/name
        row={'head':head,'checkpoint':checkpoint,'target':targets[name],'shadow':str(shadow),'phase':'created',
             'originalInventory':pub.inventory(h,tree)}
        transaction['repos'][name]=row
    record['transaction']=transaction;s['stage']='blocked';s['blockedPhase']='upstream'
    s['publication']['handoff']=False
    h.save(s,'upstream_transaction_started')


def prepare_shadow(h,s,name,row):
    pub=h.publication_module();tree=Path(s['publication']['repos'][name]['worktree']);shadow=Path(row['shadow'])
    if not shadow.exists():
        shadow.parent.mkdir(parents=True,exist_ok=True)
        pub.git(h,tree,'-c','core.hooksPath=/dev/null','worktree','add','--detach',str(shadow),row['checkpoint'])
    head=pub.out(h,shadow,'rev-parse','HEAD')
    pending=pub.git(h,shadow,'rev-parse','--verify','MERGE_HEAD',check=False)
    if pending.returncode==0:
        h.require(pending.stdout.decode().strip()==row['target'],'Integración temporal con otro destino.')
        conflicts=pub.out(h,shadow,'diff','--name-only','--diff-filter=U').splitlines()
        row['conflicts']=conflicts;h.save(s,'upstream_conflicts')
        h.require(not conflicts,'Resuelve y prepara con git add los conflictos en '+str(shadow)+': '+', '.join(conflicts[:12])+'. Después repite sync; no hagas commit.')
        h.require(not pub.out(h,shadow,'diff','--name-only'),'Hay resoluciones sin preparar en '+str(shadow))
        selected=pub.out(h,shadow,'diff','--cached','--name-only').splitlines()
        h.require(not any(pub.forbidden(v) for v in selected),'Resolución contiene archivos internos/sensibles.')
        pub.git(h,shadow,'-c','core.hooksPath=/dev/null','commit','--no-edit')
    elif head==row['checkpoint']:
        result=pub.git(h,shadow,'-c','core.hooksPath=/dev/null','merge','--ff','--no-edit','--no-overwrite-ignore',row['target'],check=False)
        if result.returncode:
            row['conflicts']=pub.out(h,shadow,'diff','--name-only','--diff-filter=U').splitlines();h.save(s,'upstream_conflicts')
            raise h.InfrastructureBlocked('Integración pendiente en '+str(shadow)+': '+', '.join(row['conflicts'][:12])+'. Resuelve allí, prepara con git add y repite sync; no cambies el worktree de producto ni hagas commit.')
    h.require(not pub.out(h,shadow,'status','--porcelain'),'Integración temporal con cambios sin guardar.')
    result=pub.out(h,shadow,'rev-parse','HEAD')
    h.require(all(pub.git(h,shadow,'merge-base','--is-ancestor',commit,result,check=False).returncode==0 for commit in (row['checkpoint'],row['target'])),'Resultado de integración inesperado.')
    row['result']=result;row['phase']='merged';row.pop('conflicts',None);h.save(s,'upstream_merged_'+name)


def plan_apply(h,s,name,row):
    pub=h.publication_module();item=s['publication']['repos'][name];tree=Path(item['worktree'])
    h.require(pub.inventory(h,tree)==row['originalInventory'],'Cambió el worktree mientras se integraba; conserva ambos árboles y revisa antes de aplicar.')
    names=set(pub.git(h,tree,'diff','--no-renames','--name-only','-z',row['checkpoint'],row['result']).stdout.decode().split('\0'))-{''}
    names.update(item['overlays']);writes={};overlays={}
    for path in sorted(names):
        h.require(not pub.forbidden(path),'Integración contiene archivo interno/sensible: '+path)
        target=h.safe_path(tree,path);h.require(not (tree/path).is_symlink(),'Ruta enlazada: '+path)
        content,mode=blob(h,tree,row['result'],path)
        previous,_=blob(h,tree,row['checkpoint'],path)
        # Git records only the executable bit; do not normalize local group/other
        # permissions and invalidate otherwise equivalent product fingerprints.
        if target.is_file() and bool(target.stat().st_mode&0o111)==bool(mode&0o111):
            mode=target.stat().st_mode&0o777
        if path in item['overlays']:
            original=content;content=restore_overlay(h,tree,item['overlays'][path],original)
            overlays[path]={'base':base64.b64encode(original).decode() if original is not None else None,
                            'prepared':base64.b64encode(content).decode()}
        elif previous is None:h.require(not target.exists(),'Archivo local no versionado colisiona con remoto: '+path)
        writes[path]={'before':disk(target),'after':digest(content,mode),
                      'content':base64.b64encode(content).decode() if content is not None else None,'mode':mode}
    row.update(writes=writes,overlays=overlays,phase='applying');h.save(s,'upstream_apply_planned_'+name)


def apply(h,s,name,row):
    pub=h.publication_module();item=s['publication']['repos'][name];tree=Path(item['worktree'])
    head=pub.out(h,tree,'rev-parse','HEAD')
    h.require(head in {row['head'],row['result']},'Cambió HEAD durante integración.')
    for path,write in row['writes'].items():
        target=h.safe_path(tree,path);current=disk(target)
        h.require(current in (write['before'],write['after']),'Cambio concurrente al aplicar integración: '+path)
        if current==write['after']:continue
        if write['content'] is None:target.unlink()
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            temp=target.with_name(target.name+'.harness-sync-'+s['resourceKey'])
            if temp.exists() or temp.is_symlink():
                h.require(not temp.is_symlink() and temp.is_file() and temp.read_bytes()==base64.b64decode(write['content']),
                          'Archivo temporal de integración distinto; conserva y revisa: '+str(temp))
            else:temp.write_bytes(base64.b64decode(write['content']))
            temp.chmod(write['mode']);os.replace(temp,target)
    pub.git(h,tree,'update-ref','refs/heads/'+item['branch'],row['result'],head)
    pub.git(h,tree,'read-tree',row['result'])
    item['overlays'].update(row['overlays']);item['base']=row['target'];item['overlayHead']=row['result']
    for field in FIELDS:item.pop(field,None)
    row['phase']='applied';h.save(s,'upstream_applied_'+name)


def sync(h,s,after=None):
    if not enabled(s):return {'status':'disabled'}
    h.require_job_config(s)
    record=s['synchronization'];pub=h.publication_module()
    h.require(s['stage'] not in h.DONE and s['stage'] not in {'preparing','checking','cleaning','paused'},'Sincronización no permitida en esta etapa.')
    if after:
        h.require(after!=s['id'],'Un trabajo no puede depender de sí mismo.');h.load(after)
        record['after']=after;h.save(s,'upstream_dependency_recorded')
    if not record.get('transaction'):
        current=observe(h,s);dependencies(h,s,current)
        if current==record['bases']:
            if s.get('blockedPhase')=='upstream':
                s['stage']=record.pop('previousStage','implementing');s.pop('blockedPhase',None);record.pop('reason',None);h.save(s,'upstream_unchanged')
            return {'status':'current','bases':current}
        # Once any own PR merged, remaining publication cannot safely be rewritten.
        for name,item in s['publication']['repos'].items():
            if item.get('pr'):
                h.require(h.github_module().cleanup_pr(h,item,s['publication']['policy']['repositories'][name])!='merged',
                          'Un PR de este trabajo ya se fusionó; requiere revisar la entrega parcial antes de integrar más cambios.')
        begin(h,s,current)
    transaction=record['transaction']
    h.require(set(transaction['repos'])=={n for n in transaction['targets'] if record['bases'][n]!=transaction['targets'][n]},
              'Checkpoint de integración incompleto; conserva los árboles y revisa la preparación.')
    for name,row in transaction['repos'].items():
        if row['phase']=='created':prepare_shadow(h,s,name,row)
    if not transaction.get('testPreflight'):
        candidate=dict(s,worktrees=dict(h.repository_roots(s)))
        candidate['worktrees'].update({n:r['shadow'] for n,r in transaction['repos'].items()})
        candidate['worktree']=candidate['worktrees'][h.config().get('primaryRepository','main')]
        transaction['testPreflight']=h.setup_module().test_readiness(h,candidate)
        h.save(s,'upstream_test_preflight_passed')
    # Preflight every repository before the first worktree mutation.
    for name,row in transaction['repos'].items():
        if row['phase']=='merged':plan_apply(h,s,name,row)
    for name,row in transaction['repos'].items():
        if row['phase']=='applying':apply(h,s,name,row)
    unchanged=h.job_fingerprint(s,True)==transaction['product']
    reusable=unchanged and transaction['qaReusable']
    if reusable:
        try:h.preview_valid(s)
        except (h.Blocked,OSError):reusable=False
    # Preserve original QA evidence. Only equivalent product/runtime can reuse it.
    if s.get('qa'):
        h.atomic(h.jobdir(s['id'])/'artifacts'/('qa-before-sync-'+transaction['id']+'.json'),s['qa'])
    for key in ('reviewRequest','userConsent'):s.pop(key,None)
    if reusable and transaction['approved']:
        s['stage']='documenting'  # finish revalidates permitted documentation and original approval.
    elif reusable:
        version=h.version(s);report=dict(s['qa'],version=version,reusedFrom=s.get('qaVersion'),reuseReason='upstream_docs_only_same_product_and_runtime')
        s.update(qaVersion=version,qa=report,approvedCandidate=version,docsBaseline=h.job_inventory(s),productBaseline=h.job_fingerprint(s,True),stage='awaiting_human_approval')
        h.atomic(h.jobdir(s['id'])/'qa.json',report)
    else:
        for key in ('approval','approvalEvidence','qa','qaVersion','approvedCandidate','checkedVersion'):s.pop(key,None)
        s['stage']='implementing'
    for item in s['publication']['repos'].values():
        for field in FIELDS:item.pop(field,None)
    s['publication']['phase']='prepared';s['publication']['handoff']=False
    record['lastResult']={'productChanged':not unchanged,'qaReused':reusable,'next': 'finish' if s['stage']=='documenting' else 'review' if reusable else 'verify'}
    s.pop('blockedPhase',None);s.pop('finalizationError',None)
    for name,row in transaction['repos'].items():
        shadow=Path(row['shadow'])
        if shadow.exists():pub.git(h,s['publication']['repos'][name]['worktree'],'worktree','remove',str(shadow))
    # Preserve checkpoints privately, without inflating every brief/state read.
    h.atomic(h.jobdir(s['id'])/'integration'/transaction['id']/'receipt.json',transaction)
    record['bases']=transaction['targets']
    record['history'].append({'at':time.time(),'targets':transaction['targets'],'productChanged':not unchanged,'qaReused':reusable})
    record.pop('transaction');record.pop('previousStage',None);record.pop('reason',None);record.pop('observed',None)
    h.save(s,'upstream_sync_completed')
    return dict(status='integrated',**record['lastResult'])


def brief(s):
    value=s.get('synchronization',{})
    tx=value.get('transaction',{})
    return {'bases':value.get('bases'),'needed':bool(value.get('reason') or tx),'reason':value.get('reason'),
            'lastResult':value.get('lastResult'),'after':value.get('after'),
            'conflicts':{n:{'worktree':r['shadow'],'paths':r.get('conflicts',[])} for n,r in tx.get('repos',{}).items() if r.get('conflicts')}}
