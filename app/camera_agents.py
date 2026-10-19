"""Central registry and bounded, authenticated reads from camera-only nodes."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import httpx
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped,mapped_column
from .config import get_settings
from .database import Base,session_scope
from .models import utcnow

class AgentUnavailable(OSError):
    """Temporary source loss: preserve partial uploads and retry later."""

class CameraAgent(Base):
    __tablename__='camera_agents'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    name:Mapped[str]=mapped_column(String(128))
    base_url:Mapped[str]=mapped_column(String(512))
    virtual_root:Mapped[str]=mapped_column(String(512),unique=True)
    token_enc:Mapped[str]=mapped_column(Text)
    ca_path:Mapped[str]=mapped_column(Text)
    enabled:Mapped[bool]=mapped_column(Boolean,default=True)
    last_seen:Mapped[object|None]=mapped_column(DateTime,nullable=True)
    last_error:Mapped[str|None]=mapped_column(Text,nullable=True)
    manifest:Mapped[str]=mapped_column(Text,default='{}')
    manifest_digest:Mapped[str]=mapped_column(String(64),default='')

_lock=threading.RLock()
_nodes={}

def _relative(value):
    if not isinstance(value,str) or not value or len(value)>2048 or '\\' in value or '\x00' in value or any(p in ('','..','.') for p in value.split('/')):
        raise ValueError('Invalid camera manifest path')
    return value

def validate_manifest(payload):
    if not isinstance(payload,dict) or payload.get('version')!=1:raise ValueError('Unsupported camera agent')
    devices,files,dirs=payload.get('devices'),payload.get('files'),payload.get('directories')
    if not all(isinstance(v,list) for v in (devices,files,dirs)) or len(devices)>20 or len(files)>30000 or len(dirs)>30000:
        raise ValueError('Invalid camera inventory')
    roots=[];clean_devices=[]
    for d in devices:
        root=_relative(d['path']);dcim=_relative(d['dcim_path'])
        if not dcim.startswith(root+'/'):raise ValueError('Camera folder escapes device')
        if root in roots:raise ValueError('Duplicate camera root')
        for key in ('free_bytes','total_bytes','file_count'):
            if type(d.get(key)) is not int or not 0<=d[key]<=2**63-1:raise ValueError('Invalid camera capacity')
        for key in ('label','connection_id'):
            if not isinstance(d.get(key),str) or len(d[key])>256:raise ValueError('Invalid camera label')
        roots.append(root)
        clean_devices.append({k:d[k] for k in ('path','dcim_path','label','free_bytes','total_bytes','file_count','connection_id')})
    rows={};directories=set()
    for value in dirs:
        value=_relative(value)
        if not any(value==r or value.startswith(r+'/') for r in roots):raise ValueError('Camera directory escapes device')
        directories.add(value)
    for f in files:
        path=_relative(f['path'])
        if path in rows or not any(path.startswith(r+'/') for r in roots):raise ValueError('Camera file escapes device')
        for key in ('size','mtime_ns','ctime_ns'):
            if type(f.get(key)) is not int or not 0<=f[key]<=2**63-1:raise ValueError('Invalid camera identity')
        for key in ('inode','device'):
            if not isinstance(f.get(key),(str,int)) or not str(f[key]).isdigit() or len(str(f[key]))>20:
                raise ValueError('Invalid camera identity')
        rows[path]={k:f[k] for k in ('path','size','mtime_ns','ctime_ns','inode','device')}
        directories.update(str(p) for p in Path(path).parents if str(p)!='.')
    return {'version':1,'devices':clean_devices,'files':list(rows.values()),'directories':sorted(directories)}

def _config(row):
    from .crypto import decrypt
    parsed=urlsplit(row.base_url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Camera agents require a pinned HTTPS origin')
    root=os.path.normpath(row.virtual_root)
    if not root.startswith('/media/') and root!='/media':raise ValueError('Camera namespace must be under /media')
    return {'id':row.id,'name':row.name,'url':row.base_url.rstrip('/'),'root':root,'token':decrypt(row.token_enc),'ca':row.ca_path}

def _client(config):
    context=ssl.create_default_context(cafile=config['ca'])
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    return httpx.Client(verify=context,timeout=httpx.Timeout(30,connect=5),follow_redirects=False,trust_env=False,
                        headers={'Authorization':'Bearer '+config['token']},limits=httpx.Limits(max_connections=4,max_keepalive_connections=2))

def install_snapshot(config,payload,online=True):
    clean=validate_manifest(payload)
    with _lock:
        _nodes[config['id']]={'config':config,'manifest':clean,'files':{f['path']:f for f in clean['files']},
            'dirs':set(clean['directories']),'online':online,'seen':time.monotonic()}
    return clean

def lookup(path):
    value=os.path.normpath(str(path))
    with _lock:
        matches=[n for n in _nodes.values() if value==n['config']['root'] or value.startswith(n['config']['root']+'/')]
        if not matches:return None
        node=max(matches,key=lambda n:len(n['config']['root']))
        relative=value[len(node['config']['root']):].lstrip('/')
        return node,relative

def is_remote(path):return lookup(path) is not None

def devices():
    rows=[]
    with _lock:
        for node in _nodes.values():
            if not node['online'] or time.monotonic()-node['seen']>35:continue
            cfg=node['config']
            for d in node['manifest']['devices']:
                rows.append(d|{'path':cfg['root']+'/'+d['path'],'dcim_path':cfg['root']+'/'+d['dcim_path'],
                    'label':cfg['name']+' · '+d['label'],'agent_id':cfg['id'],'agent_name':cfg['name']})
    return rows

def require_online(node):
    if not node['online'] or time.monotonic()-node['seen']>35:
        raise AgentUnavailable('Camera agent is offline; transfer will resume when it returns')

class RemoteFile(io.BufferedIOBase):
    def __init__(self,path):
        match=lookup(path)
        if match is None:raise FileNotFoundError(str(path))
        node,relative=match;require_online(node)
        item=node['files'].get(relative)
        if item is None:raise FileNotFoundError(str(path))
        self.config=node['config'];self.relative=relative;self.size=item['size'];self.identity=f"{item['size']}:{item['mtime_ns']}"
        self.position=0;self.client=_client(self.config);self.response=None;self.context=None;self.iterator=None;self.buffer=bytearray();self.checked=False
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.position
    def _close_response(self):
        if self.context:self.context.__exit__(None,None,None)
        self.context=None;self.response=None;self.iterator=None;self.buffer.clear()
    def seek(self,offset,whence=0):
        value=offset if whence==0 else self.position+offset if whence==1 else self.size+offset if whence==2 else -1
        if value<0:raise ValueError('Invalid seek')
        if value!=self.position:self._close_response();self.position=value
        return value
    def _request(self):
        self.context=self.client.stream('GET',self.config['url']+'/file',params={'path':self.relative},
            headers={'Range':f'bytes={self.position}-','If-Match':self.identity})
        self.response=self.context.__enter__()
        if self.response.status_code in (404,429,503):raise AgentUnavailable('Camera is disconnected or busy; transfer will resume')
        if self.response.status_code==412:raise OSError('Camera file changed; reconnect or refresh its inventory')
        expected=f'bytes {self.position}-{self.size-1}/{self.size}'
        if self.response.status_code!=206 or self.response.headers.get('Content-Range')!=expected or self.response.headers.get('Content-Length')!=str(self.size-self.position):
            self._close_response()
            raise OSError('Camera agent returned an invalid byte range')
        self.iterator=self.response.iter_bytes(256*1024)
    def _check(self):
        if self.checked:return
        response=self.client.head(self.config['url']+'/file',params={'path':self.relative},headers={'If-Match':self.identity})
        if response.status_code!=200:raise AgentUnavailable('Camera changed or disconnected before transfer completed')
        self.checked=True
    def read(self,size=-1):
        if self.closed:raise ValueError('I/O on closed camera stream')
        size=min(self.size-self.position,size if size is not None and size>=0 else self.size-self.position)
        try:
            if size<=0:
                if self.position==self.size:self._close_response();self._check()
                return b''
            if self.response is None:self._request()
            while len(self.buffer)<size:
                try:self.buffer.extend(next(self.iterator))
                except StopIteration:break
            data=bytes(self.buffer[:size]);del self.buffer[:len(data)];self.position+=len(data)
            if len(data)!=size:raise AgentUnavailable('Camera stream was interrupted; partial upload retained')
            if self.position==self.size:self._close_response();self._check()
            return data
        except httpx.HTTPError:
            raise AgentUnavailable('Camera connection interrupted; partial upload retained') from None
    def readinto(self,buffer):
        data=self.read(len(buffer));buffer[:len(data)]=data;return len(data)
    def close(self):
        if not self.closed:
            self._close_response();self.client.close()
        super().close()

class AgentMonitor:
    def __init__(self):self.stop_event=threading.Event();self.thread=None
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        with session_scope() as s:
            for row in s.query(CameraAgent):
                try:install_snapshot(_config(row),json.loads(row.manifest),False)
                except Exception:pass
        self.thread=threading.Thread(target=self.run,name='drift-camera-agents',daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def tick(self):
        configs=[]
        with session_scope() as s:
            for row in s.query(CameraAgent).filter_by(enabled=True):
                try:configs.append(_config(row))
                except Exception:row.last_error='Agent configuration needs repair'
        active={c['id'] for c in configs}
        with _lock:
            for id,node in _nodes.items():
                if id not in active:node['online']=False
        for cfg in configs:
            try:
                with _client(cfg) as client:
                    with client.stream('GET',cfg['url']+'/manifest') as response:
                        response.raise_for_status();data=bytearray()
                        for part in response.iter_bytes(65536):
                            data.extend(part)
                            if len(data)>16*1024*1024:raise ValueError('Camera inventory exceeds limit')
                clean=install_snapshot(cfg,json.loads(data));encoded=json.dumps(clean,sort_keys=True,separators=(',',':'))
                digest=hashlib.sha256(encoded.encode()).hexdigest()
                with session_scope() as s:
                    row=s.get(CameraAgent,cfg['id'])
                    if row:
                        if row.manifest_digest!=digest:row.manifest,row.manifest_digest=encoded,digest
                        if not row.last_seen or (utcnow().replace(tzinfo=None)-row.last_seen.replace(tzinfo=None)).total_seconds()>60:row.last_seen=utcnow()
                        row.last_error=None
            except (httpx.HTTPError,OSError,ValueError,KeyError):
                with _lock:
                    if cfg['id'] in _nodes:_nodes[cfg['id']]['online']=False
                with session_scope() as s:
                    row=s.get(CameraAgent,cfg['id'])
                    if row:row.last_error='Agent unavailable; check power, network and camera connection'
    def run(self):
        while not self.stop_event.is_set():
            try:self.tick()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Camera agent scan deferred')
            self.stop_event.wait(10)

monitor=AgentMonitor()
