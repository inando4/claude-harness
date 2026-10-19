"""Per-job verification receipts. Unknown inputs default to the whole repository."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate(h):
    cfg = h.config(); seen = set(); owners = []
    for phase in ('setup', 'checks', 'build'):
        for step in cfg.get(phase, []):
            if 'id' not in step:
                h.require(not step.get('cache') and not step.get('dependsOn'), 'cache/dependsOn requiere id estable.')
                continue
            name = step['id']
            h.require(isinstance(name,str) and re.fullmatch(r'[a-z][a-z0-9_-]*',name) and name not in seen, 'ID de comprobación inválido o repetido.')
            h.require(isinstance(step.get('cache', False), bool), 'cache debe ser booleano.')
            h.require(set(step.get('dependsOn',[])) <= seen, 'dependsOn debe referir a grupos anteriores; no ciclos.')
            scopes=step.get('inputs', [{'repo':step.get('repo')}])
            roots=[str(h.repository_root(None,v.get('repo'))) for v in scopes]
            h.require(len(roots)==len(set(roots)), 'Un repositorio no puede repetirse en inputs.')
            for scope in scopes:
                tree=h.repository_root(None,scope.get('repo'))
                for pattern in scope.get('exclude',[]):h.safe_path(tree,pattern)
                h.require('include' not in scope, 'Se incluye todo salvo exclusiones explícitas para cubrir archivos desconocidos.')
            h.require(step.get('inputs', [1]), 'inputs no puede estar vacío.')
            for path in step.get('outputs',[])+step.get('outputExcludes',[]):h.safe_path(h.repository_root(None,step.get('repo')),path)
            for tool in step.get('tools',[]):
                h.require(isinstance(tool,str) and tool and not tool.startswith('-'), 'Herramienta inválida.')
            if phase=='checks':owners.extend(step.get('reports',[]))
            seen.add(name)
    if any(step.get('cache') for step in cfg['checks']):
        h.require(len(owners)==len(set(owners)) and set(owners)==set(cfg['testReports']),
                  'Cada informe debe pertenecer exactamente a un check mediante reports.')


def files(h, tree, excludes=()):
    tree=Path(tree)
    names=h.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=tree).split('\0')
    result={}
    for name in sorted(set(names)):
        if not name or name.startswith(('harness/','.claude/','.harness/','graphify-out/','docs/','specs/')) or name in {'CLAUDE.md','CLAUDE.MD'}:continue
        if any(fnmatch.fnmatchcase(name,p) for p in excludes):continue
        p=tree/name
        if p.is_symlink():
            # Include target content too when internal; external links cannot be cached.
            h.require(p.resolve().is_relative_to(tree.resolve()), 'Entrada enlazada fuera del repo; no se reutiliza.')
            value=['link',os.readlink(p),h.sha(p) if p.is_file() else None]
        elif p.is_file():value=['file',h.sha(p),p.stat().st_mode & 0o777]
        else:value=['missing']
        result[name]=value
    return result


def inputs(h,s,step):
    scopes=step.get('inputs') or [{'repo':step.get('repo')}]
    return {str(h.repository_root(s,v.get('repo'))): files(h,h.repository_root(s,v.get('repo')),v.get('exclude',[])) for v in scopes}


def tool_versions(h,s,step):
    values={}
    names=step.get('tools') or h.config().get('requiredTools',[])
    for name in dict.fromkeys([step['command'][0],*names]):
        if Path(name).is_absolute():
            path=Path(name)
        elif '/' in name:
            path=h.safe_path(h.repository_root(s,step.get('repo')), str(Path(step.get('cwd','.'))/name))
        else:path=Path(shutil.which(name) or name)
        if not path.is_file():raise h.InfrastructureBlocked('Herramienta ausente: '+name)
        # Version probe once per attempt; executable hash detects replacements.
        cache_key=str(path.resolve())+h.sha(path)
        cache=h._tool_versions
        if cache_key not in cache:
            cache[cache_key]=h.run([str(path.resolve()),'--version'],env=h.clean_env(),timeout=20)
        values[name]=[str(path.resolve()),cache_key,cache[cache_key]]
        if Path(name).name=='docker':
            if 'docker-context' not in cache:cache['docker-context']=h.preflight_module().docker_identity(h)
            values[name].append(cache['docker-context'])
    return values


def evidence(h,s,step,reports):
    result={}
    for name,pattern in reports.items():
        for p in h.adapter_call('matching',s,pattern):result[str(p)]=h.sha(p)
        h.require(h.adapter_call('matching',s,pattern),'Falta informe: '+name)
    for name in step.get('outputs',[]):
        root=h.repository_root(s,step.get('repo'));p=h.safe_path(root,name)
        h.require(p.exists(),'Falta artefacto: '+name)
        result[str(p)]=h.artifact_version(root,[name],step.get('outputExcludes',[]))
    return result


def components(h,s,step):
    dependency={name:s.get('receipts',{}).get(name) for name in step.get('dependsOn',[])}
    h.require(all(dependency.values()), 'Falta resultado de una dependencia.')
    db=None
    if h.has_database() and step.get('usesDatabase',True):
        db={k:s.get(k) for k in ('containerId','dbPort','database','schema','snapshotSha256')}
        db['config']=h.database_config()
    values = {'job':s['id'],'inputs':inputs(h,s,step),'step':step,'dependencies':dependency,
                   'specs':{p:h.runtime_module('spec_status').fingerprint(h,s,p) for p in s.get('specs',[])},
                   'tools':tool_versions(h,s,step),'env':h.config().get('env',{}),'processEnv':h.clean_env(),
                   'db':db,'ports':s.get('ports'),'executor':h.controller_version()}
    return {name: digest(value) for name, value in values.items()}


def key(h,s,step):
    return digest(components(h,s,step))


def group(h,s,step,identifier,reports,execute,phase=None):
    cached=bool(step.get('cache'))
    # Live database checks must always execute; Testcontainers-only suites declare usesDatabase:false.
    eligible=cached and (not h.has_database() or step.get('usesDatabase',True) is False)
    before=key(h,s,step) if eligible else None
    receipts=s.setdefault('receipts',{})
    old=receipts.get(identifier)
    reason='sin resultado vigente'
    if s.get('forceVerification'):reason='forzado'
    elif not eligible:reason='comprobación no reutilizable'
    elif old and old.get('key')==before:
        try:
            valid=evidence(h,s,step,reports)==old['evidence']
            valid=valid and Path(old['log']).is_relative_to(h.jobdir(s['id'])) and h.sha(old['log'])==old['logSha256']
            valid=valid and old.get('status')=='passed' and archives_valid(h,s,old)
        except (OSError,KeyError,h.Blocked):valid=False
        if valid:
            h.step_event(s,identifier,'reused','entradas y evidencia vigentes',0)
            return old.get('summary',{})
        reason='evidencia ausente o modificada'
    elif old:reason='cambiaron entradas, herramientas o dependencias'
    receipts.pop(identifier,None)
    h.step_event(s,identifier,'running',reason,0)
    started=time.monotonic()
    for pattern in reports.values():
        for p in h.adapter_call('matching',s,pattern):p.unlink()
    try:
        log=execute()
        summary=h.adapter_call('read_reports',s,reports)
        outputs=evidence(h,s,step,reports)
        if eligible:h.require(key(h,s,step)==before,'Cambiaron entradas durante '+identifier)
        # Immutable receipt: no timestamps change when reused, preserving dependent keys.
        receipts[identifier]={'key':before,'status':'passed','evidence':outputs,'summary':summary,
                              'log':str(log),'logSha256':h.sha(log),'attempt':s.get('attempt'),
                              'definition':digest(step),'archives':archive_reports(h,s,identifier,reports)}
        if eligible: receipts[identifier]['components'] = components(h,s,step)
        h.step_event(s,identifier,'passed',reason,time.monotonic()-started)
        return summary
    except BaseException as error:
        receipts.pop(identifier,None)
        h.step_event(s,identifier,'failed',reason,time.monotonic()-started)
        if phase=='setup' and isinstance(error,Exception):
            raise h.InfrastructureBlocked('Falló preparación de dependencias: '+identifier) from error
        raise


def archive_reports(h,s,identifier,reports):
    result={}
    for pattern in reports.values():
        for source in h.adapter_call('matching',s,pattern):
            dest=h.attempt_path(s,'evidence')/identifier/(digest(str(source))+'.xml')
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest)
            result[str(dest)]=h.sha(dest)
    return result


def archives_valid(h,s,receipt):
    return all(Path(p).resolve().is_relative_to(h.jobdir(s['id']).resolve()) and
               Path(p).is_file() and h.sha(p)==v for p,v in receipt.get('archives',{}).items())


def validate_receipts(h,s):
    """Gate QA/review on the evidence actually produced for every reusable group."""
    for phase in ('setup','checks','build'):
        for step in h.config().get(phase,[]):
            if not step.get('cache') or (h.has_database() and step.get('usesDatabase',True) is not False):continue
            old=s.get('receipts',{}).get(step['id'],{})
            reports={name:h.config()['testReports'][name] for name in step.get('reports',[])}
            reason = 'evidencia, informe o log ausente/modificado'
            try:
                now = components(h,s,step)
                changed = [k for k in now if now[k] != old.get('components',{}).get(k)]
                if old.get('key') != digest(now): reason = 'cambiaron: ' + ', '.join(changed)
                valid=(old.get('status')=='passed' and old.get('key')==digest(now) and
                       evidence(h,s,step,reports)==old['evidence'] and archives_valid(h,s,old) and
                       Path(old['log']).resolve().is_relative_to(h.jobdir(s['id']).resolve()) and
                       h.sha(old['log'])==old['logSha256'])
            except (OSError,KeyError,h.Blocked):valid=False
            h.require(valid,'Resultado invalidado: '+step['id']+' ('+reason+'). Revisa la causa antes de recheck/verify.')
