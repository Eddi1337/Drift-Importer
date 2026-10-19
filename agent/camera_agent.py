"""Read-only camera gateway. Python standard library only; no NAS or database."""
from __future__ import annotations
import argparse
import hmac
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import ssl
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, unquote

NETWORK_FS = {'nfs','nfs4','cifs','smb3','smbfs','fuse.sshfs','fuse.rclone','9p'}
MAX_FILES = 30000
CHUNK = 256 * 1024

class CameraInventory:
    def __init__(self, root='/media', require_mount=True):
        self.root = Path(root).resolve()
        self.require_mount = require_mount
        self.lock = threading.Lock()
        self.files = {}
        self.payload = {'version':1,'devices':[],'files':[],'directories':[]}
        self.connections = {}
        self.stop = threading.Event()

    def scan(self):
        mounts = {}
        try:
            for line in Path('/proc/mounts').read_text().splitlines():
                parts=line.split()
                if len(parts)>2:
                    mount=parts[1].replace('\\040',' ')
                    if mount.startswith(str(self.root)+'/') and parts[2] not in NETWORK_FS:
                        mounts[mount]=parts[2]
        except OSError:
            pass
        roots = [Path(p) for p in mounts] if self.require_mount else [p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink()]
        devices, files, directories, entries, current = [],[],[],{},{}
        for root in sorted(roots):
            if root.is_symlink() or not root.is_dir(): continue
            dcim=next((p for p in root.iterdir() if p.name.lower()=='dcim' and p.is_dir() and not p.is_symlink()),None)
            if dcim is None: continue
            relative=root.relative_to(self.root).as_posix()
            connection=self.connections.get(relative) or secrets.token_hex(12)
            current[relative]=connection
            count=0
            for folder, dirs, names in os.walk(root,followlinks=False,onerror=self.scan_failed):
                dirs[:]=[n for n in dirs if not n.startswith('.') and not (Path(folder)/n).is_symlink()]
                directories.append((Path(folder).relative_to(self.root)).as_posix())
                for name in sorted(names):
                    path=Path(folder)/name
                    if name.startswith('.') or path.is_symlink():continue
                    try:
                        s=path.stat()
                        if not stat.S_ISREG(s.st_mode):continue
                        item={'path':path.relative_to(self.root).as_posix(),'size':s.st_size,'mtime_ns':s.st_mtime_ns,
                              'ctime_ns':s.st_ctime_ns,'inode':str(s.st_ino),'device':str(s.st_dev)}
                    except OSError:raise
                    files.append(item);entries[item['path']]=item;count+=1
                    if len(files)>MAX_FILES:raise RuntimeError('Camera inventory exceeds the file limit')
            usage=shutil.disk_usage(root)
            devices.append({'path':relative,'label':root.name,'dcim_path':dcim.relative_to(self.root).as_posix(),
                            'free_bytes':usage.free,'total_bytes':usage.total,'file_count':count,'connection_id':connection})
        with self.lock:
            self.connections=current;self.files=entries
            self.payload={'version':1,'devices':devices,'files':files,'directories':directories,'scanned_at':time.time()}

    @staticmethod
    def scan_failed(error):
        raise error

    def run(self):
        while not self.stop.is_set():
            try:self.scan()
            except (OSError,RuntimeError):
                with self.lock:self.payload={'version':1,'devices':[],'files':[],'directories':[],'scan_error':True};self.files={};self.connections={}
            self.stop.wait(8)

    def open_file(self, relative, expected):
        if not relative or '\\' in relative or '\x00' in relative or any(p in ('','..','.') for p in relative.split('/')):
            raise FileNotFoundError()
        with self.lock:item=self.files.get(relative)
        if not item:raise FileNotFoundError()
        # Walk using dirfds and O_NOFOLLOW: a changed directory cannot escape
        # the camera root between validation and open.
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            parts=PurePosixPath(relative).parts
            for part in parts[:-1]:
                nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
            result=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        finally:os.close(fd)
        s=os.fstat(result)
        identity=f'{s.st_size}:{s.st_mtime_ns}'
        if not stat.S_ISREG(s.st_mode) or identity!=expected or s.st_size!=item['size'] or s.st_mtime_ns!=item['mtime_ns']:
            os.close(result);raise ValueError('Camera file changed')
        return os.fdopen(result,'rb'),s

class Gateway(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,inventory,token):
        self.inventory=inventory;self.token=token;self.streams=threading.BoundedSemaphore(3);self.requests=threading.BoundedSemaphore(12)
        super().__init__(address,Handler)

    def handle_error(self,request,address):
        pass

    def process_request(self,request,address):
        if not self.requests.acquire(blocking=False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.requests.release();raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.requests.release()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        self.request.settimeout(20)
        super().setup()
    def log_message(self,*args):pass
    def send_body(self,status,body=b'',content_type='application/json'):
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        self.connection.settimeout(45)
        url=urlsplit(self.path)
        if url.path=='/healthz':return self.send_body(200,b'{"status":"ok","role":"camera-agent"}')
        provided=self.headers.get('Authorization','')
        if not hmac.compare_digest(provided,'Bearer '+self.server.token):return self.send_body(401,b'{}')
        if url.path=='/manifest':
            with self.server.inventory.lock:
                payload=self.server.inventory.payload
                body=json.dumps(payload,separators=(',',':')).encode()
            return self.send_body(503 if payload.get('scan_error') else 200,body)
        if url.path!='/file':return self.send_body(404,b'{}')
        relative=parse_qs(url.query).get('path',[''])[0]
        try:handle,s=self.server.inventory.open_file(relative,self.headers.get('If-Match',''))
        except ValueError:return self.send_body(412,b'{"detail":"Camera file changed"}')
        except OSError:return self.send_body(404,b'{}')
        if not self.server.streams.acquire(blocking=False):handle.close();return self.send_body(429,b'{}')
        try:
            start,end=0,s.st_size-1
            header=self.headers.get('Range')
            if header:
                import re
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',header)
                if not match:return self.send_body(416,b'{}')
                start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
                if start>end:return self.send_body(416,b'{}')
            self.send_response(206 if header else 200)
            self.send_header('Content-Length',str(max(0,end-start+1)))
            self.send_header('Content-Type',mimetypes.guess_type(relative)[0] or 'application/octet-stream')
            self.send_header('Accept-Ranges','bytes');self.send_header('ETag',f'"{s.st_size}:{s.st_mtime_ns}"')
            if header:self.send_header('Content-Range',f'bytes {start}-{end}/{s.st_size}')
            self.end_headers()
            if self.command=='HEAD':return
            handle.seek(start);remaining=end-start+1
            while remaining>0:
                data=handle.read(min(CHUNK,remaining))
                if not data:break
                self.wfile.write(data);remaining-=len(data)
        except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
        finally:handle.close();self.server.streams.release()

class Redirect(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        if self.path=='/healthz':
            body=b'{"status":"ok","role":"camera-agent"}'
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)
            return
        self.send_response(302);self.send_header('Location',self.server.target+self.path)
        self.send_header('Content-Length','0');self.end_headers()

def main():
    token=os.environ.get('DRIFT_AGENT_TOKEN','')
    if len(token)<32:raise SystemExit('A provisioned agent token is required')
    inv=CameraInventory(os.environ.get('DRIFT_AGENT_MEDIA_ROOT','/media'))
    threading.Thread(target=inv.run,daemon=True).start()
    server=Gateway(('0.0.0.0',int(os.environ.get('DRIFT_AGENT_PORT','8090'))),inv,token)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(os.environ['DRIFT_AGENT_CERT'],os.environ['DRIFT_AGENT_KEY'])
    server.socket=context.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
    redirect=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('DRIFT_AGENT_REDIRECT_PORT','8080'))),Redirect)
    redirect.target=os.environ['DRIFT_SERVER_URL'].rstrip('/')
    threading.Thread(target=redirect.serve_forever,daemon=True).start()
    try:server.serve_forever()
    finally:inv.stop.set();redirect.shutdown();server.server_close()

if __name__=='__main__':main()
