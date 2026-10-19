#!/usr/bin/env python3
"""Run inside the server environment. Read the pairing token only from stdin."""
import argparse
import json
import re
import sys
from pathlib import Path

from app.camera_agents import CameraAgent, _config, _client, validate_manifest
from app.crypto import encrypt
from app.database import init_db, session_scope


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('id','name','url','root','ca'):p.add_argument('--'+name,required=True)
    a=p.parse_args()
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,62}',a.id):p.error('Use a short lowercase agent id')
    if not Path(a.ca).is_file():p.error('Pinned agent certificate is missing')
    token=sys.stdin.read(4096).strip()
    if len(token)<32 or '\n' in token:p.error('Provide the provisioned token over private stdin')
    row=CameraAgent(id=a.id,name=a.name,base_url=a.url,virtual_root=a.root,
                    token_enc=encrypt(token),ca_path=a.ca,enabled=True,manifest='{}')
    cfg=_config(row)
    with _client(cfg) as client:
        response=client.get(a.url.rstrip('/')+'/manifest');response.raise_for_status()
        clean=validate_manifest(response.json())
    init_db()
    with session_scope() as s:
        old=s.get(CameraAgent,a.id)
        if old and old.virtual_root!=a.root:raise SystemExit('An existing agent namespace cannot be changed')
        row.manifest=json.dumps(clean,separators=(',',':'))
        s.merge(row)
    print('Camera agent paired; TLS identity, authentication and inventory verified')


if __name__=='__main__':main()
