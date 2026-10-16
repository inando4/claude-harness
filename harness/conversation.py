"""Same-session coordination and explicit user approval via Claude's prompt hook.

The hook records consent; accept-review validates all existing evidence before
transitioning. No model call, test execution, or expensive hashing in the hook.
Like terminal approval, this trusts the local user/Claude host, not a hostile
process with write access to the controller. Agents must never invoke this hook.
"""
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time


def session_id(h, value):
    h.require(isinstance(value, str) and re.fullmatch(r'[a-f0-9-]{36}', value)
              and str(h.uuid.UUID(value)) == value, 'Falta el ID real de la sesión de Claude; reabre Claude Code y /harness.')
    return value


def existing(h, sid):
    rows = [json.loads(p.read_text()) for p in (h.STORE/'runs').glob('*/state.json')]
    matches = [s for s in rows if s.get('conversation', {}).get('sessionId') == sid]
    h.require(len(matches) <= 1, 'Más de un trabajo asociado a esta conversación.')
    return matches[0] if matches else None


def ready(h, sid):
    path = h.STORE/'conversations'/sid/'hook.json'
    h.require(path.is_file() and json.loads(path.read_text()).get('sessionId') == sid,
              'Falta el hook de esta sesión. Abre una sesión nueva de Claude en el controlador y repite /harness; no hace falta repetir setup.')


def request(h, s, sid=None):
    if sid is not None:
        h.require(session_id(h, sid) == s.get('conversation', {}).get('sessionId'),
                  mismatch(s))
    h.require(s.get('conversation') and s['stage'] == 'awaiting_human_approval', 'No hay revisión conversacional pendiente.')
    # Validate now, and once more when consuming actual consent.
    with h.job_environment(s):
        h.require(s.get('approvedCandidate') == h.version(s), 'Cambió la versión; ejecuta recheck/QA.')
        h.runtime_module('synchronization').guard(h,s)
    h.preview_valid(s)
    prior = s.get('reviewRequest', {})
    if prior.get('version') != s['approvedCandidate']:
        s['reviewRequest'] = {'version': s['approvedCandidate'], 'token': secrets.token_hex(4), 'at': time.time()}
        s.pop('userConsent', None)
        h.save(s, 'chat_review_requested')
    return {'job': s['id'], 'version': s['approvedCandidate'], 'url': s.get('url'),
            'reply': 'APROBAR ' + s['reviewRequest']['token'],
            'scope': 'Documentación, PR, espera de CI y de tu merge, y limpieza posterior del trabajo; no fusiona los PR.'}


def prompt(h, event):
    if event.get('hook_event_name') != 'UserPromptSubmit': return
    text = event.get('prompt', '').strip()
    if text.upper().startswith('LISTA PARA IMPLEMENTAR'):
        module = h.runtime_module('spec_readiness')
        match = module.CONSENT.fullmatch(text)
        return module.consent(h, event, match) if match else None
    if re.match(r'^/harness(?:\s|$)', text):
        sid = session_id(h, event.get('session_id'))
        h.atomic(h.STORE/'conversations'/sid/'hook.json', {'sessionId': sid, 'at': time.time(), **owner(h)})
        return
    if re.fullmatch(r'RECUPERAR [a-z0-9-]+ [a-f0-9]{8}', text):
        return recovery_consent(h, event, text)
    if not re.fullmatch(r'APROBAR [a-f0-9]{8}', text): return
    sid = session_id(h, event.get('session_id'))
    s = existing(h, sid)
    if s is None:
        candidates = [json.loads(p.read_text()) for p in (h.STORE/'runs').glob('*/state.json')]
        matches = [v for v in candidates if v.get('reviewRequest', {}).get('token') == text.split()[1]]
        raise h.Blocked(mismatch(matches[0]) if len(matches) == 1 else
                        'Esta conversación no tiene un trabajo del harness. Reanuda la sesión original o usa /harness recover <job-id>.')
    with h.lock('job-' + s['id']):
        s = h.load(s['id'])
        h.require(s.get('conversation', {}).get('sessionId') == sid, mismatch(s))
        req = s.get('reviewRequest', {})
        h.require(s['stage'] == 'awaiting_human_approval' and req.get('token') == text.split()[1]
                  and req.get('version') == s.get('approvedCandidate'), 'Confirmación vencida o de otra entrega. Pide la revisión vigente.')
        s['userConsent'] = {'version': req['version'], 'token': req['token'], 'sessionId': sid,
                            'source': 'UserPromptSubmit', 'at': time.time()}
        h.save(s, 'chat_user_consent_received')
    return 'El usuario confirmó la entrega de ' + s['id'] + '. Ejecuta accept-review y continúa solo si devuelve documenting.'


def accept(h, s):
    c, req = s.get('userConsent', {}), s.get('reviewRequest', {})
    h.require(s.get('conversation') and c.get('source') == 'UserPromptSubmit'
              and c.get('sessionId') == s['conversation']['sessionId']
              and c.get('token') == req.get('token') and c.get('version') == req.get('version')
              and c.get('at', 0) >= req.get('at', float('inf')), 'Falta confirmación explícita del usuario en esta conversación.')
    h.approve(s, c['version'], confirm=False)



RECOVERABLE = {'implementing', 'ready_for_qa', 'qa', 'awaiting_human_approval', 'blocked', 'paused'}


def mismatch(s):
    old = s.get('conversation', {}).get('sessionId', 'desconocida')
    return (f"El trabajo {s['id']} pertenece a la sesión {old}. "
            f"Reanúdala con claude --resume {old}, o ciérrala y usa /harness recover {s['id']} "
            "desde la nueva conversación. Recuperar no aprueba cambios.")


def owner(h):
    """Remember the Claude process, never the short-lived hook/shell process."""
    pid = os.getppid()
    for _ in range(24):
        try:
            root = Path('/proc')/str(pid)
            if (root/'comm').read_text().strip() in {'claude', 'claude-code'}:
                identity = h.process_identity(pid)
                return {'ownerPid':pid, 'ownerStart':identity} if identity else {}
            pid = int(re.search(r'^PPid:\s+(\d+)', (root/'status').read_text(), re.M)[1])
            if pid <= 1: break
        except (OSError, TypeError, ValueError): break
    return {}


def require_released(h, s):
    """Fail closed if the previous coordinator cannot be proven stopped."""
    old = s['conversation']['sessionId']
    sessions = json.loads(h.run(['claude', 'agents', '--json', '--all'], timeout=20))
    h.require(isinstance(sessions, list), 'No se pudo comprobar el inventario de Claude.')
    related = [v for v in sessions if v.get('sessionId') == old or v.get('parentSessionId') == old]
    h.require(all(v.get('state') in {'stopped','failed','completed'} for v in related),
              'Cierra primero la conversación anterior y sus agentes; sigue activa. '+mismatch(s))
    marker = h.STORE/'conversations'/old/'hook.json'
    previous = json.loads(marker.read_text()) if marker.is_file() else {}
    pid, identity = previous.get('ownerPid'), previous.get('ownerStart')
    if pid and identity:
        h.require(h.process_identity(pid) != identity, 'El proceso de la conversación anterior sigue abierto.')
    else:
        h.require(any(v.get('sessionId') == old for v in related),
                  'No se pudo acreditar el cierre de la sesión anterior; reanúdala con claude --resume '+old+'.')


def recover_request(h, s, sid):
    sid = session_id(h, sid); ready(h, sid)
    h.require(s.get('conversation') and s['stage'] in RECOVERABLE, 'El trabajo no admite recuperar conversación en esta etapa.')
    old = s['conversation']['sessionId']
    h.require(old != sid, 'Esta ya es la conversación del trabajo; usa brief/review.')
    h.require(existing(h, sid) is None, 'La conversación nueva ya pertenece a otra spec.')
    h.require_job_config(s)
    require_released(h, s)
    req = s.get('recoveryRequest', {})
    if req.get('from') != old or req.get('to') != sid:
        req = {'from':old, 'to':sid, 'token':secrets.token_hex(4), 'at':time.time()}
        s['recoveryRequest'] = req
        s.pop('recoveryConsent', None)
        h.save(s, 'conversation_recovery_requested')
    return {'job':s['id'], 'from':old, 'to':sid,
            'reply':f"RECUPERAR {s['id']} {req['token']}",
            'scope':'Transferir esta conversación; anula la frase APROBAR anterior, sin aprobar ni repetir QA.'}


def recovery_consent(h, event, text):
    sid = session_id(h, event.get('session_id'))
    _, job, token = text.split()
    with h.lock('job-'+h.jobdir(job).name):
        s = h.load(job); req = s.get('recoveryRequest', {})
        h.require(s['stage'] in RECOVERABLE and req.get('to') == sid and req.get('token') == token
                  and req.get('from') == s.get('conversation', {}).get('sessionId'),
                  'Solicitud de recuperación vencida o de otra conversación.')
        s['recoveryConsent'] = {'from':req['from'], 'to':sid, 'token':token,
                                'at':time.time(), 'source':'UserPromptSubmit'}
        h.save(s, 'conversation_recovery_consent_received')
    return 'El usuario autorizó recuperar '+job+'. Ejecuta accept-recovery con el ID de esta sesión; todavía no hay aprobación de producto.'


def accept_recovery(h, s, sid):
    sid = session_id(h, sid); ready(h, sid)
    req, consent = s.get('recoveryRequest', {}), s.get('recoveryConsent', {})
    h.require(s['stage'] in RECOVERABLE and s.get('conversation') and
              consent.get('source') == 'UserPromptSubmit' and
              req.get('to') == consent.get('to') == sid and
              req.get('from') == consent.get('from') == s['conversation']['sessionId'] and
              req.get('token') == consent.get('token') and req.get('token') and
              consent.get('at', 0) >= req.get('at', float('inf')),
              'Falta consentimiento explícito vigente para recuperar esta conversación.')
    h.require(existing(h, sid) is None, 'La conversación nueva ya tiene un trabajo.')
    h.require_job_config(s)
    require_released(h, s)
    s.setdefault('conversationHistory', []).append({'from':req['from'], 'to':sid, 'at':time.time(),
                                                    'source':consent['source']})
    s['conversation']['sessionId'] = sid; s['sessionId'] = sid
    for field in ('reviewRequest', 'userConsent', 'recoveryRequest', 'recoveryConsent'):
        s.pop(field, None)
    h.save(s, 'conversation_recovered')
    # Version/preview are revalidated by review, never fabricated during transfer.
    return {'job':s['id'], 'stage':s['stage'], 'sessionId':sid,
            'next':'review --session-id '+sid if s['stage']=='awaiting_human_approval' else 'brief'}


def main():
    import harness as h
    try:
        context = prompt(h, json.load(sys.stdin))
        if context:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': context}}))
    except (h.Blocked, ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
