"""Closed-WO revision history; isolated from DRM New's IDs and storage."""
import hashlib
from html.parser import HTMLParser
import json
import re


class _Node:
    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.parts = tag, dict(attrs), []

    def walk(self):
        yield self
        for part in self.parts:
            if isinstance(part, _Node):
                yield from part.walk()

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()

    def text(self):
        if self.tag in {'script', 'style', 'template'}:
            return ''
        text = ''.join(part.text() if isinstance(part, _Node) else part for part in self.parts)
        return ' ' + text + ' ' if self.tag in {'p', 'div', 'br', 'li', 'ul', 'ol', 'tr'} else text


def _fragment(html, element_id, missing_ok=False):
    if not isinstance(html, str) or len(html) > 5_000_000:
        raise ValueError('HTML DRM tidak valid.')

    class Fragment(HTMLParser):
        void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.roots, self.stack = [], []
            self.nodes = 0

        def handle_starttag(self, tag, attrs):
            root = (dict(attrs).get('id') == element_id if element_id is not None
                    else tag == 'div' and 'card' in dict(attrs).get('class', '').split())
            if not self.stack and not root:
                return
            if len(dict(attrs)) != len(attrs):
                raise ValueError('Atribut DRM ambigu.')
            node = _Node(tag, attrs)
            if root:
                self.roots.append(node)
            if self.stack:
                self.stack[-1].parts.append(node)
            self.nodes += 1
            if self.nodes > 100_000 or len(self.stack) > 100:
                raise ValueError('HTML DRM terlalu kompleks.')
            if tag not in self.void:
                self.stack.append(node)

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag not in self.void:
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if self.stack:
                if self.stack[-1].tag != tag:
                    raise ValueError('HTML DRM tidak seimbang.')
                self.stack.pop()

        def handle_data(self, data):
            if self.stack:
                self.stack[-1].parts.append(data)

    parser = Fragment()
    parser.feed(html)
    parser.close()
    if element_id is None:
        parser.roots = [root for root in parser.roots if any(node.has_class('card-title') and ' '.join(node.text().split()) == 'Progress List' for node in root.walk())]
    if missing_ok and not parser.roots and not parser.stack:
        return None
    if len(parser.roots) != 1 or parser.stack:
        raise ValueError('Bagian DRM hilang/ambigu.')
    return parser.roots[0]


def _text(value, maximum=512):
    value = ' '.join(value.split())
    if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Teks DRM tidak valid.')
    return value


def parse_revision_notes(html, username):
    """All other-author remarks except native approval/PR events, as plain text."""
    from datetime import datetime
    own = _text(username, 255).casefold()
    root = _fragment(html, 'remarks-card', missing_ok=True)
    legacy = root is None
    if legacy:
        root = _fragment(html, None)
        titles = [node for node in root.walk() if node.has_class('card-title')]
        tables = [node for node in root.walk() if node.tag == 'table']
        if len(titles) != 1 or _text(titles[0].text()) != 'Progress List' or len(tables) != 1:
            raise ValueError('Progress legacy DRM ambigu.')
        rows = [node for node in tables[0].walk() if node.tag == 'tr']
        entries, fields = [], []
        for row in rows:
            cells = [node for node in row.parts if isinstance(node, _Node)]
            if len(cells) == 1 and cells[0].tag == 'td' and not cells[0].text().strip():
                if fields:
                    raise ValueError('Progress legacy DRM tidak lengkap.')
                continue
            expected = ('PIC', 'Update Time', 'Detail')[len(fields)]
            if len(cells) != 2 or any(node.tag != 'td' for node in cells) or _text(cells[0].text()) != expected:
                raise ValueError('Urutan progress legacy DRM tidak valid.')
            value = cells[1].text().strip()
            if not value.startswith(':'):
                raise ValueError('Nilai progress legacy DRM tidak valid.')
            fields.append(value[1:])
            if len(fields) == 3:
                entries.append(tuple(fields)); fields = []
        if fields or len(entries) > 1000 or not entries:
            raise ValueError('Progress legacy DRM tidak lengkap.')
    lists = [node for node in root.walk() if node.tag == 'ul' and node.has_class('list-group')]
    if not legacy and len(lists) != 1:
        raise ValueError('Daftar progress DRM hilang/ambigu.')
    if not legacy:
        entries = [node for node in lists[0].parts if isinstance(node, _Node)]
    if not legacy and any(node.tag != 'li' or not node.has_class('list-group-item') for node in entries):
        raise ValueError('Baris progress DRM tidak valid.')
    if len(entries) > 1000:
        raise ValueError('Progress DRM terlalu besar.')
    notes = {}
    for entry in entries:
        if legacy:
            author_text, time_text, note_text = entry
        else:
            nodes = list(entry.walk())
            bodies = [node for node in nodes if node.tag == 'div' and node.has_class('mt-2')]
            if len(bodies) != 1:
                raise ValueError('Progress DRM ambigu.')
            body = bodies[0]
            inside = set(body.walk())
            authors = [node for node in nodes if node.tag == 'strong' and node not in inside]
            times = [node for node in nodes if node.tag == 'span' and node.has_class('badge') and node not in inside]
            if len(authors) != 1 or len(times) != 1:
                raise ValueError('Identitas progress DRM ambigu.')
            author_text, time_text, note_text = authors[0].text(), times[0].text(), body.text()
        author = _text(author_text, 255)
        timestamp = _text(time_text, 19)
        if datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S') != timestamp:
            raise ValueError('Tanggal progress DRM tidak valid.')
        note = _text(note_text, 16_384)
        event = re.match(r'^(?:approve\s*drm|create\s*pr)\b|^\[?#?PR_DESCRIPTION\b', note, re.I)
        if author.casefold() == own or event:
            continue
        key = hashlib.sha256(json.dumps([author.casefold(), timestamp, note], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        notes[key] = {'remark_key': key, 'author': author, 'requested_at': timestamp, 'note': note}
    return list(notes.values())


SCOPES = ('cluster', 'subfeeder', 'feeder')


def _source_url(href, base, scope, code, number):
    from urllib.parse import parse_qsl, urljoin, urlsplit
    if not isinstance(href, str) or not href or len(href) > 2048 or any(c.isspace() or ord(c) < 32 or c == '\\' for c in href):
        raise ValueError('URL DRM tidak valid.')
    origin = urlsplit(base)
    url = urlsplit(urljoin(base.rstrip('/') + '/', href))
    if (origin.scheme != 'https' or origin.username or origin.password or origin.path not in {'', '/'}
            or origin.query or origin.fragment or not origin.netloc
            or (url.scheme, url.netloc) != (origin.scheme, origin.netloc)
            or url.path != '/material/' + scope + '/drm/requirement-document' or url.fragment):
        raise ValueError('URL DRM di luar scope.')
    pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    params = dict(pairs)
    keys = set(params) - {'work_order_number', 'vendor_label'}
    # Feeder's actual native code key is taken from its anchor, not a cluster default.
    allowed = {'feeder_code', 'osp_route_code', 'target_osp_route_code'} if scope == 'feeder' else {scope + '_code'}
    if (len(params) != len(pairs) or len(keys) != 1 or not keys.issubset(allowed)
            or params.get('work_order_number') != number or params[next(iter(keys))] != code
            or not params.get('vendor_label')):
        raise ValueError('Identitas URL DRM tidak sesuai.')
    if any(ord(c) < 32 or ord(c) == 127 for value in params.values() for c in value):
        raise ValueError('Karakter URL DRM tidak valid.')
    _text(params['vendor_label'])
    return url.geturl()


def _list_rows(html, scope, base):
    import uuid
    if scope not in SCOPES:
        raise ValueError('Scope DRM tidak valid.')
    table = _fragment(html, 'work-order-table')
    if table.tag != 'table':
        raise ValueError('Tabel DRM tidak valid.')
    native = 'target_osp_route_' if scope == 'feeder' else 'target_' + scope + '_'
    code_key, name_key = native + 'code', native + ('label' if scope == 'feeder' else 'name')
    required = {'number', 'latest_status_label', code_key, name_key}
    def key(node):
        values = [re.sub(r'(?:-|_)column$', '', c.removeprefix('work-order-')).replace('-', '_')
                  for c in node.attrs.get('class', '').split() if c.startswith('work-order-')]
        if len(values) > 1:
            raise ValueError('Kolom DRM ambigu.')
        return values[0] if values else None
    headers = [key(n) for n in table.walk() if n.tag == 'th' and key(n)]
    if len(headers) != len(set(headers)) or not required.issubset(headers):
        raise ValueError('Kontrak daftar DRM tidak lengkap.')
    if 'latest_status_name' in headers:
        required.add('latest_status_name')
    rows, seen = [], set()
    for row in [n for n in table.walk() if n.tag == 'tr']:
        children = [n for n in row.parts if isinstance(n, _Node)]
        if any(n.tag not in {'td', 'th'} for n in children):
            raise ValueError('Struktur baris DRM tidak valid.')
        cells = [n for n in children if n.tag == 'td']
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
            match = re.fullmatch(r'work_order\[([^]]+)\]\[(number|target_cluster_code|target_subfeeder_code|target_osp_route_code)\]', node.attrs.get('name', ''))
            if match:
                raw, field = match.groups()
                if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', raw):
                    raise ValueError('UUID DRM tidak valid.')
                identities.add(str(uuid.UUID(raw)))
                if field not in {'number', code_key} or node.attrs.get('value') != (number if field == 'number' else code):
                    raise ValueError('Identitas DRM bertentangan.')
        if len(identities) != 1:
            raise ValueError('UUID DRM ambigu.')
        identity = identities.pop()
        if identity in seen:
            continue
        seen.add(identity)
        item = {'scope': scope, 'wo_uuid': identity, 'code': code, 'name': _text(values[name_key]),
                'work_order_number': number, 'status': values['latest_status_label']}
        if item['status'] == 'Closed':
            if values.get('latest_status_name', 'closed') != 'closed':
                raise ValueError('Status DRM bertentangan.')
            links = [n.attrs.get('href', '') for n in row.walk() if n.tag == 'a' and '/requirement-document' in n.attrs.get('href', '')]
            if len(links) != 1:
                # One unusable anchor must not void the whole page.
                continue
            try:
                item['source_url'] = _source_url(links[0], base, scope, code, number)
            except ValueError:
                continue
        rows.append(item)
        if len(rows) > 100:
            raise ValueError('Halaman DRM melebihi batas.')
    return rows


def parse_closed_list(html, scope, base):
    rows = _list_rows(html, scope, base)
    return [row for row in rows if row['status'] == 'Closed'], len(rows)


def initial_cursor():
    return {'turn': 0, 'scopes': {scope: {'page': 1, 'pending': [], 'more': False, 'signature': '', 'cycles': 0} for scope in SCOPES}}


_ITEM_FIELDS = ('scope', 'code', 'name', 'work_order_number', 'requested_at', 'author', 'note', 'source_url')


def _dto(row):
    return dict((field, row[field]) for field in ('id', *_ITEM_FIELDS, 'detected_at')) | {'kind': 'revision', 'is_read': row['read_at'] is not None}


class RevisionStore:
    def __init__(self, connect):
        self.connect = connect
        with connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS drm_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                scope TEXT NOT NULL, wo_uuid TEXT NOT NULL, remark_key TEXT NOT NULL,
                code TEXT NOT NULL, name TEXT NOT NULL, work_order_number TEXT NOT NULL,
                requested_at TEXT NOT NULL, author TEXT NOT NULL, note TEXT NOT NULL,
                source_url TEXT NOT NULL, detected_at REAL NOT NULL,
                UNIQUE(user_id,account,scope,wo_uuid,remark_key)
            );
            CREATE INDEX IF NOT EXISTS drm_revisions_owner_id ON drm_revisions(user_id,id);
            CREATE TABLE IF NOT EXISTS drm_revision_reads (
                notification_id INTEGER PRIMARY KEY REFERENCES drm_revisions(id), read_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drm_revision_polls (
                user_id TEXT NOT NULL REFERENCES users(id), account TEXT NOT NULL,
                checked_at REAL NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(user_id,account)
            );
            ''')

    def record(self, user_id, link, items, cursor, now):
        created = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owned = db.execute("SELECT l.username,l.session_id FROM astri_links l JOIN users u ON u.id=l.user_id JOIN resources r ON r.kind='astri' AND r.reference=l.session_id AND r.user_id=l.user_id WHERE l.user_id=? AND u.active=1", (user_id,)).fetchone()
            if not owned or dict(owned) != link:
                raise PermissionError('Tautan ASTRI berubah.')
            for item in items:
                if item['status'] != 'Closed' or item['author'].strip().casefold() == link['username'].strip().casefold():
                    raise ValueError('Revisi DRM tidak memenuhi syarat.')
                fields = ('user_id', 'account', 'wo_uuid', 'remark_key', *_ITEM_FIELDS, 'detected_at')
                result = db.execute('INSERT INTO drm_revisions(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ') ON CONFLICT(user_id,account,scope,wo_uuid,remark_key) DO NOTHING',
                                    (user_id, link['username'].strip().casefold(), item['wo_uuid'], item['remark_key'], *(item[field] for field in _ITEM_FIELDS), now))
                if result.rowcount:
                    created.append(_dto(dict(item, id=result.lastrowid, detected_at=now, read_at=None)))
            db.execute('INSERT INTO drm_revision_polls VALUES(?,?,?,?) ON CONFLICT(user_id,account) DO UPDATE SET checked_at=excluded.checked_at,cursor=excluded.cursor',
                       (user_id, link['username'].strip().casefold(), now, json.dumps(cursor, separators=(',', ':'))))
        return created

    def state(self, user_id, account):
        with self.connect() as db:
            row = db.execute('SELECT checked_at,cursor FROM drm_revision_polls WHERE user_id=? AND account=?', (user_id, account.strip().casefold())).fetchone()
        return (row['checked_at'], json.loads(row['cursor'])) if row else (None, initial_cursor())

    def history(self, user_id, before=None, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100 or before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807):
            raise ValueError('Halaman riwayat tidak valid.')
        with self.connect() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM drm_revisions WHERE user_id=?', (user_id,)).fetchone()[0]
            unread = db.execute('SELECT count(*) FROM drm_revisions n LEFT JOIN drm_revision_reads r ON r.notification_id=n.id WHERE n.user_id=? AND r.notification_id IS NULL', (user_id,)).fetchone()[0]
            query = 'SELECT n.*,r.read_at FROM drm_revisions n LEFT JOIN drm_revision_reads r ON r.notification_id=n.id WHERE n.user_id=?'
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
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT active FROM users WHERE id=?', (user_id,)).fetchone()
            if not active or not active['active']:
                raise PermissionError('Pengguna EVA tidak aktif.')
            marked = 0
            if ids:
                marked = db.execute('INSERT OR IGNORE INTO drm_revision_reads SELECT id,? FROM drm_revisions WHERE user_id=? AND id IN (' + ','.join('?' for _ in ids) + ')', (now, user_id, *ids)).rowcount
            unread = db.execute('SELECT count(*) FROM drm_revisions n LEFT JOIN drm_revision_reads r ON r.notification_id=n.id WHERE n.user_id=? AND r.notification_id IS NULL', (user_id,)).fetchone()[0]
        return {'marked': marked, 'unread_count': unread}


POLL_INTERVAL = 60
# ponytail: one WO per poll fits observed slow ASTRI details; use a
# background cursor worker only when a larger verified scan is required.
MAX_DETAILS = 1
BATCH_SECONDS = 32


def _get_html(record, url, deadline, params=None):
    import time
    from urllib.parse import parse_qsl, urlsplit

    def attempt():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Batas waktu DRM.')
        return record['session'].get(url, params=params, timeout=(min(4, remaining), min(28, remaining)),
                                     allow_redirects=False, stream=True)

    # ASTRI occasionally answers past the read timeout on a cold cache. One retry
    # inside the remaining batch budget beats surfacing a spurious 503.
    try:
        response = attempt()
    except Exception as exc:
        if type(exc).__name__ not in {'ReadTimeout', 'ConnectTimeout', 'Timeout'}:
            raise
        if deadline - time.monotonic() < 8:
            raise
        response = attempt()
    try:
        actual, expected = urlsplit(response.url), urlsplit(url)
        if (response.status_code != 200 or (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path)
                or expected.query and sorted(parse_qsl(actual.query)) != sorted(parse_qsl(expected.query))):
            raise ValueError('Halaman DRM tidak tersedia.')
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > 5_000_000 or time.monotonic() > deadline:
                raise ValueError('Batas respons DRM terlampaui.')
        return body.decode(response.encoding or 'utf-8-sig')
    finally:
        response.close()


def _current_row(record, base, item, deadline):
    url = base.rstrip('/') + '/material/' + item['scope'] + '/drm/list/v3'
    text = _get_html(record, url, deadline, {'filter[global]': item['code'], 'page': 1, 'limit': 100})
    rows = _list_rows(text, item['scope'], base)
    matches = [row for row in rows if row['code'] == item['code'] and row['work_order_number'] == item['work_order_number']]
    if len(matches) != 1 or matches[0]['wo_uuid'] != item['wo_uuid']:
        # Missing/ambiguous exact identity is an error, not a completed scan.
        raise ValueError('Status WO DRM tidak dapat dipastikan.')
    return matches[0]


def scan_batch(record, base, cursor, max_details=MAX_DETAILS):
    """Round-robin scopes, durable page queues; failure never advances caller state."""
    import copy
    import time
    cursor = copy.deepcopy(cursor)
    deadline = time.monotonic() + BATCH_SECONDS
    items, examined, pages = [], 0, 0
    # ponytail: offset pages are revisited cyclically; ASTRI has no snapshot cursor.
    while examined < max_details and pages < 3 and time.monotonic() < deadline:
        scope = SCOPES[cursor['turn']]
        state = cursor['scopes'][scope]
        if not state['pending']:
            html = _get_html(record, base.rstrip('/') + '/material/' + scope + '/drm/list/v3', deadline,
                             {'page': state['page'], 'limit': 100, 'filter[latest_status_name]': 'closed'})
            rows, count = parse_closed_list(html, scope, base)
            signature = hashlib.sha256(json.dumps(sorted(row['wo_uuid'] for row in rows)).encode()).hexdigest() if count else ''
            if state['page'] > 1 and signature and signature == state['signature']:
                raise ValueError('Halaman DRM berulang.')
            state.update(pending=rows, more=count == 100, signature=signature)
            pages += 1
        if state['pending']:
            item = state['pending'][0]
            current = _current_row(record, base, item, deadline)
            if current['status'] == 'Closed':
                # Revalidate even persisted URLs. Never follow links inside remarks.
                url = _source_url(current['source_url'], base, scope, item['code'], item['work_order_number'])
                html = _get_html(record, url, deadline)
                notes = parse_revision_notes(html, record['username'])
                items.extend(dict(current, **note) for note in notes)
            state['pending'].pop(0)
            examined += 1
        if not state['pending']:
            if state['more']:
                state['page'] += 1
            else:
                state.update(page=1, signature='', cycles=state['cycles'] + 1)
        cursor['turn'] = (cursor['turn'] + 1) % len(SCOPES)
    if not examined and not pages:
        raise TimeoutError('Tidak ada batch DRM yang selesai.')
    # Final status pass occurs after all detail reads, immediately before commit.
    live = {}
    for item in items:
        identity = (item['scope'], item['wo_uuid'])
        if identity not in live:
            live[identity] = _current_row(record, base, item, deadline)
            if live[identity]['status'] == 'Closed' and live[identity]['source_url'] != item['source_url']:
                raise ValueError('Tautan detail DRM berubah selama pemeriksaan.')
    items = [item for item in items if live[item['scope'], item['wo_uuid']]['status'] == 'Closed']
    return items, cursor


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


def register_drm_revisions(app, env, clock=None):
    import fcntl
    import os
    from pathlib import Path
    import time
    from flask import g, jsonify, request

    clock = clock or time.time
    gate = env['EVA_AUTH']
    store = RevisionStore(gate.store.connect)
    app.extensions['eva_drm_revisions'] = store

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
        directory = Path(gate.store.path).parent / 'drm_revision_locks'
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
                items, cursor = scan_batch(record, env['ASTRI_BASE_URL'], cursor)
                now = clock()
                created = store.record(uid, link, items, cursor, now)
                return result(uid, True, True, created, now)
            except PermissionError:
                return jsonify(ok=False, code='DRM_LINK_CHANGED', error='Tautan ASTRI berubah. Muat ulang EVA.'), 409
            except Exception as exc:
                # Class name only: URLs/HTML/transport text may carry credentials.
                app.logger.warning('drm-revisions poll failed: %s', type(exc).__name__)
                return jsonify(ok=False, code='DRM_UPSTREAM_UNAVAILABLE', error='DRM belum dapat diperiksa. Riwayat tetap tersimpan.'), 503
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    app.add_url_rule('/api/astri/drm-revisions', 'drm_revisions_history', history, methods=['GET'])
    app.add_url_rule('/api/astri/drm-revisions/poll', 'drm_revisions_poll', poll, methods=['POST'])
    app.add_url_rule('/api/astri/drm-revisions/read', 'drm_revisions_read', read, methods=['POST'])
