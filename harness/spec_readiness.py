"""Versioned permission to implement a spec, recorded by UserPromptSubmit.

This uses the same local-user/Claude-host trust boundary as delivery approval.
An agent may request or consume consent, but must never invoke the hook or write
its receipts. Readiness authorizes a document version, not a running job.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

DRAFT = 'borrador'
READY = 'lista para implementar'
MAX_BYTES = 2 * 1024 * 1024
STATUS = re.compile(r'(?im)^[ \t]*(?:\*\*)?Estado(?:\*\*)?[ \t]*:[ \t]*([^\r\n]*)')
CONSENT = re.compile(r'LISTA PARA IMPLEMENTAR ([a-f0-9]{8})', re.I)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def header(text):
    """Metadata is before the first section/code block, never a body example."""
    end = re.search(r'(?m)^[ \t]*(?:#{2,}\s|`{3,}|~{3,})', text)
    return text[:end.start()] if end else text


def with_status(text, status):
    matches = list(STATUS.finditer(header(text)))
    if len(matches) > 1:
        raise ValueError('La cabecera de la spec tiene varios campos Estado.')
    if matches:
        match = matches[0]
        return text[:match.start()] + 'Estado: ' + status + text[match.end():]
    title = re.match(r'\A(?:\ufeff)?# [^\r\n]*(?:\r?\n|$)', text)
    if title:
        return text[:title.end()].rstrip('\r\n') + '\n\nEstado: ' + status + '\n' + text[title.end():]
    return 'Estado: ' + status + '\n\n' + text


def document(h, value):
    if Path(value).is_absolute():
        value = os.path.relpath(value, h.ROOT)
    layout = h.runtime_module('spec_layout')
    layout.regular(h, h.ROOT / value)
    path = layout.resolve(h, value)
    layout.regular(h, path)
    h.require(path.is_file() and path.suffix.lower() == '.md',
              'La spec debe ser un archivo Markdown regular dentro del proyecto.')
    h.require(path.stat().st_size <= MAX_BYTES, 'Spec demasiado grande para revisar; máximo 2 MiB.')
    return describe(h, str(path.relative_to(h.ROOT.resolve())), path.read_bytes())


def describe(h, relative, raw):
    """Describe an actual or planned document without changing authorization."""
    path = h.safe_path(h.ROOT, relative)
    text = raw.decode('utf-8-sig')
    matches = list(STATUS.finditer(header(text)))
    h.require(len(matches) <= 1, 'La cabecera de la spec tiene varios campos Estado.')
    declared = matches[0][1].strip().strip('*').strip().casefold() if matches else DRAFT
    return {'path': path, 'relative': relative, 'raw': raw, 'text': text, 'declared': declared,
            'key': digest(h.runtime_module('spec_layout').logical(relative).encode()), 'sha256': digest(raw),
            'bodySha256': digest(with_status(text, DRAFT).encode())}


def record_path(h, key):
    h.require(re.fullmatch(r'[a-f0-9]{64}', key), 'Clave de spec inválida.')
    h.require(not (h.STORE / 'spec-readiness' / key / 'state.json').is_symlink(), 'Recibo de autorización enlazado.')
    path = h.safe_path(h.STORE, 'spec-readiness/' + key + '/state.json')
    return path


def record(h, doc):
    path = record_path(h, doc['key'])
    if not path.exists(): path = record_path(h, digest(doc['relative'].encode()))
    return json.loads(path.read_text()) if path.is_file() else {}


def valid(doc, value):
    approval = value.get('approval', {})
    return (doc['declared'] == READY and value.get('spec') == doc['relative']
            and approval.get('source') == 'UserPromptSubmit'
            and approval.get('documentSha256') == doc['sha256']
            and approval.get('bodySha256') == doc['bodySha256'])


def inspect(h, value):
    doc = document(h, value)
    saved = record(h, doc)
    approved = valid(doc, saved)
    return {'spec': doc['relative'], 'declaredStatus': doc['declared'],
            'status': READY if approved else DRAFT, 'authorized': approved,
            'version': doc['sha256'], 'approval': saved.get('approval') if approved else None}


def require_all(h, values):
    h.require(values, 'Falta una spec.')
    result = {}
    for value in values:
        doc = document(h, value)
        with h.lock('spec-' + doc['key']):
            doc = document(h, value)
            saved = record(h, doc)
            h.require(valid(doc, saved),
                      'Spec en borrador o sin autorización vigente: ' + doc['relative'] +
                      '. Revisa esta versión con /harness ready ' + doc['relative'] +
                      '; el usuario debe confirmar LISTA PARA IMPLEMENTAR. No se preparó el trabajo.')
            result[doc['relative']] = dict(saved['approval'])
    return result


def require_idle(h, doc):
    layout = h.runtime_module('spec_layout')
    owned = layout.unit(layout.logical(doc['relative']))
    for path in (h.STORE / 'runs').glob('*/state.json'):
        job = json.loads(path.read_text())
        h.require(job.get('stage') in h.DONE or not any(
                  Path(layout.logical(value)) == owned or Path(layout.logical(value)).is_relative_to(owned)
                  for value in job.get('specs', [])),
                  'La spec pertenece a un trabajo abierto: ' + job.get('id', path.parent.name) +
                  '. Conserva su autorización y continúa ese trabajo.')


def relocation_records(h, operation, excluded=()):
    """Carry valid receipts across controller-only link/path rewrites.

    User edits never enter this path: valid() first checks the old exact bytes.
    Preserve the real consent and record its previous version for audit.
    """
    updates = {}
    for path in (h.STORE / 'spec-readiness').glob('*/state.json'):
        saved = json.loads(path.read_text())
        relative = saved.get('spec')
        change = operation['changes'].get(relative)
        if not change or relative in excluded: continue
        old = describe(h, relative, change['before'])
        if not valid(old, saved): continue
        new = describe(h, change['target'], change['after'])
        if new['declared'] != READY: continue
        saved.setdefault('relocations', []).append({'from': relative, 'to': new['relative'],
             'beforeSha256': old['sha256'], 'afterSha256': new['sha256'], 'at': time.time()})
        saved['spec'] = new['relative']
        saved['approval'].update(documentSha256=new['sha256'], bodySha256=new['bodySha256'])
        target = record_path(h, new['key'])
        updates[target] = saved
        if target != path: updates[path] = {'spec': relative, 'supersededBy': str(target), 'revokedAt': time.time()}
    return updates


def move(h, doc, stage, status, update=None):
    layout = h.runtime_module('spec_layout')
    operation = layout.plan(h, {doc['relative']: stage},
                            {doc['relative']: with_status(doc['text'], status).encode()})
    change = operation['changes'].get(doc['relative'])
    new = describe(h, change['target'], change['after']) if change else doc
    updates = relocation_records(h, operation, [doc['relative']] if update else [])
    if update:
        updates[record_path(h, new['key'])] = update(new)
        legacy = record_path(h, digest(doc['relative'].encode()))
        if legacy != record_path(h, new['key']) and legacy.exists():
            updates[legacy] = {'spec': doc['relative'], 'supersededBy': new['relative'], 'revokedAt': time.time()}
    layout.apply(h, operation, updates)
    return new


def session(h, sid):
    h.require(isinstance(sid, str) and re.fullmatch(r'[a-f0-9-]{36}', sid)
              and str(h.uuid.UUID(sid)) == sid, 'Falta el ID real de la sesión de Claude.')
    return sid


def request(h, value, sid):
    sid = session(h, sid)
    doc = document(h, value)
    with h.lock(), h.lock('spec-' + doc['key']):
        doc = document(h, value)
        require_idle(h, doc)
        saved = record(h, doc)
        if valid(doc, saved):
            return {**inspect(h, value), 'reply': None, 'next': 'Esperar una orden de implementación; no iniciar automáticamente.'}
        h.require(doc['declared'] in {DRAFT, READY}, 'Estado de spec no revisable: ' + doc['declared'])
        doc = move(h, doc, 'Draft', DRAFT)
        prior = saved.get('request', {})
        if (saved.get('spec') != doc['relative'] or saved.get('approval') or prior.get('sessionId') != sid or prior.get('bodySha256') != doc['bodySha256']
                or prior.get('documentSha256') != doc['sha256']):
            saved = {'spec': doc['relative'], 'request': {
                'sessionId': sid, 'bodySha256': doc['bodySha256'], 'documentSha256': doc['sha256'],
                'token': secrets.token_hex(4), 'at': time.time()}}
            h.atomic(record_path(h, doc['key']), saved)
        return {'spec': doc['relative'], 'status': DRAFT, 'version': doc['sha256'],
                'reply': 'LISTA PARA IMPLEMENTAR ' + saved['request']['token'],
                'scope': 'Habilitar únicamente esta versión de la spec; no inicia implementación ni aprueba la entrega final.'}


def consent(h, event, match):
    sid = session(h, event.get('session_id'))
    token = match[1].lower()
    candidates = []
    for path in (h.STORE / 'spec-readiness').glob('*/state.json'):
        saved = json.loads(path.read_text())
        req = saved.get('request', {})
        if req.get('sessionId') == sid and req.get('token') == token:
            candidates.append(saved)
    h.require(len(candidates) == 1, 'Confirmación de spec vencida o de otra conversación; solicita la revisión vigente.')
    value = candidates[0]['spec']
    doc = document(h, value)
    with h.lock(), h.lock('spec-' + doc['key']):
        doc = document(h, value)
        require_idle(h, doc)
        saved = record(h, doc)
        req = saved.get('request', {})
        h.require(req.get('sessionId') == sid and req.get('token') == token
                  and req.get('documentSha256') == doc['sha256']
                  and req.get('bodySha256') == doc['bodySha256'],
                  'La spec cambió desde la revisión; vuelve a mostrarla antes de pedir autorización.')
        saved['consent'] = {'source': 'UserPromptSubmit', 'sessionId': sid, 'token': token,
                            'bodySha256': doc['bodySha256'], 'at': time.time()}
        h.atomic(record_path(h, doc['key']), saved)
    return ('El usuario autorizó marcar ' + value +
            ' como lista para implementar. Ejecuta spec-accept con esa ruta y esta sesión. '
            'No inicies un trabajo: esta confirmación solo habilita la spec.')


def accept(h, value, sid):
    sid = session(h, sid)
    doc = document(h, value)
    with h.lock(), h.lock('spec-' + doc['key']):
        doc = document(h, value)
        require_idle(h, doc)
        saved = record(h, doc)
        if valid(doc, saved):
            h.require(saved['approval']['sessionId'] == sid, 'La autorización pertenece a otra sesión.')
            return inspect(h, value)
        req, consent = saved.get('request', {}), saved.get('consent', {})
        h.require(consent.get('source') == 'UserPromptSubmit'
                  and consent.get('sessionId') == req.get('sessionId') == sid
                  and consent.get('token') == req.get('token') and req.get('token')
                  and consent.get('bodySha256') == req.get('bodySha256') == doc['bodySha256']
                  and consent.get('at', 0) >= req.get('at', float('inf')),
                  'Falta autorización explícita vigente del usuario para esta versión de la spec.')
        h.require(doc['sha256'] == req.get('documentSha256'), 'La spec cambió después de autorizarla; repite la revisión.')
        def approve(approved):
            saved['spec'] = approved['relative']
            saved['approval'] = {'source': 'UserPromptSubmit', 'sessionId': sid, 'at': consent['at'],
                                 'bodySha256': approved['bodySha256'], 'documentSha256': approved['sha256'],
                                 'reviewedSpec': doc['relative'], 'reviewedSha256': doc['sha256']}
            return saved
        approved = move(h, doc, 'Ready', READY, approve)
    return inspect(h, approved['relative'])


def draft(h, value):
    doc = document(h, value)
    with h.lock(), h.lock('spec-' + doc['key']):
        doc = document(h, value)
        require_idle(h, doc)
        doc = move(h, doc, 'Draft', DRAFT,
                   lambda new: {'spec': new['relative'], 'revokedAt': time.time()})
    return inspect(h, doc['relative'])


def organize(h, apply=False):
    """Explicit legacy migration; install alone never reclassifies specs."""
    layout = h.runtime_module('spec_layout')
    with h.lock():
        h.require(not layout.active_inputs(h), 'Hay trabajos activos; organización aplazada.')
        roots = [h.ROOT / 'specs', *(h.ROOT / 'specs' / name for name in ('Draft', 'Ready'))]
        candidates = {p for root in roots for p in root.glob('*.md')
                      if p.name.lower() not in {'readme.md', 'plantilla-spec.md'}}
        candidates.update(p for p in (h.ROOT / 'specs').rglob('spec.md')
                          if 'Done' not in p.relative_to(h.ROOT / 'specs').parts)
        selections, contents, results = {}, {}, []
        for path in sorted(candidates):
            doc = document(h, str(path))
            approved = valid(doc, record(h, doc))
            stage = 'Done' if doc['declared'] == 'terminado' else 'Ready' if approved else 'Draft'
            selections[doc['relative']] = stage
            if stage != 'Done': contents[doc['relative']] = with_status(doc['text'], READY if approved else DRAFT).encode()
            results.append({'from': doc['relative'], 'to': layout.staged(doc['relative'], stage),
                            'authorized': approved, 'stage': stage})
        operation = layout.plan(h, selections, contents)
        backup = layout.apply(h, operation, relocation_records(h, operation)) if apply else None
        return {'applied': apply, 'specs': results, 'changedFiles': len(operation['changes']), 'backup': backup}


def verify_copy(h, job):
    """Refuse a source edit racing worktree creation before provisioning runtime."""
    for relative, approval in job.get('specReadiness', {}).items():
        path = h.safe_path(Path(job['worktree']), relative)
        h.require(path.is_file() and h.sha(path) == approval['documentSha256'],
                  'La copia de la spec no coincide con la versión autorizada: ' + relative)
