#!/usr/bin/env python3
"""Two real MCP connections / headed Chrome, synthetic localhost only."""
import concurrent.futures
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import browser_mcp as gateway

class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<title>Harness browser smoke</title><h1>Local isolation test</h1>')
    def log_message(self,*args):pass

class Peer(gateway.Server):
    def __init__(self,root):super().__init__(None);self.root=root
    def start(self,artifacts=None):
        code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import harness as h;h.ROOT=Path(sys.argv[2]);h.STORE=h.ROOT/'.harness';import browser_mcp;browser_mcp.serve(h.CoreContext())"
        self.proc=subprocess.Popen([sys.executable,'-B','-c',code,str(Path(gateway.__file__).parent),str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,bufsize=0);self.buffer=b''
    def call(self,name,**args):
        response=self.request('tools/call',{'name':name,'arguments':args})
        if 'error' in response or response.get('result',{}).get('isError'):raise AssertionError(response)
        return response

def main():
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='harness-browser-smoke-') as temp:
        root=Path(temp);(root/'harness').mkdir();(root/'harness/config.json').write_text(json.dumps({'playwrightMcpVersion':'0.0.80'}))
        for job in ('smoke-one','smoke-two'):
            p=root/'.harness/runs'/job;(p/'artifacts').mkdir(parents=True)
            (p/'state.json').write_text(json.dumps({'id':job,'stage':'qa','qaVersion':'v1'}))
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start()
        peers=[Peer(root),Peer(root)]
        try:
            for peer,job in zip(peers,('smoke-one','smoke-two')):
                peer.start();peer.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'harness-smoke','version':'1'}})
                peer.send({'jsonrpc':'2.0','method':'notifications/initialized'})
                listing=peer.request('tools/list');assert any(t['name']=='harness_browser_start' for t in listing['result']['tools'])
                denied=peer.request('tools/call',{'name':'browser_navigate','arguments':{'url':'http://127.0.0.1'}});assert denied['result']['isError']
                peer.call('harness_browser_start',job=job,version='v1')
            url='http://127.0.0.1:'+str(server.server_port)
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(p.call,'browser_navigate',url=url) for p in peers]
                for f in futures:f.result()
            for peer,value in zip(peers,('ONE','TWO')):peer.call('browser_evaluate',function="() => {localStorage.setItem('harnessSmoke', '"+value+"'); return document.title}")
            for peer,value in zip(peers,('ONE','TWO')):
                result=peer.call('browser_evaluate',function="() => localStorage.getItem('harnessSmoke')")
                assert value in json.dumps(result),result
            # Closing job one must not interrupt job two.
            peers[0].call('browser_close')
            assert 'TWO' in json.dumps(peers[1].call('browser_evaluate',function="() => localStorage.getItem('harnessSmoke')"))
            peers[1].call('browser_close')
            assert not list((root/'.harness/runs').glob('*/browser-active.json'))
            print('PASS: two isolated headed Chrome MCP sessions; independent storage and close; leases released. Seconds:',round(time.monotonic()-start,1))
        finally:
            for p in peers:p.stop()
            server.shutdown();server.server_close()

if __name__=='__main__':main()
