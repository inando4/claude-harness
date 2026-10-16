#!/usr/bin/env python3
"""Opt-in integration: disposable synthetic PostgreSQL only. No production connections."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

module = Path(__file__).resolve().parents[1] / 'harness.py'
spec = importlib.util.spec_from_file_location('harness', module)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Create and delete labelled test containers.')
    parser.add_argument('--browser', action='store_true', help='Also open visible Chrome using a cached Playwright install.')
    parser.add_argument('--generic', action='store_true', help='Exercise the commands adapter against the synthetic clone.')
    a = parser.parse_args()
    if not a.run:
        parser.error('Explicit --run required')
    original_root = h.ROOT
    jar = h.ROOT / h.config()['jar']
    h.require(jar.is_file(), 'Build with ./mvnw -B -DskipTests package first.')
    artifacts = h.STORE / 'smoke-artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sj-harness-smoke-') as tmp:
        h.STORE = Path(tmp)
        key = uuid.uuid4().hex[:12]
        password = secrets.token_hex(24)
        source = {'id': 'fixture-source', 'stage': 'preparing', 'resourceKey': key,
                  'container': 'sj-harness-smoke-' + key, 'volume': 'sj-harness-smoke-' + key,
                  'database': 'sj_' + key, 'schema': 'sistema_juridico', 'httpPort': h.available_port(),
                  'worktree': str(h.ROOT)}
        source['url'] = f'http://localhost:{source["httpPort"]}'
        envfile = Path(tmp) / 'postgres.env'
        envfile.write_text(f'POSTGRES_USER=harness_owner\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB={source["database"]}\n')
        envfile.chmod(0o600)
        clone = None
        try:
            source['containerId'] = h.run(['docker', 'run', '-d', '--name', source['container'],
                '--label', f'{h.LABEL}={key}', '--env-file', str(envfile), '-p', '127.0.0.1::5432',
                '--mount', f'type=volume,source={source["volume"]},target=/var/lib/postgresql/data',
                h.config()['postgresImage']])
            info = json.loads(h.run(['docker', 'inspect', source['containerId']]))[0]
            source['dbPort'] = int(info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'])
            for _ in range(60):
                if subprocess.run(['docker', 'exec', source['container'], 'pg_isready', '-h', '127.0.0.1', '-U', 'harness_owner'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    break
                time.sleep(1)
            ddl = (f"CREATE ROLE sistema_juridico_migrator LOGIN PASSWORD '{password}';"
                   f"CREATE ROLE sistema_juridico_app LOGIN PASSWORD '{password}';"
                   'CREATE SCHEMA sistema_juridico AUTHORIZATION sistema_juridico_migrator;'
                   f'GRANT CONNECT ON DATABASE {source["database"]} TO sistema_juridico_migrator,sistema_juridico_app;')
            # Fixture contains no real users, names, cases, tokens, or credentials.
            proc = subprocess.run(['docker', 'exec', '-i', source['container'], 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                    '-U', 'harness_owner', '-d', source['database']], input=ddl.encode(), capture_output=True)
            h.require(proc.returncode == 0, 'Fixture roles failed')
            h.atomic(h.jobdir(source['id']) / 'secrets.json', {'password': password, 'rateLimitKey': secrets.token_hex(32)})
            h.run(['java', '-jar', str(jar), '--app.command=migrate'], env=h.app_env(source, True), log=artifacts/'source-migrate.log')
            history = h.migration_history(source)
            h.require(history, 'Fixture migrations missing')
            h.sql(source, "CREATE TABLE sistema_juridico.harness_fixture (id int primary key, value text);"
                         "ALTER TABLE sistema_juridico.harness_fixture OWNER TO sistema_juridico_migrator;"
                         "INSERT INTO sistema_juridico.harness_fixture VALUES (1,'synthetic-only');")
            backup = Path(tmp)/'synthetic.dump'
            with backup.open('wb') as out:
                subprocess.run(['docker', 'exec', source['container'], 'pg_dump', '-Fc', '-n', 'sistema_juridico',
                    '-U', 'harness_owner', '-d', source['database']], stdout=out, check=True)
            key2 = uuid.uuid4().hex[:12]
            clone = {'id': 'fixture-clone', 'stage': 'preparing', 'resourceKey': key2,
                  'container': 'sj-harness-smoke-' + key2, 'volume': 'sj-harness-smoke-' + key2,
                  'database': 'sj_' + key2, 'schema': 'sistema_juridico', 'httpPort': h.available_port(),
                  'worktree': str(h.ROOT), 'snapshot': str(backup), 'snapshotSha256': h.sha(backup)}
            clone['url'] = f'http://localhost:{clone["httpPort"]}'
            if a.generic:
                controller = Path(tmp)/'generic-controller'
                (controller/'harness/adapters').mkdir(parents=True)
                shutil.copy2(original_root/'harness/adapters/commands.py', controller/'harness/adapters/commands.py')
                props = {
                    'spring.profiles.active':'local',
                    'spring.datasource.url':'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}?currentSchema=${SCHEMA}',
                    'spring.datasource.username':'${DB_USER}', 'spring.datasource.password':'${DB_PASSWORD}',
                    'spring.flyway.enabled':False, 'spring.flyway.url':'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}?currentSchema=${SCHEMA}',
                    'spring.flyway.user':'${DB_USER}', 'spring.flyway.password':'${DB_PASSWORD}',
                    'server.address':'127.0.0.1', 'server.port':'${PORT}',
                    'app.public-base-url':'${URL}', 'app.rate-limit-key':secrets.token_hex(32),
                    'logging.level.pe.org.beneficencia.legalcontrol':'INFO'}
                generic = {'adapter':'adapters/commands.py','timeoutSeconds':120,
                    'database':{'provider':'postgres','image':h.config()['postgresImage'],'schema':'sistema_juridico',
                        'migratorRole':'sistema_juridico_migrator','appRole':'sistema_juridico_app',
                        'historySql':'SELECT installed_rank,version,script,coalesce(checksum::text,\'\'),success FROM "${SCHEMA}".flyway_schema_history ORDER BY installed_rank'},
                    'env':{'SPRING_APPLICATION_JSON':json.dumps(props)},
                    'services':[{'name':'web','command':['java','-jar',str(jar)],'cwd':'.',
                        'healthPath':'/actuator/health','healthJson':{'status':'UP'},'artifacts':['target/sistema-juridico.jar']}]}
                (controller/'harness/config.json').write_text(json.dumps(generic))
                h.ROOT = controller
                clone['ports'] = {'web':clone['httpPort']}
            h.create_clone(clone)
            assert h.migration_history(clone) == history
            assert h.sql(clone, 'SELECT value FROM sistema_juridico.harness_fixture') == 'synthetic-only'
            h.sql(clone, "UPDATE sistema_juridico.harness_fixture SET value='changed-in-clone'")
            assert h.sql(source, 'SELECT value FROM sistema_juridico.harness_fixture') == 'synthetic-only'
            h.run(['java', '-jar', str(jar), '--app.command=migrate'], env=h.app_env(clone, True), log=artifacts/'clone-migrate.log')
            clone['migrationAfter'] = h.hashlib.sha256(h.migration_history(clone).encode()).hexdigest()
            h.preview(clone)
            h.preview_valid(clone)
            if a.browser:
                candidates = list(Path.home().glob('.npm/_npx/*/node_modules/playwright/package.json'))
                h.require(candidates, 'No cached Playwright module. Install Playwright MCP first.')
                env = h.clean_env()
                env['PLAYWRIGHT_MODULE_PATH'] = str(candidates[0].parent)
                h.run(['node', str(Path(__file__).with_name('browser_smoke.cjs')), clone['url'], str(artifacts/'login.png')],
                      env=env, log=artifacts/'browser.log')
            print('PASS: snapshot restored; history/data preserved; writes isolated; migrated clone healthy' +
                  ('; Chrome visible login verified.' if a.browser else '.'))
            print('Evidence:', artifacts)
        finally:
            for log in Path(tmp).glob('runs/*/*.log'):
                shutil.copy2(log, artifacts / (log.parent.name + '-' + log.name))
            for s in (clone, source):
                if s:
                    h.stop_preview(s)
                    if s.get('containerId'):
                        h.remove_clone(s)
            h.ROOT = original_root
            print('Disposable test resources removed; no production source was used.')


if __name__ == '__main__':
    main()
