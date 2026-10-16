"""Named Git repositories, coordinated worktrees and combined review identity."""
from pathlib import Path
import hashlib
import json
import re
import shutil


def sources(h):
    cfg=h.config()
    entries=cfg.get('repositories')
    if entries is None:return {'main':{'path':str(h.ROOT),'base':'HEAD'}}
    h.require(isinstance(entries,dict) and entries,'repositories debe ser un objeto no vacío.')
    result={}
    common=set()
    for name,item in entries.items():
        h.require(re.fullmatch(r'[a-z][a-z0-9_]*',name),'Nombre de repositorio inválido.')
        path=(h.ROOT/Path(item['path']).expanduser()).resolve()
        h.require(path.is_dir(),'Repositorio inexistente: '+str(path))
        top=h.run(['git','rev-parse','--show-toplevel'],cwd=path)
        h.require(Path(top).resolve()==path,'Usa la raíz Git: '+str(path))
        directory=h.run(['git','rev-parse','--git-common-dir'],cwd=path)
        identity=str((path/directory).resolve())
        h.require(identity not in common,'Dos entradas apuntan al mismo repositorio Git.')
        common.add(identity)
        result[name]=dict(item,path=str(path))
    primary=cfg.get('primaryRepository')
    h.require(primary in result,'Falta primaryRepository válido.')
    h.require(Path(result[primary]['path'])==h.ROOT.resolve(),'El controlador debe estar en la raíz del repositorio principal.')
    return result


def primary(h):return h.config().get('primaryRepository','main')


def roots(h,s=None):
    if s is not None:return s.get('worktrees',{primary(h):s['worktree']})
    return {name:item['path'] for name,item in sources(h).items()}


def root(h,s=None,name=None):
    values=roots(h,s)
    name=name or primary(h)
    h.require(name in values,'Repositorio no declarado: '+name)
    return Path(values[name])


def plan(h,branch,base):
    planned={}
    for name,item in sources(h).items():
        ref=base if name==primary(h) else item.get('base','HEAD')
        commit=h.run(['git','rev-parse','--verify',ref+'^{commit}'],cwd=item['path'])
        exists=h.run(['git','branch','--list',branch],cwd=item['path'])
        h.require(not exists,'La rama ya existe en '+name+': '+branch)
        supplemental(h, item)  # preflight before creating any worktree
        planned[name]={'source':item['path'],'base':commit,'branch':branch}
    return planned


def supplemental(h,item):
    result=[]
    for relative in item.get('copyPaths',[]):
        source=h.safe_path(item['path'],relative)
        h.require(source.is_dir() or source.is_file(),'copyPaths inexistente: '+relative)
        names=h.run(['git','ls-files','-z','--cached','--others','--exclude-standard','--',relative],cwd=item['path']).split('\0')
        for name in names:
            if not name:continue
            raw=Path(item['path'])/name
            rel=Path(name)
            h.require(not raw.is_symlink(),'copyPaths no admite enlaces.')
            path=h.safe_path(item['path'],name)
            h.require(not any(v.startswith('.env') or v in {'node_modules','.git','.harness','__pycache__'} for v in rel.parts)
                      and path.suffix not in {'.pem','.key','.dump','.backup'},'Archivo sensible/no permitido en copyPaths.')
            if path.is_file():result.append(path)
    return result


def create(h,s):
    for name,item in s['repositories'].items():
        tree=Path(s['worktrees'][name]);tree.parent.mkdir(parents=True,exist_ok=True)
        h.run(['git','-c','core.hooksPath=/dev/null','worktree','add','-b',item['branch'],str(tree),item['base']],cwd=item['source'])
        item['created']=True;h.save(s,'worktree_created_'+name)
        for path in supplemental(h, sources(h)[name]):
            rel=path.relative_to(item['source'])
            dest=h.safe_path(tree,str(rel));dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
        for relative in ('AGENTS.md','CLAUDE.md','CLAUDE.MD'):
            source=Path(item['source'])/relative
            if source.is_file() and not source.is_symlink():shutil.copy2(source,tree/relative)
        h.atomic(tree/'.harness-parent.json',{'controller':str(h.ROOT),'job':s['id'],'repository':name})
        with (tree/'.gitignore').open('a') as f:f.write('\n.harness-parent.json\n.harness/\n')


def fingerprint(h,s,product_only=False):
    values={name:{'base':s.get('repositories',{}).get(name,{}).get('base'),
                  'files':h.fingerprint(tree,product_only)} for name,tree in roots(h,s).items()}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def inventories(h,s):return {name:h.inventory(tree) for name,tree in roots(h,s).items()}
