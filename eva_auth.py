try:
    from eva_email import send_welcome_email_async
except ImportError:
    send_welcome_email_async = None
"""EVA Google identity and revocable app sessions. Never persists Google tokens."""
import hashlib
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
import base64
import json
from urllib.parse import unquote, urlsplit, urlencode

import requests
from flask import abort, g, has_request_context, jsonify, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request as GoogleRequest
from google.auth.exceptions import GoogleAuthError
from google.oauth2.id_token import verify_oauth2_token


def token_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_next(value):
    if not isinstance(value,str) or len(value)>2048:return '/'
    decoded=value
    for _ in range(4):
        next_value=unquote(decoded)
        if next_value==decoded:break
        decoded=next_value
    if '%' in decoded:return '/'
    if any(part in {'.','..'} for part in urlsplit(decoded).path.split('/')):return '/'
    if any(c in decoded for c in ('\\','\r','\n','\x00')) or any(ord(c)<32 for c in decoded):return '/'
    url=urlsplit(decoded)
    if not decoded.startswith('/') or decoded.startswith('//') or url.scheme or url.netloc:return '/'
    if decoded.startswith(('/auth/','/login')):return '/'
    return value


class AuthStore:
    def __init__(self, path, owner_emails=()):
        self.path=Path(path)
        self.owner_emails={v.strip().casefold() for v in owner_emails}
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
                last_login REAL NOT NULL, last_seen REAL NOT NULL, login_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL, expires_at REAL NOT NULL, csrf TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owner_subjects (
                email TEXT PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS resources (
                kind TEXT NOT NULL, reference TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
                PRIMARY KEY(kind,reference)
            );
            CREATE TABLE IF NOT EXISTS oauth_transactions (
                digest TEXT PRIMARY KEY, browser_digest TEXT NOT NULL, origin TEXT NOT NULL,
                nonce TEXT NOT NULL, verifier TEXT NOT NULL, next_path TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS astri_links (
                user_id TEXT PRIMARY KEY REFERENCES users(id), username TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE
            );
            ''')
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def login(self, claims):
        if not isinstance(claims,dict) or claims.get('email_verified') is not True:
            raise ValueError('Identitas Google belum terverifikasi.')
        sub,email,name=claims.get('sub'),claims.get('email'),claims.get('name','')
        if (not isinstance(sub,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}',sub)
                or not isinstance(email,str) or len(email)>254
                or not re.fullmatch(r'[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+',email)
                or not isinstance(name,str)):
            raise ValueError('Identitas Google tidak valid.')
        now=time.time();email=email.casefold();name=(name or email)[:160]
        authoritative=email.endswith(('@gmail.com','@googlemail.com')) or (
            isinstance(claims.get('hd'),str) and claims['hd'].casefold()==email.rpartition('@')[2])
        raw=secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT active FROM users WHERE google_sub=?',(sub,)).fetchone()
            if existing and not existing['active']:
                raise PermissionError('Akun EVA dinonaktifkan.')
            db.execute('DELETE FROM sessions WHERE expires_at<?',(now,))
            role='user'
            if email in self.owner_emails and authoritative:
                db.execute('INSERT OR IGNORE INTO owner_subjects VALUES(?,?)',(email,sub))
                pin=db.execute('SELECT google_sub FROM owner_subjects WHERE email=?',(email,)).fetchone()
                if pin and pin['google_sub']==sub:role='owner'
            # Deteksi apakah registrasi baru (user belum ada di database)
            is_new_user = db.execute('SELECT 1 FROM users WHERE google_sub=?',(sub,)).fetchone() is None
            db.execute('INSERT INTO users(id,google_sub,email,name,role,created_at,last_login,last_seen) '
                       'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(google_sub) DO UPDATE SET '
                       'email=excluded.email,name=excluded.name,role=excluded.role,last_login=excluded.last_login,'
                       'last_seen=excluded.last_seen,login_count=users.login_count+1',
                       (uuid.uuid4().hex,sub,email,name,role,now,now,now))
            user=db.execute('SELECT * FROM users WHERE google_sub=?',(sub,)).fetchone()
            if is_new_user and send_welcome_email_async:
                send_welcome_email_async(email, name)
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (hashlib.sha256(raw.encode()).hexdigest(),user['id'],now,now+7*86400,secrets.token_urlsafe(32)))
        return dict(user),raw

    def current(self, raw):
        if not isinstance(raw,str) or len(raw)>128:return None
        now=time.time()
        with self.connect() as db:
            user=db.execute('SELECT u.*,s.csrf,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id '
                            'WHERE s.digest=? AND s.expires_at>? AND u.active=1',
                            (hashlib.sha256(raw.encode()).hexdigest(),now)).fetchone()
            if user:db.execute('UPDATE users SET last_seen=? WHERE id=? AND last_seen<?',
                               (now,user['id'],now-60))
        return dict(user) if user else None

    def logout(self, raw):
        if isinstance(raw,str) and len(raw)<=128:
            with self.connect() as db:
                db.execute('DELETE FROM sessions WHERE digest=?',(token_digest(raw),))

    def set_active(self, user_id, active):
        if type(active) is not bool:raise ValueError('Status akun tidak valid.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user=db.execute('SELECT role FROM users WHERE id=?',(user_id,)).fetchone()
            if not user:raise FileNotFoundError('Pengguna tidak ditemukan.')
            if user['role']=='owner':raise PermissionError('Akun owner tidak dapat dinonaktifkan.')
            db.execute('UPDATE users SET active=? WHERE id=?',(int(active),user_id))
            if not active:db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))

    def set_plan(self, user_id, plan, plan_until):
        valid_plans = {'', 'free', 'standar', 'premium'}
        plan_str = str(plan or '').strip().lower()
        if plan_str not in valid_plans:
            raise ValueError('Paket tidak valid. Pilihan: free, standar, premium.')
        if plan_str in {'', 'free'}:
            plan_str = ''
            until_val = 0.0
        else:
            try:
                until_val = float(plan_until)
            except (TypeError, ValueError):
                raise ValueError('Masa aktif tidak valid.')
            if until_val < 0:
                raise ValueError('Masa aktif tidak valid.')

        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
            if not user:
                raise FileNotFoundError('Pengguna tidak ditemukan.')
            if user['role'] == 'owner':
                raise PermissionError('Akun owner tidak memerlukan paket.')
            db.execute('UPDATE users SET plan=?, plan_until=? WHERE id=?', (plan_str, until_val, user_id))

    def claim(self, kind, reference, user_id):
        if not all(isinstance(v,str) and 0<len(v)<=512 for v in (kind,reference,user_id)):
            raise ValueError('Identitas resource tidak valid.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO resources VALUES(?,?,?)',(kind,reference,user_id))
            row=db.execute('SELECT user_id FROM resources WHERE kind=? AND reference=?',(kind,reference)).fetchone()
            if row['user_id']!=user_id:raise PermissionError('Resource sudah dimiliki akun lain.')

    def resource_owner(self, kind, reference):
        with self.connect() as db:
            row=db.execute('SELECT user_id FROM resources WHERE kind=? AND reference=?',(kind,reference)).fetchone()
        return row['user_id'] if row else None

    def user_active(self, user_id):
        with self.connect() as db:
            row=db.execute('SELECT active FROM users WHERE id=?',(user_id,)).fetchone()
        return bool(row and row['active'])

    def can_access(self, kind, reference, user):
        if not user or not self.user_active(user.get('id')):return False
        # Owner administration of conversion jobs does not authorize borrowing
        # another account's upstream credentials or private cache tokens.
        if kind=='job' and user.get('role')=='owner':return True
        return self.resource_owner(kind,reference)==user.get('id')

    def astri_link(self, user_id):
        # The permanent identity survives missing/revoked SIDs. Transport callers
        # must independently check resource ownership and active-user status.
        with self.connect() as db:
            row=db.execute('SELECT username,session_id FROM astri_links WHERE user_id=?',(user_id,)).fetchone()
        return dict(row) if row else None

    def link_astri(self, user_id, username, sid):
        if not all(isinstance(v,str) and 0<len(v)<=255 for v in (user_id,username,sid)):
            raise ValueError('Identitas akun ASTRI tidak valid.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            user=db.execute('SELECT active FROM users WHERE id=?',(user_id,)).fetchone()
            resource=db.execute("SELECT user_id FROM resources WHERE kind='astri' AND reference=?",(sid,)).fetchone()
            if not user or not user['active'] or not resource or resource['user_id']!=user_id:
                raise PermissionError('Sesi ASTRI bukan milik pengguna aktif.')
            current=db.execute('SELECT username FROM astri_links WHERE user_id=?',(user_id,)).fetchone()
            if current and current['username']!=username:
                raise PermissionError('Akun ASTRI terikat permanen pada akun EVA; gunakan username yang sama.')
            db.execute('INSERT INTO astri_links VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id',
                       (user_id,username,sid))

    def unlink_astri(self, user_id):
        raise PermissionError('Akun ASTRI terikat permanen pada akun EVA; tautan tidak dapat dilepas.')

    def begin_oauth(self, origin, next_path='/'):
        state,browser,nonce,verifier=(secrets.token_urlsafe(32) for _ in range(4))
        target=safe_next(next_path);now=time.time()
        with self.connect() as db:
            db.execute('DELETE FROM oauth_transactions WHERE expires_at<?',(now,))
            db.execute('INSERT INTO oauth_transactions VALUES(?,?,?,?,?,?,?)',
                       (token_digest(state),token_digest(browser),origin,nonce,verifier,target,now+600))
        return {'state':state,'browser':browser,'nonce':nonce,'verifier':verifier,'next':target}

    def consume_oauth(self, state, browser, origin):
        if not all(isinstance(v,str) and 0<len(v)<=512 for v in (state,browser,origin)):
            raise ValueError('Sesi login tidak valid.')
        digest=token_digest(state)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM oauth_transactions WHERE digest=?',(digest,)).fetchone()
            if (not row or row['expires_at']<=time.time() or row['origin']!=origin
                    or not secrets.compare_digest(row['browser_digest'],token_digest(browser))):
                raise ValueError('Sesi login kedaluwarsa atau tidak valid.')
            db.execute('DELETE FROM oauth_transactions WHERE digest=?',(digest,))
        return {'nonce':row['nonce'],'verifier':row['verifier'],'next':row['next_path']}

    def summary(self):
        now=time.time();today=now-(now+7*3600)%86400
        with self.connect() as db:
            return dict(db.execute('SELECT count(*) total,COALESCE(sum(last_seen>=?),0) active_today,'
                                   'COALESCE(sum(active=1 AND last_seen>=?),0) online FROM users',
                                   (today,now-300)).fetchone())


COOKIE='__Host-eva-auth'
FLOW_COOKIE='__Host-eva-google-flow'
ALLOWED_HOSTS={'eva.dyena.id','dashboard.dyena.id','eva-mrdesign.my.id','dashboard.eva-mrdesign.my.id'}
PUBLIC_PATHS={'/login','/auth/google/start','/auth/google/callback','/healthz','/readyz','/favicon.ico','/tutorial-preview'}


class AuthGate:
    def __init__(self, app):
        self.app=app
        self.store=AuthStore(app.config.get('EVA_AUTH_DB',str(Path(app.root_path)/'data/users.db')),
                             app.config.get('EVA_OWNER_EMAILS',()))
        path=Path(app.config.get('EVA_OAUTH_CLIENT_FILE',str(Path(app.root_path)/'.secrets/google-login-web.json')))
        self.client={}
        asset_file=Path(__file__).with_name('eva_auth_assets.json')
        self.public_assets=set(json.loads(asset_file.read_text()))
        self.policies=json.loads(Path(__file__).with_name('eva_auth_policy.json').read_text())
        try:
            raw=json.loads(path.read_text())
            node=raw.get('web') if isinstance(raw,dict) else None
            if isinstance(node,dict) and node.get('client_id') and node.get('client_secret'):
                self.client=node
        except (OSError,ValueError):
            pass

    def origin(self):
        host=request.host.partition(':')[0].casefold()
        return 'https://'+host if host in ALLOWED_HOSTS else None

    def require_login(self):
        g.eva_user=None
        path=request.path
        # Only bounded health probes/static assets work without a canonical host.
        if path not in {'/healthz','/readyz'} and not path.startswith('/static/') and not self.origin():
            return jsonify({'error':'Gunakan https://eva.dyena.id untuk masuk EVA.'}),400
        rule=self.policies.get(request.endpoint,{})
        policy=rule.get('policy','')
        public_static=path in self.public_assets and request.endpoint=='static'
        public=policy in {'PUBLIC_LOGIN','PUBLIC_PROBE'} or request.endpoint=='favicon' or public_static
        if request.method in {'GET','HEAD'} and public:
            return None
        g.eva_user=self.store.current(request.cookies.get(COOKIE,''))
        if not g.eva_user:
            page=(path=='/' or not path.startswith(('/api/','/status/','/download/','/jobs/','/preview/'))
                  and request.accept_mimetypes.best=='text/html')
            if request.method in {'GET','HEAD'} and page:
                return redirect(url_for('eva_login',next=safe_next(request.full_path.rstrip('?'))))
            return jsonify({'error':'Masuk dengan Google untuk memakai EVA.','code':'EVA_LOGIN_REQUIRED',
                            'login_url':'/login'}),401
        user=g.eva_user
        disabled=(policy=='DISABLED' or path=='/api/astri/magic-token' or path.startswith('/astri/magic-login/')
                  or path=='/admin/password' or path=='/admin/login' and request.method=='POST')
        if disabled:return jsonify({'error':'Fitur ini tidak tersedia.'}),410
        if not rule:return jsonify({'error':'Halaman tidak ditemukan.'}),404
        if request.method not in rule['methods']:
            return jsonify({'error':'Metode request tidak diizinkan.'}),405
        if request.method not in {'GET','HEAD'}:
            actual=request.headers.get('X-EVA-CSRF','') or request.form.get('eva_csrf','')
            origin=request.headers.get('Origin')
            if not actual or not secrets.compare_digest(user['csrf'],actual) or origin and origin!=self.origin():
                return jsonify({'error':'Sesi request tidak valid. Muat ulang EVA.','code':'EVA_CSRF_REQUIRED'}),403
        if policy.startswith('OWNER_') and user['role']!='owner':
            return jsonify({'error':'Menu ini khusus owner EVA.','code':'EVA_OWNER_REQUIRED'}),403
        if request.endpoint=='static':
            if not path.startswith('/static/downloads/') or user['role']!='owner':
                return jsonify({'error':'File tidak tersedia.'}),404
        reference=(request.view_args or {}).get('job_id')
        if policy=='SELF_ASTRI_BODY_JOB':
            reference=request.form.get('convert_job_id','').strip() or reference
        if reference and policy in {'SELF_JOB','SELF_JOB_ASTRI','SELF_ASTRI_BODY_JOB'}:
            if not self.store.can_access('job',reference,user):
                return jsonify({'error':'Pekerjaan tidak ditemukan.'}),404
        if session.get('eva_user_id')!=user['id']:
            session.clear();session['eva_user_id']=user['id']
        return None

    def create_job(self, creator, job_id, *args, **kwargs):
        if has_request_context():
            user=getattr(g,'eva_user',None)
            if not user:abort(401)
            self.store.claim('job',job_id,user['id'])
            kwargs['owner_user_id']=user['id']
            kwargs['owner_name']=user['name']
            kwargs['owner_email']=user['email']
            sid=session.get('astri_session_id','')
            kwargs['astri_session_id']=sid if sid and self.store.can_access('astri',sid,user) else ''
        return creator(job_id,*args,**kwargs)

    def get_job(self, getter, job_id):
        if has_request_context():
            if not self.store.can_access('job',job_id,getattr(g,'eva_user',None)):return None
        return getter(job_id)

    def list_jobs(self, getter, limit=50):
        if not has_request_context():return getter(limit)
        user=getattr(g,'eva_user',None)
        if not user:return []
        if user['role']=='owner':return getter(limit)
        with self.store.connect() as db:
            allowed={row['reference'] for row in db.execute(
                "SELECT reference FROM resources WHERE kind='job' AND user_id=?",(user['id'],))}
        # The existing store already scans/sorts all jobs. Do not truncate
        # another account's rows before filtering this user's resources.
        return [row for row in getter(2**31-1) if row.get('job_id') in allowed][:limit]

    def users(self):
        if g.eva_user['role']!='owner':abort(403)
        with self.store.connect() as db:
            rows=[dict(row) for row in db.execute('SELECT id,email,name,role,active,created_at,last_login,last_seen,'
                                                  'login_count,plan,plan_until FROM users ORDER BY last_seen DESC LIMIT 1000')]
        return jsonify({'users':rows,'summary':self.store.summary()})

    def user_plan(self, user_id):
        if g.eva_user['role'] != 'owner':
            abort(403)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({'error': 'Data permintaan tidak valid.'}), 400
        plan = payload.get('plan')
        plan_until = payload.get('plan_until')
        try:
            self.store.set_plan(user_id, plan, plan_until)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except PermissionError as e:
            return jsonify({'error': str(e)}), 403
        except FileNotFoundError:
            return jsonify({'error': 'Pengguna tidak ditemukan.'}), 404
        return jsonify({'ok': True})

    def user_status(self, user_id):
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict) or type(payload.get('active')) is not bool:
            return jsonify({'error':'Status akun tidak valid.'}),400
        try:self.store.set_active(user_id,payload['active'])
        except PermissionError:return jsonify({'error':'Akun owner tidak dapat dinonaktifkan.'}),403
        except FileNotFoundError:return jsonify({'error':'Pengguna tidak ditemukan.'}),404
        return jsonify({'ok':True})

    def login_page(self):
        if self.store.current(request.cookies.get(COOKIE,'')):
            return redirect(safe_next(request.args.get('next','/')))
        return render_template('login.html',auth_start_url=url_for('eva_google_start',next=safe_next(request.args.get('next','/'))),
                               auth_ready=bool(self.client),auth_error=request.args.get('error',''))

    def google_start(self):
        origin=self.origin()
        if not origin:return jsonify({'error':'Host login tidak valid.'}),400
        callback=origin+'/auth/google/callback'
        if not self.client or callback not in self.client.get('redirect_uris',[]):
            return jsonify({'error':'Google login belum dikonfigurasi.'}),503
        tx=self.store.begin_oauth(origin,request.args.get('next','/'))
        challenge=base64.urlsafe_b64encode(hashlib.sha256(tx['verifier'].encode()).digest()).decode().rstrip('=')
        params={'client_id':self.client['client_id'],'redirect_uri':callback,'response_type':'code',
                'scope':'openid email profile','state':tx['state'],'nonce':tx['nonce'],
                'code_challenge':challenge,'code_challenge_method':'S256','prompt':'select_account'}
        result=redirect('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params))
        result.set_cookie(FLOW_COOKIE,tx['browser'],max_age=600,secure=True,httponly=True,samesite='Lax',path='/')
        return result


    def exchange_google(self, code, tx, origin):
        result=requests.post('https://oauth2.googleapis.com/token',data={
            'code':code,'client_id':self.client['client_id'],'client_secret':self.client['client_secret'],
            'redirect_uri':origin+'/auth/google/callback','grant_type':'authorization_code',
            'code_verifier':tx['verifier']},timeout=20,allow_redirects=False)
        if result.status_code!=200:raise ValueError('Google tidak menerima login.')
        data=result.json()
        token=data.get('id_token') if isinstance(data,dict) else None
        if not isinstance(token,str) or len(token)>20000:raise ValueError('Identitas Google tidak tersedia.')
        return verify_oauth2_token(token,GoogleRequest(),self.client['client_id'])

    def callback(self):
        try:
            origin=self.origin()
            if not origin or not self.client:raise ValueError('Konfigurasi login belum siap.')
            tx=self.store.consume_oauth(request.args.get('state',''),request.cookies.get(FLOW_COOKIE,''),origin)
            if request.args.get('error'):raise ValueError('Login Google dibatalkan.')
            code=request.args.get('code','')
            if not code or len(code)>4096:raise ValueError('Kode login tidak tersedia.')
            claims=self.exchange_google(code,tx,origin)
            nonce=claims.get('nonce')
            if not isinstance(nonce,str) or not secrets.compare_digest(nonce,tx['nonce']):
                raise ValueError('Identitas login tidak cocok.')
            user,raw=self.store.login(claims)
            self.store.logout(request.cookies.get(COOKIE,''))
            session.clear();session['eva_user_id']=user['id']
            linked=self.store.astri_link(user['id'])
            if linked:session['astri_session_id']=linked['session_id']
            # Upstream validation runs in the app's status request, not this
            # Google callback: an ASTRI outage must not prevent EVA login.
            response=redirect(tx['next'])
            response.set_cookie(COOKIE,raw,max_age=7*86400,secure=True,httponly=True,samesite='Lax',path='/')
        except (ValueError,PermissionError,requests.RequestException,GoogleAuthError):
            response=redirect(url_for('eva_login',error='Login belum berhasil. Coba masuk lagi.'))
        response.delete_cookie(FLOW_COOKIE,path='/',secure=True,httponly=True,samesite='Lax')
        return response

    def account_page(self):
        return render_template('account.html')

    def account_history(self):
        user = getattr(g, 'eva_user', None)
        if not user or not self.store.user_active(user['id']):
            return jsonify({'error': 'Masuk EVA terlebih dahulu.'}), 401
        try:
            kind = request.args.get('kind', 'convert')
            page = int(request.args.get('page', '1'))
            raw_query = request.args.get('q', '')
            if len(raw_query) > 256:
                raise ValueError('query too long')
            query = raw_query.strip()
            scope = request.args.get('scope', '')
            status = request.args.get('status', '')
            if (kind not in {'convert', 'boq'} or not 1 <= page <= 100000 or len(query) > 256
                    or scope not in {'', 'cluster', 'subfeeder', 'feeder'}
                    or status not in {'', 'all', 'active', 'done', 'error', 'failed', 'cancelled', 'queued', 'running', 'processing', 'retrying'}):
                raise ValueError('invalid filter')
            if kind == 'boq':
                from astri_boq_submissions import RedisBoqSubmissionStore
                from config import Config
                result = RedisBoqSubmissionStore(Config.REDIS_URL, prefix=Config.JOB_STORE_KEY_PREFIX,
                                                ttl_seconds=Config.JOB_STORE_TTL_SECONDS).list_success_history(
                    page=page, q=query, scope=scope, user_id=user['id'])
                return jsonify(result)
            from jobs import list_jobs as system_jobs
            with self.store.connect() as db:
                allowed = {r['reference'] for r in db.execute(
                    "SELECT reference FROM resources WHERE kind='job' AND user_id=?", (user['id'],))}
            # Personal history remains personal for owners; never use admin list_jobs().
            jobs = [j for j in system_jobs(2**31 - 1)
                    if isinstance(j, dict) and j.get('job_id') in allowed and j.get('owner_user_id') == user['id']]
            if scope:
                jobs = [j for j in jobs if str(j.get('scope_type', '')).casefold() == scope]
            if status not in {'', 'all'}:
                group = {'active': {'queued','running','processing','retrying'},
                         'error': {'error','failed'}, 'failed': {'error','failed'},
                         'done': {'done','completed'}}.get(status, {status})
                jobs = [j for j in jobs if j.get('status') in group]
            if query:
                jobs = [j for j in jobs if query.casefold() in ' '.join(str(j.get(k) or '') for k in
                        ('job_id','download_name','original_filenames','output_mode','scope_type')).casefold()]
            def clean(value):
                text = str(value or '')[:3000]
                text = re.sub(r'(?i)\b(Bearer|Basic)\s+\S+', r'\1 [disembunyikan]', text)
                text = re.sub(r'''(?i)((?:password|passwd|token|api[_-]?key|secret|cookie|ASTRIOSP|csrf_value|session[_-]?id)["']?\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''', r'\1[disembunyikan]', text)
                text = re.sub(r'(?:/app/|/opt/eva/|/home/)[^\s:,\'"<>]+', '[path server]', text)
                return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            items = []
            for job in jobs[(page - 1) * 20:page * 20]:
                item = {key: clean(job.get(key)) for key in
                        ('job_id','status','scope_type','network_type','output_mode','message','error','download_name')}
                item.update(progress=max(0, min(100, int(job.get('progress') or 0))),
                            created_at=job.get('created_at'), updated_at=job.get('updated_at'),
                            original_filenames=[clean(n) for n in (job.get('original_filenames') or [])[:20]])
                items.append(item)
            return jsonify({'jobs': items, 'total': len(jobs), 'page': page, 'page_size': 20})
        except ValueError:
            return jsonify({'error': 'Filter riwayat tidak valid.'}), 400
        except Exception:
            return jsonify({'error': 'Riwayat belum bisa dimuat. Coba muat ulang.'}), 503

    def me(self):
        user=g.eva_user
        return jsonify({'user':{k:user[k] for k in ('id','name','email','role')},'csrf_token':user['csrf']})

    def logout(self):
        self.store.logout(request.cookies.get(COOKIE,''));session.clear()
        response=jsonify({'ok':True,'login_url':'/login'})
        response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='Lax')
        return response

    def response_headers(self, response):
        if request.path not in self.public_assets or response.status_code>=400:
            response.headers['Cache-Control']='private, no-store'
            response.vary.add('Cookie')
        if request.path.startswith('/auth/'):
            response.headers['Referrer-Policy']='no-referrer'
        return response


def register_auth(app):
    gate=AuthGate(app)
    app.extensions['eva_auth']=gate
    app.before_request_funcs.setdefault(None,[]).insert(0,gate.require_login)
    app.add_url_rule('/login','eva_login',gate.login_page)
    app.add_url_rule('/auth/google/start','eva_google_start',gate.google_start)
    app.add_url_rule('/auth/google/callback','eva_google_callback',gate.callback)
    app.add_url_rule('/auth/me','eva_auth_me',gate.me)
    app.add_url_rule('/account','eva_account_page',gate.account_page)
    app.add_url_rule('/api/account/history','eva_account_history',gate.account_history)
    app.add_url_rule('/auth/logout','eva_auth_logout',gate.logout,methods=['POST'])
    app.add_url_rule('/admin/users','eva_auth_users_page',lambda:render_template('admin_users.html'))
    app.add_url_rule('/api/dashboard/users','eva_auth_users',gate.users)
    app.add_url_rule('/api/dashboard/users/<user_id>/status','eva_auth_user_status',gate.user_status,methods=['POST'])
    app.add_url_rule('/api/dashboard/users/<user_id>/plan','eva_auth_user_plan',gate.user_plan,methods=['POST'])
    app.after_request(gate.response_headers)
    @app.context_processor
    def eva_account_context():
        user=getattr(g,'eva_user',None)
        return {'eva_account':{k:user[k] for k in ('id','name','email','role','csrf')} if user else None}
    return gate
