"""Exercise restoration and the actual migration command on a disposable clone."""
import hashlib
import json
from pathlib import Path
import shutil
import time


def docker_identity(h):
    return {'context': h.run(['docker', 'context', 'show']),
            'engine': h.run(['docker', 'info', '--format', '{{.ID}}'])}


def database(h, snapshot):
    if not h.has_database(): return {'status': 'not_required'}
    h.require(snapshot and h.sha(snapshot['path']) == snapshot['sha256'], 'Falta snapshot autorizado vigente para comprobar infraestructura.')
    identity = docker_identity(h)
    key = h.uuid.uuid4().hex[:12]
    job = 'setup-check-' + key
    directory = h.jobdir(job)
    trees = directory/'repositories'
    primary = h.config().get('primaryRepository', 'main')
    sources = h.repositories_module().sources(h)
    ports = {name: h.available_port() for name in h.adapter_call('service_names')}
    preview = h.config().get('previewService', next(iter(ports)))
    s = {'id': job, 'purpose': 'setup_preflight', 'stage': 'preparing', 'resourceKey': key,
         'container': 'claude-harness-' + key, 'volume': 'claude-harness-' + key,
         'database': 'h_' + key, 'schema': h.database_config()['schema'],
         'snapshot': snapshot['path'], 'snapshotSha256': snapshot['sha256'],
         'worktrees': {name: str(trees/name) for name in sources},
         'worktree': str(trees/primary), 'ports': ports, 'httpPort': ports[preview],
         'url': f'http://localhost:{ports[preview]}', 'cycles': 0}
    h.save(s, 'setup_preflight_started')
    result = None
    try:
        for name, item in sources.items():
            tree = trees/name
            # A local copy avoids modifying branch/worktree registrations in user repos.
            h.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--local', '--no-hardlinks',
                   '--quiet', item['path'], str(tree)])
            h.run(['git', '-c', 'core.hooksPath=/dev/null', 'checkout', '--detach', item.get('base', 'HEAD')], cwd=tree)
            for path in h.repositories_module().supplemental(h, item):
                dest = tree/path.relative_to(item['path'])
                dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, dest)
        shutil.copytree(h.ROOT/'harness', Path(s['worktree'])/'harness', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        h.create_clone(s)
        h.adapter_call('migration_setup', s)
        h.adapter_call('migrate', s)
        history = h.migration_history(s)
        h.require(history, 'Historial vacío después de comprobar migraciones.')
        result = {'status': 'passed', 'at': time.time(), 'job': job, 'docker': identity,
                  'snapshotSha256': snapshot['sha256'], 'migrationVersion': h.migrations_version(s),
                  'historySha256': hashlib.sha256(history.encode()).hexdigest()}
        s['preflightResult'] = result
        h.save(s, 'setup_migration_passed')
    finally:
        # Only this probe's labelled container and directory are disposable.
        # Cleanup failure remains blocked and discoverable; never claim ready.
        s['stage'] = 'blocked'; h.save(s, 'setup_preflight_cleanup_started')
        if s.get('containerId'): h.remove_clone(s)
        (directory/'secrets.json').unlink(missing_ok=True)
        if trees.exists(): shutil.rmtree(trees)
        s['stage'] = 'completed' if result else 'cancelled'
        h.save(s, 'setup_preflight_cleaned')
    return result


def repair_flyway_network(h, s):
    """Narrow audited recovery; no arbitrary config-hash bypass or DB reset."""
    h.require(s['stage'] == 'blocked' and s.get('migrationFailed'), 'Solo recupera un trabajo bloqueado en migración.')
    h.require_job_config(s)
    sessions = json.loads(h.run(['claude', 'agents', '--json', '--all']))
    relevant = [v for v in sessions if v.get('cwd') == s['worktree'] or v.get('sessionId') == s.get('sessionId')]
    h.require(all(v.get('state') in {'stopped','failed','completed'} or
                  (v.get('status') == 'idle' and v.get('state') in {'blocked',None}) for v in relevant),
              'La sesión está trabajando; espera a que se detenga antes de reparar infraestructura.')
    with h.lock():
        other = [v for v in h.setup_module().active_jobs(h) if v['id'] != s['id']]
        h.require(not other, 'Termina otros trabajos antes de cambiar la configuración compartida.')
        cfg = h.config()
        h.require(cfg['adapter'] == 'adapters/commands.py', 'La reparación requiere el adaptador de comandos.')
        step = cfg['migrate']; argv = step['command']
        h.require(argv[:3] == ['docker','run','--rm'] and argv[-1] == 'migrate' and
                  '--network' in argv and argv[argv.index('--network')+1] == 'host' and
                  any(v.startswith('flyway/flyway:') for v in argv), 'No es el caso Flyway Docker host conocido.')
        h.require(step.get('env',{}).get('FLYWAY_URL') == 'jdbc:postgresql://127.0.0.1:${DB_PORT}/${DB_NAME}',
                  'URL de migración distinta; requiere diagnóstico específico.')
        log_path=h.jobdir(s['id'])/'migrate.log'
        if s.get('attempt'):
            log_path=h.jobdir(s['id'])/'attempts'/f"{s['attempt']:05d}"/'migrate.log'
        log = log_path.read_text()
        h.require('SQL State  : 08001' in log and 'Connection refused' in log and
                  f'127.0.0.1:{s["dbPort"]}/{s["database"]}' in log and 'Successfully applied' not in log,
                  'No hay evidencia suficiente de fallo previo a conectar; no se omite reset-db.')
        h.db_info(s)
        h.require(h.migrations_version(s) == s['migrationAttempt'], 'Cambiaron las migraciones desde el fallo.')
        history = h.migration_history(s)
        h.require(hashlib.sha256(history.encode()).hexdigest() == s['migrationBefore'], 'Cambió el historial; no se recupera automáticamente.')
        setup_path = h.STORE/'setup.json'
        record = json.loads(setup_path.read_text())
        h.require(record['snapshot']['sha256'] == s['snapshotSha256'], 'El snapshot autorizado no coincide.')
        audit = h.jobdir(s['id'])/('network-repair-' + h.uuid.uuid4().hex[:8])
        audit.mkdir(mode=0o700)
        h.atomic(audit/'config-before.json', cfg); h.atomic(audit/'state-before.json', s)
        h.atomic(audit/'setup-before.json', record)
        old_cfg = json.loads(json.dumps(cfg))
        argv[argv.index('--network')+1] = 'container:${DB_CONTAINER}'
        step['env']['FLYWAY_URL'] = 'jdbc:postgresql://127.0.0.1:5432/${DB_NAME}'
        h.atomic(h.ROOT/'harness/config.json', cfg)
        try:
            h.validate_config()
            result = database(h, record['snapshot'])
        except Exception:
            h.atomic(h.ROOT/'harness/config.json', old_cfg)
            raise
        # Probe passed on a separate clone. Original job clone remains untouched.
        record.update(configSha256=h.sha(h.ROOT/'harness/config.json'), databasePreflight=result,
                      manifestSha256=h.setup_module().signature(h), status='ready', runtimeValidation='pending')
        h.atomic(setup_path, record)
        s['configVersion'] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
        s['migrationFailed'] = False
        s['cycles'] = max(0, s['cycles']-1)
        s['stage'] = 'implementing'
        for key in ('approval','qa','checkedVersion','approvedCandidate'): s.pop(key, None)
        h.save(s, 'flyway_network_repaired_preflight_passed')
        return {'job':s['id'], 'stage':s['stage'], 'preflight':result, 'checks':'must_revalidate'}
