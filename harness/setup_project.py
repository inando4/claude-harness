"""Read-only discovery; setup validation tests migrations on a disposable database."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import unicodedata
import re

MANIFESTS = {'package.json', 'pyproject.toml', 'pom.xml', 'requirements.txt', 'go.mod',
             'Cargo.toml', 'composer.json', 'Gemfile', 'manage.py'}


def ensure_controller(h):
    # Job worktrees contain a copy of the controller, but must not own a setup.
    marker = h.ROOT/'.harness-parent.json'
    h.require(not marker.exists(), 'Ejecuta setup desde la raíz del controlador, no desde un worktree de trabajo.')


def active_jobs(h):
    return [json.loads(p.read_text()) for p in (h.STORE/'runs').glob('*/state.json')
            if json.loads(p.read_text()).get('stage') not in h.DONE]


def signature(h, s=None):
    selected = []
    for repo, tree in h.repository_roots(s).items():
        paths = h.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=tree).split('\0')
        for name in paths:
            if not name or name.startswith(('harness/', '.harness/', '.claude/', 'graphify-out/')):
                continue
            p = h.safe_path(tree, name)
            if p.is_file() and (p.name in MANIFESTS or p.name.endswith('.csproj') or
                               p.name in {'package-lock.json','pnpm-lock.yaml','yarn.lock','uv.lock','poetry.lock'}):
                selected.append(repo + '/' + name + h.sha(p))
    return hashlib.sha256(''.join(sorted(set(selected))).encode()).hexdigest()


def inspect(h):
    ensure_controller(h)
    result = {'root':str(h.ROOT), 'repositories':h.repository_roots(),
              'activeJobs':[s['id'] for s in active_jobs(h)],
              'manifests':[], 'testAndMigrationConfigs':[],
              'configured':not h.config().get('needsConfiguration',False),
              'next':'Claude debe interpretar estos indicios y consultar Graphify en cada repositorio; no se han ejecutado scripts.'}
    for repo, tree in h.repository_roots().items():
        files = h.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=tree).split('\0')
        for name in sorted(set(files)):
            if not name or name.startswith(('harness/','.harness/','.claude/','graphify-out/','.worktrees/')):
                continue
            p = Path(name)
            if p.name in MANIFESTS or p.suffix == '.csproj':
                full=h.safe_path(tree,name)
                item={'repo':repo,'path':name}
                if p.name=='package.json':
                    data=json.loads(full.read_text())
                    item['scripts']=list(data.get('scripts',{}))
                    item['dependencies']=sorted(set(data.get('dependencies',{})) | set(data.get('devDependencies',{})))
                    item['lockfiles']=[v for v in ('package-lock.json','pnpm-lock.yaml','yarn.lock','bun.lock') if (full.parent/v).is_file()]
                result['manifests'].append(item)
            elif (p.name.startswith(('vite.config','vitest.config','playwright.config','jest.config')) or
                  p.name in {'schema.prisma','alembic.ini','pytest.ini'} or 'migrations' in p.parts or 'migration' in p.parts):
                result['testAndMigrationConfigs'].append({'repo':repo,'path':name})
    result['qaReferences']=qa_references(h)
    result['cleanupPolicy']=h.publication_module().settings(h).get('repositories',{}) if h.config().get('publication',{}).get('enabled') else {}
    result['permissions']={'requestedMode':'manual','effectiveSubagentMode':'not_verified',
                           'note':'La política real se confirma en Claude; no se prueban permisos ejecutando comandos destructivos.'}
    return result


def qa_references(h, s=None):
    """Paths only: reusable project helpers, not credentials or duplicated contents."""
    result=[]
    for repo,tree in h.repository_roots(s).items():
        names=h.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=tree).split('\0')
        for name in sorted(set(names)):
            if not name or name.startswith(('harness/','.harness/','.worktrees/','.git/')):continue
            p=Path(name)
            if p.suffix not in {'.ts','.js','.py','.md','.java'}:continue
            if (any(v in p.parts for v in ('e2e','tests','test')) and
                any(v in p.stem.lower() for v in ('helper','ayuda','fixture','preparar','base-de-datos'))) or 'dev-login' in p.stem:
                result.append({'repo':repo,'path':name})
    return result[:40]


def validate(h, args):
    ensure_controller(h)
    with h.lock():
        h.require(not active_jobs(h), 'Hay trabajos abiertos: no reconfigures su controlador.')
        h.validate_config()
        old = json.loads((h.STORE/'setup.json').read_text()) if (h.STORE/'setup.json').exists() else {}
        db_signature = hashlib.sha256(json.dumps(h.database_config(), sort_keys=True).encode()).hexdigest()
        snapshot = old.get('snapshot') if old.get('databaseSha256') == db_signature else None
        if snapshot and not args.snapshot:
            h.require(Path(snapshot['path']).is_file() and h.sha(snapshot['path']) == snapshot['sha256'],
                      'El backup guardado cambió o no existe; indica --snapshot para autorizar el nuevo archivo.')
        if args.snapshot:
            h.require(h.has_database(), 'El proyecto declara que no usa base de datos.')
            h.require(args.safe_snapshot, 'Confirma --safe-snapshot solo después de autorización del usuario.')
            p=Path(args.snapshot).expanduser().resolve()
            h.require(p.is_file(), 'Backup local inexistente.')
            with p.open('rb') as f:h.require(f.read(5)==b'PGDMP','El backup debe ser pg_dump custom (-Fc).')
            h.require(args.test_account, 'Falta la cuenta sintética del snapshot; no incluyas contraseña.')
            snapshot={'path':str(p),'sha256':h.sha(p),'testAccount':args.test_account,'authorizedAt':time.time()}
        if not h.has_database():snapshot=None
        record={'at':time.time(),'configSha256':h.sha(h.ROOT/'harness/config.json'),
                'manifestSha256':signature(h),'snapshot':snapshot,'databaseSha256':db_signature,
                'status':'configured','runtimeValidation':'pending'}
        # Doctor checks installed tools only. It never executes project scripts.
        if h.doctor()!=0:
            if old:
                record['status']='blocked';record['blockedPhase']='tools';h.atomic(h.STORE/'setup.json',record)
            raise h.Blocked('Faltan herramientas o escritorio: setup no queda validado.')
        record['status']='checking_database' if h.has_database() and snapshot else 'needs_snapshot'
        h.atomic(h.STORE/'setup.json',record)
        phase='testPreflight'
        try:
            if h.config().get('publication',{}).get('enabled'):
                phase='publicationPreflight';record[phase]=h.github_module().setup(h)
            phase='testPreflight'
            record['testPreflight']=test_readiness(h)
            phase='databasePreflight'
            record['databasePreflight']=h.preflight_module().database(h, snapshot) if snapshot or not h.has_database() else {'status':'needs_snapshot'}
        except Exception:
            record['status']='blocked'; record[phase]={'status':'failed'};record['blockedPhase']=phase
            h.atomic(h.STORE/'setup.json',record)
            raise
        record['status']='ready' if not h.has_database() or snapshot else 'needs_snapshot'
        h.atomic(h.STORE/'setup.json',record)
        return record


def start(h, args):
    sid = getattr(args, 'session_id', None)
    if sid:
        conversation = h.runtime_module('conversation')
        conversation.session_id(h, sid)
        h.require(len(args.spec) == 1 and not args.launch, 'Una spec por conversación; no combines --session-id y --launch.')
        conversation.ready(h, sid)
        with h.lock('conversation-' + sid):
            previous = conversation.existing(h, sid)
            if previous:
                layout = h.runtime_module('spec_layout')
                paths = [layout.logical(str((h.ROOT / p).resolve().relative_to(h.ROOT))) for p in args.spec]
                h.require([layout.logical(p) for p in previous['specs']] == paths, 'Esta sesión ya tiene otra spec. Abre otra sesión para el siguiente trabajo.')
                print(json.dumps(h.brief(previous), ensure_ascii=False))
                return
            return start_prepared(h, args)
    return start_prepared(h, args)


def start_prepared(h, args):
    ensure_controller(h)
    h.runtime_module('spec_readiness').require_all(h, args.spec)
    path=h.STORE/'setup.json'
    h.require(path.is_file(), 'Ejecuta /harness setup una vez en este proyecto.')
    record=json.loads(path.read_text())
    h.require(record['configSha256']==h.sha(h.ROOT/'harness/config.json') and
              record['manifestSha256']==signature(h), 'Cambió la configuración o los manifests: repite /harness setup.')
    h.require(record['status']=='ready', 'Setup pendiente: falta un snapshot autorizado.')
    current_tests=test_readiness(h)
    h.require(record.get('testPreflight', {}).get('format') == 2,
              'Actualiza la validación estática con setup --refresh-tests (sin trabajos abiertos).')
    h.require(infrastructure(record['testPreflight']) == infrastructure(current_tests),
              'Cambió el runner/configuración E2E: repite /harness setup; cambiar tests no exige setup.')
    if h.config().get('publication',{}).get('enabled'):
        h.require(record.get('publicationPreflight',{}).get('status')=='passed','Falta validar publicación en /harness setup.')
    snapshot=record.get('snapshot')
    if h.has_database():
        probe=record.get('databasePreflight', {})
        h.require(probe.get('status')=='passed', 'Falta comprobar restauración/migraciones: repite /harness setup.')
        h.require(probe.get('docker')==h.preflight_module().docker_identity(h), 'Cambió el contexto/motor Docker: repite /harness setup.')
        h.require(snapshot and Path(snapshot['path']).is_file(), 'El backup autorizado ya no existe; repite setup.')
        h.require(h.sha(snapshot['path'])==snapshot['sha256'], 'El backup cambió; necesita nueva autorización en setup.')
    stem=unicodedata.normalize('NFKD',Path(args.spec[0]).stem).encode('ascii','ignore').decode().lower()
    stem=re.sub('[^a-z0-9]+','-',stem).strip('-')[:28] or 'spec'
    job=args.job or (stem+'-'+time.strftime('%Y%m%d%H%M%S')+'-'+h.uuid.uuid4().hex[:4])
    branch=args.branch or 'feat/'+job
    prepared=argparse.Namespace(job=job,branch=branch,base=args.base,spec=args.spec,
             snapshot=snapshot['path'] if snapshot else None,safe_snapshot=bool(snapshot),schema=None,
             test_account=snapshot['testAccount'] if snapshot else None, session_id=getattr(args,'session_id',None),
             test_preflight={'infrastructure': infrastructure(current_tests), 'manifest': record['manifestSha256']})
    h.prepare(prepared)
    if prepared.session_id:
        print(json.dumps(h.brief(h.load(job)), ensure_ascii=False))
        return
    print('Trabajo preparado. Abre su sesión con: python3 harness/harness.py launch '+job)
    if args.launch:
        h.launch(h.load(job), background=True)


def record_runtime(h, s):
    path = h.STORE/'setup.json'
    if not path.exists(): return
    record = json.loads(path.read_text())
    if record.get('configSha256') != h.sha(h.ROOT/'harness/config.json'): return
    record['runtimeValidation'] = {'job':s['id'],'at':time.time(),'result':'checks_and_preview_passed',
                                  'qa':'pending','humanApproval':'pending'}
    h.atomic(path,record)


def test_readiness(h, s=None):
    """Static preflight only: actual test evidence is required later by verify/QA."""
    h.runtime_module('report_paths').validate(h, s)
    cfg=h.config();suites={}
    for name in ('e2e','apiE2E'):
        item=cfg.get(name)
        if item is None:continue
        h.require(isinstance(item,dict),name+' debe ser un objeto.')
        h.require(type(item.get('enabled',True)) is bool,name+'.enabled debe ser booleano.')
        if item.get('enabled',True) is False:
            suites[name]={'status':'disabled'};continue
        h.require(not cfg.get('pendientes',{}).get(name), 'Hay pendientes de infraestructura en '+name+': resuélvelos en setup antes de iniciar una spec.')
        try:suites[name]=suite_readiness(h,item,name,s)
        except h.Blocked as error:raise h.Blocked(name+': '+str(error)) from error
    if not suites:return {'format':2,'status':'not_configured','note':'El setup asistido debe comprobar el runner del proyecto.'}
    return {'format':2,'status':'static_passed' if any(v['status']=='static_passed' for v in suites.values()) else 'disabled',
            'suites':suites,'runtime':'pending'}


def external_connection_url(text):
    """Ignore exact XML namespace declarations, never an entire helper/domain.

    This remains a conservative static check, not a JS/Python dataflow proof.
    Connection literals elsewhere in the same file are still inspected.
    """
    namespaces = {
        'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
        'http://schemas.openxmlformats.org/package/2006/relationships',
        'http://www.w3.org/2001/XMLSchema',
        'http://www.w3.org/2001/XMLSchema-instance',
        'http://www.w3.org/XML/1998/namespace',
    }
    declaration = r'''(\b(?:ns|namespaces|namespace_map)\s*=\s*\{\s*['"][\w:-]+['"]\s*:\s*)(['"])(https?://[^'"\s]+)\2'''
    text = re.sub(declaration, lambda m: m[1] + m[2] + 'XML_NAMESPACE' + m[2]
                  if m[3] in namespaces else m[0], text)
    return re.search(r"(?:postgres(?:ql)?|https?)://(?!localhost(?=[:/])|127\.0\.0\.1(?=[:/]))[^\s'\"`]+", text)


def suite_readiness(h,e2e,name,s=None):
    tree=h.repository_root(s,e2e.get('repo'))
    cwd=h.safe_path(tree,e2e.get('cwd','.'))
    # Existing API-suite paths include cwd (e2e/pruebas, e2e/test-results/...).
    paths_root=tree if name=='apiE2E' else cwd
    tests=h.safe_path(paths_root,e2e.get('testDir','tests'))
    h.require(tests.is_dir(),'No existe testDir E2E.')
    command=e2e.get('command',[])
    h.require(command and all(isinstance(v,str) for v in command),'Falta comando E2E repetible.')
    report=h.safe_path(paths_root,e2e.get('report',''))
    h.require(report.suffix=='.xml','Declara el informe XML E2E esperado.')
    if e2e.get('runner')=='playwright-test':
        specs=[p for p in tests.rglob('*') if p.is_file() and re.search(r'\.(?:spec|test)\.[cm]?[jt]sx?$',p.name)]
        h.require(specs,'Suite habilitada sin archivos *.spec.* o *.test.*; añade pruebas útiles o deshabilita la suite opcional.')
        for p in specs:h.require(p.resolve().is_relative_to(Path(tree).resolve()),'Test E2E fuera del repo.')
        h.require(any(p.read_text().strip() for p in specs),'La suite habilitada solo contiene archivos de pruebas vacíos.')
        manifest=cwd/'package.json'
        h.require(manifest.is_file(),'Falta package.json del runner E2E.')
        package=json.loads(manifest.read_text())
        deps=package.get('dependencies',{})|package.get('devDependencies',{})
        h.require('@playwright/test' in deps or 'playwright' in deps,'Falta dependencia local de Playwright.')
        if command[:2]==['npm','run']:
            h.require(len(command)>2 and command[2] in package.get('scripts',{}),'Script E2E inexistente.')
        configs=list(cwd.glob('playwright.config.*'))
        h.require(configs,'Falta configuración de Playwright.')
        reporter=' '.join(command)+' '+json.dumps(e2e.get('env',{}))+' '+''.join(p.read_text() for p in configs)
        h.require('junit' in reporter.lower(),'No se ha configurado el reportero JUnit E2E.')
    # Catch common fixed Docker/psql destinations, including fallback defaults.
    configs = list(cwd.glob('playwright.config.*'))
    infra_sources = source_closure(h, tree, configs)
    sources = source_closure(h, tree, [*tests.rglob('*'), *configs])
    checked={}
    for source in sources:
        if not source.is_file() or source.suffix not in {'.js','.ts','.mjs','.cjs','.tsx','.jsx','.mts','.cts','.py','.java','.json'}:continue
        h.require(source.resolve().is_relative_to(Path(tree).resolve()),'Auxiliar E2E fuera del repo.')
        raw=source.read_bytes(); text=raw.decode()
        # Literal arguments are unsafe even when an environment variable overrides another argument.
        fixed_role=re.search(r"['\"]-(?:U|d)['\"]\s*,\s*['\"][A-Za-z_][A-Za-z0-9_-]*['\"]",text)
        fixed_container=re.search(r"['\"]exec['\"]\s*,\s*['\"][A-Za-z_][A-Za-z0-9_-]*['\"]",text)
        fixed_url=external_connection_url(text)
        # Only scan URL literals in connection config/helpers, not arbitrary assertions/links.
        fixed_fallback=re.search(r"(?:PGUSER|PGDATABASE|PGHOST|PGPORT|E2E_PG|DATABASE_URL)\b[^\n;]{0,60}(?:\?\?|\|\|)\s*['\"][^'\"]+['\"]",text)
        connection_file=source.name.startswith('playwright.config') or 'psql' in text or 'PGDATABASE' in text
        if fixed_fallback or ('psql' in text and fixed_role) or ('docker' in text and fixed_container) or (connection_file and fixed_url):
            raise h.Blocked('Destino fijo/externo en auxiliar E2E: '+str(source.relative_to(tree))+
                            '. Parametriza las conexiones con variables del trabajo. Si es un identificador de formato, '+
                            'revisa esa declaración; no edites el controlador/configuración ni sus hashes durante el trabajo: '+
                            'invalidaría la publicación. Registra el defecto para una actualización independiente.')
        checked[str(source.relative_to(tree))] = hashlib.sha256(raw).hexdigest()
        h.require(h.sha(source) == checked[str(source.relative_to(tree))], 'Cambió un auxiliar durante el preflight E2E.')
    return {'status':'static_passed','fingerprint':digest(checked),
            'infrastructure':digest({str(p.relative_to(tree)):h.sha(p) for p in infra_sources}),
            'inputs':checked,'files':len(checked),'runtime':'pending'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def infrastructure(readiness):
    return {name: {key: row.get(key) for key in ('status', 'infrastructure')}
            for name, row in readiness.get('suites', {}).items()}


def source_closure(h, tree, seeds):
    """Include relative local imports outside testDir; do not execute project JS."""
    tree = Path(tree).resolve()
    pending = list(seeds); result = set()
    extensions = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts', '.py', '.java', '.json')
    while pending:
        path = pending.pop()
        if not path.is_file() or path.suffix not in extensions: continue
        path = path.resolve()
        h.require(path.is_relative_to(tree), 'Auxiliar E2E fuera del repo.')
        if path in result: continue
        result.add(path)
        text = path.read_text()
        # Static imports, export-from, require() and dynamic import() literals.
        imports = re.findall(r"(?:from\s*|import\s*(?:\(\s*)?|require\s*\(\s*)['\"]([^'\"]+)['\"]", text)
        for name in imports:
            h.require(not name.startswith(('/', 'file:')), 'Import E2E fuera del repo: '+name)
            h.require(not name.startswith(('@/', '~/')), 'Alias local E2E no resuelto: '+name+'; usa imports relativos verificables.')
            if not name.startswith('.'): continue  # Package dependencies are covered by manifests/lockfiles.
            base = (path.parent/name).resolve()
            h.require(base.is_relative_to(tree), 'Import E2E fuera del repo: '+name)
            candidates = [base, *[Path(str(base)+ext) for ext in extensions],
                          *[base/('index'+ext) for ext in extensions]]
            # TypeScript commonly imports a .js path whose source is .ts.
            if base.suffix in ('.js', '.mjs', '.cjs'):
                candidates += [base.with_suffix('.ts'), base.with_suffix('.tsx')]
            found = next((p for p in candidates if p.is_file()), None)
            h.require(found is not None, 'Import local E2E inexistente: '+name)
            pending.append(found)
    return sorted(result)


def capture_job_preflight(h, s, expected=None):
    """Validate the actual prepared trees before any installs/clone/migrations."""
    manifest = signature(h, s)
    current = test_readiness(h, s)
    if expected:
        h.require(manifest == expected['manifest'], 'Los manifests de la base preparada difieren del setup.')
        h.require(infrastructure(current) == expected['infrastructure'],
                  'La infraestructura E2E de la base preparada difiere del setup.')
    h.require(current == test_readiness(h, s) and manifest == signature(h, s),
              'Las entradas E2E cambiaron durante la preparación; no se crearon servicios ni clon.')
    h.require_job_config(s)
    s['testPreflight'] = {'at':time.time(), 'readiness':current, 'manifest':manifest,
                          'configVersion':s['configVersion'],
                          'bases':{name:item['base'] for name,item in s.get('repositories',{}).items()} or {'main':s['base']}}
    h.save(s, 'job_test_preflight_passed')


def refresh_tests(h):
    """Explicit, static-only format upgrade; never change an open job or DB evidence."""
    ensure_controller(h)
    with h.lock():
        h.require(not active_jobs(h), 'Hay trabajos abiertos: no actualices setup.')
        path = h.STORE/'setup.json'
        h.require(path.is_file(), 'Falta setup; ejecuta /harness setup.')
        record = json.loads(path.read_text())
        h.require(record.get('status') == 'ready', 'Setup no está listo; ejecuta /harness setup.')
        h.require(record['configSha256'] == h.sha(h.ROOT/'harness/config.json') and
                  record['manifestSha256'] == signature(h), 'Cambió configuración/manifests; ejecuta /harness setup.')
        current = test_readiness(h)
        if record.get('testPreflight', {}).get('format') == 2:
            h.require(infrastructure(record['testPreflight']) == infrastructure(current),
                      'Cambió infraestructura del runner; ejecuta /harness setup completo.')
        h.require(current == test_readiness(h), 'Cambió E2E durante la validación estática.')
        record['testPreflight'] = current
        record['testPreflightUpdatedAt'] = time.time()
        h.atomic(path, record)
        return record
