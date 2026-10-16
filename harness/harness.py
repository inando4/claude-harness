#!/usr/bin/env python3
"""Local environment/gates for native Claude Code. No SDK calls or paid API fallback."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / '.harness'
LABEL = 'claude-harness.job'
DONE = {'completed', 'cancelled'}


class Blocked(Exception):
    pass


class InfrastructureBlocked(Blocked):
    pass


class RuntimeStopped(InfrastructureBlocked):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def config():
    return json.loads((ROOT / 'harness/config.json').read_text())


def safe_path(root, value):
    root = Path(root).resolve()
    require(isinstance(value, str) and not Path(value).is_absolute(), 'Usa rutas relativas al proyecto.')
    path = (root / value).resolve()
    require(path.is_relative_to(root), 'La ruta sale del proyecto: ' + value)
    return path


class CoreContext:
    def __getattr__(self, name):
        return globals()[name]


_adapters = {}


def adapter_call(method, *args, **kwargs):
    path = safe_path(ROOT / 'harness', config()['adapter'])
    require(path.is_file(), 'Adaptador inexistente: ' + str(path))
    key = (str(path), sha(path))
    if key not in _adapters:
        spec = importlib.util.spec_from_file_location('project_adapter', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _adapters.clear()
        _adapters[key] = module
    require(hasattr(_adapters[key], method), 'El adaptador no implementa ' + method)
    return getattr(_adapters[key], method)(CoreContext(), *args, **kwargs)


def database_config():
    return adapter_call('database_config')


def has_database():
    return database_config()['provider'] != 'none'


def repositories_module():
    spec = importlib.util.spec_from_file_location('harness_repositories', ROOT/'harness/repositories.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repository_roots(s=None):
    return repositories_module().roots(CoreContext(), s)


def repository_root(s=None, name=None):
    return repositories_module().root(CoreContext(), s, name)


def job_fingerprint(s, product_only=False):
    if not s.get('worktrees'): return fingerprint(s['worktree'], product_only)
    return repositories_module().fingerprint(CoreContext(), s, product_only)


def job_inventory(s):
    return {name: inventory(tree) for name, tree in repository_roots(s).items()}


def validate_config():
    cfg = config()
    require(cfg.get('schemaVersion') == 2, 'Se requiere config schemaVersion 2; no reanudes estados v1 sin adaptación.')
    require(sys.platform.startswith('linux'), 'Esta versión necesita Linux (/proc y fcntl).')
    for key in ('maxJobs', 'maxCorrectionCycles', 'timeoutSeconds'):
        require(isinstance(cfg.get(key), int) and cfg[key] > 0, 'Valor inválido: ' + key)
    repository_roots()
    require(type(config().get('browser',{}).get('maxExplorations',2)) is int and config().get('browser',{}).get('maxExplorations',2) in (1,2), 'browser.maxExplorations debe ser 1 o 2.')
    adapter_call('validate')
    if cfg.get('publication',{}).get('enabled'):publication_module().validate(CoreContext())
    db = database_config()
    require(db.get('provider') in {'postgres', 'none'}, 'Proveedor no soportado: usa postgres o none.')
    if has_database():
        for key in ('schema', 'migratorRole', 'appRole'):
            require(re.fullmatch(r'[a-z_][a-z0-9_]*', db.get(key, '')), 'Identificador PostgreSQL inválido: ' + key)
        require(db['migratorRole'] != db['appRole'] and 'harness_owner' not in (db['migratorRole'], db['appRole']), 'Los roles deben ser diferentes.')
    names = adapter_call('service_names')
    require(names and len(set(names)) == len(names), 'Declara servicios con nombres únicos.')
    require(all(re.fullmatch(r'[a-z][a-z0-9_]*', n) for n in names), 'Nombre de servicio inválido.')
    require(cfg.get('previewService', names[0]) in names, 'previewService debe ser uno de los servicios.')


def controller_version():
    files = sorted((ROOT / 'harness').rglob('*.py'))
    return hashlib.sha256(''.join(str(p.relative_to(ROOT)) + sha(p) for p in files).encode()).hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def lock(name='registry', blocking=True):
    STORE.mkdir(exist_ok=True, mode=0o700)
    with (STORE / (name + '.lock')).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Blocked('Ya hay una operación activa para este trabajo.')
        yield


def jobdir(job):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', job), 'ID inválido: usa minúsculas, números y guiones.')
    return STORE / 'runs' / job


def load(job):
    path = jobdir(job) / 'state.json'
    require(path.exists(), f'Trabajo no encontrado: {job}')
    return json.loads(path.read_text())


def save(s, event):
    s['updatedAt'] = time.time()
    if s['stage'] in DONE:
        s.setdefault('metricsWindow', {}).setdefault('endedAt', s['updatedAt'])
    atomic(jobdir(s['id']) / 'state.json', s)
    path = jobdir(s['id']) / 'events.jsonl'
    with path.open('a') as f:
        f.write(json.dumps({'at': s['updatedAt'], 'event': event, 'stage': s['stage']}, ensure_ascii=False) + '\n')
    path.chmod(0o600)


_execution = None
_tool_versions = {}


def verification_module():
    spec = importlib.util.spec_from_file_location('harness_verification', ROOT/'harness/verification.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def begin_attempt(s, operation):
    global _execution, _tool_versions
    d=jobdir(s['id'])/'attempts';d.mkdir(parents=True,exist_ok=True)
    number=max([int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit()]+[0])+1
    attempt=d/f'{number:05d}';attempt.mkdir(mode=0o700)
    s['attempt']=number; s['steps']=[]; s['operationStartedAt']=time.time()
    s.pop('operationFinishedAt',None)
    s['executionOwner']=runtime_module('execution').identity(os.getpid())
    _execution={'state':s,'directory':attempt};_tool_versions={}
    save(s,operation+'_attempt_started')


def end_attempt(s):
    global _execution
    s.pop('currentCommand',None)
    s['operationFinishedAt']=time.time()
    save(s,'attempt_finished')
    _execution=None


def attempt_path(s, name):
    if _execution and _execution['state']['id']==s['id']:
        return _execution['directory']/name
    return jobdir(s['id'])/name


def step_event(s, identifier, status, reason, duration):
    row={'id':identifier,'status':status,'reason':reason,'durationSeconds':round(duration,3),'at':time.time()}
    s.setdefault('steps',[]).append(row)
    with (attempt_path(s,'steps.jsonl')).open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    save(s,'step_'+status)


def diagnostic(s):
    value=public(s)
    value['specs']=[s.get('specStatusInputs',{}).get(p,{}).get('stagedPath',p) for p in s.get('specs',[])]
    value['execution']=runtime_module('execution').observed(s)
    value['synchronization']=runtime_module('synchronization').brief(s)
    value.update(attempt=s.get('attempt'),steps=s.get('steps',[]),currentCommand=s.get('currentCommand'),
                 lastActivityAt=s.get('updatedAt'),blockedPhase=s.get('blockedPhase'))
    if s.get('publication'):
        value['publication']={'phase':s['publication'].get('phase'),'lastPollAt':s['publication'].get('lastPollAt'),
            'error':s.get('finalizationError'),'repositories':{k:{field:v.get(field) for field in ('commit','pr','prState','ci','worktreeRemoved','branchRemoved')} for k,v in s['publication']['repos'].items()}}
        worker=s['publication'].get('worker')
        alive=bool(worker and worker.get('start') and process_identity(worker['pid'])==worker['start'])
        value['publication']['workerStatus']='running' if alive else 'stopped' if worker else 'unknown'
        if not alive and s['stage'] in {'publishing','awaiting_ci','awaiting_merge','cleaning'}:
            value['publication']['recovery']='Comprueba el cierre y retoma con finalize '+s['id']+'. El estado guardado no confirma que haya un proceso esperando.'
    current=s.get('currentCommand')
    if current:
        log=Path(current['log'])
        if log.is_file():value['lastActivityAt']=max(value['lastActivityAt'] or 0,log.stat().st_mtime)
        value['currentCommand']=dict(current,elapsedSeconds=round(time.time()-current['startedAt'],1))
    if s.get('operationStartedAt'):
        value['elapsedOperationSeconds']=round((s.get('operationFinishedAt') or time.time())-s['operationStartedAt'],1)
        value['elapsedOperationMeaning']='Tiempo transcurrido, incluye interrupciones y esperas; no es tiempo activo.'
    return value


def run(args, cwd=None, env=None, stdin=None, log=None, timeout=None, resource_slot=None):
    """No shell interpolation. Logs stay local and are not printed on failure."""
    cwd = cwd or ROOT
    timeout = timeout or config()['timeoutSeconds']
    if log:
        log = Path(log)
        if _execution and log.is_relative_to(jobdir(_execution['state']['id'])):
            base=jobdir(_execution['state']['id'])
            if not log.is_relative_to(base/'attempts'):log=_execution['directory']/log.relative_to(base)
        command_state=_execution['state'] if _execution else None
        stamp=time.time()
        if command_state:
            command_state['currentCommand']={'executable':Path(args[0]).name,'log':str(log),'startedAt':stamp}
            save(command_state,'command_started')
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        code=None
        failure=None
        command_identity=None
        try:
            with log.open('wb') as out:
                log.chmod(0o600)
                def record(identity):
                    nonlocal command_identity
                    command_identity=identity
                    if command_state:
                        command_state['currentCommand']['identity']=identity
                        save(command_state,'command_identity_recorded')
                p = runtime_module('execution').launch(args, cwd=cwd, env=env, stdin=stdin,
                                                      stdout=out, slot=resource_slot, record=record)
                try:
                    code = p.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    try:os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:os.killpg(p.pid, signal.SIGKILL)
                        except ProcessLookupError:pass
                        p.wait()
                    # The supervisor may exit before a child that ignores TERM.
                    if runtime_module('execution').status(command_identity)=='alive':
                        try:os.killpg(p.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                    code=124
                if runtime_module('execution').status(command_identity)!='dead':
                    if command_state:command_state['commandResidue']=command_identity
                    failure=InfrastructureBlocked('El comando dejó procesos propios vivos o de identidad dudosa; no se duplicará. Log: '+str(log))
        except OSError as error:
            failure=InfrastructureBlocked(f'No se pudo iniciar {Path(args[0]).name}: {error.strerror}. Log: {log}')
        if command_state:
            command_state.pop('currentCommand',None)
            with (_execution['directory']/'commands.jsonl').open('a') as f:
                f.write(json.dumps({'executable':Path(args[0]).name,'log':str(log),'startedAt':stamp,
                                    'finishedAt':time.time(),'durationSeconds':round(time.time()-stamp,3),'exitCode':code})+'\n')
            save(command_state,'command_finished')
        if failure:raise failure
        if code == 124:raise InfrastructureBlocked(f'Comando interrumpido o agotó tiempo. Log: {log}')
        require(code == 0, f'Comando falló ({code}). Log: {log}')
        return ''
    p = subprocess.run(args, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    require(p.returncode == 0, f'Falló {Path(args[0]).name}: {p.stderr.decode(errors="replace")[:350]}')
    return p.stdout.decode(errors='replace').strip()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(tree, product_only=False):
    """Include tracked and new nonignored files; don't trust git diff alone."""
    tree = Path(tree)
    names = run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=tree)
    digest = hashlib.sha256()
    for name in sorted(set(names.split('\0'))):
        if not name or name.startswith(('graphify-out/', '.harness/', 'harness/', '.claude/')):
            continue
        if product_only and (name.startswith(('docs/', 'specs/')) or name in {'CLAUDE.md', 'CLAUDE.MD'}):
            continue
        p = tree / name
        digest.update(name.encode() + b'\0')
        if p.is_symlink():
            digest.update(b'link:' + os.readlink(p).encode())
        elif p.is_file():
            digest.update(sha(p).encode())
            digest.update(str(p.stat().st_mode & 0o777).encode())
        else:
            digest.update(b'deleted')
    return digest.hexdigest()


def inventory(tree):
    names = run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=tree)
    return {n: sha(Path(tree) / n) if (Path(tree) / n).is_file() else None
            for n in set(names.split('\0')) if n and not n.startswith(('graphify-out/', '.harness/'))}


ENV_KEYS = ('PATH', 'HOME', 'JAVA_HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'DISPLAY',
            'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DOCKER_HOST', 'DOCKER_CONTEXT')


def clean_env():
    # Never inherit application/production credentials or JVM option injection.
    return {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def capture_environment(s):
    values = clean_env()
    s['executionEnvironment'] = {'version': 1, 'values': values}
    s['executionEnvironmentSha256'] = hashlib.sha256(json.dumps(s['executionEnvironment'], sort_keys=True).encode()).hexdigest()


@contextmanager
def job_environment(s):
    """One execution environment per job; legacy jobs keep their original behavior."""
    recorded = s.get('executionEnvironment')
    if recorded is None:
        require(not s.get('executionEnvironmentSha256'), 'Falta el entorno registrado del trabajo.')
        yield
        return
    require(isinstance(recorded, dict) and recorded.get('version') == 1 and isinstance(recorded.get('values'), dict), 'Formato de entorno de trabajo inválido.')
    values = recorded['values']
    require(set(values) <= set(ENV_KEYS) and all(isinstance(v, str) for v in values.values()), 'Entorno fuera de la lista permitida.')
    require(hashlib.sha256(json.dumps(recorded, sort_keys=True).encode()).hexdigest() == s.get('executionEnvironmentSha256'),
            'Cambió el entorno registrado del trabajo; no se reparan huellas automáticamente.')
    previous = {k: os.environ.get(k) for k in ENV_KEYS}
    try:
        for k in ENV_KEYS:
            if k in values: os.environ[k] = values[k]
            else: os.environ.pop(k, None)
        yield
    finally:
        for k, value in previous.items():
            if value is None: os.environ.pop(k, None)
            else: os.environ[k] = value


def in_job_environment(function):
    from functools import wraps
    @wraps(function)
    def wrapped(s, *args, **kwargs):
        with job_environment(s):
            return function(s, *args, **kwargs)
    return wrapped


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def db_info(s):
    if not has_database():
        require(not s.get('containerId'), 'No cambies el proveedor de un trabajo con clon activo.')
        return None
    info = json.loads(run(['docker', 'inspect', s['container']]))[0]
    require(info['Id'] == s.get('containerId'), 'El contenedor fue sustituido; hay que revalidar.')
    require(info['Config'].get('Labels', {}).get(LABEL) == s['resourceKey'], 'Contenedor ajeno al trabajo.')
    if not info['State']['Running']:
        raise RuntimeStopped('El clon está detenido. verify puede recuperar el clon existente con identidad, volumen y puerto fijos; no restaura datos.')
    bindings = info['NetworkSettings']['Ports'].get('5432/tcp')
    require(bindings and len(bindings) == 1 and bindings[0]['HostIp'] == '127.0.0.1', 'DB sin aislamiento loopback.')
    require(int(bindings[0]['HostPort']) == s['dbPort'], 'Cambió el puerto del clon.')
    require(info['State']['Running'], 'El clon está detenido.')
    return info


def sql(s, statement):
    db_info(s)
    # psql lives inside our labelled container, never accepts a remote URL.
    return run(['docker', 'exec', s['container'], 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                '-U', 'harness_owner', '-d', s['database'], '-Atc', statement])


def migration_history(s):
    return adapter_call("migration_history", s)


def app_env(s, migrating=False):
    return adapter_call("app_env", s, migrating=migrating)


def migrations_version(s):
    return adapter_call("migrations_version", s)


def migrations_compatible(s):
    if not s.get('migrationAttempt') or s['migrationAttempt']==migrations_version(s):return True
    if config()['adapter']!='adapters/commands.py' or 'appliedMigrationFiles' not in s:return False
    current=adapter_call('migration_files',s)
    # New migrations may be appended, never edit/delete previously applied files.
    return all(current.get(path)==digest for path,digest in s['appliedMigrationFiles'].items())


def test_summary(tree):
    return adapter_call("test_summary", tree)


def create_clone(s):
    d = jobdir(s['id'])
    if not has_database():
        s['migrationBefore'] = hashlib.sha256(b'no-database').hexdigest()
        save(s, 'no_database_declared')
        return
    db = database_config()
    password = secrets.token_hex(24)
    atomic(d / 'secrets.json', {'password': password, 'rateLimitKey': secrets.token_hex(32)})
    secret_env = d / 'postgres.env'
    secret_env.write_text(f'POSTGRES_USER=harness_owner\nPOSTGRES_DB={s["database"]}\nPOSTGRES_PASSWORD={password}\n')
    secret_env.chmod(0o600)
    if s.get('publication'):
        volumes=run(['docker','volume','ls','--format','{{.Name}}']).splitlines()
        require(s['volume'] not in volumes,'El volumen ya existe; no se reutiliza.')
        run(['docker','volume','create','--label',f'{LABEL}={s["resourceKey"]}',s['volume']])
        s['volumeCreated']=True;save(s,'owned_volume_created')
    port=available_port()
    while port in s.get('ports',{}).values():port=available_port()
    s['dbRequestedPort']=port;save(s,'clone_port_reserved')
    try:
        cid = run(['docker', 'run', '-d', '--name', s['container'], '--label', f'{LABEL}={s["resourceKey"]}',
                   '--env-file', str(secret_env), '-p', f'127.0.0.1:{port}:5432',
                   '--mount', f'type=volume,source={s["volume"]},target=/var/lib/postgresql/data',
                   db['image']])
    finally:
        secret_env.unlink(missing_ok=True)
    s['containerId'] = cid
    info = json.loads(run(['docker', 'inspect', cid]))[0]
    s['dbPort'] = int(info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
    save(s, 'clone_created')
    for _ in range(60):
        # The image's initialization server accepts Unix sockets before restarting.
        # TCP only becomes available on the final server, avoiding a startup race.
        if subprocess.run(['docker', 'exec', cid, 'pg_isready', '-h', '127.0.0.1', '-U', 'harness_owner', '-d', s['database']],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise Blocked('PostgreSQL no quedó disponible.')
    schema = s['schema']
    migrator, app = db['migratorRole'], db['appRole']
    # Password is generated hex; the supplied schema is validated as an identifier.
    script = (f"CREATE ROLE {migrator} LOGIN PASSWORD '{password}';\n"
              f"CREATE ROLE {app} LOGIN PASSWORD '{password}';\n"
              f'GRANT CONNECT ON DATABASE "{s["database"]}" TO {migrator},{app};\n'
              f'GRANT CREATE ON DATABASE "{s["database"]}" TO {migrator};\n')
    if schema == 'public':
        script += f'ALTER SCHEMA public OWNER TO {migrator};\n'
    else:
        # pg_restore --schema filters out the SCHEMA entry itself.
        script += f'CREATE SCHEMA "{schema}" AUTHORIZATION {migrator};\n'
    script += (f'REVOKE CREATE ON SCHEMA "{schema}" FROM PUBLIC;\n'
               f'GRANT USAGE ON SCHEMA "{schema}" TO {app};\n')
    p = subprocess.run(['docker', 'exec', '-i', cid, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                        '-U', 'harness_owner', '-d', s['database']], input=script.encode(), capture_output=True)
    require(p.returncode == 0, 'No se pudieron preparar roles locales: ' +
            p.stderr.decode(errors='replace').replace(password, '[REDACTED]')[:300])
    # Custom-format, schema-scoped, pre-sanitized pg_dump; retain source ACLs.
    with Path(s['snapshot']).open('rb') as backup:
        run(['docker', 'exec', '-i', cid, 'pg_restore', '--exit-on-error', '--no-owner',
             '--role=' + migrator, '--schema=' + schema,
             '-U', 'harness_owner', '-d', s['database']], stdin=backup, log=d / 'restore.log')
    require(sha(s['snapshot']) == s['snapshotSha256'], 'El snapshot cambió durante la restauración.')
    history = migration_history(s)
    require(history, 'El snapshot no contiene el historial configurado; no se acepta como clon verificado.')
    (d / 'migrations-before.txt').write_text(history)
    s['migrationBefore'] = hashlib.sha256(history.encode()).hexdigest()
    save(s, 'snapshot_restored')


@in_job_environment
def resume_runtime(s):
    release_browser(s)
    require_job_config(s)
    if s['stage']=='checking':runtime_module('execution').recover(CoreContext(),s)
    require(s['stage'] in {'implementing','blocked','ready_for_qa','qa','awaiting_human_approval'},'No se cambia runtime durante publicación o tras cierre.')
    require(not s.get('approval'),'La entrega aprobada no se modifica; registra feedback si necesita cambios.')
    require(not s.get('migrationFailed'),'Migración incierta: revisa su historial antes de recuperar runtime; no se reintenta automáticamente.')
    require(has_database(),'Este trabajo no tiene clon PostgreSQL.')
    info=json.loads(run(['docker','inspect',s['container']]))[0]
    require(info['Id']==s.get('containerId') and info['Config'].get('Labels',{}).get(LABEL)==s['resourceKey'],'Contenedor ajeno o sustituido; no se inicia.')
    require(any(v.get('Name')==s['volume'] and v.get('Destination')=='/var/lib/postgresql/data' for v in info.get('Mounts',[])),'Cambió el volumen del clon.')
    volume=json.loads(run(['docker','volume','inspect',s['volume']]))[0]
    require(volume.get('Labels',{}).get(LABEL)==s['resourceKey'],'Volumen ajeno; no se inicia.')
    bindings=info.get('HostConfig',{}).get('PortBindings',{}).get('5432/tcp',[])
    require(len(bindings)==1 and bindings[0].get('HostIp')=='127.0.0.1' and bindings[0].get('HostPort')==str(s['dbPort']),
            'El clon no tiene puerto fijo verificado; no se restaura ni cambia su destino automáticamente.')
    if not info['State']['Running']:run(['docker','start',s['container']])
    db_info(s)
    # No pg_restore/reset: retain the existing clone and force fresh live QA.
    stop_preview(s);release_browser(s)
    for field in ('qa','qaVersion','approvedCandidate','reviewRequest','userConsent','checkedVersion'):
        s.pop(field,None)
    s['stage']='blocked';s['blockedPhase']='infrastructure'
    save(s,'runtime_resumed_revalidation_required')
    print('Clon existente iniciado sin restaurar datos. Continúa verify y QA; los recibos válidos se reutilizan.')


@in_job_environment
def graph(s):
    own_attempt = _execution is None
    if own_attempt: begin_attempt(s, 'graph')
    try:
        before = job_fingerprint(s)
        module = verification_module()
        tool = shutil.which('graphify')
        require(tool, 'Graphify no está instalado.')
        tool_key = sha(tool)+run(['graphify','--version'],timeout=20)
        for name, directory in repository_roots(s).items():
            tree = Path(directory)
            output = tree/'graphify-out/graph.json'
            source = module.digest(module.files(CoreContext(), tree))
            receipt = s.setdefault('graphs', {}).get(name, {})
            if (receipt.get('source') == source and receipt.get('tool') == tool_key and
                    output.is_file() and receipt.get('output') == sha(output)):
                step_event(s, 'graph-'+name, 'reused', 'fuentes y grafo vigentes', 0)
                continue
            started = time.monotonic()
            step_event(s, 'graph-'+name, 'running', 'fuentes o grafo sin verificar', 0)
            run(['graphify', '.', '--code-only'], cwd=tree, log=attempt_path(s, 'graphify-'+name+'.log'))
            require(output.is_file(), 'Graphify no produjo graph.json en '+name)
            json.loads(output.read_text())
            run(['graphify', 'query', 'application entry point'], cwd=tree, log=attempt_path(s, 'graphify-query-'+name+'.log'))
            require(module.digest(module.files(CoreContext(),tree)) == source, 'El árbol cambió durante Graphify.')
            s['graphs'][name] = {'source':source, 'tool':tool_key, 'output':sha(output)}
            step_event(s, 'graph-'+name, 'passed', 'grafo actualizado', time.monotonic()-started)
        require(job_fingerprint(s) == before, 'El árbol cambió mientras Graphify trabajaba.')
        s['graphVersion'] = before
        save(s, 'graph_ready')
    finally:
        if own_attempt: end_attempt(s)


def prepare(a):
    validate_config()
    snapshot = None
    db = database_config()
    if has_database():
        require(a.safe_snapshot, 'Confirma --safe-snapshot: backup autorizado y anonimizado, con cuenta de prueba.')
        require(a.snapshot, 'Falta --snapshot; nunca se conecta al origen automáticamente.')
        snapshot = Path(a.snapshot).expanduser().resolve()
        require(snapshot.is_file(), 'No existe el backup local. No se conecta a la principal ni se crean fixtures.')
        with snapshot.open('rb') as f:
            require(f.read(5) == b'PGDMP', 'Usa un pg_dump en formato custom (-Fc).')
        require(a.test_account, 'Indica --test-account del snapshot autorizado.')
    else:
        require(not a.snapshot, 'El proyecto declara database.provider=none; no se ignorará un backup suministrado.')
    schema = a.schema or db.get('schema')
    if has_database():
        require(re.fullmatch(r'[a-z_][a-z0-9_]*', schema), 'Esquema PostgreSQL inválido.')
    require(a.branch.startswith('feat/'), 'Indica una rama feat/... explícita.')
    specs = []
    for value in a.spec:
        p = (ROOT / value).resolve()
        require(p.is_relative_to(ROOT) and p.is_file(), 'Spec fuera del proyecto o inexistente.')
        specs.append(str(p.relative_to(ROOT)))
    runtime_module('spec_readiness').require_all(CoreContext(), specs)
    run(['git', 'check-ref-format', '--branch', a.branch])
    if config().get('publication',{}).get('enabled'):github_module().setup(CoreContext())
    if has_database():
        run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=20)
    require(shutil.which('graphify'), 'Graphify no está instalado.')
    key = uuid.uuid4().hex[:12]
    with lock():
        spec_readiness = runtime_module('spec_readiness').require_all(CoreContext(), specs)
        require(not jobdir(a.job).exists(), 'El ID ya existe. Conserva el trabajo o elige otro ID.')
        active = [json.loads(p.read_text()) for p in (STORE / 'runs').glob('*/state.json')]
        if getattr(a, 'session_id', None):
            require(runtime_module('conversation').existing(CoreContext(), a.session_id) is None,
                    'Esta conversación ya tiene un trabajo; recupera su estado.')
        require(sum(s['stage'] not in DONE for s in active) < config()['maxJobs'], 'Dos trabajos abiertos; completa o cancela uno.')
        repo_plan = repositories_module().plan(CoreContext(), a.branch, a.base)
        if config().get('publication',{}).get('enabled'):
            mod=publication_module()
            for name,item in repo_plan.items():
                policy=config()['publication']['repositories'][name]
                latest=runtime_module('synchronization').fetch(CoreContext(),item['source'],policy,a.job,name)
                require(mod.git(CoreContext(),item['source'],'merge-base','--is-ancestor',item['base'],latest,check=False).returncode==0,
                        'La base de '+name+' contiene commits ajenos al destino del PR; elige una base publicada.')
                item['base']=latest
        primary = config().get('primaryRepository', 'main')
        base = repo_plan[primary]['base']
        reserved = {p for job in active for p in job.get('ports', {}).values()}
        ports = {}
        for name in adapter_call('service_names'):
            port = available_port()
            while port in reserved:
                port = available_port()
            ports[name] = port
            reserved.add(port)
        s = {'id': a.job, 'stage': 'preparing', 'base': base, 'branch': a.branch,
             'worktree': str(STORE / 'worktrees' / a.job), 'specs': specs,
             'resourceKey': key, 'container': 'claude-harness-' + key, 'volume': 'claude-harness-' + key,
             'database': 'h_' + key if has_database() else None, 'schema': schema,
             'snapshot': str(snapshot) if snapshot else None,
             'snapshotSha256': sha(snapshot) if snapshot else None,
             'ports': ports, 'httpPort': ports[config().get('previewService', next(iter(ports)))], 'cycles': 0,
             'configVersion': hashlib.sha256(json.dumps(config(), sort_keys=True).encode()).hexdigest(),
             'stateVersion': 2, 'specReadiness': spec_readiness,
             'testAccount': a.test_account, 'sessionId': str(uuid.uuid4())}
        if getattr(a, 'session_id', None):
            s['sessionId'] = a.session_id
            s['conversation'] = {'sessionId': a.session_id, 'mode': 'inline', 'owned': False}
        started = time.time()
        if s.get('conversation'):
            hook = json.loads((STORE/'conversations'/s['sessionId']/'hook.json').read_text())
            if isinstance(hook.get('at'), (int, float)) and 0 < hook['at'] <= started:
                started = hook['at']
        s['metricsWindow'] = {'startedAt': started}
        if config().get('repositories'):
            s['repositories'] = repo_plan
            s['worktrees'] = {name: str(STORE / 'worktrees' / a.job / name) for name in repo_plan}
            s['worktree'] = s['worktrees'][primary]
        capture_environment(s)
        s['url'] = f'http://localhost:{s["httpPort"]}'
        save(s, 'registered')
    try:
        Path(s['worktree']).parent.mkdir(parents=True, exist_ok=True)
        # Disable repository git hooks for this one git operation, not globally.
        if s.get('repositories'):
            repositories_module().create(CoreContext(), s)
        else:
            run(['git', '-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '-b', a.branch, s['worktree'], base])
        tree = Path(s['worktree'])
        # These harness files may not be committed yet. Never copy .env or product dirt.
        for name in ('harness', '.claude/skills/harness', '.claude/agents'):
            shutil.copytree(ROOT / name, tree / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('.gitignore', 'CLAUDE.md', 'docs/harness-claude-spec.md', *specs):
            if not (ROOT / name).is_file():
                continue
            dest = tree / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        atomic(tree / '.harness-parent.json', {'controller': str(ROOT), 'job': s['id']})
        with (tree / '.gitignore').open('a') as ignored:
            ignored.write('\n.harness-parent.json\n')
        if config().get('publication',{}).get('enabled'):
            publication_module().capture(CoreContext(),s)
            runtime_module('synchronization').initialize(CoreContext(),s)
        runtime_module('spec_readiness').verify_copy(CoreContext(),s)
        runtime_module('spec_status').capture(CoreContext(),s)
        setup_module().capture_job_preflight(CoreContext(), s, getattr(a, 'test_preflight', None))
        begin_attempt(s,'prepare')
        create_clone(s)
        if config()['adapter']=='adapters/commands.py':adapter_call('prepare_tools',s)
        graph(s)
        s['stage'] = 'implementing'
        save(s, 'prepared')
        if not getattr(a, 'session_id', None):
            print(json.dumps(public(s), indent=2, ensure_ascii=False))
    except Exception:
        s['stage'] = 'blocked'
        save(s, 'prepare_failed_resources_preserved')
        raise
    finally:
        if _execution and _execution['state']['id']==s['id']:end_attempt(s)


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return stat[19] if stat[0] != 'Z' else None
    except (FileNotFoundError, ProcessLookupError):
        return None


@in_job_environment
def stop_preview(s):
    processes = s.get('previews', {}).values() or ([s['preview']] if s.get('preview') else [])
    for p in reversed(list(processes)):
        if p.get('start') and process_identity(p['pid']) == p['start']:
            os.killpg(p['pid'], signal.SIGTERM)
            for _ in range(50):
                if process_identity(p['pid']) != p['start']:
                    break
                time.sleep(.1)
            # Kill any remaining children in this session as well.
            try:
                os.killpg(p['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
    s.pop('preview', None)
    s.pop('previews', None)


def owns_listener(pid, port):
    """A healthy response from another app on a reused port is not our preview."""
    try:
        sockets = set()
        for path in Path(f'/proc/{pid}/fd').iterdir():
            try:
                sockets.add(os.readlink(path))
            except FileNotFoundError:
                continue  # Descriptors may close while /proc is being read.
        for family in ('tcp', 'tcp6'):
            for line in Path(f'/proc/{pid}/net/{family}').read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) == port:
                    if 'socket:[' + fields[9] + ']' in sockets:
                        return True
    except OSError:
        pass
    return False


def group_owns_listener(pid, port):
    if owns_listener(pid, port):
        return True
    # npm and other launchers run a child server in their process group.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
            if fields[2] == str(pid) and fields[3] == str(pid) and owns_listener(int(proc.name), port):
                return True
        except (OSError, ProcessLookupError):
            continue
    return False


def artifact_version(tree, paths, excludes=()):
    result = {}
    for name in paths:
        p = safe_path(tree, name)
        require(p.exists(), 'Falta artefacto del preview: ' + name)
        files = [p] if p.is_file() else sorted(p.rglob('*'))
        for f in files:
            if any(fnmatch.fnmatchcase(str(f.relative_to(tree)),pattern) for pattern in excludes):continue
            require(f.resolve().is_relative_to(Path(tree).resolve()), 'Artefacto fuera del proyecto.')
            if f.is_file(): result[str(f.relative_to(tree))] = sha(f)
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def health_ok(s, service, process):
    require(process.get('start') and process_identity(process['pid']) == process['start'],
            'Servicio detenido: ' + service['name'])
    if not group_owns_listener(process['pid'], service['port']):
        return False
    url = f'http://127.0.0.1:{service["port"]}' + service['healthPath']
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != service.get('healthStatus', 200): return False
            expected = service.get('healthJson')
            if expected:
                data = json.load(response)
                return all(data.get(k) == v for k, v in expected.items())
            return True
    except (OSError, ValueError):
        return False


def service_version(s, service):
    module=verification_module()
    return module.digest({'inputs':module.inputs(CoreContext(),s,service),
                          'service':service, 'container':s.get('containerId'),
                          'migration':s.get('migrationAfter'),
                          'artifacts':artifact_version(repository_root(s,service.get('repo')),service.get('artifacts',[]))})


def prune_previews(s):
    """Stop only services whose inputs, output, identity or health changed."""
    if not s.get('previews'):return
    for service in adapter_call('services',s):
        old=s.get('previews',{}).get(service['name'])
        if not old:continue
        try:
            valid=old.get('serviceVersion')==service_version(s,service) and health_ok(s,service,old)
        except (Blocked,OSError):valid=False
        if not valid:
            stop_preview({'previews':{service['name']:old}})
            s['previews'].pop(service['name'],None)
            save(s,'preview_invalidated_'+service['name'])


def preview(s):
    prune_previews(s)
    s.setdefault('previews',{})
    try:
        for service in adapter_call('services',s):
            name=service['name']
            if name in s['previews']:
                step_event(s,'preview-'+name,'reused','proceso propio saludable y vigente',0)
                continue
            started=time.monotonic()
            artifacts=artifact_version(repository_root(s,service.get('repo')),service.get('artifacts',[]))
            signature=service_version(s,service)
            log=attempt_path(s,'preview-'+name+'.log')
            with log.open('ab') as out:
                p=subprocess.Popen(service['command'],cwd=safe_path(repository_root(s,service.get('repo')),service.get('cwd','.')),
                                   env=service['env'],stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True)
            state={'pid':p.pid,'start':process_identity(p.pid),'artifactsVersion':artifacts,'serviceVersion':signature,'log':str(log)}
            s['previews'][name]=state
            save(s,'preview_started_'+name)
            for _ in range(config().get('previewTimeoutSeconds',90)):
                if health_ok(s,service,state):break
                time.sleep(1)
            else:raise Blocked('El servicio no quedó saludable: '+name)
            step_event(s,'preview-'+name,'passed','servicio iniciado',time.monotonic()-started)
    except Exception:
        stop_preview(s)
        save(s,'preview_failed_stopped')
        raise


@in_job_environment
def verify(s, force=False):
    global _execution
    require_job_config(s)
    if s.get('commandResidue'):
        require(runtime_module('execution').status(s['commandResidue'])=='dead','Quedan hijos vivos o sin identidad acreditada del comando anterior; no se duplica verify.')
        s.pop('commandResidue')
    if s['stage']=='checking':runtime_module('execution').recover(CoreContext(),s)
    require(s['stage'] in {'implementing', 'blocked', 'ready_for_qa'}, 'Verifica desde implementación, bloqueo o listo para QA.')
    require(s['cycles'] < s.get('maxCycles', config()['maxCorrectionCycles']), 'Límite de correcciones alcanzado; requiere decisión humana.')
    if s.get('synchronization'):
        result=runtime_module('synchronization').sync(CoreContext(),s)
        if s['stage'] in {'documenting','awaiting_human_approval'}:return result
    require(not s.get('migrationFailed'), 'Migración fallida: el usuario debe ejecutar reset-db antes de reintentar.')
    try:
        db_info(s)
    except RuntimeStopped:
        # Reuse the same labelled clone, never restore a snapshot or change ports.
        resume_runtime(s)
    require(migrations_compatible(s),
            'Cambió una migración después de aplicarse al clon: ejecuta reset-db y repite desde el snapshot.')
    s.pop('reviewRequest', None)
    s.pop('userConsent', None)
    s.pop('approval', None)
    s.pop('qa', None)
    s.pop('blockedPhase',None)
    s['stage'] = 'checking'
    s['forceVerification']=force
    s.pop('operationFinishedAt',None)
    begin_attempt(s,'verify')
    save(s, 'checking')
    try:
        phase = 'checks'
        before = job_fingerprint(s)
        d = jobdir(s['id'])
        prune_previews(s)
        s['tests'] = adapter_call('checks', s)
        if config()['adapter']=='adapters/commands.py':
            verification_module().validate_receipts(CoreContext(),s)
        require(job_fingerprint(s) == before, 'Cambió código durante las comprobaciones.')
        phase = 'migration'
        if has_database() and (not s.get('migrationAfter') or s.get('migrationAttempt') != migrations_version(s)):
            stop_preview(s)
        s['migrationAttempt'] = migrations_version(s)
        s['migrationFailed'] = has_database()
        save(s, 'migration_started')
        adapter_call('migrate', s)
        history = migration_history(s)
        s['migrationAfter'] = hashlib.sha256(history.encode()).hexdigest()
        (d / 'migrations-after.txt').write_text(history)
        attempt_path(s,'migrations-after.txt').write_text(history)
        s['migrationFailed'] = False
        if config()['adapter']=='adapters/commands.py':s['appliedMigrationFiles']=adapter_call('migration_files',s)
        phase = 'integration'
        s['tests'].update(adapter_call('integration_checks', s))
        require(job_fingerprint(s) == before, 'Cambió código durante migración/integración.')
        phase = 'infrastructure'
        graph(s)
        s['checkedVersion'] = job_fingerprint(s)
        preview(s)
        s['stage'] = 'ready_for_qa'
        save(s, 'checks_passed')
        setup_module().record_runtime(CoreContext(), s)
    except Exception as error:
        # Infrastructure blocks need operator action, not product correction cycles.
        if isinstance(error,(InfrastructureBlocked,FileNotFoundError,subprocess.TimeoutExpired)):
            phase='infrastructure'
        if phase in {'checks', 'integration'}: s['cycles'] += 1
        s['blockedPhase'] = phase
        s['stage'] = 'blocked'
        if s['cycles'] >= s.get('maxCycles', config()['maxCorrectionCycles']):
            s['stage'] = 'paused'
        save(s, 'checks_failed')
        raise
    finally:
        s.pop('currentCommand',None)
        s.pop('forceVerification',None)
        s['operationFinishedAt']=time.time()
        save(s,'verify_attempt_finished')
        _execution=None


@in_job_environment
def preview_valid(s):
    require_job_config(s)
    db_info(s)
    if config()['adapter']=='adapters/commands.py':verification_module().validate_receipts(CoreContext(),s)
    for service in adapter_call('services', s):
        process = s.get('previews', {}).get(service['name'], {})
        require(health_ok(s, service, process), 'Preview no saludable: ' + service['name'])
        if process.get('serviceVersion'):
            require(process['serviceVersion']==service_version(s,service), 'Entradas del preview cambiaron.')
        require(artifact_version(repository_root(s, service.get('repo')), service.get('artifacts', [])) == process['artifactsVersion'],
                'El artefacto del preview cambió.')
    require(hashlib.sha256(migration_history(s).encode()).hexdigest() == s['migrationAfter'], 'Cambió el historial de migraciones.')


def version(s):
    return hashlib.sha256(json.dumps({'files': job_fingerprint(s),
                         'snapshot': s.get('snapshotSha256'), 'migration': s.get('migrationAfter'),
                         'container': s.get('containerId'), 'port': s.get('dbPort'),
                         'ports': s.get('ports'), 'controller': controller_version(),
                         'httpPort': s['httpPort'], 'executionEnvironment': s.get('executionEnvironment'), 'config': config()}, sort_keys=True).encode()).hexdigest()


def require_job_config(s):
    expected = s.get('configVersion')
    require(not expected or expected == hashlib.sha256(json.dumps(config(), sort_keys=True).encode()).hexdigest(),
            'Cambió la configuración del trabajo. Restáurala para continuar o crea un trabajo nuevo.')


@in_job_environment
def qa_start(s):
    require(s['stage'] == 'ready_for_qa', 'Primero deben pasar las comprobaciones.')
    runtime_module('synchronization').guard(CoreContext(),s)
    require(job_fingerprint(s) == s['checkedVersion'], 'Cambió código: vuelve a verificar.')
    preview_valid(s)
    runtime_module('browser_mcp').ensure_released(CoreContext(),s)
    s.pop('reviewRequest', None)
    s.pop('userConsent', None)
    s['qaVersion'] = version(s)
    runtime_module('qa_context').capture(CoreContext(), s)
    s['stage'] = 'qa'
    save(s, 'qa_started')
    print('Versión de QA:', s['qaVersion'])


def release_browser(s):
    runtime_module('browser_mcp').ensure_released(CoreContext(),s)


def report_errors(report, s):
    """Pure validation shared by qa-check and qa-result; no state transitions."""
    errors = []
    def check(ok, message):
        if not ok: errors.append(message)
    if not isinstance(report, dict): return ['El reporte debe ser un objeto JSON.']
    check(bool(s.get('qaVersion')) and report.get('version') == s.get('qaVersion'), 'Reporte de otra versión; usa qaVersion tras qa-start.')
    check(isinstance(report.get('status'), str) and report.get('status') in {'pass', 'fail', 'blocked'}, 'status debe ser pass, fail o blocked (no passed).')
    check(isinstance(report.get('findings'), list), 'findings debe ser una lista de hallazgos pendientes.')
    if report.get('status') != 'pass': return errors
    check(report.get('findings') == [], 'No puede pasar QA con hallazgos pendientes.')
    check(report.get('browser') == 'chrome' and report.get('headed') is True, 'QA requiere Google Chrome visible.')
    criteria = report.get('criteria', [])
    check(isinstance(criteria, list) and bool(criteria) and all(isinstance(c, dict) and c.get('status') == 'pass' and c.get('evidence') for c in criteria),
          'Cada criterio requiere status pass y evidencia.')
    check((not has_database() or report.get('migrationVerified') is True) and report.get('visualInspection') is True,
          'Falta validación de migraciones o inspección visual.')
    check(bool(report.get('integration')) and bool(report.get('e2e')), 'Falta evidencia de integración/E2E.')
    evidence = report.get('artifacts', [])
    check(isinstance(evidence, list) and bool(evidence) and all(isinstance(v, str) for v in evidence), 'artifacts debe listar rutas de evidencia.')
    if not isinstance(evidence, list): evidence = []
    exploration = report.get('exploration', {})
    if not isinstance(exploration, dict): exploration = {}
    check(exploration.get('tool') == 'playwright-mcp' and bool(exploration.get('scenarios')) and bool(exploration.get('evidence')), 'Falta exploración con Playwright MCP.')
    repeatable = report.get('repeatableE2E', {})
    if not isinstance(repeatable, dict): repeatable = {}
    check(repeatable.get('framework') == 'playwright' and bool(repeatable.get('runner')) and bool(repeatable.get('command')) and bool(repeatable.get('testFiles')) and
          type(repeatable.get('exitCode')) is int and repeatable['exitCode'] == 0 and type(repeatable.get('passed')) is int and repeatable['passed'] > 0 and bool(repeatable.get('report')),
          'Faltan tests E2E repetibles de Playwright ejecutados con éxito.')
    check(isinstance(exploration.get('evidence'), list) and all(name in evidence for name in exploration['evidence']) and repeatable.get('report') in evidence,
          'Las evidencias MCP y el informe CLI deben aparecer en artifacts.')
    for name in evidence:
        if not isinstance(name, str): continue
        p = (jobdir(s['id']) / 'artifacts' / name).resolve()
        check(p.is_relative_to((jobdir(s['id']) / 'artifacts').resolve()) and p.is_file(), 'Evidencia ausente o fuera de artifacts: ' + name)
    return errors


def validate_report(report, s):
    errors = report_errors(report, s)
    require(not errors, 'Reporte QA inválido: ' + '; '.join(errors))


def qa_template(s):
    require(s['stage'] == 'qa' and s.get('qaVersion'), 'Ejecuta qa-start antes de preparar el reporte.')
    path = jobdir(s['id']) / 'artifacts' / ('qa-report-' + s['qaVersion'] + '.json')
    if not path.exists():
        report = json.loads((ROOT / 'harness/qa-report.example.json').read_text())
        report['version'] = s['qaVersion']
        report['job'] = s['id']
        ids = []
        for relative in s.get('specs', []):
            actual=s.get('specStatusInputs',{}).get(relative,{}).get('stagedPath',relative)
            text = safe_path(repository_root(s), actual).read_text()
            ids.extend(re.findall(r'^-\s+(AC-[0-9]+):', text, re.M))
        if ids: report['criteria'] = [{'id': v, 'status': 'pending', 'evidence': ''} for v in dict.fromkeys(ids)]
        atomic(path, report)
    print(str(path))


def brief(s):
    cfg=config()
    value={k:s.get(k) for k in ('id','stage','worktrees','worktree','specs','qaVersion','approvedCandidate','testAccount','ports','url','cycles')}
    value['specs']=[s.get('specStatusInputs',{}).get(p,{}).get('stagedPath',p) for p in s.get('specs',[])]
    value['execution']=runtime_module('execution').observed(s)
    value['synchronization']=runtime_module('synchronization').brief(s)
    value['cycleLimit']=s.get('maxCycles',cfg['maxCorrectionCycles'])
    value['conversation']=s.get('conversation')
    value['qaContext']=s.get('qaContext')
    value['browser']={'tool':'harness_browser_start','job':s['id'],'version':s.get('qaVersion'),'isolated':True}
    value['qaCheckpoint']=runtime_module('qa_context').checkpoint_brief(CoreContext(),s)
    value['feedback']=s.get('feedback')
    value['specCompletion']=s.get('specCompletion')
    value['bases']={n:v.get('base') for n,v in s.get('repositories',{}).items()}
    value['checks']={k:{f:v.get(f) for f in ('status','summary','attempt')} for k,v in s.get('receipts',{}).items()}
    value['checkPlan']=[{k:step[k] for k in ('id','repo','cwd','command','reports') if k in step}
                        for step in cfg.get('checks',[])]
    value['testReports']=cfg.get('testReports',{})
    value['qaFindings']=[{k:v[k] for k in ('id','severity','criteria','title','files') if k in v}
                         for v in s.get('qa',{}).get('findings',[]) if isinstance(v,dict)]
    value['qaReport']=str(jobdir(s['id'])/'qa.json') if s.get('qa') else None
    value['qaReportTemplate']='harness/qa-report.example.json'
    value['artifacts']=str(jobdir(s['id'])/'artifacts')
    value['suites']={k:{f:v.get(f) for f in ('enabled','repo','cwd','testDir','runner','command','report')} for k in ('e2e','apiE2E') if isinstance((v:=cfg.get(k)),dict)}
    value['qaReferences']=setup_module().qa_references(CoreContext(),s)
    value['database']={k:s.get(k) for k in ('container','database','schema','dbPort','resourceKey')}
    value['permissions']={'requestedMode':'inherited' if s.get('conversation') else 'manual','note':'No garantiza permisos efectivos de subagentes. Ante denegación entrega operación y motivo; no la eludas.'}
    return value


@in_job_environment
def qa_result(s, path):
    require(s['stage'] == 'qa', 'QA no está activo.')
    release_browser(s)
    report = json.loads(Path(path).read_text())
    validate_report(report, s)
    require_job_config(s)
    # A negative review can safely invalidate work after QA added regression tests.
    # Only a pass certifies a revision and therefore needs exact current evidence.
    if report['status'] == 'pass':
        require(version(s) == s['qaVersion'], 'Cambió la versión: vuelve a verificar antes de aprobar QA.')
    s['qa'] = report
    if report['status'] == 'pass':
        preview_valid(s)
        s['stage'] = 'awaiting_human_approval'
        s['approvedCandidate'] = version(s)
        s['docsBaseline'] = job_inventory(s)
        s['productBaseline'] = job_fingerprint(s, product_only=True)
    else:
        if report['status']=='fail': s['cycles'] += 1
        s['stage'] = ('paused' if s['cycles'] >= s.get('maxCycles', config()['maxCorrectionCycles'])
                      else 'blocked' if report['status'] == 'blocked' else 'implementing')
    atomic(jobdir(s['id']) / 'qa.json', report)
    save(s, 'qa_' + report['status'])
    release_browser(s)
    print(json.dumps(public(s), indent=2, ensure_ascii=False))


@in_job_environment
def approve(s, supplied_version, confirm=True):
    require(s['stage'] == 'awaiting_human_approval', 'No hay una entrega pendiente de aprobación.')
    runtime_module('synchronization').guard(CoreContext(),s)
    require(supplied_version == s.get('approvedCandidate') == version(s), 'La versión cambió; vuelve a comprobar y pasar QA.')
    preview_valid(s)
    if s.get('publication'):publication_module().index_ready(CoreContext(),s)
    if confirm:
        require(sys.stdin.isatty(), 'La aprobación se ejecuta por el usuario en una terminal interactiva.')
        if s.get('publication'):
            print('Después de documentación: publicar PR, esperar CI satisfactorio y que fusiones todos los PR, y después retirar previews, clon, worktrees y ramas locales del trabajo.')
        require(input(f'Escribe APROBAR {s["id"]} para aprobar esta versión: ') == 'APROBAR ' + s['id'], 'No aprobado.')
    s['approval'] = {'version': supplied_version, 'at': time.time()}
    s['stage'] = 'documenting'
    if s.get('publication'):s['approvalEvidence']=publication_module().evidence(CoreContext(),s)
    save(s, 'human_approved')


@in_job_environment
def finish(s):
    require_job_config(s)
    runtime_module('synchronization').guard(CoreContext(),s)
    retrying=s['stage']=='blocked' and s.get('blockedPhase')=='publishing'
    require((s['stage']=='documenting' or retrying) and s.get('approval'), 'Falta aprobación humana.')
    require(job_fingerprint(s, product_only=True) == s['productBaseline'], 'Documentación cambió el producto: revalidar.')
    if not retrying:runtime_module('spec_status').stage_tracked(CoreContext(),s)
    now = job_inventory(s)
    baseline = s['docsBaseline']
    if not s.get('worktrees') and set(baseline) != set(now):
        baseline = {next(iter(now)): baseline}  # legacy single-repository state
    for repo, files in now.items():
        old = baseline.get(repo, {})
        changed = {p for p in files.keys() | old.keys() if files.get(p) != old.get(p)}
        require(all(p.startswith(('docs/', 'specs/')) or p in {'CLAUDE.md', 'CLAUDE.MD'} for p in changed),
                'Documentación escribió fuera del alcance permitido en ' + repo)
        for name in changed:
            p = repository_root(s, repo) / name
            if not p.exists() or p.suffix.lower() != '.md': continue
            for target in re.findall(r'\]\(([^)]+)\)', p.read_text()):
                target = target.split('#')[0].strip('<>')
                if not target or '://' in target or target.startswith(('mailto:', 'data:')): continue
                require((p.parent / target).exists(), f'Enlace local roto en {repo}/{name}: {target}')
    if s.get('publication'):
        try:
            pub=publication_module();ctx=CoreContext()
            pub.index_ready(ctx,s)
            if retrying:pub.retry(ctx,s)
            elif s.get('approvalEvidence'):require(pub.evidence(ctx,s)==s['approvalEvidence'],'Cambió evidencia de aprobación: revalidar.')
            pub.archive_final_specs(ctx,s)
            for name in s['publication']['repos']:finalization_module().ignored_ready(ctx,s,name)
            publication_module().freeze(CoreContext(),s)
            publication_module().publish(CoreContext(),s)
        except Exception as error:
            s['stage']='blocked'
            if s.get('blockedPhase')!='upstream':s['blockedPhase']='publishing'
            s['finalizationError']=str(error)
            save(s,'publication_blocked');raise
        finalization_module().start(CoreContext(),s)
    else:
        runtime_module('spec_status').prepare(CoreContext(),s)
        runtime_module('spec_status').complete(CoreContext(),s)
        s['stage'] = 'completed'
        save(s, 'documentation_complete_preview_preserved')


def public(s):
    return {k: s.get(k) for k in ('id', 'stage', 'branch', 'worktree', 'worktrees', 'repositories', 'specs', 'url', 'database',
            'dbPort', 'ports', 'schema', 'snapshotSha256', 'testAccount', 'cycles', 'specCompletion', 'approvedCandidate', 'sessionId', 'backgroundId', 'conversation')}


def feedback(s, message):
    release_browser(s)
    require(s['stage'] not in {'cancelled', 'preparing'}, 'No se puede corregir desde esta etapa.')
    require(not s.get('publication') or s['stage'] not in {'cleaning','completed'},'El cierre está limpiando o ya terminó; prepara otro trabajo.')
    require(not any(v.get('removalStarted') for v in s.get('publication',{}).get('repos',{}).values()),'Limpieza parcial: reanuda finalize; las correcciones requieren otro trabajo.')
    if s.get('publication'):
        s['publication']['handoff']=False
        s['publication']['phase']='prepared'
        for item in s['publication']['repos'].values():
            for key in ('tree','inventory','headBefore','commit','changed','ci','pushed') :item.pop(key,None)
    s['cycles'] += 1
    s['stage'] = 'implementing' if s['cycles'] < s.get('maxCycles', config()['maxCorrectionCycles']) else 'paused'
    s.pop('reviewRequest', None)
    s.pop('userConsent', None)
    s.pop('approval', None)
    s.pop('qa', None)
    s['feedback'] = message
    save(s, 'feedback')
    release_browser(s)


def human_confirmation(s, verb):
    require(sys.stdin.isatty(), 'Operación reservada al usuario en una terminal interactiva.')
    text = f'{verb} {s["id"]}'
    require(input(f'Escribe {text}: ') == text, 'Operación no confirmada.')


@in_job_environment
def remove_clone(s):
    # Never remove a container/volume just because its name matches.
    if s.get('containerId'):
        info = json.loads(run(['docker', 'inspect', s['container']]))[0]
        require(info['Id'] == s['containerId'] and info['Config'].get('Labels', {}).get(LABEL) == s['resourceKey'],
                'No se eliminará un contenedor sustituido o ajeno.')
        mounts = info.get('Mounts', [])
        require(any(m.get('Name') == s['volume'] for m in mounts), 'Volumen no pertenece al clon.')
        run(['docker', 'rm', '-f', s['containerId']])
        run(['docker', 'volume', 'rm', s['volume']])
        s.pop('containerId', None)
        s.pop('volumeCreated',None)
    elif s.get('volumeCreated'):
        volumes=run(['docker','volume','ls','--format','{{.Name}}']).splitlines()
        if s['volume'] in volumes:
            info=json.loads(run(['docker','volume','inspect',s['volume']]))[0]
            require(info.get('Labels',{}).get(LABEL)==s['resourceKey'],'Volumen ajeno; no se elimina.')
            run(['docker','volume','rm',s['volume']])
        s.pop('volumeCreated',None)


@in_job_environment
def reset_db(s):
    release_browser(s)
    require_job_config(s)
    require(s['stage'] not in {'awaiting_human_approval', 'documenting', 'completed'},
            'Primero registra feedback; no se restaura una entrega en revisión/aprobada.')
    require(has_database(), 'Este proyecto no usa base de datos.')
    require(sha(s['snapshot']) == s['snapshotSha256'], 'El snapshot original cambió.')
    human_confirmation(s, 'RESTAURAR')
    stop_preview(s)
    remove_clone(s)
    for key in ('approval', 'qa', 'migrationAttempt', 'migrationFailed', 'migrationAfter', 'appliedMigrationFiles'):
        s.pop(key, None)
    s['stage'] = 'blocked'
    save(s, 'reset_started')
    create_clone(s)
    s['stage'] = 'implementing'
    save(s, 'clone_reset')
    release_browser(s)


@in_job_environment
def recheck(s):
    release_browser(s)
    require(not s.get('publication') or s['stage'] not in {'cleaning','completed'},'Entorno retirado o retirándose; prepara otro trabajo.')
    require(not any(v.get('removalStarted') for v in s.get('publication',{}).get('repos',{}).values()),'Limpieza parcial: reanuda finalize; las correcciones requieren otro trabajo.')
    if s.get('publication'):
        s['publication']['handoff']=False;s['publication']['phase']='prepared'
        for item in s['publication']['repos'].values():
            for key in ('tree','inventory','headBefore','commit','changed','ci','pushed'):item.pop(key,None)
    require(s['stage'] not in {'preparing', 'cancelled', 'paused'}, 'No se puede revalidar desde esta etapa.')
    s.pop('reviewRequest', None)
    s.pop('userConsent', None)
    s.pop('approval', None)
    s.pop('qa', None)
    s['stage'] = 'implementing'
    save(s, 'revalidation_requested')
    release_browser(s)
    verify(s)


@in_job_environment
def launch(s, background):
    require(not s.get('conversation'), 'Trabajo vinculado a tu conversación: reanúdala en Claude, sin launch.')
    require_job_config(s)
    with lock('launch-' + s['id'], blocking=False):
        require_no_active_session(s)
        launch_native(s, background)


def require_no_active_session(s):
    if not s.get('launched'):
        return
    sessions = json.loads(run(['claude', 'agents', '--json', '--all'], timeout=20))
    require(isinstance(sessions, list), 'No se pudo comprobar el inventario de Claude.')
    active = [v for v in sessions if (v.get('sessionId') == s['sessionId'] or v.get('id') == s.get('backgroundId') or
               v.get('cwd') == s['worktree']) and
              v.get('state') not in {'stopped', 'failed', 'completed'}]
    require(not active, 'La sesión sigue activa. Usa claude agents para abrirla o detenerla antes de esta operación.')


@in_job_environment
def launch_native(s, background):
    require(s['stage'] in {'implementing', 'ready_for_qa', 'qa', 'awaiting_human_approval', 'documenting', 'blocked'}, 'Trabajo no ejecutable.')
    d = jobdir(s['id'])
    artifacts = d / 'artifacts'
    artifacts.mkdir(exist_ok=True, mode=0o700)
    cfg = {'mcpServers': {'playwright': {'command': 'python3', 'args': [str(ROOT/'harness/browser_mcp.py')]}}}
    atomic(d / 'mcp.json', cfg)
    # Hooks/global plugins are not inherited; the workflow lives in the worktree.
    argv = ['claude', '--model', 'claude-opus-5', '--effort', 'xhigh',
            '--setting-sources', 'project', '--strict-mcp-config',
            '--mcp-config', str(d / 'mcp.json'), '--permission-mode', 'manual',
            '--settings', str(ROOT / 'harness/claude-settings.json')]
    for name, tree in repository_roots(s).items():
        if Path(tree) != Path(s['worktree']): argv += ['--add-dir', str(tree)]
    if s.get('launched'):
        argv += ['--resume', s['sessionId']]
    elif not background:
        argv += ['--session-id', s['sessionId']]
    if background:
        argv += ['--bg']
    prompt = f'/harness {s["id"]} {ROOT}'
    if background:
        # Dispatch ordinary text: startup must not depend on slash-command injection.
        prompt = ('Ejecuta la skill /harness en modo trabajo preparado con estos argumentos: '
                  + s['id'] + ' ' + str(ROOT) + '. Lee .claude/skills/harness/SKILL.md y sigue su flujo. '
                  'No prepares otro trabajo; usa el estado y los worktrees existentes.')
    # --bg is stripped before Claude parses its variadic --add-dir option.
    # Keep the prompt outside all options, including when starting two repos.
    argv += ['--', prompt]
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_USE_')):
            env.pop(key)
    env['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'] = '1'
    env['CLAUDE_CODE_EFFORT_LEVEL'] = 'xhigh'
    env['CLAUDE_CODE_SUBAGENT_MODEL'] = 'claude-opus-5'
    was_launched = s.get('launched', False)
    s['launched'] = True
    save(s, 'native_session_launch_requested')
    if background:
        # --bg chooses its own ID; never assume --session-id was honored.
        result = subprocess.run(argv, cwd=s['worktree'], env=env, text=True, capture_output=True)
        output = result.stdout + result.stderr
        (d / 'launch.log').write_text(output)
        match = re.search(r'claude attach ([a-f0-9-]+)', output)
        if not match:
            save(s, 'native_session_identity_unknown')
            raise Blocked('Claude no devolvió un ID verificable. Revisa launch.log y claude agents; no relances a ciegas.')
        short = match.group(1)
        s['backgroundId'] = short
        sessions = json.loads(run(['claude', 'agents', '--json', '--all'], timeout=20))
        matches = [v for v in sessions if v.get('id') == short and v.get('cwd') == s['worktree']]
        require(len(matches) == 1 and matches[0].get('sessionId'), 'No se pudo confirmar la sesión creada; revisa claude agents.')
        s['sessionId'] = matches[0]['sessionId']
        save(s, 'native_session_identified')
        require(result.returncode == 0, 'Claude devolvió error; revisa launch.log antes de continuar.')
        plain_output = re.sub(r'\x1b\[[0-9;]*m', '', output).lower()
        if 'send a prompt to start' in plain_output:
            save(s, 'native_session_missing_prompt')
            raise Blocked(f'Claude creó la sesión sin recibir el prompt inicial. Abre: claude attach {short}. '
                          f'Escribe dentro: /harness {s["id"]} {ROOT}. '
                          'El trabajo ya está preparado; no repitas start ni crees otra sesión. Revisa launch.log.')
        print(f'Abre: claude attach {short}\nSalida reciente: claude logs {short}')
        return
    code = subprocess.call(argv, cwd=s['worktree'], env=env)
    if code != 0 and not was_launched:
        sessions = json.loads(run(['claude', 'agents', '--json', '--all'], timeout=20))
        if not any(v.get('sessionId') == s['sessionId'] for v in sessions):
            s['launched'] = False
            save(s, 'native_session_launch_failed')
    require(code == 0, 'Claude no inició correctamente. Conserva el estado; revisa login/permisos.')


def doctor():
    checks = {}
    validate_config()
    for tool in dict.fromkeys(['git', 'python3', 'claude', 'graphify', 'node', 'npx', 'google-chrome'] + adapter_call('tools')):
        checks[tool] = shutil.which(tool) or 'FALTA'
    checks['desktop'] = os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') or 'FALTA'
    try:
        checks['dockerServer'] = run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=15) if has_database() else 'NO REQUERIDO'
    except (Blocked, OSError, subprocess.TimeoutExpired):
        checks['dockerServer'] = 'INACCESIBLE'
    checks['snapshot'] = ('Se exige --snapshot y --safe-snapshot; nunca se obtiene del origen automáticamente.'
                          if has_database() else 'No requerido: proyecto explícitamente sin base de datos.')
    print(json.dumps(checks, indent=2, ensure_ascii=False))
    return 1 if 'FALTA' in checks.values() or checks['dockerServer'] == 'INACCESIBLE' else 0


def preflight_module():
    spec = importlib.util.spec_from_file_location('harness_preflight', ROOT/'harness/preflight.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def runtime_module(name):
    spec=importlib.util.spec_from_file_location('harness_'+name,ROOT/'harness'/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def publication_module():return runtime_module('publication')
def github_module():return runtime_module('github')
def finalization_module():return runtime_module('finalization')


def setup_module():
    spec = importlib.util.spec_from_file_location('harness_setup', ROOT/'harness/setup_project.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('status')
    p = sub.add_parser('setup', help='Inspeccionar o validar configuración y migraciones en un clon desechable.')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--refresh-tests', action='store_true', help='Renovar solo preflight estático sin ejecutar suites ni restaurar DB.')
    p.add_argument('--snapshot')
    p.add_argument('--safe-snapshot', action='store_true')
    p.add_argument('--test-account')
    for name in ('spec-review', 'spec-accept', 'spec-draft', 'spec-status'):
        p = sub.add_parser(name, help='Revisar o consultar autorización previa de una spec, sin iniciar trabajos.')
        p.add_argument('spec')
        if name in {'spec-review', 'spec-accept'}: p.add_argument('--session-id', required=True)
    p = sub.add_parser('start', help='Preparar una spec autorizada usando setup local, con ID y rama automáticos.')
    p.add_argument('spec', nargs='+')
    p.add_argument('--job')
    p.add_argument('--branch')
    p.add_argument('--base', default='HEAD')
    p.add_argument('--launch', action='store_true')
    p.add_argument('--session-id', help='Coordinar una única spec en esta sesión existente de Claude.')
    p = sub.add_parser('prepare')
    p.add_argument('job')
    p.add_argument('--spec', action='append', required=True)
    p.add_argument('--branch', required=True)
    p.add_argument('--base', default='HEAD')
    p.add_argument('--snapshot')
    p.add_argument('--schema')
    p.add_argument('--safe-snapshot', action='store_true')
    p.add_argument('--test-account', help='Identificador de cuenta sintética; nunca contraseña.')
    for name in ('show', 'brief', 'review', 'accept-review', 'recover', 'accept-recovery', 'metrics', 'qa-template', 'qa-checkpoint', 'qa-check', 'verify', 'recheck', 'graph', 'qa-start', 'qa-result', 'approve', 'finish', 'feedback', 'launch', 'cancel', 'stop', 'reset-db', 'cleanup', 'extend', 'repair-flyway-network', 'finalize', 'resume-runtime', 'sync'):
        p = sub.add_parser(name)
        p.add_argument('job')
        if name in {'review', 'recover', 'accept-recovery'}: p.add_argument('--session-id', required=True)
        if name == 'sync':p.add_argument('--after',help='Esperar una dependencia explícita del mismo proyecto antes de integrar.')
        if name == 'verify': p.add_argument('--force',action='store_true')
        if name in {'qa-result','qa-check','qa-checkpoint'}: p.add_argument('report')
        if name == 'metrics': p.add_argument('--transcript', help='Conversación local de esta sesión; lectura bajo demanda.')
        if name == 'approve': p.add_argument('--version', required=True)
        if name == 'feedback': p.add_argument('message')
        if name == 'launch': p.add_argument('--background', action='store_true')
        if name == 'extend': p.add_argument('--cycles', type=int, required=True)
    a = parser.parse_args()
    if a.command in {'spec-review', 'spec-accept', 'spec-draft', 'spec-status'}:
        setup_module().ensure_controller(CoreContext())
        module = runtime_module('spec_readiness')
        action = {'spec-review': module.request, 'spec-accept': module.accept,
                  'spec-draft': module.draft, 'spec-status': module.inspect}[a.command]
        args = (a.spec, a.session_id) if hasattr(a, 'session_id') else (a.spec,)
        print(json.dumps(action(CoreContext(), *args), ensure_ascii=False))
        return 0
    if a.command == 'setup':
        require(a.validate or not (a.snapshot or a.safe_snapshot or a.test_account), 'Usa --validate para guardar un snapshot.')
        module = setup_module()
        result = (module.validate(CoreContext(), a) if a.validate else
                  module.refresh_tests(CoreContext()) if a.refresh_tests else module.inspect(CoreContext()))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if a.command == 'start': setup_module().start(CoreContext(), a); return 0
    if a.command == 'doctor': return doctor()
    if a.command == 'prepare': prepare(a); return 0
    if a.command == 'status':
        print(json.dumps([diagnostic(json.loads(p.read_text())) for p in (STORE / 'runs').glob('*/state.json')], indent=2, ensure_ascii=False))
        return 0
    if a.command in {'recover', 'accept-recovery'}:
        # Registry first: assignment cannot race another start/recovery. A job
        # operation already holding its lock makes recovery fail instead of waiting.
        with lock(), lock('job-' + jobdir(a.job).name, blocking=False):
            s = load(a.job); module = runtime_module('conversation')
            action = module.recover_request if a.command == 'recover' else module.accept_recovery
            print(json.dumps(action(CoreContext(), s, a.session_id), ensure_ascii=False))
        return 0
    if a.command in {'show','brief','metrics'}:
        s=load(a.job)
        result=diagnostic(s) if a.command=='show' else brief(s) if a.command=='brief' else runtime_module('metrics').summarize(CoreContext(),s,a.transcript)
        print(json.dumps(result,indent=2,ensure_ascii=False));return 0
    with lock('job-' + jobdir(a.job).name, blocking=False):
        s = load(a.job)
        if a.command == 'show': print(json.dumps(diagnostic(s), indent=2, ensure_ascii=False))
        elif a.command == 'brief': print(json.dumps(brief(s), indent=2, ensure_ascii=False))
        elif a.command == 'review': print(json.dumps(runtime_module('conversation').request(CoreContext(), s, a.session_id), ensure_ascii=False))
        elif a.command == 'accept-review':
            runtime_module('conversation').accept(CoreContext(), s)
            print(json.dumps(brief(s), ensure_ascii=False))
        elif a.command == 'metrics': print(json.dumps(runtime_module('metrics').summarize(CoreContext(),s,a.transcript),indent=2,ensure_ascii=False))
        elif a.command == 'qa-template': qa_template(s)
        elif a.command == 'qa-checkpoint': print(json.dumps(runtime_module('qa_context').checkpoint(CoreContext(),s,a.report),ensure_ascii=False))
        elif a.command == 'qa-check':
            require(s['stage']=='qa', 'QA no está activo; usa qa-start después de verificar.')
            errors=report_errors(json.loads(Path(a.report).read_text()),s)
            print(json.dumps({'valid':not errors,'errors':errors},ensure_ascii=False))
            return 1 if errors else 0
        elif a.command == 'verify': verify(s, force=a.force)
        elif a.command == 'repair-flyway-network': print(json.dumps(preflight_module().repair_flyway_network(CoreContext(), s), indent=2))
        elif a.command == 'recheck': recheck(s)
        elif a.command == 'graph': graph(s)
        elif a.command == 'qa-start': qa_start(s)
        elif a.command == 'qa-result': qa_result(s, a.report)
        elif a.command == 'approve': approve(s, a.version)
        elif a.command == 'finish': finish(s)
        elif a.command == 'finalize': finalization_module().start(CoreContext(),s)
        elif a.command == 'feedback': feedback(s, a.message)
        elif a.command == 'reset-db': reset_db(s)
        elif a.command == 'resume-runtime': resume_runtime(s)
        elif a.command == 'sync':
            with job_environment(s):print(json.dumps(runtime_module('synchronization').sync(CoreContext(),s,a.after),ensure_ascii=False))
        elif a.command == 'extend':
            require(a.cycles > s.get('maxCycles', config()['maxCorrectionCycles']), 'El nuevo límite debe ser mayor.')
            human_confirmation(s, 'AMPLIAR')
            s['maxCycles'] = a.cycles
            if s['stage'] == 'paused': s['stage'] = 'implementing'
            save(s, 'limit_extended_by_user')
        elif a.command == 'cleanup':
            require(s['stage'] in DONE, 'Completa o cancela el trabajo antes de limpiar.')
            require_no_active_session(s)
            human_confirmation(s, 'ELIMINAR-CLON')
            stop_preview(s); remove_clone(s); save(s, 'clone_removed_worktree_preserved')
            (jobdir(s['id']) / 'secrets.json').unlink(missing_ok=True)
        elif a.command == 'stop':
            stop_preview(s); s['stage'] = 'blocked'; save(s, 'preview_stopped'); release_browser(s)
        elif a.command == 'cancel':
            require_no_active_session(s)
            stop_preview(s); s['stage'] = 'cancelled'; save(s, 'cancelled_resources_preserved'); release_browser(s)
    # Never keep a Python job lock while the Claude session calls back into this CLI.
    if a.command == 'launch': launch(s, a.background)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Blocked, OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.TimeoutExpired) as e:
        print(f'BLOQUEADO: {e}', file=sys.stderr)
        sys.exit(2)
