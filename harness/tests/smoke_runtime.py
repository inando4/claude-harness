#!/usr/bin/env python3
"""Opt-in restart smoke with a labelled, synthetic PostgreSQL container only."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--image',required=True)
    args=parser.parse_args()
    if not args.run:parser.error('Explicit --run required')
    source=Path(__file__).resolve().parents[1]
    loader=importlib.util.spec_from_file_location('runtime_smoke',source/'harness.py')
    h=importlib.util.module_from_spec(loader);loader.loader.exec_module(h)
    with tempfile.TemporaryDirectory(prefix='harness-runtime-') as directory:
        root=Path(directory);shutil.copytree(source,root/'harness',ignore=shutil.ignore_patterns('__pycache__'))
        h.ROOT=root;h.STORE=root/'.harness'
        cfg=json.loads((source/'templates/node-postgres.json').read_text());cfg['timeoutSeconds']=60
        (root/'harness/config.json').write_text(json.dumps(cfg))
        key=uuid.uuid4().hex[:12];name='harness-restart-smoke-'+key;port=h.available_port()
        s={'id':'restart-probe','stage':'implementing','cycles':0,'container':name,'volume':name,
           'resourceKey':key,'database':'probe','dbPort':port,'previews':{}}
        def docker(*argv):return h.run(['docker',*argv],timeout=60)
        created=False
        try:
            docker('volume','create','--label',h.LABEL+'='+key,name);created=True
            secret=root/'postgres.env';secret.write_text('POSTGRES_PASSWORD='+uuid.uuid4().hex+'\nPOSTGRES_DB=probe\n');secret.chmod(0o600)
            s['containerId']=docker('run','-d','--name',name,'--label',h.LABEL+'='+key,
                '--env-file',str(secret),'-p',f'127.0.0.1:{port}:5432','--mount',f'type=volume,source={name},target=/var/lib/postgresql/data',args.image)
            secret.unlink();h.save(s,'fixture')
            def ready():
                for _ in range(60):
                    result=subprocess.run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres','-d','probe'],capture_output=True)
                    if result.returncode==0:return
                    time.sleep(.5)
                raise RuntimeError('Synthetic PostgreSQL did not start')
            ready();docker('exec',name,'psql','-U','postgres','-d','probe','-c',"CREATE TABLE marker (value text); INSERT INTO marker VALUES ('synthetic-retained');")
            docker('stop',name);h.resume_runtime(s);ready()
            h.db_info(s)
            assert docker('exec',name,'psql','-U','postgres','-d','probe','-Atc','SELECT value FROM marker')=='synthetic-retained'
            assert s['cycles']==0 and s['stage']=='blocked'
            print('PASS: same container, owned volume, fixed loopback port and synthetic data retained; revalidation required')
        finally:
            if s.get('containerId'):
                info=json.loads(docker('inspect',name))[0]
                assert info['Id']==s['containerId'] and info['Config']['Labels'][h.LABEL]==key
                docker('rm','-f',s['containerId'])
            if created:
                volume=json.loads(docker('volume','inspect',name))[0]
                assert volume['Labels'][h.LABEL]==key
                docker('volume','rm',name)
            print('Synthetic smoke resources removed')


if __name__=='__main__':main()
