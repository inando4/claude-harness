"""Project adapter: SistemaJuridico (Spring Boot/Maven/Flyway)."""
import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

def migration_history(h, s):
    schema = s['schema']
    return h.sql(s, f'''SELECT installed_rank,version,script,coalesce(checksum::text,''),success FROM "{schema}".flyway_schema_history ORDER BY installed_rank''')

def app_env(h, s, migrating=False):
    h.db_info(s)
    creds = json.loads((h.jobdir(s['id']) / 'secrets.json').read_text())
    url = f"jdbc:postgresql://127.0.0.1:{s['dbPort']}/{s['database']}?currentSchema={s['schema']}"
    role = 'sistema_juridico_migrator' if migrating else 'sistema_juridico_app'
    props = {'spring.profiles.active': 'local', 'spring.datasource.url': url, 'spring.datasource.username': role, 'spring.datasource.password': creds['password'], 'spring.flyway.enabled': False, 'spring.flyway.url': url, 'spring.flyway.user': role if migrating else '', 'spring.flyway.password': creds['password'] if migrating else '', 'server.address': '127.0.0.1', 'server.port': s['httpPort'], 'server.servlet.session.cookie.name': 'HARNESS_' + s['resourceKey'], 'server.servlet.session.cookie.secure': False, 'app.public-base-url': s['url'], 'app.rate-limit-key': creds['rateLimitKey'], 'logging.level.pe.org.beneficencia.legalcontrol': 'INFO'}
    env = h.clean_env()
    env['SPRING_APPLICATION_JSON'] = json.dumps(props)
    return env

def migrations_version(h, s):
    paths = sorted((Path(s['worktree']) / 'src/main/resources/db/migration').glob('*.sql'))
    return hashlib.sha256(''.join((p.name + h.sha(p) for p in paths)).encode()).hexdigest()

def test_summary(h, tree):
    result = {}
    for suite in ('surefire', 'failsafe'):
        files = list((Path(tree) / f'target/{suite}-reports').glob('TEST-*.xml'))
        h.require(files, f'No se ejecutaron pruebas {suite}.')
        counts = dict(tests=0, failures=0, errors=0, skipped=0)
        for path in files:
            xml = ET.parse(path).getroot()
            for key in counts:
                counts[key] += int(xml.get(key, 0))
        h.require(counts['tests'] > 0 and (not any((counts[k] for k in ('failures', 'errors', 'skipped')))), f'Pruebas {suite} fallidas/omitidas: {counts}')
        result[suite] = counts
    return result

def database_config(h):
    return {'provider': 'postgres', 'image': h.config()['postgresImage'],
            'schema': h.config()['databaseSchema'],
            'migratorRole': 'sistema_juridico_migrator', 'appRole': 'sistema_juridico_app'}


def validate(h):
    for key in ('postgresImage', 'databaseSchema', 'checkCommand', 'buildCommand', 'jar', 'healthPath'):
        h.require(key in h.config(), 'Falta configuración de SistemaJuridico: ' + key)
    h.safe_path(h.ROOT, h.config()['jar'])


def tools(h):
    return ['java', 'docker']


def service_names(h):
    return ['web']


def checks(h, s):
    for kind in ('surefire', 'failsafe'):
        reportdir = Path(s['worktree']) / f'target/{kind}-reports'
        if reportdir.exists(): shutil.rmtree(reportdir)
    d = h.jobdir(s['id'])
    h.run(h.config()['checkCommand'], cwd=s['worktree'], env=h.clean_env(), log=d/'checks.log')
    result = test_summary(h, s['worktree'])
    h.run(h.config()['buildCommand'], cwd=s['worktree'], env=h.clean_env(), log=d/'build.log')
    return result


def migration_setup(h, s):
    h.run(h.config()['buildCommand'], cwd=s['worktree'], env=h.clean_env(), log=h.jobdir(s['id'])/'migration-build.log')


def migrate(h, s):
    h.run(['java', '-jar', h.config()['jar'], '--app.command=migrate'], cwd=s['worktree'],
          env=app_env(h, s, migrating=True), log=h.jobdir(s['id'])/'migrate.log')


def integration_checks(h, s):
    # Maven verify already ran integration on isolated Testcontainers.
    return {}


def services(h, s):
    return [{'name': 'web', 'command': ['java', '-jar', h.config()['jar']], 'cwd': '.',
             'env': app_env(h, s), 'port': s['httpPort'], 'healthPath': h.config()['healthPath'],
             'healthJson': {'status': 'UP'}, 'artifacts': [h.config()['jar']]}]
