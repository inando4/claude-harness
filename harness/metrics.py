"""On-demand local accounting. No model calls, conversation export or polling."""
from collections import Counter
import json
from datetime import datetime
from pathlib import Path
import time
from statistics import median

TOKEN_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def transcript_counts(path, session, seen=None, window=None):
    groups = {}; models = set(); efforts = set(); calls = Counter(); role = 'coordinator'; stamps = []
    undated = 0
    for line in path.read_text().splitlines():
        try: row = json.loads(line)
        except json.JSONDecodeError: continue  # A live writer may have an incomplete last line.
        if row.get('sessionId', row.get('session_id')) != session: continue
        at = timestamp(row.get('timestamp'))
        if window is not None:
            if at is None:
                undated += row.get('type') == 'assistant'
                continue
            if not window[0] <= at <= window[1]: continue
        if at is not None: stamps.append(at)
        message = row.get('message', {})
        if row.get('type') != 'assistant' or not message.get('id'): continue
        if seen is not None and message['id'] in seen:continue
        role = row.get('attributionAgent', role)
        if message.get('model'): models.add(message['model'])
        if row.get('effort'): efforts.add(row['effort'])
        record = groups.setdefault(message['id'], {'usage': Counter(), 'tools': set()})
        for key in TOKEN_KEYS:
            value = message.get('usage', {}).get(key, 0)
            if isinstance(value, int) and value >= 0: record['usage'][key] = max(record['usage'][key], value)
        for block in message.get('content', []):
            if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id') not in record['tools']:
                record['tools'].add(block.get('id')); calls[block.get('name','unknown')] += 1
    if seen is not None:seen.update(groups)
    total = Counter()
    for record in groups.values(): total.update(record['usage'])
    contexts = [sum(r['usage'][k] for k in TOKEN_KEYS[:3]) for r in groups.values()]
    return {'agent': role, 'messages': len(groups), 'tokens': dict(total), 'tools': dict(calls),
            'models': sorted(models), 'efforts': sorted(efforts), 'file':path.name,
            'undatedAssistantRows':undated,
            'inputContextTokens':{'median':median(contexts) if contexts else None,'max':max(contexts) if contexts else None},
            'observedSpanSeconds':round(max(stamps)-min(stamps),1) if stamps else None,
            'gapsOverFiveMinutes':sum(b-a>300 for a,b in zip(sorted(set(stamps)),sorted(set(stamps))[1:])),
            'newInputTokens':total['input_tokens']+total['cache_creation_input_tokens'],
            'cachedInputTokens':total['cache_read_input_tokens']}


def summarize(h, s, transcript=None):
    directory = h.jobdir(s['id']); attempts = []; command_seconds = 0
    for folder in sorted((directory/'attempts').glob('*')):
        p = folder/'steps.jsonl'
        if not p.is_file(): continue
        rows = read_rows(p)
        commands = read_rows(folder/'commands.jsonl')
        command_seconds += sum(v.get('durationSeconds',0) for v in commands)
        last = {v['id']:v for v in rows}
        attempts.append({'attempt': folder.name, 'executed': [v['id'] for v in rows if v['status']=='passed'],
                         'reused': [v['id'] for v in rows if v['status']=='reused'],
                         'failed': [v['id'] for v in rows if v['status']=='failed'],
                         'unfinished': [k for k,v in last.items() if v['status']=='running'],
                         'executedSeconds': round(sum(v.get('durationSeconds',0) for v in rows),3)})
    events = read_rows(directory/'events.jsonl')
    now=time.time(); durations=Counter()
    boundary=s.get('metricsWindow',{})
    start=boundary.get('startedAt',events[0]['at'] if events else None)
    terminal=next((v['at'] for v in events if v['stage'] in h.DONE),None)
    end=boundary.get('endedAt',terminal if terminal is not None else now)
    for i,row in enumerate(events):
        next_at=events[i+1]['at'] if i+1<len(events) else end
        durations[row['stage']]+=max(0,min(end,next_at)-max(start if start is not None else row['at'],row['at']))
    result={'job':s['id'],'stage':s['stage'],'partial':s['stage'] not in h.DONE,'attempts':attempts,
            'stageWallSeconds':{k:round(v,1) for k,v in durations.items()},
            'elapsedWallSeconds':round(sum(durations.values()),1),
            'humanReviewWallSeconds':round(durations['awaiting_human_approval'],1),
            'recordedCommandSeconds':round(command_seconds,3),
            'resourceWaitSeconds':round(sum(v.get('seconds',0) for v in s.get('resourceWaits',[])),3),
            'interruptions':s.get('interruptions',[]),
            'interruptedAttemptWallSeconds':round(sum(max(0,v['at']-v['operationStartedAt']) for v in s.get('interruptions',[]) if v.get('operationStartedAt') is not None),1),
            'upstreamIntegrations':s.get('synchronization',{}).get('history',[]),
            'activeAgentSeconds':None,
            'browserExplorationSeconds':round(sum(max(0,v.get('endedAt',0)-v.get('startedAt',0)) for v in read_rows(directory/'browser-intervals.jsonl')),1),
            'notes':['Los intervalos de etapa incluyen inactividad; no miden tiempo pensando ni cuota de suscripción.',
                     'Comandos y etapas se solapan: no sumarlos. Los silencios del transcript no prueban inactividad.',
                     'Solo se contabilizan comandos del controlador; exploración y comandos de agentes pueden no estar instrumentados.',
                     'El intervalo de un intento interrumpido incluye trabajo y espera; no permite calcular inactividad exacta.'],
            'tokens':{'available':False,'agents':[]}}
    if transcript:
        p=Path(transcript).expanduser().resolve()
        h.require(p.is_file() and p.name==s['sessionId']+'.jsonl','Usa la conversación local de la sesión registrada.')
        # Old jobs lack the prompt boundary: use registration as a conservative
        # lower bound and report partial attribution, never count the whole chat.
        window=(start,end) if start is not None else None
        sessions={s['sessionId']:window};current=s['sessionId'];upper=end
        incomplete=not boundary.get('startedAt') or window is None
        for transfer in reversed(s.get('conversationHistory',[])):
            if transfer.get('source')=='UserPromptSubmit' and transfer.get('to')==current:
                at=transfer.get('at')
                if window and isinstance(at,(int,float)):
                    sessions[current]=(max(start,at),upper);upper=min(upper,at)
                else: incomplete=True
                current=transfer['from'];sessions[current]=(start,upper) if window else None
        seen=set();agents=[];missing=[]
        for sid in sorted(sessions,key=lambda v:v!=s['sessionId']):
            primary=p.parent/(sid+'.jsonl')
            if not primary.is_file():missing.append(sid);continue
            paths=[primary,*sorted((p.parent/sid/'subagents').glob('*.jsonl'))]
            if sessions[sid] is None: continue  # No trustworthy job boundary.
            agents.extend(dict(transcript_counts(path,sid,seen,sessions[sid]),session=sid) for path in paths)
        totals=Counter()
        for agent in agents: totals.update(agent['tokens'])
        result['tokens']={'available':any(a['messages'] for a in agents),'agents':agents,'total':dict(totals),'missingSessions':missing,
                          'partial':bool(missing) or incomplete or any(a['undatedAssistantRows'] for a in agents),
                          'window':{'startedAt':start,'endedAt':end,'source':'prompt' if 'startedAt' in boundary else 'registration_lower_bound'},
                          'newInputTokens':totals['input_tokens']+totals['cache_creation_input_tokens'],
                          'cachedInputTokens':totals['cache_read_input_tokens'],
                          'method':'Solo dentro del trabajo y traspasos registrados; IDs únicos y máximo acumulado por campo, sin sumar iterations. Thinking es parte de output, no se añade. Caché separada; no representa facturación ni cuota de suscripción.'}
    return result


def read_rows(path):
    if not path.is_file(): return []
    rows = []
    for line in path.read_text().splitlines():
        try: rows.append(json.loads(line))
        except json.JSONDecodeError: continue  # A live/interrupted writer may leave a partial row.
    return rows
