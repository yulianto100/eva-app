"""Per-user DRM New history. Additive SQLite storage; no document inspection."""
import csv
from datetime import datetime
import io
import re
import uuid

SCOPES=('cluster','subfeeder','feeder')
POLL_INTERVAL=60


def parse_new_rows(text,scope):
    if scope not in SCOPES or not isinstance(text,str) or len(text)>5_000_000:
        raise ValueError('Daftar DRM tidak valid.')
    reader=csv.DictReader(io.StringIO(text),strict=True)
    normalize=lambda k:str(k or'').strip().casefold().replace(' ','_').replace('-','_')
    headers=[normalize(k)for k in(reader.fieldnames or[])]
    required={'uuid','number','latest_status_label','created_at','target_'+scope+'_code'}
    if len(set(headers))!=len(headers) or not required.issubset(headers):
        raise ValueError('Kontrak daftar DRM tidak lengkap.')
    found={}
    for count,raw in enumerate(reader,1):
        if count>50_000 or None in raw or any(value is None for value in raw.values()):
            raise ValueError('Baris DRM tidak lengkap.')
        row={normalize(k):v.strip()for k,v in raw.items()}
        if row.get('latest_status_label','').casefold()!='new':continue
        if row.get('is_deleted','0') not in {'0','1','false','true'}:
            raise ValueError('Status hapus DRM tidak valid.')
        if row.get('is_deleted','0')in{'1','true'}:continue
        if row.get('latest_status_name','new').casefold()!='new':
            raise ValueError('Status DRM bertentangan.')
        item={'scope':scope,'wo_uuid':row['uuid'],'code':row['target_'+scope+'_code'],
              'name':(row.get('target_feeder_label')if scope=='feeder'else '')or row.get('target_'+scope+'_name',''),
              'work_order_number':row['number'],'requested_at':row['created_at']}
        if (not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',item['wo_uuid'])
                or any(not value or len(value)>512 or any(ord(c)<32 for c in value)for value in item.values())):
            raise ValueError('Identitas DRM tidak valid.')
        item['wo_uuid']=str(uuid.UUID(item['wo_uuid']))
        date=datetime.strptime(item['requested_at'],'%Y-%m-%d %H:%M:%S')
        if date.strftime('%Y-%m-%d %H:%M:%S')!=item['requested_at']:
            raise ValueError('Tanggal DRM tidak valid.')
        if item['wo_uuid']in found and found[item['wo_uuid']]!=item:
            raise ValueError('Identitas DRM ambigu.')
        found[item['wo_uuid']]=item
    return list(found.values())


class NotificationStore:
    def __init__(self, connect):
        self.connect=connect
        with connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS drm_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                scope TEXT NOT NULL, wo_uuid TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT NOT NULL, work_order_number TEXT NOT NULL,
                requested_at TEXT NOT NULL, detected_at REAL NOT NULL,
                UNIQUE(user_id,account,scope,wo_uuid)
            );
            CREATE INDEX IF NOT EXISTS drm_notifications_owner_id ON drm_notifications(user_id,id);
            CREATE TABLE IF NOT EXISTS drm_notification_polls (
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                checked_at REAL NOT NULL, PRIMARY KEY(user_id,account)
            );
            ''')

    def record(self,user_id,link,items,now):
        created=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owned=db.execute("SELECT l.username,l.session_id FROM astri_links l JOIN users u ON u.id=l.user_id JOIN resources r ON r.kind='astri' AND r.reference=l.session_id AND r.user_id=l.user_id WHERE l.user_id=? AND u.active=1",(user_id,)).fetchone()
            if not owned or dict(owned)!=link:
                raise PermissionError('Tautan ASTRI berubah. Muat ulang EVA.')
            for item in items:
                result=db.execute('INSERT INTO drm_notifications(user_id,account,scope,wo_uuid,code,name,work_order_number,requested_at,detected_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,account,scope,wo_uuid) DO NOTHING',
                    (user_id,link['username'],item['scope'],item['wo_uuid'],item['code'],item['name'],item['work_order_number'],item['requested_at'],now))
                if result.rowcount:
                    created.append(dict(item,id=result.lastrowid,detected_at=now))
            db.execute('INSERT INTO drm_notification_polls VALUES(?,?,?) ON CONFLICT(user_id,account) DO UPDATE SET checked_at=excluded.checked_at',(user_id,link['username'],now))
        return created

    def last_checked(self,user_id,account):
        with self.connect() as db:
            row=db.execute('SELECT checked_at FROM drm_notification_polls WHERE user_id=? AND account=?',(user_id,account)).fetchone()
        return row['checked_at']if row else None

    def history(self,user_id,before=None,limit=30):
        with self.connect() as db:
            total=db.execute('SELECT count(*) FROM drm_notifications WHERE user_id=?',(user_id,)).fetchone()[0]
            query='SELECT id,scope,code,name,work_order_number,requested_at,detected_at FROM drm_notifications WHERE user_id=?'
            args=[user_id]
            if before is not None:query+=' AND id<?';args.append(before)
            rows=db.execute(query+' ORDER BY id DESC LIMIT ?',(*args,limit+1)).fetchall()
        return {'items':[dict(r)for r in rows[:limit]],'total':total,'next_before':rows[limit-1]['id']if len(rows)>limit else None}


def parse_new_list(text,scope):
    from html.parser import HTMLParser
    if scope not in SCOPES:raise ValueError('Scope DRM tidak valid.')
    def key(value):return re.sub(r'(?:-|_)column$','',re.sub(r'^work-order-','',value)).replace('-','_')
    class Table(HTMLParser):
        def __init__(self):
            super().__init__();self.active=False;self.tables=0;self.rows=[];self.headers=set();self.row=None;self.cell=None;self.parts=[];self.uuids=set()
        def handle_starttag(self,tag,attrs):
            attr=dict(attrs)
            if tag=='table' and attr.get('id')=='work-order-table':self.active=True;self.tables+=1
            if not self.active:return
            if tag=='tr':self.row={};self.uuids=set()
            if tag in {'td','th'}:
                names=[key(c)for c in attr.get('class','').split()if c.startswith('work-order-')]
                self.cell=names[0]if len(names)==1 else None;self.parts=[]
                if tag=='th' and self.cell:self.headers.add(self.cell)
            if tag=='input' and self.row is not None:
                match=re.fullmatch(r'work_order\[([^]]+)\]\[(?:number|target_cluster_code|target_subfeeder_code|target_osp_route_code)\]',attr.get('name',''))
                if match:self.uuids.add(match[1])
        def handle_data(self,data):
            if self.active and self.cell:self.parts.append(data)
        def handle_endtag(self,tag):
            if not self.active:return
            if tag in {'td','th'}:
                if tag=='td' and self.cell and self.row is not None:
                    value=' '.join(''.join(self.parts).split())
                    if self.cell in self.row:raise ValueError('Kolom DRM duplikat.')
                    self.row[self.cell]=value
                self.cell=None
            if tag=='tr':
                if self.row and self.row.get('number'):
                    if self.row.get('latest_status_label','').casefold()=='new':
                        if len(self.uuids)!=1:raise ValueError('UUID DRM ambigu.')
                        self.row['uuid']=next(iter(self.uuids))
                    self.rows.append(self.row)
                elif self.row and 'number'in self.row:raise ValueError('Nomor DRM kosong.')
                self.row=None
            if tag=='table':self.active=False
    table=Table();table.feed(text);table.close()
    native='target_osp_route_'if scope=='feeder'else 'target_'+scope+'_'
    required={'number','latest_status_label','created_at',native+'code',native+('label'if scope=='feeder'else'name')}
    if table.tables!=1 or table.active or table.row is not None or not required.issubset(table.headers):raise ValueError('Daftar DRM tidak lengkap.')
    rows=[]
    for row in table.rows:
        if not required.issubset(row):raise ValueError('Baris DRM tidak lengkap.')
        row=dict(row)
        if scope=='feeder':
            row['target_feeder_code']=row['target_osp_route_code'];row['target_feeder_label']=row['target_osp_route_label']
        rows.append(row)
    fields=sorted({'uuid',*required,'target_'+scope+'_code'}.union(*(set(r)for r in rows)))
    output=io.StringIO();writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    return parse_new_rows(output.getvalue(),scope),len(rows)


def fetch_new_rows(record,base,scope):
    from urllib.parse import urlsplit
    url=base.rstrip('/')+'/material/'+scope+'/drm/list/v3'
    found={};seen_pages=set()
    for page in range(1,51):
        response=record['session'].get(url,params={'page':page,'limit':100,'filter[latest_status_name]':'new'},timeout=(5,20),allow_redirects=False,stream=True)
        try:
            actual=urlsplit(response.url);expected=urlsplit(url)
            if response.status_code!=200 or(actual.scheme,actual.netloc,actual.path)!=(expected.scheme,expected.netloc,expected.path):
                raise ValueError('Daftar DRM tidak tersedia.')
            body=bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body)>5_000_000:raise ValueError('Daftar DRM terlalu besar.')
            items,count=parse_new_list(body.decode(response.encoding or'utf-8-sig'),scope)
        finally:response.close()
        if count and bytes(body)in seen_pages:raise ValueError('Halaman DRM berulang.')
        seen_pages.add(bytes(body))
        for item in items:
            identity=item['wo_uuid']
            if identity in found and found[identity]!=item:raise ValueError('Identitas DRM ambigu.')
            found[identity]=item
        if count<100:return list(found.values())
    raise ValueError('Daftar DRM melebihi batas pemeriksaan.')


def register_drm_notifications(app,env,clock=None):
    import fcntl
    import hashlib
    import os
    from pathlib import Path
    import time
    from flask import g,jsonify,request

    clock=clock or time.time
    gate=env['EVA_AUTH']
    store=NotificationStore(gate.store.connect)
    app.extensions['eva_drm_notifications']=store

    def owner():
        user=getattr(g,'eva_user',None)
        return user.get('id')if isinstance(user,dict)else None

    def history():
        uid=owner()
        if not uid:return jsonify({'code':'EVA_LOGIN_REQUIRED'}),401
        try:
            limit=int(request.args.get('limit','30'));before=request.args.get('before')
            if before is not None:before=int(before)
            if not 1<=limit<=100 or before is not None and not 1<=before<=9223372036854775807:
                raise ValueError('pagination')
        except (ValueError,TypeError):
            return jsonify({'error':'Halaman riwayat tidak valid.'}),400
        return jsonify(store.history(uid,before,limit))

    def poll():
        uid=owner()
        if not uid:return jsonify({'code':'EVA_LOGIN_REQUIRED'}),401
        link=gate.store.astri_link(uid)
        if not link:
            return jsonify({'ok':True,'linked':False,'new_items':[],'total':store.history(uid)['total'],'poll_interval':POLL_INTERVAL})
        # Shared lock is separate from ASTRI renewal's lock; never nest that lock.
        directory=Path(gate.store.path).parent/'drm_notification_locks'
        directory.mkdir(mode=0o700,exist_ok=True)
        path=directory/(hashlib.sha256(uid.encode()).hexdigest()+'.lock')
        with path.open('a') as handle:
            os.chmod(path,0o600)
            try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return jsonify({'ok':True,'linked':True,'checked':False,'new_items':[],'total':store.history(uid)['total'],'poll_interval':POLL_INTERVAL})
            try:
                now=clock();previous=store.last_checked(uid,link['username'])
                if previous is not None and now-previous<POLL_INTERVAL:
                    return jsonify({'ok':True,'linked':True,'checked':False,'new_items':[],'total':store.history(uid)['total'],'last_checked_at':previous,'poll_interval':POLL_INTERVAL})
                record=env['require_astri_session']()
                if record.get('username')!=link['username'] or record.get('_session_id')!=link['session_id']:
                    raise PermissionError('Tautan berubah.')
                items=[]
                for scope in SCOPES:items.extend(fetch_new_rows(record,env['ASTRI_BASE_URL'],scope))
                created=store.record(uid,link,items,clock())
                return jsonify({'ok':True,'linked':True,'checked':True,'new_items':created,'total':store.history(uid)['total'],'last_checked_at':store.last_checked(uid,link['username']),'poll_interval':POLL_INTERVAL})
            except PermissionError:
                return jsonify({'ok':False,'code':'DRM_LINK_CHANGED','error':'Tautan ASTRI berubah. Muat ulang EVA.'}),409
            except Exception:
                # Upstream exception/URL/body can contain credentials. Never echo/log it.
                return jsonify({'ok':False,'code':'DRM_UPSTREAM_UNAVAILABLE','error':'DRM belum dapat diperiksa. Riwayat tetap tersimpan.'}),503
            finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    app.add_url_rule('/api/astri/drm-notifications','drm_notifications_history',history,methods=['GET'])
    app.add_url_rule('/api/astri/drm-notifications/poll','drm_notifications_poll',poll,methods=['POST'])
