"""DRM Successful history: tracked WO turns Successful in ASTRI and its PR is issued.

Event policy (user-approved 2026-10-15):
- Candidates are WOs already tracked by EVA for this user+account (DRM New history
  or Closed-WO revision history). Untracked WOs are ignored; this channel extends
  the lifecycle of WOs EVA already knows.
- A candidate becomes a notification only when the native list shows status
  Successful AND a purchase requisition number is already present.
  Successful without a PR stays a candidate and is rechecked on later polls
  (retroactive for currently-Successful tracked WOs).
- Dedupe once per user + account + scope + WO UUID, same identity as the other
  channels. Status/PR changes never create a second notification.
"""
import json
import re
from datetime import datetime

SCOPES = ('cluster', 'subfeeder', 'feeder')
POLL_INTERVAL = 60
# ponytail: a handful of exact per-WO checks per poll; the tracked set is small.
MAX_CHECKS = 5
BATCH_SECONDS = 32
MAX_QUEUE = 200
MAX_ROWS = 100

PR_KEY = {
    'cluster': 'purchase_requisition_number',
    'subfeeder': 'target_subfeeder_purchase_requisition_number',
    'feeder': 'target_osp_route_purchase_requisition_number',
}
_UUID_RE = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
_ITEM_FIELDS = ('scope', 'code', 'name', 'work_order_number', 'pr_number', 'source_url', 'requested_at')


def parse_rows(html, scope, base):
    """One native DRM list page -> row dicts. Malformed contracts fail closed."""
    import uuid
    from eva_drm_revisions import _fragment, _Node, _source_url, _text
    if scope not in SCOPES:
        raise ValueError('Scope DRM tidak valid.')
    table = _fragment(html, 'work-order-table')
    if table.tag != 'table':
        raise ValueError('Tabel DRM tidak valid.')
    native = 'target_osp_route_' if scope == 'feeder' else 'target_' + scope + '_'
    code_key, name_key = native + 'code', native + ('label' if scope == 'feeder' else 'name')

    def key(node):
        values = [re.sub(r'(?:-|_)column$', '', c.removeprefix('work-order-')).replace('-', '_')
                  for c in node.attrs.get('class', '').split() if c.startswith('work-order-')]
        if len(values) > 1:
            raise ValueError('Kolom DRM ambigu.')
        return values[0] if values else None

    required = {'number', 'latest_status_label', code_key, name_key, 'created_at', PR_KEY[scope]}
    headers = [key(node) for node in table.walk() if node.tag == 'th' and key(node)]
    if len(headers) != len(set(headers)) or not required.issubset(headers):
        raise ValueError('Kontrak daftar DRM tidak lengkap.')
    rows, seen = [], set()
    for row in [node for node in table.walk() if node.tag == 'tr']:
        children = [node for node in row.parts if isinstance(node, _Node)]
        if any(node.tag not in {'td', 'th'} for node in children):
            raise ValueError('Struktur baris DRM tidak valid.')
        cells = [node for node in children if node.tag == 'td']
        if not cells:
            continue
        values = {}
        for cell in cells:
            k = key(cell)
            if k:
                if k in values:
                    raise ValueError('Kolom DRM duplikat.')
                values[k] = ' '.join(cell.text().split())
        if not required.issubset(values):
            raise ValueError('Baris DRM tidak lengkap.')
        number, code = _text(values['number']), _text(values[code_key])
        identities = set()
        for node in row.walk():
            if node.tag != 'input':
                continue
            match = re.fullmatch(r'work_order\[([^]]+)\]\[([^]]+)\]', node.attrs.get('name', ''))
            if not match:
                continue
            raw, field = match.groups()
            if not re.fullmatch(_UUID_RE, raw):
                raise ValueError('UUID DRM tidak valid.')
            if field not in {'number', code_key}:
                raise ValueError('Identitas DRM bertentangan.')
            if node.attrs.get('value') != (number if field == 'number' else code):
                raise ValueError('Identitas DRM bertentangan.')
            identities.add(str(uuid.UUID(raw)))
        if len(identities) != 1:
            raise ValueError('UUID DRM ambigu.')
        identity = identities.pop()
        if identity in seen:
            raise ValueError('Baris DRM duplikat.')
        seen.add(identity)
        pr_number = _text(values[PR_KEY[scope]], 64) if values[PR_KEY[scope]] else ''
        if pr_number and not re.fullmatch(r'[0-9]{6,20}', pr_number):
            raise ValueError('Nomor PR DRM tidak valid.')
        created_at = _text(values['created_at'], 19)
        if datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S') != created_at:
            raise ValueError('Tanggal DRM tidak valid.')
        links = [node.attrs.get('href', '') for node in row.walk()
                 if node.tag == 'a' and '/requirement-document' in node.attrs.get('href', '')]
        if len(links) > 1:
            raise ValueError('Tautan requirement DRM ambigu.')
        source_url = ''
        if links:
            try:
                source_url = _source_url(links[0], base, scope, code, number)
            except ValueError:
                source_url = ''
        rows.append({'scope': scope, 'wo_uuid': identity, 'code': code, 'name': _text(values[name_key]),
                     'work_order_number': number, 'status': values['latest_status_label'],
                     'status_name': values.get('latest_status_name', ''),
                     'pr_number': pr_number, 'source_url': source_url, 'requested_at': created_at})
        if len(rows) > MAX_ROWS:
            raise ValueError('Halaman DRM melebihi batas.')
    return rows


def eligible(rows, wanted):
    """Rows matching a wanted (code, number, uuid) that are Successful with a PR."""
    items = []
    for code, number, identity in wanted:
        matches = [row for row in rows if row['code'] == code and row['work_order_number'] == number]
        if not matches:
            continue
        if len(matches) != 1 or matches[0]['wo_uuid'] != identity:
            raise ValueError('Status WO DRM tidak dapat dipastikan.')
        row = matches[0]
        if row['status'].strip().casefold() != 'successful':
            continue
        if row['status_name'] and row['status_name'].strip().casefold() != 'successful':
            raise ValueError('Status DRM bertentangan.')
        if not row['pr_number']:
            continue
        items.append(dict((field, row[field]) for field in _ITEM_FIELDS) | {'wo_uuid': row['wo_uuid']})
    return items


def update_queue(pending, candidates, limit=MAX_QUEUE):
    """Durable rotation: keep still-valid entries, rebuild sorted when drained."""
    wanted = {tuple(item) for item in candidates}
    queue = [tuple(item) for item in pending if tuple(item) in wanted]
    if not queue:
        queue = sorted(wanted)[:limit]
    return queue


def scan_batch(record, base, cursor, candidates, max_checks=MAX_CHECKS):
    """Check up to max_checks candidates within BATCH_SECONDS; failure preserves cursor."""
    import copy
    import time
    from eva_drm_revisions import _get_html
    cursor = copy.deepcopy(cursor)
    queue = update_queue(cursor.get('pending', []), candidates)
    deadline = time.monotonic() + BATCH_SECONDS
    items, checked = [], 0
    while queue and checked < max_checks and time.monotonic() < deadline:
        scope, identity, code, number = queue[0]
        url = base.rstrip('/') + '/material/' + scope + '/drm/list/v3'
        html = _get_html(record, url, deadline, {'filter[global]': code, 'page': 1, 'limit': 100})
        rows = parse_rows(html, scope, base)
        items.extend(eligible(rows, [(code, number, identity)]))
        queue.pop(0)
        checked += 1
    if queue and not checked:
        raise TimeoutError('Tidak ada pemeriksaan DRM yang selesai.')
    return items, {'pending': queue, 'cycle': cursor.get('cycle', 0) + (1 if checked and not queue else 0)}


def _dto(row):
    return dict((field, row[field]) for field in ('id', *_ITEM_FIELDS, 'detected_at')) | {'kind': 'successful', 'is_read': row['read_at'] is not None}


class SuccessfulStore:
    def __init__(self, connect):
        self.connect = connect
        with connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS drm_successful (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                scope TEXT NOT NULL, wo_uuid TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT NOT NULL, work_order_number TEXT NOT NULL, pr_number TEXT NOT NULL,
                source_url TEXT NOT NULL, requested_at TEXT NOT NULL, detected_at REAL NOT NULL,
                UNIQUE(user_id,account,scope,wo_uuid)
            );
            CREATE INDEX IF NOT EXISTS drm_successful_owner_id ON drm_successful(user_id,id);
            CREATE TABLE IF NOT EXISTS drm_successful_reads (
                notification_id INTEGER PRIMARY KEY REFERENCES drm_successful(id), read_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drm_successful_polls (
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                checked_at REAL NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(user_id,account)
            );
            ''')

    def candidates(self, user_id, accounts):
        marks = ','.join('?' for _ in accounts)
        with self.connect() as db:
            rows = db.execute(
                'SELECT scope,wo_uuid,code,work_order_number FROM drm_notifications WHERE user_id=? AND account IN (' + marks + ') '
                'UNION SELECT scope,wo_uuid,code,work_order_number FROM drm_revisions WHERE user_id=? AND account IN (' + marks + ') '
                'EXCEPT SELECT scope,wo_uuid,code,work_order_number FROM drm_successful WHERE user_id=? AND account IN (' + marks + ') '
                'ORDER BY scope,code', (user_id, *accounts, user_id, *accounts, user_id, *accounts)).fetchall()
        return [tuple(row) for row in rows]

    def record(self, user_id, link, items, cursor, now):
        from eva_drm_revisions import _text
        created = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owned = db.execute("SELECT l.username,l.session_id FROM astri_links l JOIN users u ON u.id=l.user_id JOIN resources r ON r.kind='astri' AND r.reference=l.session_id AND r.user_id=l.user_id WHERE l.user_id=? AND u.active=1", (user_id,)).fetchone()
            if not owned or dict(owned) != link:
                raise PermissionError('Tautan ASTRI berubah.')
            for item in items:
                if (not re.fullmatch(_UUID_RE, str(item.get('wo_uuid') or ''))
                        or not re.fullmatch(r'[0-9]{6,20}', str(item.get('pr_number') or ''))
                        or item.get('scope') not in SCOPES
                        or any(not isinstance(item.get(field), str) or not _text(item[field]) for field in ('code', 'name', 'work_order_number'))):
                    raise ValueError('Notifikasi DRM tidak valid.')
                if datetime.strptime(item['requested_at'], '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S') != item['requested_at']:
                    raise ValueError('Tanggal DRM tidak valid.')
                fields = ('user_id', 'account', 'wo_uuid', *_ITEM_FIELDS, 'detected_at')
                result = db.execute('INSERT INTO drm_successful(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ') ON CONFLICT(user_id,account,scope,wo_uuid) DO NOTHING',
                                    (user_id, link['username'].strip().casefold(), item['wo_uuid'], *(item[field] for field in _ITEM_FIELDS), now))
                if result.rowcount:
                    created.append(_dto(dict(item, id=result.lastrowid, detected_at=now, read_at=None)))
            db.execute('INSERT INTO drm_successful_polls VALUES(?,?,?,?) ON CONFLICT(user_id,account) DO UPDATE SET checked_at=excluded.checked_at,cursor=excluded.cursor',
                       (user_id, link['username'].strip().casefold(), now, json.dumps(cursor, separators=(',', ':'))))
        return created

    def state(self, user_id, account):
        with self.connect() as db:
            row = db.execute('SELECT checked_at,cursor FROM drm_successful_polls WHERE user_id=? AND account=?', (user_id, account.strip().casefold())).fetchone()
        return (row['checked_at'], json.loads(row['cursor'])) if row else (None, {'pending': [], 'cycle': 0})

    def history(self, user_id, before=None, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100 or before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807):
            raise ValueError('Halaman riwayat tidak valid.')
        with self.connect() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM drm_successful WHERE user_id=?', (user_id,)).fetchone()[0]
            unread = db.execute('SELECT count(*) FROM drm_successful n LEFT JOIN drm_successful_reads r ON r.notification_id=n.id WHERE n.user_id=? AND r.notification_id IS NULL', (user_id,)).fetchone()[0]
            query = 'SELECT n.*,r.read_at FROM drm_successful n LEFT JOIN drm_successful_reads r ON r.notification_id=n.id WHERE n.user_id=?'
            args = [user_id]
            if before is not None:
                query += ' AND n.id<?'
                args.append(before)
            rows = db.execute(query + ' ORDER BY n.id DESC LIMIT ?', (*args, limit + 1)).fetchall()
        return {'items': [_dto(row) for row in rows[:limit]], 'total': total, 'unread_count': unread,
                'next_before': rows[limit - 1]['id'] if len(rows) > limit else None}

    def mark_read(self, user_id, ids, now):
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or not 1 <= i <= 9223372036854775807 for i in ids):
            raise ValueError('Daftar notifikasi tidak valid.')
        ids = list(dict.fromkeys(ids))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT active FROM users WHERE id=?', (user_id,)).fetchone()
            if not active or not active['active']:
                raise PermissionError('Pengguna EVA tidak aktif.')
            marked = 0
            if ids:
                marked = db.execute('INSERT OR IGNORE INTO drm_successful_reads SELECT id,? FROM drm_successful WHERE user_id=? AND id IN (' + ','.join('?' for _ in ids) + ')', (now, user_id, *ids)).rowcount
            unread = db.execute('SELECT count(*) FROM drm_successful n LEFT JOIN drm_successful_reads r ON r.notification_id=n.id WHERE n.user_id=? AND r.notification_id IS NULL', (user_id,)).fetchone()[0]
        return {'marked': marked, 'unread_count': unread}


def _drm_session(env, link):
    """Best-effort session for DRM polling.

    A slow/failed liveness probe must not abort the poll: scan_batch already
    fails on a genuinely dead session, and a 503 here is what the UI shows as
    a hang. Fall back to the stored session record.
    """
    try:
        return env['require_astri_session']()
    except Exception:
        fallback = env.get('current_astri_session')
        if fallback:
            try:
                record = fallback()
            except Exception:
                record = None
            if record:
                return record
        raise


def register_drm_successful(app, env, clock=None):
    import fcntl
    import hashlib
    import os
    from pathlib import Path
    import time
    from flask import g, jsonify, request

    clock = clock or time.time
    gate = env['EVA_AUTH']
    store = SuccessfulStore(gate.store.connect)
    app.extensions['eva_drm_successful'] = store

    def owner():
        user = getattr(g, 'eva_user', None)
        return user.get('id') if isinstance(user, dict) else None

    def history():
        uid = owner()
        if not uid:
            return jsonify(code='EVA_LOGIN_REQUIRED'), 401
        try:
            before = request.args.get('before')
            return jsonify(store.history(uid, int(before) if before is not None else None, int(request.args.get('limit', '30'))))
        except (TypeError, ValueError):
            return jsonify(error='Halaman riwayat tidak valid.'), 400

    def read():
        uid = owner()
        if not uid:
            return jsonify(code='EVA_LOGIN_REQUIRED'), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or 'ids' not in payload:
            return jsonify(error='Daftar notifikasi tidak valid.'), 400
        try:
            return jsonify(ok=True, **store.mark_read(uid, payload['ids'], clock()))
        except ValueError:
            return jsonify(error='Daftar notifikasi tidak valid.'), 400
        except PermissionError:
            return jsonify(error='Sesi pengguna tidak aktif.'), 403

    def result(uid, linked, checked=False, created=None, previous=None):
        history = store.history(uid, limit=1)
        return jsonify(ok=True, linked=linked, checked=checked, new_items=created or [],
                       total=history['total'], unread_count=history['unread_count'],
                       last_checked_at=previous, poll_interval=POLL_INTERVAL)

    def poll():
        uid = owner()
        if not uid:
            return jsonify(code='EVA_LOGIN_REQUIRED'), 401
        link = gate.store.astri_link(uid)
        if not link:
            return result(uid, False)
        directory = Path(gate.store.path).parent / 'drm_successful_locks'
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (hashlib.sha256(uid.encode()).hexdigest() + '.lock')
        with path.open('a') as handle:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return result(uid, True)
            try:
                previous, cursor = store.state(uid, link['username'])
                if previous is not None and clock() - previous < POLL_INTERVAL:
                    return result(uid, True, previous=previous)
                record = _drm_session(env, link)
                if record.get('username') != link['username'] or record.get('_session_id') != link['session_id']:
                    raise PermissionError('Tautan berubah.')
                accounts = list(dict.fromkeys([link['username'], link['username'].strip().casefold()]))
                candidates = store.candidates(uid, accounts)
                items, cursor = scan_batch(record, env['ASTRI_BASE_URL'], cursor, candidates)
                now = clock()
                created = store.record(uid, link, items, cursor, now)
                return result(uid, True, True, created, now)
            except PermissionError:
                return jsonify(ok=False, code='DRM_LINK_CHANGED', error='Tautan ASTRI berubah. Muat ulang EVA.'), 409
            except Exception as exc:
                # Class name only: URLs/HTML/transport text may carry credentials.
                app.logger.warning('drm poll failed: %s', type(exc).__name__)
                return jsonify(ok=False, code='DRM_UPSTREAM_UNAVAILABLE', error='DRM belum dapat diperiksa. Riwayat tetap tersimpan.'), 503
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    app.add_url_rule('/api/astri/drm-successful', 'drm_successful_history', history, methods=['GET'])
    app.add_url_rule('/api/astri/drm-successful/poll', 'drm_successful_poll', poll, methods=['POST'])
    app.add_url_rule('/api/astri/drm-successful/read', 'drm_successful_read', read, methods=['POST'])
