"""Linux process receipts and bounded, installation-local heavy-command slots."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def identity(pid):
    try:
        tail=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return {'pid':int(pid),'start':tail[19],'session':int(tail[3]),'state':tail[0],
                'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError,ValueError,IndexError):return None


def status(record):
    if not record:return 'unknown'
    try:boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:return 'unknown'
    if record.get('boot')!=boot:return 'dead' if record.get('boot') else 'unknown'
    current=identity(record['pid'])
    if current:
        if current['start']!=record.get('start'):return 'unknown'
        if current['state'] not in {'Z','X'}:return 'alive'
    elif Path(f'/proc/{record["pid"]}').exists():return 'unknown'
    if record.get('group'):
        # A dead leader is not proof that its orphaned descendants stopped.
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():continue
            other=identity(int(path.name))
            if other and other['session']==record['session'] and other['state'] not in {'Z','X'}:
                return 'alive'
            if other is None and path.exists():return 'unknown'
    return 'dead'


def observed(s):
    residue=status(s.get('commandResidue')) if s.get('commandResidue') else 'dead'
    if (s.get('operationFinishedAt') or not s.get('operationStartedAt')) and residue=='dead':return {'status':'idle'}
    owner=status(s.get('executionOwner'))
    command=s.get('currentCommand')
    child=status(command.get('identity')) if command else 'dead'
    states={owner,child,residue}
    state='running' if 'alive' in states else 'unknown' if 'unknown' in states else 'interrupted'
    result={'status':state,'controller':owner,'command':child,'resourceWait':s.get('resourceWait')}
    if state=='interrupted':
        result.update(note='La etapa guardada quedó interrumpida; el tiempo transcurrido no es ejecución activa.',
                      next='verify' if s.get('stage')=='checking' and not s.get('migrationFailed') else 'diagnose',
                      recovery='verify reconcilia el intento y recupera el clon detenido solo si conserva identidad, volumen y puerto fijos.')
    return result


def recover(h,s):
    h.require(s['stage']=='checking','Solo se recuperan verificaciones interrumpidas.')
    h.require(observed(s)['status']=='interrupted','Controlador/hijos vivos o identidad desconocida; no se duplicará la ejecución.')
    row={'at':time.time(),'attempt':s.get('attempt'),'operationStartedAt':s.get('operationStartedAt'),'owner':s.get('executionOwner'),
         'command':s.get('currentCommand'),'migrationUncertain':bool(s.get('migrationFailed'))}
    s.setdefault('interruptions',[]).append(row)
    s['stage']='blocked';s['blockedPhase']='infrastructure';s['operationFinishedAt']=time.time()
    s.pop('currentCommand',None);s.pop('resourceWait',None)
    h.save(s,'interrupted_verification_recovered')
    h.require(not s.get('migrationFailed'),'Interrupción durante migración: conserva el clon y revisa su historial; reset-db requiere autorización humana.')


def available_memory():
    values=[]
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):values.append(int(line.split()[1])*1024)
    except (OSError,ValueError):pass
    try:
        relative=next(v.split(':',2)[2] for v in Path('/proc/self/cgroup').read_text().splitlines() if v.startswith('0::'))
        directory=Path('/sys/fs/cgroup')/relative.lstrip('/')
        root=Path('/sys/fs/cgroup')
        while directory.is_relative_to(root):
            limit=(directory/'memory.max').read_text().strip()
            if limit!='max':values.append(max(0,int(limit)-int((directory/'memory.current').read_text())))
            if directory==root:break
            directory=directory.parent
    except (OSError,ValueError,StopIteration):pass
    return min(values) if values else None


def heavy(step):
    if 'resourceClass' in step:return step['resourceClass']=='heavy'
    command=step.get('command',[])
    names={Path(v).name for v in command}
    return bool(names & {'mvn','mvnw','gradle','gradlew'}) or bool(names & {'npm','pnpm','yarn'} and names & {'build','test','test:ci','vitest','playwright'})


def limits(h):
    cfg=h.config().get('resources',{})
    result={'heavySlots':cfg.get('heavySlots',1),'waitSeconds':cfg.get('waitSeconds',1800),'minAvailableMiB':cfg.get('minAvailableMiB',512)}
    for key,value in result.items():
        h.require(isinstance(value,int) and not isinstance(value,bool) and value>=(0 if key=='minAvailableMiB' else 1),'resources.'+key+' inválido.')
    return result


@contextmanager
def reservation(h,s,step):
    if not heavy(step):yield None;return
    cfg=limits(h);root=h.STORE/'resources';root.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();slot=None;waiting=False
    try:
        while slot is None:
            memory=available_memory();reason='capacity'
            if memory is None or memory>=cfg['minAvailableMiB']*1024*1024:
                for number in range(cfg['heavySlots']):
                    candidate=(root/f'heavy-{number}.lock').open('a+')
                    try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:candidate.close();continue
                    candidate.seek(0);previous=candidate.read()
                    if previous:
                        try:owner=json.loads(previous)
                        except ValueError:owner={}
                        if status(owner)!='dead':candidate.close();continue
                    slot=candidate;break
            else:reason='memory'
            if slot is not None:break
            if not waiting:
                s['resourceWait']={'since':time.time(),'reason':reason,'step':step.get('id','heavy')}
                h.save(s,'resource_wait_started');waiting=True
            if time.monotonic()-started>=cfg['waitSeconds']:
                raise h.InfrastructureBlocked('Espera de recursos agotada; no se inició el comando pesado. Consulta show y reintenta verify.')
            time.sleep(min(1,cfg['waitSeconds']))
        waited=round(time.monotonic()-started,3)
        if waiting:
            s.setdefault('resourceWaits',[]).append(dict(s.pop('resourceWait',{}),seconds=waited))
            h.save(s,'resource_acquired');waiting=False
        yield slot.fileno()
    except KeyboardInterrupt as error:
        raise h.InfrastructureBlocked('Espera o comando cancelado por el usuario; conserva el intento.') from error
    finally:
        if waiting:
            s.setdefault('resourceWaits',[]).append(dict(s.pop('resourceWait',{}),seconds=round(time.monotonic()-started,3)))
            h.save(s,'resource_wait_finished')
        if slot is not None:
            slot.seek(0);previous=slot.read()
            if previous:
                try:owner=json.loads(previous)
                except ValueError:owner={}
                if status(owner)=='dead':slot.seek(0);slot.truncate();slot.flush()
            slot.close()


def launch(argv,*,cwd,env,stdin,stdout,slot=None,record):
    # The child cannot launch the tool before its identity is durably recorded.
    # Keeping the slot in this supervisor also protects orphaned commands.
    read_fd,write_fd=os.pipe()
    p=None;released=False
    try:
        fds=(read_fd,) if slot is None else (read_fd,slot)
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--supervise',str(read_fd),*argv],
            cwd=cwd,env=env,stdin=stdin,stdout=stdout,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=fds)
        os.close(read_fd);read_fd=None
        receipt=identity(p.pid)
        if receipt is None:raise OSError('No se pudo acreditar identidad del supervisor')
        receipt['group']=True
        if slot is not None:
            os.lseek(slot,0,os.SEEK_SET);os.ftruncate(slot,0);os.write(slot,json.dumps(receipt).encode());os.fsync(slot)
        record(receipt)
        os.write(write_fd,b'1');released=True
        return p
    finally:
        if read_fd is not None:os.close(read_fd)
        os.close(write_fd)
        if p is not None and not released:p.wait(timeout=5)


if __name__=='__main__' and sys.argv[1:2]==['--supervise']:
    fd=int(sys.argv[2]);token=os.read(fd,1);os.close(fd)
    if token!=b'1':sys.exit(125)
    try:sys.exit(subprocess.call(sys.argv[3:]))
    except OSError:sys.exit(127)
