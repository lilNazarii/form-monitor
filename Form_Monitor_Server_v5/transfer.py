"""Offline transfer of one account's monitor data. Never exports credentials."""
import argparse
import json
import re
import sqlite3
import zipfile
from pathlib import Path

SAFE=re.compile(r'(workspace/profiles\.json|monitors/[a-f0-9]{32}/reports/MONITOR-[a-f0-9]{32}/(report\.json|page\.png)|monitors/[a-f0-9]{32}/service/[0-9]+/events\.jsonl)')

def account_data(root,email):
    db=sqlite3.connect(f'file:{(root/"results/accounts.sqlite3").as_posix()}?mode=ro',uri=True)
    try:row=db.execute('SELECT id FROM users WHERE email=?',(email.strip().lower(),)).fetchone()
    finally:db.close()
    if not row:raise ValueError('Account not found. Create it first.')
    return root/'results/users'/row[0]/'results'

def export_data(root,email,archive):
    folder=account_data(root,email)
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob('*'):
            if p.is_file() and SAFE.fullmatch(p.relative_to(folder).as_posix()):z.write(p,p.relative_to(folder).as_posix())

def import_data(root,email,archive):
    folder=account_data(root,email)
    existing=folder/'workspace/profiles.json'
    if existing.exists() and json.loads(existing.read_text()):raise ValueError('Destination account must have no monitors.')
    with zipfile.ZipFile(archive) as z:
        items=z.infolist()
        if len(items)>10000 or sum(i.file_size for i in items)>250*1024*1024:raise ValueError('Archive too large.')
        if len(set(i.filename for i in items))!=len(items) or any(not SAFE.fullmatch(i.filename) for i in items):raise ValueError('Unexpected archive content.')
        profiles=json.loads(z.read('workspace/profiles.json'))
        if not isinstance(profiles,list) or len(profiles)>200:raise ValueError('Invalid profiles.')
        for p in profiles:
            if not re.fullmatch('[a-f0-9]{32}',p['id']):raise ValueError('Invalid monitor ID.')
            p['connection_id']='';p['revision']=int(p['revision'])+1
        for item in items:
            if item.filename=='workspace/profiles.json':continue
            target=folder/item.filename;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(item))
        existing.parent.mkdir(parents=True,exist_ok=True);existing.write_text(json.dumps(profiles,ensure_ascii=False,indent=2),encoding='utf-8')
    # No schedules, tokens, passwords or prior notification-state files are imported.

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['export','import']);p.add_argument('--root',type=Path,required=True);p.add_argument('--email',required=True);p.add_argument('--archive',type=Path,required=True);a=p.parse_args()
    if a.action=='export':export_data(a.root,a.email,a.archive)
    else:import_data(a.root,a.email,a.archive)
    print('Transfer complete. Imported monitors need a public URL and a new Google connection. Restart the server after import.')
