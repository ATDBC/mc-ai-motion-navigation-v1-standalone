"""Loopback-only evidence viewer. No game connection or actor imports."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from catalog import discover, inside
from known_map import normalize_known_map_scenario
from physics_replay import PhysicsReplayStore

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
WEB=HERE/'web'


def fingerprint(evidence):
    entries=[]
    for name in ('run-manifest.json','terminal.json','initial-state.json','memory-observations.json','terrain-change.json','../evidence-result.json','source-archive/manifest.json'):
        path=evidence/name
        if path.exists():entries.append((name,hashlib.sha256(path.read_bytes()).hexdigest()))
    for name in ('runtime-trace/trace','trajectory','source-archive/files'):
        directory=evidence/name
        for path in sorted(directory.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                stat=path.stat();entries.append((path.relative_to(evidence).as_posix(),stat.st_size,stat.st_mtime_ns))
    for name in ('extract.py','catalog.py'):
        entries.append((name,hashlib.sha256((HERE/name).read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries,separators=(',',':')).encode()).hexdigest()


def standalone_fingerprint(data_path, result_path, *sources):
    entries=[]
    for path in (data_path,result_path,HERE/'catalog.py',HERE/'server.py',*sources):
        entries.append((path.name,hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries,separators=(',',':')).encode()).hexdigest()


class Store:
    def __init__(self,root):
        self.root=Path(root).resolve();self.cache=self.root/'artifacts/navigation-replay-cache'
        self.physics=PhysicsReplayStore(self.root,self.cache)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='replay-parser')
        self.lock=threading.Lock();self.jobs={};self.batches=[];self.runs={};self.loaded=0

    def catalog(self,force=False):
        with self.lock:
            if force or not self.batches or time.monotonic()-self.loaded>30:
                self.batches=discover(self.root)
                self.runs={r['id']:r for b in self.batches for r in b['runs']}
                self.loaded=time.monotonic()
            return self.batches

    def physics_catalog(self,force=False):
        return self.physics.catalog(force=force)

    def get_physics(self,run_id):
        return self.physics.get(run_id)

    def get(self,run_id):
        self.catalog()
        with self.lock:
            if run_id not in self.runs:raise KeyError('未找到该场实验')
            run=dict(self.runs[run_id])
            if not run['complete']:return {'status':'error','message':'该场记录尚未完整封存，暂不能回放'}
            if run.get('kind') in {'motion_shape','known_map'}:
                data_path=inside(self.root,run['data_file'])
                result_path=inside(self.root,run['directory'])/'result.json'
                sources=(HERE/'known_map.py',) if run.get('kind')=='known_map' else ()
                key=standalone_fingerprint(data_path,result_path,*sources)
                output=self.cache/(run_id+'-'+key[:16]+'.json')
                if not output.exists():
                    payload=json.loads(data_path.read_text('utf-8-sig'))
                    scenarios=payload.get('scenarios',())
                    index=run['scenario_index']
                    if type(index) is not int or not 0<=index<len(scenarios):
                        return {'status':'error','message':'运动测试记录缺少对应场景'}
                    scenario=dict(scenarios[index])
                    recorded_case=scenario.get('case') if run.get('kind')=='motion_shape' else scenario.get('name')
                    if recorded_case!=run['case']:
                        return {'status':'error','message':'回放场景索引与目录不一致'}
                    if run.get('kind')=='known_map':
                        scenario=normalize_known_map_scenario(payload,scenario)
                    self.cache.mkdir(parents=True,exist_ok=True)
                    temporary=output.with_suffix('.pending')
                    temporary.write_text(json.dumps(scenario,ensure_ascii=False,separators=(',',':')),
                                         encoding='utf-8')
                    temporary.replace(output)
                return {'status':'ready','path':output}
            evidence=inside(self.root,run['directory'])/'evidence'
            key=fingerprint(evidence)
            output=self.cache/(run_id+'-'+key[:16]+'.json')
            if output.exists():return {'status':'ready','path':output}
            job=self.jobs.get((run_id,key))
            if job is None:
                job={'status':'loading','message':'正在读取记录并按原版本还原记忆…'}
                self.jobs[(run_id,key)]=job
                self.executor.submit(self.build,evidence,output,job)
            return dict(job)

    def build(self,evidence,output,job):
        self.cache.mkdir(parents=True,exist_ok=True)
        temporary=output.with_suffix('.pending')
        try:
            result=subprocess.run([sys.executable,'-B',str(HERE/'extract.py'),str(evidence),str(temporary)],
                                  cwd=HERE,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:
                raise ValueError((result.stderr or result.stdout)[-1600:])
            # Never publish an output from a source that changed during parsing.
            key=fingerprint(evidence)
            if not output.name.endswith(key[:16]+'.json'):
                raise ValueError('解析期间记录发生变化，请刷新后重试')
            temporary.replace(output)
            job.update(status='ready',message='解析完成')
        except Exception as error:
            job.update(status='error',message='无法读取该场：'+str(error))


def handler(store,allowed_hosts=()):
    permitted_hosts={'127.0.0.1','localhost'} | {host.lower() for host in allowed_hosts}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def send(self,code,data,content_type='application/json; charset=utf-8'):
            if isinstance(data,(dict,list)):data=json.dumps(data,ensure_ascii=False).encode('utf-8')
            if isinstance(data,str):data=data.encode('utf-8')
            compressed='gzip' in self.headers.get('Accept-Encoding','') and len(data)>4096
            if compressed:data=gzip.compress(data,compresslevel=3)
            self.send_response(code)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if compressed:self.send_header('Content-Encoding','gzip')
            self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass

        def do_GET(self):
            host=self.headers.get('Host','').split(':')[0].lower()
            if host not in permitted_hosts:
                self.send(403,{'message':'未允许的访问域名'});return
            path=urlparse(self.path).path
            try:
                if path=='/api/catalog':
                    self.send(200,{'batches':store.catalog(force='refresh=1' in self.path)});return
                if path=='/api/physics/catalog':
                    self.send(200,{'runs':store.physics_catalog(force='refresh=1' in self.path)});return
                if path.startswith('/api/physics/run/'):
                    payload=store.get_physics(path.removeprefix('/api/physics/run/'))
                    if payload is None:self.send(404,{'message':'未找到该组物理证据'});return
                    if payload.get('status')=='error':self.send(422,payload);return
                    self.send(200,payload);return
                if path.startswith('/api/run/'):
                    status=store.get(path.removeprefix('/api/run/'))
                    if status['status']=='ready' and 'path' in status:
                        self.send(200,status['path'].read_bytes());return
                    self.send(422 if status['status']=='error' else 202,status);return
                name='index.html' if path=='/' else path.removeprefix('/')
                if name not in ('index.html','style.css','app.js','timeline.mjs',
                                'physics.html','physics.css','physics.js'):
                    self.send(404,{'message':'未找到页面'});return
                content_type='text/javascript' if name.endswith(('.js','.mjs')) else mimetypes.guess_type(name)[0]
                self.send(200,(WEB/name).read_bytes(),(content_type or 'text/plain')+'; charset=utf-8')
            except KeyError as error:self.send(404,{'message':str(error)})
            except Exception as error:self.send(500,{'message':str(error)})
    return Handler


def main():
    parser=argparse.ArgumentParser(description='本机导航实验回放页')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--allow-host',action='append',default=[],
                        help='允许反向代理使用的完整域名，可重复指定；不含协议、端口或路径')
    args=parser.parse_args()
    store=Store(ROOT)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),handler(store,args.allow_host))
    print(f'导航实验回放：http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();store.executor.shutdown(wait=True)


if __name__=='__main__':main()
