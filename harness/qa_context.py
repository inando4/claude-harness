"""Small, per-review handoffs. Changed paths guide QA; they never certify reuse."""
import json


def capture(h, s):
    path = h.jobdir(s['id'])/'qa-baseline.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    current = {name:h.verification_module().files(h, tree)
               for name,tree in h.repository_roots(s).items()}
    changes = {}
    for name in set(current) | set(previous.get('files', {})):
        before, after = previous.get('files', {}).get(name, {}), current.get(name, {})
        changes[name] = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    objects=snapshots(h,s,current)
    delta=write_delta(h,s,previous.get('objects',{}),objects,s['qaVersion'])
    runtime = {k:s.get(k) for k in ('containerId','snapshotSha256','migrationAfter','ports')}
    runtime['artifacts'] = {name:row.get('artifactsVersion') for name,row in s.get('previews', {}).items()}
    context = {'cycle':s['cycles'], 'version':s['qaVersion'], 'previousVersion':previous.get('version'),
               'mode':'delta' if previous else 'initial', 'changedFiles':changes,
               'testDelta':delta, 'checkpoint':checkpoint_brief(h,s),
               'runtimeChanged':not previous or previous.get('runtime') != runtime,
               'liveEvidenceRequired':True,
               'note':'Las rutas orientan el alcance; no validan datos vivos ni autorizan reutilizar evidencias.'}
    artifact = h.jobdir(s['id'])/'artifacts'/('qa-context-'+s['qaVersion']+'.json')
    h.atomic(artifact, context)
    h.atomic(path, {'version':s['qaVersion'], 'files':current, 'objects':objects, 'runtime':runtime})
    s['qaContext'] = {k:v for k,v in context.items() if k != 'changedFiles'}
    s['qaContext']['changedFileCount'] = sum(map(len, changes.values()))
    s['qaContext']['path'] = str(artifact)
    s['qaContext']['changedFiles'] = {name:paths[:30] for name,paths in changes.items()}


def test_source(path):
    from pathlib import Path
    p=Path(path)
    return (p.suffix in {'.ts','.tsx','.js','.jsx','.py','.java','.kt','.json'} and
            (any(v.lower() in {'tests','test','e2e','__tests__','pruebas'} for v in p.parts) or
             '.test.' in p.name or '.spec.' in p.name or p.name.startswith('test_') or p.stem.endswith(('Test','Tests','IT'))))


def snapshots(h, s, current):
    """Private content-addressed test snapshots, including untracked files."""
    result={};root=h.jobdir(s['id'])/'qa-test-objects';root.mkdir(exist_ok=True,mode=0o700)
    for name,files in current.items():
        result[name]={};tree=h.repository_root(s,name)
        for relative,entry in files.items():
            p=tree/relative
            if not test_source(relative) or entry[0]!='file' or p.is_symlink() or p.stat().st_size>262144:continue
            raw=p.read_bytes()
            try:raw.decode('utf-8')
            except UnicodeDecodeError:continue
            import hashlib
            sha=hashlib.sha256(raw).hexdigest()
            h.require(sha==entry[1],'Cambió test durante checkpoint de QA: '+relative)
            obj=root/sha
            if not obj.exists():obj.write_bytes(raw);obj.chmod(0o600)
            result[name][relative]=sha
    return result


def write_delta(h,s,before,after,label):
    import difflib
    root=h.jobdir(s['id'])/'qa-test-objects';parts=[]
    for repo in sorted(set(before)|set(after)):
        for path in sorted(set(before.get(repo,{}))|set(after.get(repo,{}))):
            a=before.get(repo,{}).get(path);b=after.get(repo,{}).get(path)
            if a==b:continue
            old=(root/a).read_text() if a else '';new=(root/b).read_text() if b else ''
            parts.extend(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile=repo+'/'+path,tofile=repo+'/'+path))
    target=h.jobdir(s['id'])/'artifacts'/('qa-test-delta-'+label+'.patch')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    target.write_text(''.join(parts));target.chmod(0o600)
    return str(target)


def checkpoint(h,s,report):
    from pathlib import Path
    h.require(s['stage']=='qa','Checkpoint solo durante QA.')
    path=Path(report).resolve();artifacts=(h.jobdir(s['id'])/'artifacts').resolve()
    h.require(path.is_relative_to(artifacts) and path.is_file(),'Reporte de avance fuera de artifacts.')
    value=json.loads(path.read_text())
    h.require(value.get('version')==s['qaVersion'],'Reporte de otra versión.')
    current={n:h.verification_module().files(h,t) for n,t in h.repository_roots(s).items()}
    objects=snapshots(h,s,current)
    target=artifacts/('qa-progress-'+s['qaVersion']+'.json');h.atomic(target,value)
    receipt={'version':s['qaVersion'],'report':str(target),'objects':objects,'note':'Avance parcial: no acredita pase ni vigencia del clon.'}
    h.atomic(h.jobdir(s['id'])/'qa-checkpoint.json',receipt)
    return checkpoint_brief(h,s)


def checkpoint_brief(h,s):
    path=h.jobdir(s['id'])/'qa-checkpoint.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    return {k:value.get(k) for k in ('version','report','note')}
