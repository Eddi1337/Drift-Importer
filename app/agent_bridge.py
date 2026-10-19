"""Loopback-only range reader for FFmpeg; agent credentials never enter argv."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import mimetypes
import re
import threading
from urllib.parse import parse_qs,urlsplit
from .camera_paths import Path
from .camera_agents import AgentUnavailable,is_remote
from .config import get_settings

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def do_HEAD(self):self.do_GET()
    def do_GET(self):
        self.connection.settimeout(45)
        url=urlsplit(self.path);value=parse_qs(url.query).get('path',[''])[0]
        if url.path!='/source' or not is_remote(value):return self.fail(404)
        path=Path(value)
        sent_headers=False
        try:
            size=path.stat().st_size
            start,end=0,size-1
            header=self.headers.get('Range')
            if header:
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',header)
                if not match:return self.fail(416)
                start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
                if start>end:return self.fail(416)
            with path.open('rb') as source:
                source.seek(start)
                self.send_response(206 if header else 200)
                self.send_header('Content-Length',str(max(0,end-start+1)))
                self.send_header('Accept-Ranges','bytes');self.send_header('Content-Type',mimetypes.guess_type(value)[0] or 'application/octet-stream')
                if header:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                sent_headers=True
                if self.command=='HEAD':return
                left=end-start+1
                while left>0:
                    part=source.read(min(256*1024,left))
                    if not part:break
                    self.wfile.write(part);left-=len(part)
        except (AgentUnavailable,OSError,ValueError):
            if not sent_headers:self.fail(503)
            self.close_connection=True
    def fail(self,status):
        self.send_response(status);self.send_header('Content-Length','0');self.end_headers()

_server=None

def start():
    global _server
    if _server:return
    _server=ThreadingHTTPServer(('127.0.0.1',get_settings().agent_bridge_port),Handler)
    _server.daemon_threads=True
    threading.Thread(target=_server.serve_forever,name='drift-agent-source',daemon=True).start()

def stop():
    global _server
    if _server:_server.shutdown();_server.server_close();_server=None
