"""Configuration adapter for any stack with commands and JUnit reports.

PostgreSQL and explicitly database-free projects are supported. No shell expansion.
"""
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def database_config(h):
    return h.config()['database']


def service_names(h):
    return [v['name'] for v in h.config()['services']]


def tools(h):
    return h.config().get('requiredTools', []) + (['docker'] if h.has_database() else [])


def validate(h):
    cfg = h.config()
    h.runtime_module('execution').limits(h)
    h.runtime_module('test_selectors').validate(h)
    h.verification_module().validate(h)
    h.require(isinstance(cfg.get('database'), dict), 'Declara database.provider: postgres o none.')
    h.require(cfg.get('services'), 'Configura al menos un servicio; el primero es la URL de revisión.')
    h.require(cfg.get('checks') and cfg.get('testReports'), 'Configura checks y testReports JUnit (sin omisiones).')
    h.require(not cfg.get('needsConfiguration', False), 'Plantilla sin configurar: adapta comandos/rutas y cambia needsConfiguration a false.')
    for pattern in (cfg['testReports'] | cfg.get('integrationReports', {})).values():
        tree, glob = location(h, None, pattern)
        h.safe_path(tree, glob)
        h.require('*' in glob or glob.endswith('.xml'), 'Usa un archivo o glob XML para testReports.')
    steps = cfg.get('migrationSetup', []) + cfg.get('setup', []) + cfg['checks'] + cfg.get('build', []) + cfg['services'] + cfg.get('integrationChecks', [])
    h.require(bool(cfg.get('integrationChecks')) == bool(cfg.get('integrationReports')), 'Configura integrationChecks e integrationReports juntos.')
    if cfg['database']['provider'] == 'postgres':
        h.require(cfg.get('migrate') and cfg.get('migrationFiles'), 'Configura migrate y migrationFiles.')
        steps += [cfg['migrate']]
        h.require(cfg['database'].get('historySql', '').lstrip().upper().startswith('SELECT '), 'historySql debe ser SELECT del historial de migraciones, con orden estable.')
    for step in steps:
        h.require(isinstance(step.get('command'), list) and step['command'] and all(isinstance(v, str) for v in step['command']), 'command debe ser una lista de argumentos.')
        h.require(step.get('resourceClass','heavy') in {'heavy','light'}, 'resourceClass debe ser heavy o light.')
        h.safe_path(h.repository_root(None, step.get('repo')), step.get('cwd', '.'))
        h.require(isinstance(step.get('env', {}), dict), 'env debe ser un objeto.')
    for item in cfg['services']:
        h.require(item.get('healthPath', '').startswith('/'), 'Cada servicio necesita healthPath local.')
        for path in item.get('artifacts', []): h.safe_path(h.repository_root(None, item.get('repo')), path)
    for pattern in cfg.get('migrationFiles', []):
        tree, glob = location(h, None, pattern)
        h.safe_path(tree, glob)


def variables(h, s, migrating=False, repo=None):
    ports = s.get('ports', {'web': s['httpPort']})
    values = {'PORT': str(s['httpPort']), 'URL': s['url'], 'HOST': '127.0.0.1',
              'JOB_ID': s['id'], 'WORKTREE': str(h.repository_root(s, repo)), 'RUN_DIR': str(h.jobdir(s['id']))}
    for name, tree in h.repository_roots(s).items():
        values['WORKTREE_' + name.upper()] = str(tree)
    for name, port in ports.items():
        values['PORT_' + name.upper()] = str(port)
        values['URL_' + name.upper()] = f'http://localhost:{port}'
    if h.has_database():
        h.db_info(s)
        secret = json.loads((h.jobdir(s['id'])/'secrets.json').read_text())
        role = database_config(h)['migratorRole' if migrating else 'appRole']
        values.update(DB_HOST='127.0.0.1', DB_PORT=str(s['dbPort']), DB_NAME=s['database'],
                      DB_USER=role, DB_PASSWORD=secret['password'], SCHEMA=s['schema'], DB_CONTAINER=s['container'],
                      DB_RESOURCE_KEY=s['resourceKey'])
        values['DATABASE_URL'] = f'postgresql://{role}:{secret["password"]}@127.0.0.1:{s["dbPort"]}/{s["database"]}'
    return values


def expand(h, text, values):
    h.require(isinstance(text, str), 'Variables y argumentos deben ser texto.')
    def replace(match):
        key = match.group(1)
        h.require(key in values, 'Variable desconocida: ' + key)
        return values[key]
    return re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', replace, text)


def app_env(h, s, migrating=False):
    values = variables(h, s, migrating)
    env = h.clean_env()
    # Supply isolated DB defaults as well as explicitly configured app variable names.
    if h.has_database():
        env.update(DATABASE_URL=values['DATABASE_URL'], PGHOST=values['DB_HOST'],
                   PGPORT=values['DB_PORT'], PGDATABASE=values['DB_NAME'], PGUSER=values['DB_USER'],
                   PGPASSWORD=values['DB_PASSWORD'], DB_RESOURCE_KEY=values['DB_RESOURCE_KEY'],
                   E2E_CLONE_ID=values['DB_RESOURCE_KEY'], PGOPTIONS='-c search_path=' + values['SCHEMA'])
    env.update({k: expand(h, v, values) for k, v in h.config().get('env', {}).items()})
    return env


def command(h, s, step, migrating=False):
    values = variables(h, s, migrating, step.get('repo'))
    env = app_env(h, s, migrating)
    env.update({k: expand(h, v, values) for k, v in step.get('env', {}).items()})
    return ([expand(h, v, values) for v in step['command']],
            h.safe_path(h.repository_root(s, step.get('repo')), step.get('cwd', '.')), env)


def location(h, state, pattern):
    repo = pattern.get('repo') if isinstance(pattern, dict) else None
    glob = pattern.get('glob') if isinstance(pattern, dict) else pattern
    if state is None or isinstance(state, dict):
        tree = h.repository_root(state, repo)
    else:
        h.require(repo is None, 'El reporte con repo necesita el estado completo del trabajo.')
        tree = Path(state)
    h.require(isinstance(glob, str), 'Falta glob de reporte/migración.')
    return tree, glob


def matching(h, tree, pattern):
    tree, pattern = location(h, tree, pattern)
    h.safe_path(tree, pattern)
    paths = sorted(Path(tree).glob(pattern))
    for path in paths:
        h.require(path.resolve().is_relative_to(Path(tree).resolve()), 'Reporte/migración fuera del árbol.')
    return [p for p in paths if p.is_file()]


def test_summary(h, tree):
    return read_reports(h, tree, h.config()['testReports'])


def read_reports(h, tree, patterns):
    result = {}
    for name, pattern in patterns.items():
        files = matching(h, tree, pattern)
        h.require(files, 'Faltan informes JUnit: ' + name)
        counts = dict(tests=0, failures=0, errors=0, skipped=0)
        for path in files:
            root = ET.parse(path).getroot()
            suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
            leaves = [v for v in suites if not v.findall('testsuite')]
            for suite in leaves:
                for key in counts: counts[key] += int(suite.get(key, 0))
        h.require(counts['tests'] > 0 and not any(counts[k] for k in ('failures', 'errors', 'skipped')), 'Pruebas fallidas, omitidas o vacías: ' + name)
        result[name] = counts
    return result


def execute(h, s, step, log, migrating=False):
    argv, cwd, env = command(h, s, step, migrating)
    path=h.attempt_path(s,log)
    with h.runtime_module('execution').reservation(h,s,step) as slot:
        generated=h.runtime_module('generated');baseline=generated.before(h,s,step)
        try:h.run(argv, cwd=cwd, env=env, log=path, resource_slot=slot)
        finally:generated.after(h,s,baseline)
    return path


def checks(h, s):
    if any(v.get('id') for phase in ('setup','checks','build') for v in h.config().get(phase,[])):
        return grouped_checks(h,s)
    for i, step in enumerate(h.config().get('setup', [])):
        try:execute(h, s, step, f'setup-{i}.log')
        except h.Blocked as error:raise h.InfrastructureBlocked('Falló instalación de dependencias.') from error
    for pattern in h.config()['testReports'].values():
        for path in matching(h, s, pattern): path.unlink()
    for i, step in enumerate(h.config()['checks']):
        execute(h, s, step, f'checks-{i}.log')
    result = test_summary(h, s)
    for i, step in enumerate(h.config().get('build', [])):
        execute(h, s, step, f'build-{i}.log')
    return result


def grouped_checks(h,s):
    summary={}
    all_reports=h.config()['testReports']
    grouped=any(v.get('cache') for v in h.config()['checks'])
    if not grouped:
        for pattern in all_reports.values():
            for path in matching(h,s,pattern):path.unlink()
    for phase in ('setup','checks','build'):
        for i,step in enumerate(h.config().get(phase,[])):
            identifier=step.get('id',f'{phase}-{i}')
            reports={name:all_reports[name] for name in step.get('reports',[])} if phase=='checks' else {}
            # An ungrouped legacy check cannot acquire a partial-success cache receipt.
            result=h.verification_module().group(h,s,step,identifier,reports,
                lambda step=step,identifier=identifier:execute(h,s,step,identifier+'.log'),phase=phase)
            summary.update(result)
        if phase=='checks': summary=read_reports(h,s,all_reports)
    return summary


def migration_files(h, s):
    items = {}
    for pattern in h.config().get('migrationFiles', []):
        tree, glob = location(h, s, pattern)
        repo = pattern.get('repo', h.config().get('primaryRepository', 'main')) if isinstance(pattern, dict) else h.config().get('primaryRepository', 'main')
        for p in matching(h, s, pattern):
            items[repo + '/' + str(p.relative_to(tree))] = h.sha(p)
    return items


def migrations_version(h, s):
    return hashlib.sha256(''.join(sorted(k+v for k,v in migration_files(h,s).items())).encode()).hexdigest()


def migration_history(h, s):
    if not h.has_database(): return 'no-database'
    # PostgreSQL enforces read-only even if a project accidentally supplies write SQL.
    query = database_config(h)['historySql'].replace('${SCHEMA}', s['schema']).rstrip(';')
    return h.sql(s, 'BEGIN TRANSACTION READ ONLY; ' + query + '; COMMIT;')


def migration_setup(h, s):
    for i, step in enumerate(h.config().get('migrationSetup', [])):
        execute(h, s, step, f'migration-setup-{i}.log')


def migrate(h, s):
    if h.has_database(): execute(h, s, h.config()['migrate'], 'migrate.log', migrating=True)


def integration_checks(h, s):
    patterns = h.config().get('integrationReports', {})
    for pattern in patterns.values():
        for path in matching(h, s, pattern): path.unlink()
    for i, step in enumerate(h.config().get('integrationChecks', [])):
        execute(h, s, step, f'integration-{i}.log')
    return read_reports(h, s, patterns)


def services(h, s):
    result = []
    for item in h.config()['services']:
        argv, cwd, env = command(h, s, item)
        result.append(dict(item, command=argv, cwd=str(cwd.relative_to(h.repository_root(s, item.get('repo')))), env=env,
                           port=s['ports'][item['name']]))
    return result


def prepare_tools(h,s):
    for i,step in enumerate(h.config().get('setup',[])):
        identifier=step.get('id',f'setup-{i}')
        h.verification_module().group(h,s,step,identifier,{},
            lambda step=step,identifier=identifier:execute(h,s,step,identifier+'.log'),phase='setup')
