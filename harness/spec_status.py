"""Complete input specs without publishing local specs or changing tracked main."""
import hashlib
import json
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
        ignored=h.publication_module().git(h,h.ROOT,'check-ignore','-q','--',relative,check=False).returncode==0
        records[relative]={'sourceSha256':h.sha(path),'localIgnored':ignored and not tracked,'tracked':tracked}
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
        results[relative]=result
    h.save(s,'specs_completed')


def done_path(relative):
    p=Path(relative)
    if not p.parts or p.parts[0]!='specs':return str(p.parent/'Done'/p.name)
    if len(p.parts)>1 and p.parts[1]=='Done':return relative
    return str(Path('specs/Done').joinpath(*p.parts[1:]))


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
    for relative,record in s.get('specStatusInputs',{}).items():
        if not record.get('tracked') or record.get('stagedPath'):continue
        target_relative=done_path(relative);source=h.safe_path(tree,relative);target=h.safe_path(tree,target_relative)
        h.require(source.is_file() and not (tree/relative).is_symlink(),'Spec versionada ausente/enlazada')
        h.require(source!=target and not target.exists(),'Destino Done ya existe; resuelve la spec antes de publicar.')
        target.parent.mkdir(parents=True,exist_ok=True)
        raw=relocate(marked(source.read_bytes()),Path(relative),Path(target_relative))
        h.publication_module().git(h,tree,'mv','--',relative,target_relative)
        target.write_bytes(raw)
        h.publication_module().git(h,tree,'add','--update','--',target_relative)
        record['stagedPath']=target_relative;h.save(s,'spec_staged_done')
        # Update explicit Markdown links in existing documentation only.
        candidates=[*tree.glob('CLAUDE*.md'),*(tree/'docs').rglob('*.md'),*(tree/'specs').rglob('*.md')]
        for file in candidates:
            if file.is_symlink() or not file.is_file():continue
            def replace(m):
                url=m.group(1)
                if not url or url.startswith(('#','/','<')) or ':' in url or ' ' in url:return m.group(0)
                path,sep,anchor=url.partition('#')
                if (file.parent/path).resolve()!=source.resolve():return m.group(0)
                return ']('+os.path.relpath(target,file.parent)+(sep+anchor if sep else '')+')'
            raw=file.read_text();new=re.sub(r'\]\(([^)]+)\)',replace,raw)
            if new!=raw:file.write_text(new)
        h.publication_module().git(h,tree,'add','--update','--',target_relative)
