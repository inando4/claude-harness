"""Lazy, explicitly job-bound Playwright MCP gateway (stdio JSON-RPC)."""
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

BIND = {'name':'harness_browser_start',
        'description':'Reserve this job Chrome for visual exploration only. Call with brief.id and qaVersion immediately before exploring. Close with browser_close before tests or returning.',
        'inputSchema':{'type':'object','properties':{'job':{'type':'string'},'version':{'type':'string'}},
                       'required':['job','version'],'additionalProperties':False}}


def command(config, artifacts=None):
    version = config['playwrightMcpVersion']
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('playwrightMcpVersion debe ser una versión exacta.')
    argv = ['npx', '--yes', '@playwright/mcp@' + version, '--browser', 'chrome', '--isolated']
    if artifacts is not None: argv += ['--output-dir', str(artifacts)]
    return argv


class Lease:
    def __init__(self, h): self.h=h;self.stream=None;self.serial=None;self.job=None;self.version=None

    def acquire(self, job, version):
        h=self.h
        h.require(self.stream is None,'Cierra browser_close antes de reservar otra exploración.')
        with h.lock('job-'+h.jobdir(job).name, blocking=False):
            s=h.load(job);h.require(s['stage']=='qa' and s.get('qaVersion')==version,'Trabajo/versión de QA no vigente.')
            h.require_job_config(s)
            path=h.jobdir(job)/'browser.lock'
            stream=path.open('a+')
            try:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                limit=h.config().get('browser',{}).get('maxExplorations',2)
                h.require(type(limit) is int and limit in (1,2),'maxExplorations debe ser 1 o 2.')
                for slot in range(limit):
                    candidate=(h.STORE/('browser-visual-'+str(slot)+'.lock')).open('a+')
                    try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:candidate.close();continue
                    self.serial=candidate;break
                if self.serial is None:raise BlockingIOError()
            except BlockingIOError:
                stream.close()
                if self.serial:self.serial.close();self.serial=None
                raise h.Blocked('Exploración ocupada; continúa revisión sin navegador y reintenta al liberarse.')
            except BaseException:
                stream.close()
                if self.serial:self.serial.close();self.serial=None
                raise
            self.stream=stream;self.job=job;self.version=version
            stream.seek(0);stream.truncate();stream.write(json.dumps({'job':job,'version':version,'pid':os.getpid()}));stream.flush()
            self.started=time.time()
            h.atomic(h.jobdir(job)/'browser-active.json',{'job':job,'version':version,'pid':os.getpid(),'start':h.process_identity(os.getpid())})
        artifacts=h.jobdir(job)/'artifacts';artifacts.mkdir(exist_ok=True,mode=0o700)
        return artifacts

    def validate(self):
        self.h.require(self.stream is not None,'Primero llama harness_browser_start con ID y qaVersion del brief.')
        s=self.h.load(self.job)
        self.h.require(s['stage']=='qa' and s.get('qaVersion')==self.version,'La exploración ya no corresponde al QA vigente; cierra browser_close.')

    def release(self):
        if self.stream:
            try:
                p=self.h.jobdir(self.job)
                (p/'browser-active.json').unlink(missing_ok=True)
                with (p/'browser-intervals.jsonl').open('a') as log:
                    log.write(json.dumps({'job':self.job,'version':self.version,'startedAt':self.started,'endedAt':time.time()})+'\n')
            finally:self.stream.close();self.stream=None
        if self.serial:self.serial.close();self.serial=None


class Server:
    """One upstream per MCP connection; Chrome itself launches on browser use."""
    def __init__(self,h):self.h=h;self.proc=None;self.initial=None;self.counter=0

    def start(self, artifacts=None):
        self.proc=subprocess.Popen(command(self.h.config(),artifacts),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=sys.stderr,start_new_session=True,bufsize=0)
        self.buffer=b''

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj)+'\n').encode());self.proc.stdin.flush()

    def request(self, method, params=None, timeout=180):
        self.counter+=1;ident=self.counter
        self.send({'jsonrpc':'2.0','id':ident,'method':method,'params':params or {}})
        deadline=time.monotonic()+timeout
        while True:
            if b'\n' not in self.buffer:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([self.proc.stdout],[],[],remaining)[0]:raise TimeoutError('Playwright MCP no respondió.')
                chunk=os.read(self.proc.stdout.fileno(),65536)
                if not chunk:raise RuntimeError('Playwright MCP terminó inesperadamente.')
                self.buffer+=chunk;continue
            line,self.buffer=self.buffer.split(b'\n',1)
            if not line.strip():continue
            obj=json.loads(line)
            if obj.get('id')==ident:return obj
            if 'method' in obj and 'id' not in obj:emit(obj)
            elif 'method' in obj and 'id' in obj:
                self.send({'jsonrpc':'2.0','id':obj['id'],'error':{'code':-32601,'message':'Client requests unavailable'}})

    def restart(self, artifacts):
        self.stop();self.start(artifacts)
        result=self.request('initialize',self.initial)
        if 'error' in result:raise RuntimeError(str(result['error']))
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})

    def stop(self):
        if self.proc:
            p=self.proc
            try:
                if p.poll() is None:
                    try:self.request('tools/call',{'name':'browser_close','arguments':{}},timeout=10)
                    except (OSError,ValueError,RuntimeError,TimeoutError):pass
                    if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
                    try:p.wait(timeout=10)
                    except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
            finally:
                p.stdin.close();p.stdout.close();self.proc=None


def emit(obj):
    print(json.dumps(obj),flush=True)


def serve(h):
    server=Server(h);lease=Lease(h)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:sys.exit(0))
    try:
        for line in sys.stdin:
            msg=json.loads(line);method=msg.get('method');ident=msg.get('id');params=msg.get('params',{})
            if ident is None:
                if server.proc:server.send(msg)
                continue
            try:
                if method=='initialize':
                    server.initial=params;server.start();response=server.request(method,params)
                elif method=='tools/list':
                    response=server.request(method,params)
                    if 'result' in response:response['result']['tools'].append(BIND)
                elif method=='tools/call':
                    name=params.get('name')
                    if name==BIND['name']:
                        a=params.get('arguments',{});artifacts=lease.acquire(a.get('job',''),a.get('version'))
                        try:server.restart(artifacts)
                        except BaseException:lease.release();raise
                        response={'result':{'content':[{'type':'text','text':'Chrome aislado reservado para '+lease.job+'. Usa browser_close al terminar la exploración.'}]}}
                    elif name=='browser_close':
                        try:response=server.request(method,params)
                        finally:server.stop();lease.release();server.start();server.request('initialize',server.initial);server.send({'jsonrpc':'2.0','method':'notifications/initialized'})
                    else:
                        lease.validate();response=server.request(method,params)
                elif method=='ping':response={'result':{}}
                else:response={'error':{'code':-32601,'message':'Method unavailable'}}
            except (h.Blocked,OSError,ValueError,RuntimeError,TimeoutError,KeyError,TypeError) as error:
                if method=='tools/call':response={'result':{'isError':True,'content':[{'type':'text','text':str(error)}]}}
                else:response={'error':{'code':-32603,'message':str(error)}}
            emit(dict(response,jsonrpc='2.0',id=ident))
    finally:
        try:server.stop()
        finally:lease.release()


def ensure_released(h,s):
    """A dead connection releases flock automatically; never kill another job."""
    path=h.jobdir(s['id'])/'browser.lock'
    if not path.exists():return
    with path.open('a+') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise h.Blocked('Cierra browser_close o termina el QA anterior antes de continuar.')
        (h.jobdir(s['id'])/'browser-active.json').unlink(missing_ok=True)


if __name__=='__main__':
    import harness as h
    serve(h.CoreContext())
