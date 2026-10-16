"""Detached, restartable publication/CI/cleanup worker. No agent/model invocation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def start(h,s):
    h.require(s.get('publication',{}).get('handoff') and s.get('approval'),'No hay cierre aprobado para reanudar.')
    h.require(s['stage'] in {'publishing','awaiting_ci','awaiting_merge','cleaning','blocked'},'Cierre no ejecutable en esta etapa.')
    h.require(s['publication'].get('phase') in {'publishing','awaiting_ci','awaiting_merge','cleaning'},'Fase de cierre inválida.')
    log=h.jobdir(s['id'])/'finalize.log'
    with log.open('ab') as stream:
        subprocess.Popen([sys.executable,str(h.ROOT/'harness/finalization.py'),'--controller',str(h.ROOT),'--job',s['id']],
                         cwd=h.ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,start_new_session=True)
    print('Cierre en segundo plano. Consulta show '+s['id']+'.')


def known_generated(h,s,name,path):
    mod=h.publication_module()
    item=s['publication']['repos'][name]
    record=item.get('finalSpecs',{}).get(path) or item.get('copiedSpecs',{}).get(path)
    if record:
        copied=h.safe_path(Path(item['worktree']),path)
        archive=h.safe_path(h.jobdir(s['id']),record['archive'])
        return (copied.is_file() and archive.is_file() and not (Path(item['worktree'])/path).is_symlink()
                and not (h.jobdir(s['id'])/record['archive']).is_symlink()
                and h.sha(copied)==h.sha(archive)==record['sha256'])
    generated=item.get('generatedFiles',{}).get(path)
    if generated:
        target=h.safe_path(Path(item['worktree']),path)
        return target.is_file() and not (Path(item['worktree'])/path).is_symlink() and h.sha(target)==generated['sha256']
    if path in s['publication']['repos'][name].get('ownedFiles',[]):return True
    policy=s['publication']['policy']['repositories'][name]
    return any(path==p or path.startswith(p.rstrip('/')+'/') for p in policy.get('disposablePaths',[]))


def worktree_identity(h,s,name):
    item=s['publication']['repos'][name];tree=Path(item['worktree']);mod=h.publication_module()
    h.require(tree.resolve().is_relative_to((h.STORE/'worktrees').resolve()) and not tree.is_symlink(),'Worktree fuera del área propia.')
    rows=mod.out(h,item['source'],'worktree','list','--porcelain')
    entries=[dict(line.split(' ',1) for line in block.splitlines() if ' ' in line) for block in rows.strip().split('\n\n')]
    matching=[v for v in entries if v.get('worktree')==str(tree)]
    h.require(len(matching)==1 and matching[0].get('branch')=='refs/heads/'+item['branch'],'Registro de worktree distinto.')
    h.require(sum(v.get('branch')=='refs/heads/'+item['branch'] for v in entries)==1,'Rama utilizada por otro worktree.')
    h.require(str((tree/mod.out(h,tree,'rev-parse','--git-common-dir')).resolve())==item['commonDir'],'Cambió identidad Git.')
    h.require(mod.out(h,tree,'rev-parse','HEAD')==item['commit'],'Hay commits locales no respaldados.')
    marker=tree/'.harness-parent.json'
    data=json.loads(marker.read_text())
    h.require(data.get('job')==s['id'] and Path(data.get('controller','')).resolve()==h.ROOT.resolve(),'Worktree de otro trabajo.')
    ignored_ready(h,s,name)


def ignored_ready(h,s,name):
    item=s['publication']['repos'][name];tree=Path(item['worktree']);mod=h.publication_module()
    ignored=mod.git(h,tree,'ls-files','-z','--others','--ignored','--exclude-standard').stdout.decode().split('\0')
    unknown=[p for p in ignored if p and not known_generated(h,s,name,p)]
    h.require(not unknown,'Archivos ignorados desconocidos; no se borran: '+', '.join(unknown[:5]))
    for relative in ignored:
        if relative: h.require((tree/relative).resolve().is_relative_to(tree.resolve()), 'Archivo ignorado enlazado fuera del worktree.')
    for relative in s['publication']['policy']['repositories'][name].get('disposablePaths',[]):
        target=tree/relative
        h.require(target.resolve().is_relative_to(tree.resolve()) and not target.is_symlink(), 'Ruta desechable enlazada fuera del worktree.')


def session_stop(h,s):
    if s.get('conversation', {}).get('mode') == 'inline':
        h.require(s['conversation'].get('owned') is False and not s.get('backgroundId') and not s.get('launched'),
                  'Identidad de conversación inconsistente; no se detiene ni limpia.')
        h.require(s['publication'].get('handoff'), 'Conversación sin entrega de control.')
        if not s['publication'].get('sessionPreserved'):
            s['publication']['sessionPreserved'] = True
            h.save(s, 'finalization_user_conversation_preserved')
        return
    if not s.get('launched'):return
    if s['publication'].get('sessionStopped'):
        h.require_no_active_session(s);return
    h.require(s['publication'].get('handoff'),'Sesión sin entrega de control verificada.')
    if not s.get('backgroundId'):
        h.require_no_active_session(s)
        s['publication']['sessionStopped']=True;h.save(s,'finalization_session_already_stopped');return
    sessions=json.loads(h.run(['claude','agents','--json','--all'],timeout=20))
    own=[v for v in sessions if v.get('id')==s['backgroundId'] and v.get('sessionId')==s.get('sessionId') and v.get('cwd')==s['worktree']]
    related=[v for v in sessions if v.get('cwd') in h.repository_roots(s).values() and v.get('state') not in {'stopped','failed','completed'}]
    h.require(len(own)==1 and all(v in own for v in related),'No se puede identificar exclusivamente la sesión del trabajo.')
    if own[0].get('state') not in {'stopped','failed','completed'}:
        h.run(['claude','stop',s['backgroundId']],timeout=20)
        for attempt in range(10):
            try:h.require_no_active_session(s);break
            except h.Blocked:
                if attempt==9:raise
                time.sleep(.5)
    s['publication']['sessionStopped']=True;h.save(s,'finalization_session_stopped')


def remove_database(h,s):
    """Tolerate a crash between container and volume removal; never remove replacements."""
    if not h.has_database():return
    owned=s['publication'].setdefault('database',{'containerId':s.get('containerId'),'volume':s.get('volume')})
    h.save(s,'cleanup_database_identity_saved')
    # Inspect by recorded name too: a reused name with another id must be preserved.
    import subprocess
    p=subprocess.run(['docker','container','ls','-a','--no-trunc','--format','{{json .}}'],capture_output=True,text=True,timeout=20)
    h.require(p.returncode==0,'No se puede consultar Docker; no se limpia.')
    rows=[json.loads(v) for v in p.stdout.splitlines() if v]
    found=[v for v in rows if v.get('Names')==s['container'] or v.get('ID')==owned['containerId']]
    if found:
        h.require(len(found)==1 and found[0]['ID']==owned['containerId'],'Contenedor sustituido; no se elimina.')
        info=json.loads(h.run(['docker','inspect',owned['containerId']],timeout=20))[0]
        h.require(info['Config'].get('Labels',{}).get(h.LABEL)==s['resourceKey'] and
                  any(v.get('Name')==owned['volume'] for v in info.get('Mounts',[])),'Contenedor/volumen ajenos.')
        h.run(['docker','rm','-f',owned['containerId']],timeout=30)
    volumes=h.run(['docker','volume','ls','--format','{{.Name}}'],timeout=20).splitlines()
    if owned['volume'] in volumes:
        info=json.loads(h.run(['docker','volume','inspect',owned['volume']],timeout=20))[0]
        h.require(info.get('Labels',{}).get(h.LABEL)==s['resourceKey'],'Volumen sustituido; no se elimina.')
        h.run(['docker','volume','rm',owned['volume']],timeout=30)
    s.pop('containerId',None);owned['removed']=True
    (h.jobdir(s['id'])/'secrets.json').unlink(missing_ok=True)
    h.save(s,'cleanup_database_removed')


def cleanup(h,s):
    pub=s['publication'];mod=h.publication_module()
    h.require(s.get('approval') and pub.get('handoff') and pub['phase']=='cleaning','Limpieza sin aprobación/CI.')
    # Validate ALL repositories before destroying anything, also after interruptions.
    mod.unchanged(h,s)
    for name,item in pub['repos'].items():
        if item['changed']:
            h.require(item.get('ci',{}).get('status')=='passed','CI pendiente.')
            policy=pub['policy']['repositories'][name]
            h.require(mod.out(h,item['source'],'remote','get-url','--all','--push',policy['remote'])==item['github']['remoteUrl'] and
                      mod.out(h,item['source'],'remote','get-url','--all',policy['remote'])==item['github']['remoteUrl'],
                      'Cambió el remoto; no se limpia.')
            h.require(h.github_module().cleanup_pr(h,item,policy)=='merged','PR aún no fusionado: '+name)
        else:h.require(item['commit']==item['base'],'Hay commits sin PR aunque el diff final esté vacío.')
        if not item.get('worktreeRemoved'):
            if not Path(item['worktree']).exists() and item.get('removalStarted'):
                # A prior removal may have completed before its checkpoint was persisted.
                entries=mod.out(h,item['source'],'worktree','list','--porcelain')
                h.require('worktree '+item['worktree']+'\n' not in entries,'Registro huérfano; revisar antes de borrar la rama.')
                item['worktreeRemoved']=True;h.save(s,'cleanup_worktree_reconciled_'+name)
            else:worktree_identity(h,s,name)
    h.release_browser(s)
    h.runtime_module('spec_status').prepare(h,s)
    session_stop(h,s)
    h.stop_preview(s);h.save(s,'cleanup_previews_stopped')
    remove_database(h,s)
    for name,item in pub['repos'].items():
        if not item.get('worktreeRemoved'):
            # --force permits only already inventoried controller files and known build outputs.
            if item['changed']:
                h.require(h.github_module().cleanup_pr(h,item,pub['policy']['repositories'][name])=='merged',
                          'PR aún no fusionado: '+name)
            worktree_identity(h,s,name)
            item['removalStarted']=True;h.save(s,'cleanup_worktree_removal_started_'+name)
            mod.git(h,item['source'],'worktree','remove','--force',item['worktree'])
            item['worktreeRemoved']=True;h.save(s,'cleanup_worktree_removed_'+name)
        if not item.get('branchRemoved'):
            ref='refs/heads/'+item['branch']
            current=mod.git(h,item['source'],'rev-parse','--verify',ref,check=False)
            if current.returncode==0:
                h.require(current.stdout.decode().strip()==item['commit'],'La rama local cambió; no se elimina.')
                entries=mod.out(h,item['source'],'worktree','list','--porcelain')
                h.require('branch '+ref+'\n' not in entries,'Rama usada por otro worktree.')
                mod.git(h,item['source'],'update-ref','-d',ref,item['commit'])
            item['branchRemoved']=True;h.save(s,'cleanup_branch_removed_'+name)
    h.runtime_module('spec_status').complete(h,s)
    pub['phase']='completed';s['stage']='completed';s.pop('blockedPhase',None)
    h.save(s,'publication_and_cleanup_completed')


def tick(h,s):
    with h.job_environment(s):
        return _tick(h,s)


def guard_unmerged(h, s):
    """Re-read PRs only on remote advance: a merge can race the first PR read."""
    sync = h.runtime_module('synchronization')
    if not sync.enabled(s): return True
    observed = sync.observe(h, s)
    if observed != s['synchronization']['bases']:
        merged(h, s)
        if any(v.get('prState') == 'merged' for v in s['publication']['repos'].values()):
            # Partial delivery waits; a complete delivery revalidates CI on the
            # next tick. Neither path rewrites a branch already merged by a user.
            h.save(s, 'own_merge_race_reconciled')
            return False
    sync.guard(h, s, observed)
    return True


def _tick(h,s):
    h.require_job_config(s)
    pub=s['publication']
    h.require(pub.get('executor')==h.controller_version(),
              'Cambió el controlador; se conserva el trabajo. Restaura su versión exacta desde el respaldo; '+
              'no cambies hashes ni repitas verify/QA para resolverlo. Las actualizaciones requieren cerrar los trabajos activos.')
    h.require(s.get('approval') and pub.get('handoff'),'La aprobación/entrega fue retirada.')
    h.require(pub['policy']==h.publication_module().settings(h),'Cambió la política del cierre.')
    s.pop('blockedPhase',None)
    s.pop('finalizationError',None)
    merge_ready=False
    if pub['phase']=='publishing':
        s['stage']='publishing';h.publication_module().publish(h,s)
    if pub['phase']=='awaiting_merge':
        # Human review has no CI timeout and does not rerun checks or agents.
        if not merged(h,s):
            if not any(v.get('prState')=='merged' for v in pub['repos'].values()):guard_unmerged(h,s)
            return False
        merge_ready=True
        pub['phase']='awaiting_ci'
    if pub['phase'] in {'awaiting_ci','cleaning'}:
        # A resumed cleanup must also observe CI; it may have been rerun since the last receipt.
        if not merge_ready and h.runtime_module('synchronization').enabled(s):
            states=[h.github_module().cleanup_pr(h,v,pub['policy']['repositories'][n]) for n,v in pub['repos'].items() if v.get('changed') and v.get('pr')]
            if 'merged' not in states and not guard_unmerged(h,s):return False
        s['stage']='awaiting_ci';pending=False
        for name,item in pub['repos'].items():
            if not item['changed']:continue
            item['ci']=h.github_module().ci(h,item,pub['policy']['repositories'][name])
            pub['lastPollAt']=time.time();h.save(s,'ci_observed_'+name)
            h.require(item['ci']['status']!='failed','CI falló: '+name+'. Se conserva el entorno.')
            pending |= item['ci']['status']!='passed'
        if pending:
            pub['phase']='awaiting_ci'
            h.save(s,'ci_pending')
            return False
        if not merge_ready and not merged(h,s):return False
        pub['phase']='cleaning';s['stage']='cleaning';h.save(s,'ci_passed_cleanup_pending')
    if pub['phase']=='cleaning':cleanup(h,s)
    return pub['phase']=='completed'


def merged(h,s):
    pub=s['publication'];pending=False
    for name,item in pub['repos'].items():
        if not item['changed']:continue
        item['prState']=h.github_module().cleanup_pr(h,item,pub['policy']['repositories'][name])
        pending |= item['prState']!='merged'
    pub['lastPollAt']=time.time()
    if pending:
        pub['phase']='awaiting_merge';s['stage']='awaiting_merge'
    h.save(s,'merge_observed')
    return not pending


def worker(h,job):
    with h.lock('finalize-'+job,blocking=False):
        with h.lock('job-'+job):
            s=h.load(job)
            if s['stage'] not in {'publishing','awaiting_ci','awaiting_merge','cleaning','blocked'}:return
            s['publication']['worker']={'pid':os.getpid(),'start':h.process_identity(os.getpid())}
            h.save(s,'finalization_worker_started')
        deadline=time.monotonic()+h.config().get('publication',{}).get('timeoutSeconds',3600)
        while True:
            with h.lock('job-'+job):
                s=h.load(job)
                if (s.get('publication',{}).get('phase')=='completed' or not s.get('publication',{}).get('handoff') or
                        s['stage'] not in {'publishing','awaiting_ci','awaiting_merge','cleaning','blocked'}):return
                try:
                    if tick(h,s):return
                    if s['publication']['phase']=='awaiting_merge':
                        deadline=time.monotonic()+h.config().get('publication',{}).get('timeoutSeconds',3600)
                    else:
                        h.require(time.monotonic()<deadline,'Tiempo de espera CI agotado; finalize permite retomarlo.')
                except Exception as error:
                    if s.get('blockedPhase')!='upstream':s['blockedPhase']=s.get('publication',{}).get('phase','publication')
                    s['stage']='blocked';s['finalizationError']=str(error)
                    h.save(s,'finalization_blocked')
                    return
            time.sleep(h.config()['publication'].get('pollSeconds',30))


def main():
    p=argparse.ArgumentParser();p.add_argument('--controller',required=True);p.add_argument('--job',required=True);a=p.parse_args()
    path=Path(a.controller)/'harness/harness.py'
    spec=importlib.util.spec_from_file_location('controller',path);h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
    worker(h.CoreContext(),a.job)


if __name__=='__main__':main()
