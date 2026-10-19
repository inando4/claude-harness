"""Complete input specs without publishing local specs or changing tracked main."""
import hashlib
import os
from pathlib import Path
import re


def sha(raw):return hashlib.sha256(raw).hexdigest()


def capture(h,s):
    records={}
    for relative in s.get('specs',[]):
        path=h.safe_path(h.ROOT,relative)
        h.require(not (h.ROOT/relative).is_symlink() and path.suffix.lower()=='.md','Spec no regular Markdown.')
        tracked=h.publication_module().git(h,h.ROOT,'ls-files','--error-unmatch','--',relative,check=False).returncode==0
        tracked=tracked or relative in s.get('specTrackedInputs',[])
        ignored=h.publication_module().git(h,h.ROOT,'check-ignore','-q','--',relative,check=False).returncode==0
        records[relative]={'sourceSha256':h.sha(path),'localIgnored':ignored and not tracked,'tracked':tracked}
        layout=h.runtime_module('spec_layout')
        if layout.unit(relative)!=Path(relative):
            records[relative]['members']={name:h.sha(h.ROOT/name) for name in layout.input_files(h,[relative])}
    s['specStatusInputs']=records


def marked(raw):
    text=raw.decode('utf-8')
    pattern=r'(?im)^\s*(?:\*\*)?Estado(?:\*\*)?\s*:\s*[^\r\n]*'
    if re.search(pattern,text):return re.sub(pattern,'Estado: TERMINADO',text,count=1).encode('utf-8')
    return ('Estado: TERMINADO\n\n'+text).encode('utf-8')


def prepare(h,s):
    """Archive approved final inputs before worktree removal; not marked done yet."""
    if 'specCompletionPlan' in s:return
    result={}
    for relative,record in s.get('specStatusInputs',{}).items():
        p=h.safe_path(Path(s['worktree']),record.get('stagedPath',relative))
        h.require(p.is_file() and not p.is_symlink(),'Spec final ausente/enlazada: '+relative)
        target=h.safe_path(h.jobdir(s['id']),'artifacts/completion-inputs/'+relative)
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700);target.write_bytes(p.read_bytes());target.chmod(0o600)
        result[relative]=dict(record,archive=str(target.relative_to(h.jobdir(s['id']))),sha256=h.sha(target))
        if record.get('members'):
            layout=h.runtime_module('spec_layout');tree=Path(s['worktree'])
            source_unit=layout.unit(record.get('stagedPath',relative));original_unit=layout.unit(relative)
            members={}
            for file in sorted((tree/source_unit).rglob('*')):
                h.require(not file.is_symlink(),'Recurso final enlazado: '+str(file))
                if not file.is_file():continue
                name=str(original_unit/file.relative_to(tree/source_unit))
                archived=h.safe_path(h.jobdir(s['id']),'artifacts/completion-inputs/'+name)
                archived.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                archived.write_bytes(file.read_bytes());archived.chmod(0o600)
                members[name]={'archive':str(archived.relative_to(h.jobdir(s['id']))),'sha256':h.sha(archived)}
            result[relative]['finalMembers']=members
    s['specCompletionPlan']=result;h.save(s,'spec_completion_prepared')


def complete(h,s):
    results=s.setdefault('specCompletion',{})
    for relative,record in s.get('specCompletionPlan',{}).items():
        archive=h.safe_path(h.jobdir(s['id']),record['archive'])
        h.require(h.sha(archive)==record['sha256'],'Archivo final de spec cambió; no sobrescribir.')
        final=marked(archive.read_bytes())
        if not record.get('stagedPath'):final=relocate(final,Path(relative),Path(done_path(relative)))
        dest=h.safe_path(h.jobdir(s['id']),'artifacts/completed-specs/'+done_path(relative))
        dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700);dest.write_bytes(final);dest.chmod(0o600)
        result={'status':'TERMINADO','archive':str(dest),'source':'preserved_tracked_or_unmanaged'}
        if record.get('members'):
            complete_bundle(h,s,relative,record,result)
            results[relative]=result
            continue
        # Ignored inputs are local workflow files; tracked moves travel in the
        # approved PR instead of silently dirtying the user's checkout.
        if record['localIgnored']:
            try:
                source=h.safe_path(h.ROOT,relative);target=h.safe_path(h.ROOT,done_path(relative))
                h.require(not (h.ROOT/relative).is_symlink() and not (h.ROOT/done_path(relative)).is_symlink(),'Spec enlazada')
                expected=record.get('completionSha256')
                if not source.exists() and expected and target.is_file() and h.sha(target)==expected:
                    result.update(source='moved_local',path=str(target))
                else:
                    raw=source.read_bytes()
                    if sha(raw)==record['sourceSha256']:
                        desired=final
                        record['completionSha256']=sha(desired);h.save(s,'spec_completion_intent')
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if target.exists():
                            # Only resume our own already-persisted write, never
                            # overwrite an unrelated Done file even if similar.
                            h.require(expected==sha(desired) and target.read_bytes()==desired,'Ya existe la spec en Done')
                        else:
                            with target.open('xb') as f:f.write(desired)
                            target.chmod(source.stat().st_mode & 0o777)
                        h.require(source.read_bytes()==raw,'Spec modificada durante cierre')
                        source.unlink();result.update(source='moved_local',path=str(target))
                    else:result['source']='preserved_user_changes'
            except (OSError,ValueError,h.Blocked):result['source']='preserved_unavailable_changed_or_collision'
        elif record.get('stagedPath'):
            result.update(source='published_in_pr',path=record['stagedPath'])
            retire(h,s,relative,record,result)
        results[relative]=result
    h.save(s,'specs_completed')


def complete_bundle(h,s,relative,record,result):
    """Move an unchanged ignored bundle together; tracked sources stay in Git."""
    layout=h.runtime_module('spec_layout')
    source_unit=layout.unit(relative);target_unit=layout.unit(done_path(relative))
    moves={source_unit:target_unit};contents={}
    for name,member in record['finalMembers'].items():
        archive=h.safe_path(h.jobdir(s['id']),member['archive'])
        h.require(h.sha(archive)==member['sha256'],'Recurso final de spec modificado: '+name)
        raw=marked(archive.read_bytes()) if name==relative else archive.read_bytes()
        contents[name]=raw
        target=layout.mapped(Path(name),moves)
        if not record.get('stagedPath') and Path(name).suffix=='.md':raw=layout.links(raw,Path(name),target,moves)
        dest=h.safe_path(h.jobdir(s['id']),'artifacts/completed-specs/'+str(target))
        dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700);dest.write_bytes(raw);dest.chmod(0o600)
    if record.get('stagedPath'):
        result.update(source='published_in_pr',path=record['stagedPath']);retire(h,s,relative,record,result);return
    if not record['localIgnored']:return
    try:
        expected=record.get('completionMembers',{})
        if not (h.ROOT/source_unit).exists() and expected:
            current=layout.input_files(h,[done_path(relative)])
            h.require(set(current)==set(expected) and all(h.sha(h.ROOT/p)==v for p,v in expected.items()),'Destino Done cambió')
            result.update(source='moved_local',path=str(h.ROOT/done_path(relative)));return
        names=layout.input_files(h,[relative])
        if set(names)!=set(record['members']) or any(h.sha(h.ROOT/name)!=value for name,value in record['members'].items()):
            result['source']='preserved_user_changes';return
        # A mixed tracked/local directory belongs to Git. New/deleted final
        # resources remain archived for explicit reconciliation rather than
        # silently discarding either version of the bundle.
        h.require(set(contents)==set(names),'Cambió el inventario final de la spec')
        for name in names:
            h.require(h.publication_module().git(h,h.ROOT,'ls-files','--error-unmatch','--',name,check=False).returncode!=0
                      and h.publication_module().git(h,h.ROOT,'check-ignore','-q','--',name,check=False).returncode==0,
                      'Carpeta local con recursos versionados o no ignorados')
        with h.lock():
            operation=layout.plan(h,{relative:'Done'},contents,incoming=False,closing_job=s['id'])
            record['completionMembers']={change['target']:sha(change['after']) for change in operation['changes'].values()}
            h.save(s,'spec_completion_intent')
            layout.apply(h,operation)
        result.update(source='moved_local',path=str(h.ROOT/done_path(relative)))
    except (OSError,ValueError,h.Blocked):result['source']='preserved_unavailable_changed_or_collision'


def retire(h,s,relative,record,result):
    """Drop the unchanged untracked Ready copy once its PR is merged.

    The next pull brings the Done version; a local Done copy would abort that
    pull. Tracked sources, ignored resources and user changes stay in place.
    """
    if s.get('publication',{}).get('phase')!='cleaning':return
    layout=h.runtime_module('spec_layout');git=h.publication_module().git;unit=layout.unit(relative)
    try:
        with h.lock():
            h.require(not any(layout.unit(layout.logical(v))==layout.unit(layout.logical(relative))
                              for v in layout.active_inputs(h,exclude_job=s['id'])),'Spec de un trabajo abierto')
            if not (h.ROOT/unit).exists() and not (h.ROOT/unit).is_symlink():
                result['local']='absent';return
            expected=record.get('members') or {relative:record['sourceSha256']}
            names=layout.input_files(h,[relative])
            if set(names)!=set(expected) or any(h.sha(h.ROOT/name)!=value for name,value in expected.items()):
                result['local']='preserved_user_changes';return
            if any(git(h,h.ROOT,'ls-files','--error-unmatch','--',name,check=False).returncode==0 for name in names):
                result['local']='tracked_pull_required';return
            if any(git(h,h.ROOT,'check-ignore','-q','--',name,check=False).returncode==0 for name in names):
                result['local']='preserved_local_resources';return
            for name in names:(h.ROOT/name).unlink()
            folders=[h.ROOT/unit,*(h.ROOT/unit).rglob('*')] if (h.ROOT/unit).is_dir() else []
            for folder in sorted(folders,key=lambda p:len(p.parts),reverse=True):
                try:folder.rmdir()
                except OSError:pass
        result['local']='removed_after_merge'
    except (OSError,ValueError,h.Blocked):result['local']='preserved_unavailable_changed_or_collision'


def done_path(relative):
    p=Path(relative)
    if not p.parts or p.parts[0]!='specs':return str(p.parent/'Done'/p.name)
    if len(p.parts)>1 and p.parts[1]=='Done':return relative
    rest=p.parts[2:] if len(p.parts)>2 and p.parts[1] in {'Draft','Ready'} else p.parts[1:]
    return str(Path('specs/Done').joinpath(*rest))


def relocate(raw, source, target):
    """Keep relative Markdown destinations valid after moving one directory."""
    def link(m):
        value=m.group(1);angle=value.startswith('<') and value.endswith('>');url=value[1:-1] if angle else value
        if not url or url.startswith(('#','/')) or ':' in url or ' ' in url:return m.group(0)
        path,sep,fragment=url.partition('#')
        adjusted=os.path.relpath(source.parent/path,target.parent)+(sep+fragment if sep else '')
        return ']('+('<'+adjusted+'>' if angle else adjusted)+')'
    return re.sub(r'\]\(([^)]+)\)',link,raw.decode('utf-8')).encode('utf-8')


def stage_tracked(h,s):
    tree=Path(s['worktree'])
    layout=h.runtime_module('spec_layout')
    ctx=layout.worktree_context(h,s)
    for relative,record in s.get('specStatusInputs',{}).items():
        # Versionable inputs, tracked or new, reach Done inside the approved PR.
        if record.get('localIgnored') or record.get('stagedPath'):continue
        target_relative=done_path(relative);source=h.safe_path(tree,relative)
        h.require(source.is_file() and not (tree/relative).is_symlink(),'Spec versionada ausente/enlazada')
        if not record.get('tracked'):h.publication_module().git(h,tree,'add','--',str(layout.unit(relative)))
        operation=layout.plan(ctx,{relative:'Done'},{relative:marked(source.read_bytes())})
        h.require(operation['moves'],'La spec ya está en Done.')
        layout.stage_move(h,tree,operation)
        for item in s.get('publication',{}).get('repos',{}).values():
            if Path(item['worktree'])!=tree:continue
            for key in ('copiedSpecs','finalSpecs'):
                if key in item:item[key]={str(layout.mapped(Path(p),operation['moves'])):v for p,v in item[key].items()}
        record['stagedPath']=target_relative;h.save(s,'spec_staged_done')
