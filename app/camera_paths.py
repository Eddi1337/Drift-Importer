"""Path interface for read-only camera files; metadata reads use the RAM cache.

Local NAS paths retain normal pathlib behavior. Remote camera bytes are streamed
only when a worker or a preview explicitly opens a file; never copied to the Pi.
"""
from __future__ import annotations
import errno
import fnmatch
import os
from pathlib import Path as LocalPath
import stat as statmod
from types import SimpleNamespace
from .config import get_settings

class Path(type(LocalPath())):
    def _remote(self):
        from .camera_agents import lookup
        return lookup(self)
    def resolve(self,strict=False):
        if self._remote():
            value=Path(os.path.abspath(self))
            if strict and not value.exists():raise FileNotFoundError(str(self))
            return value
        return super().resolve(strict=strict)
    def stat(self,*,follow_symlinks=True):
        found=self._remote()
        if not found:return super().stat(follow_symlinks=follow_symlinks)
        from .camera_agents import require_online
        node,relative=found;require_online(node)
        item=node['files'].get(relative)
        directory=relative in node['dirs'] or not relative
        if not item and not directory:
            from .camera_agents import AgentUnavailable
            if not any(relative==d['path'] or relative.startswith(d['path']+'/') for d in node['manifest']['devices']):
                raise AgentUnavailable('Camera is disconnected; its pending uploads will resume')
            raise FileNotFoundError(str(self))
        item=item or {'size':0,'mtime_ns':0,'ctime_ns':0,'inode':'0','device':'0'}
        return SimpleNamespace(st_size=item['size'],st_mtime_ns=item['mtime_ns'],st_ctime_ns=item['ctime_ns'],
            st_mtime=item['mtime_ns']/1e9,st_ctime=item['ctime_ns']/1e9,st_atime=0,st_mode=(statmod.S_IFDIR|0o555) if directory else (statmod.S_IFREG|0o444),
            st_ino=int(item['inode']),st_dev=int(item['device']),st_nlink=1,st_uid=0,st_gid=0)
    def lstat(self):return self.stat(follow_symlinks=False)
    def exists(self,*,follow_symlinks=True):
        if not self._remote():return super().exists()
        try:self.stat();return True
        except FileNotFoundError:return False
    def is_file(self):
        if not self._remote():return super().is_file()
        try:return statmod.S_ISREG(self.stat().st_mode)
        except FileNotFoundError:return False
    def is_dir(self):
        if not self._remote():return super().is_dir()
        try:return statmod.S_ISDIR(self.stat().st_mode)
        except FileNotFoundError:return False
    def is_symlink(self):return False if self._remote() else super().is_symlink()
    def iterdir(self):
        found=self._remote()
        if not found:yield from super().iterdir();return
        from .camera_agents import require_online
        node,relative=found;require_online(node)
        if not self.is_dir():raise NotADirectoryError(str(self))
        prefix=relative+'/' if relative else ''
        children={p[len(prefix):].split('/')[0] for p in set(node['files'])|node['dirs'] if p.startswith(prefix) and p!=relative}
        for name in sorted(children):yield self/name
    def rglob(self,pattern,**kwargs):
        if not self._remote():yield from super().rglob(pattern);return
        for child in self.iterdir():
            if fnmatch.fnmatch(child.name,pattern):yield child
            if child.is_dir():yield from child.rglob(pattern)
    def glob(self,pattern,**kwargs):
        if not self._remote():yield from super().glob(pattern);return
        if pattern.startswith('**/'):
            yield from self.rglob(pattern[3:]);return
        for child in self.iterdir():
            if fnmatch.fnmatch(child.name,pattern):yield child
    def open(self,mode='r',buffering=-1,encoding=None,errors=None,newline=None):
        if not self._remote():return super().open(mode,buffering,encoding,errors,newline)
        if mode!='rb':raise OSError(errno.EROFS,'Camera agents are read-only')
        from .camera_agents import RemoteFile
        return RemoteFile(self)
    def mkdir(self,*a,**k):
        if self._remote():raise OSError(errno.EROFS,'Camera agents are read-only')
        return super().mkdir(*a,**k)
    def unlink(self,*a,**k):
        if self._remote():raise OSError(errno.EROFS,'Camera agents are read-only')
        return super().unlink(*a,**k)
    def rename(self,target):
        if self._remote():raise OSError(errno.EROFS,'Camera originals are read-only; edit the NAS copy')
        return super().rename(target)

def walk(root, onerror=None):
    root=Path(root)
    if not root._remote():
        yield from os.walk(root,followlinks=False,onerror=onerror);return
    children=list(root.iterdir());dirs=[p.name for p in children if p.is_dir()];files=[p.name for p in children if p.is_file()]
    yield str(root),dirs,files
    for name in dirs:yield from walk(root/name,onerror=onerror)

def source_url(path):
    from .camera_agents import is_remote,lookup,require_online
    if not is_remote(path):return str(path)
    found=lookup(path);require_online(found[0])
    from urllib.parse import quote
    return 'http://127.0.0.1:'+str(get_settings().agent_bridge_port)+'/source?path='+quote(str(path),safe='')
