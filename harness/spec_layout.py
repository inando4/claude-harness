"""Physical spec stages, with guarded moves and private rollback copies."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import uuid

STAGES = ('Draft', 'Ready', 'Done')


def logical(value):
    p = Path(value)
    if len(p.parts) > 2 and p.parts[0] == 'specs' and p.parts[1] in STAGES:
        return str(Path('specs', *p.parts[2:]))
    return str(p)


def staged(value, stage):
    p = Path(logical(value))
    if p.parts[0] != 'specs':
        return str(p.parent / 'Done' / p.name) if stage == 'Done' else str(p)
    return str(Path('specs', stage, *p.parts[1:]))


def resolve(h, value):
    relative = os.path.relpath(value, h.ROOT) if Path(value).is_absolute() else value
    regular(h, h.ROOT / relative)
    path = h.safe_path(h.ROOT, relative)
    if path.exists() or Path(relative).parts[:1] != ('specs',):
        return path
    # Old commands remain usable after a controller move. Ambiguity never picks
    # a spec implicitly, and directory placement never supplies authorization.
    values = [logical(relative), *(staged(relative, stage) for stage in STAGES)]
    for name in values: regular(h, h.ROOT / name)
    candidates = [h.safe_path(h.ROOT, name) for name in values]
    matches = list(dict.fromkeys(p for p in candidates if p.is_file()))
    h.require(len(matches) == 1, 'Spec inexistente o ruta ambigua: ' + relative)
    return matches[0]


def unit(relative):
    p = Path(relative)
    return p.parent if (p.parts[:1] == ('specs',) and p.name == 'spec.md'
                        and p.parent not in [Path('specs'), *(Path('specs') / s for s in STAGES)]) else p


def mapped(path, moves):
    for source, target in moves.items():
        if path == source or path.is_relative_to(source):
            return target / path.relative_to(source)
    return path


def links(raw, before, after, moves):
    """Rewrite Markdown destinations, including images/reference definitions.

    Resolve against the old document, then map both ends of a moved bundle.
    Internal image links consequently retain their original spelling.
    """
    text = raw.decode('utf-8')
    def destination(match):
        original = match[2]
        angle = original.startswith('<') and original.endswith('>')
        value = original[1:-1] if angle else original
        url = urlsplit(value)
        if not url.path or url.scheme or url.netloc or value.startswith(('#', '/')):
            return match[0]
        old_target = Path(os.path.normpath(before.parent / unquote(url.path)))
        new_target = mapped(old_target, moves)
        adjusted = os.path.relpath(new_target, after.parent)
        if adjusted == unquote(url.path):
            return match[0]
        if '%' in url.path or (not angle and ' ' in adjusted):
            adjusted = quote(adjusted, safe='/@:+~!$&\'*,;=-._')
        value = urlunsplit(('', '', adjusted, url.query, url.fragment))
        return match[1] + ('<' + value + '>' if angle else value)
    output = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None: fence = token
            elif token[0] == fence[0] and len(token) >= len(fence): fence = None
            output.append(line); continue
        if fence is None:
            line = re.sub(r'(\]\(\s*)(<[^>\n]+>|[^\s)]+)', destination, line)
            line = re.sub(r'^(\s{0,3}\[[^\]\n]+\]:\s*)(<[^>\n]+>|\S+)', destination, line)
        output.append(line)
    return ''.join(output).encode('utf-8')


def regular(h, path):
    h.require(path.is_relative_to(h.ROOT), 'Ruta fuera del controlador.')
    h.require(not any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(h.ROOT)),
              'No se mueven specs o recursos enlazados: ' + str(path))


def active_inputs(h, exclude_job=None):
    return [value for p in (h.STORE / 'runs').glob('*/state.json')
            for job in [json.loads(p.read_text())] if job.get('stage') not in h.DONE and job.get('id') != exclude_job
            for value in job.get('specs', [])]


def plan(h, selections, contents=None, incoming=True, closing_job=None):
    """Preflight the entire batch before any file or approval is changed."""
    contents = contents or {}
    moves = {}
    for relative, stage in selections.items():
        h.require(stage in STAGES, 'Carpeta de specs desconocida.')
        source, target = unit(relative), unit(staged(relative, stage))
        if source == target: continue
        regular(h, h.ROOT / source); regular(h, h.ROOT / target)
        h.require((h.ROOT / source).exists(), 'Spec ausente: ' + str(source))
        h.require(not (h.ROOT / target).exists(), 'Ya existe el destino: ' + str(target))
        h.require(source not in moves or moves[source] == target, 'Estados incompatibles en una misma carpeta.')
        h.require(target not in moves.values() or moves.get(source) == target, 'Dos specs tienen el mismo destino.')
        moves[source] = target
    sources = list(moves)
    h.require(not any(a != b and a.is_relative_to(b) for a in sources for b in sources), 'Carpetas de specs superpuestas.')
    files, members = {}, {}
    for source in moves:
        root = h.ROOT / source
        paths = sorted(root.rglob('*')) if root.is_dir() else [root]
        members[str(source)] = [str(p.relative_to(h.ROOT)) for p in paths]
        for p in paths:
            regular(h, p)
            if p.is_file(): files[p.relative_to(h.ROOT)] = p.read_bytes()
            else: h.require(p.is_dir(), 'Recurso no regular en una spec: ' + str(p))
    candidates = [h.ROOT / name for name in contents]
    if incoming:
        candidates += [*h.ROOT.glob('CLAUDE*.md'), *h.ROOT.glob('README*.md'),
                       *(h.ROOT / 'docs').rglob('*.md'), *(h.ROOT / 'specs').rglob('*.md')]
    for p in candidates:
        regular(h, p)
        if p.is_file(): files.setdefault(p.relative_to(h.ROOT), p.read_bytes())
    changes = {}
    for relative, before in files.items():
        target = mapped(relative, moves)
        raw = contents.get(str(relative), before)
        after = links(raw, relative, target, moves) if relative.suffix == '.md' else raw
        if target != relative or before != after:
            changes[str(relative)] = {'target': str(target), 'before': before, 'after': after,
                                      'mode': (h.ROOT / relative).stat().st_mode & 0o777}
    active = active_inputs(h, exclude_job=closing_job)
    for value in active:
        owned = unit(logical(value))
        h.require(not any(Path(logical(p)) == owned or Path(logical(p)).is_relative_to(owned)
                          for p in changes), 'La spec pertenece a un trabajo abierto: ' + value)
    return {'moves': moves, 'changes': changes, 'members': members}


def replace(path, raw, mode):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode); os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def input_files(h, specs):
    files = set()
    for relative in specs:
        source = h.ROOT / unit(relative)
        for path in source.rglob('*') if source.is_dir() else [source]:
            regular(h, path)
            h.require(path.is_file() or path.is_dir(), 'Recurso no regular: ' + str(path))
            if not path.is_file(): continue
            name = str(path.relative_to(h.ROOT))
            h.require(not any(part in {'.git', '.harness', 'node_modules', '__pycache__'} or part.startswith('.env')
                              for part in path.relative_to(source.parent).parts)
                      and path.suffix.lower() not in {'.dump', '.backup', '.key', '.pem'},
                      'Archivo ajeno a una spec: ' + name)
            files.add(name)
    return sorted(files)


def worktree_context(h, s):
    class Context:
        ROOT = Path(s['worktree'])
        STORE = h.jobdir(s['id']) / 'artifacts/spec-layout'
        def __getattr__(self, name): return getattr(h, name)
    return Context()


def stage_move(h, tree, operation):
    """Move tracked units and update their index without staging other docs."""
    for old, new in operation['moves'].items():
        (tree / new).parent.mkdir(parents=True, exist_ok=True)
        h.publication_module().git(h, tree, 'mv', '--', str(old), str(new))
    for change in operation['changes'].values():
        path = tree / change['target']
        h.require(path.read_bytes() == change['before'], 'Archivo cambió al mover la spec: ' + str(path))
        if change['before'] != change['after']: replace(path, change['after'], change['mode'])
    for target in operation['moves'].values():
        h.publication_module().git(h, tree, 'add', '--update', '--', str(target))


def prepare_worktree(h, s):
    """Reflect an uncommitted source stage move in the isolated worktree.

    The user's Git index stays untouched. Otherwise HEAD could leave a second
    tracked Draft copy beside the freshly approved Ready input.
    """
    ctx = worktree_context(h, s)
    names = h.publication_module().git(h, ctx.ROOT, 'ls-files', '-z', '--', 'specs').stdout.decode().split('\0')
    tracked = []
    for relative in s.get('specs', []):
        if Path(relative).parts[:2] != ('specs', 'Ready'): continue
        candidates = [name for name in names if name and logical(name) == logical(relative)]
        h.require(len(candidates) <= 1, 'Hay varias copias versionadas de la spec: ' + relative)
        if not candidates: continue
        old = candidates[0]
        h.require(Path(old).parts[:2] != ('specs', 'Done'), 'La base ya contiene esta spec terminada: ' + old)
        if old != relative: stage_move(h, ctx.ROOT, plan(ctx, {old: 'Ready'}))
        tracked.append(relative)
    s['specTrackedInputs'] = tracked


def rename(source, target):
    # This controller supports Linux. RENAME_NOREPLACE closes the destination
    # collision race, including another process creating an empty directory.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, 'renameat2'):
        raise OSError(errno.ENOSYS, 'El sistema no admite movimientos sin sobrescritura.')
    if libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno(); raise OSError(code, os.strerror(code), str(target))


def apply(h, operation, records=None):
    records = records or {}
    changes, moves = operation['changes'], operation['moves']
    if not changes and not records: return None
    for relative, change in changes.items():
        regular(h, h.ROOT / relative)
        h.require((h.ROOT / relative).read_bytes() == change['before'], 'Archivo modificado durante la organización: ' + relative)
    for source, expected in operation['members'].items():
        p = h.ROOT / source
        current = [str(v.relative_to(h.ROOT)) for v in sorted(p.rglob('*'))] if p.is_dir() else [source]
        h.require(current == expected, 'Cambió el contenido de la carpeta: ' + source)
    backup = h.STORE / 'spec-moves' / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    receipt_before = {p: p.read_bytes() if p.exists() else None for p in records}
    for relative, change in changes.items():
        p = backup / 'files' / relative; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(change['before']); p.chmod(0o600)
    h.atomic(backup / 'manifest.json', {'moves': {str(k): str(v) for k, v in moves.items()},
             'files': {k: {'target': v['target'], 'beforeSha256': hashlib.sha256(v['before']).hexdigest(),
                           'afterSha256': hashlib.sha256(v['after']).hexdigest()} for k, v in changes.items()}})
    moved, written = [], []
    try:
        for source, target in moves.items():
            regular(h, h.ROOT / target)
            (h.ROOT / target).parent.mkdir(parents=True, exist_ok=True)
            rename(h.ROOT / source, h.ROOT / target); moved.append((source, target))
        for relative, change in changes.items():
            path = h.ROOT / change['target']
            h.require(path.read_bytes() == change['before'], 'Archivo modificado durante el movimiento: ' + relative)
            if change['before'] != change['after']:
                replace(path, change['after'], change['mode']); written.append(relative)
        for path, value in records.items(): h.atomic(path, value)
    except Exception:
        for relative in reversed(written):
            change = changes[relative]; path = h.ROOT / change['target']
            h.require(path.read_bytes() == change['after'], 'Conserva cambios concurrentes y el respaldo: ' + str(backup))
            replace(path, change['before'], change['mode'])
        for source, target in reversed(moved): rename(h.ROOT / target, h.ROOT / source)
        for path, raw in receipt_before.items():
            if raw is None: path.unlink(missing_ok=True)
            else: replace(path, raw, 0o600)
        raise
    return str(backup)
