"""Isolated, same-origin ABD CAD endpoints for the EVA web app."""
import copy
import fcntl
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, render_template, request, send_file, session

if __package__:
    from .abd_cad import _validate
else:
    from abd_cad import _validate


LOG = logging.getLogger(__name__)
MAX_JSON_BYTES = 65536
JOB_TTL = 3600
JOB_TIMEOUT = 900
TIMEOUT_ERROR = 'Proses ABD CAD melewati batas waktu. Silakan coba lagi.'
RESTART_ERROR = 'Proses ABD CAD terhenti. Silakan coba lagi.'


def register_abd_cad(app, generate, template_path):
    """Register isolated synchronous and opt-in job endpoints."""
    template_path = Path(template_path)
    job_root = Path(app.config.get('ABD_CAD_JOB_ROOT', '/tmp/eva-abd-cad-jobs')).resolve()

    def acquire_lock():
        job_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (job_root / '.generation.lock').open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def write_status(folder, state):
        pending = folder / 'status.tmp'
        try:
            pending.write_text(json.dumps(state))
            pending.replace(folder / 'status.json')
        finally:
            pending.unlink(missing_ok=True)

    def owner():
        token = session.get('abd_cad_csrf_token', '')
        return hashlib.sha256(token.encode()).hexdigest() if token else ''

    def json_response(data, code=200):
        response = jsonify(data)
        response.status_code = code
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    def remove_output(output):
        if output.is_symlink():
            output.unlink()
        else:
            shutil.rmtree(output, ignore_errors=True)

    def expire_job(folder, state):
        # Only call while holding the generation lock: never erase active output.
        if state['status'] == 'running':
            remove_output(folder / 'output')
            (folder / 'ABD_CAD.zip').unlink(missing_ok=True)
            error = TIMEOUT_ERROR if time.time() - state['created'] > JOB_TIMEOUT else RESTART_ERROR
            state = dict(state, status='error', error=error, finished=time.time())
            write_status(folder, state)
        elif time.time() - state['finished'] > JOB_TTL:
            shutil.rmtree(folder)
            return None
        return state

    def cleanup_jobs():
        for folder in job_root.iterdir():
            if folder.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', folder.name):
                continue
            try:
                expire_job(folder, json.loads((folder / 'status.json').read_text()))
            except (OSError, ValueError, KeyError):
                continue

    def owned_job(job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id) or not owner():
            return None, None
        folder = job_root / job_id
        try:
            if folder.is_symlink():
                return None, None
            state = json.loads((folder / 'status.json').read_text())
            if not secrets.compare_digest(state.get('owner', ''), owner()):
                return None, None
            expired = state['status'] != 'running' and time.time() - state['finished'] > JOB_TTL
            if state['status'] == 'running' or expired:
                lock = acquire_lock()
                if lock is not None:
                    try:
                        state = expire_job(folder, json.loads((folder / 'status.json').read_text()))
                    finally:
                        lock.close()
                elif state['status'] == 'running' and time.time() - state['created'] > JOB_TIMEOUT:
                    # A live thread cannot be killed safely: deny download, keep lock/output.
                    state = dict(state, status='error', error=TIMEOUT_ERROR)
                if expired or state is None:
                    return None, None
            return folder, state
        except (OSError, ValueError, KeyError):
            return None, None

    def run_job(folder, spec, state, lock):
        output = folder / 'output'
        try:
            output.mkdir()
            archive = Path(generate(spec, template_path, output)).resolve()
            if output.is_symlink() or not archive.is_file() or not archive.is_relative_to(output):
                raise RuntimeError('Generator produced no archive inside output directory')
            latest = json.loads((folder / 'status.json').read_text())
            if time.time() - latest['created'] > JOB_TIMEOUT:
                raise ValueError(TIMEOUT_ERROR)
            shutil.copyfile(archive, folder / 'ABD_CAD.zip')
            state = dict(state, status='done')
        except Exception as exc:
            if not isinstance(exc, ValueError):
                LOG.exception('ABD CAD job failed')
            (folder / 'ABD_CAD.zip').unlink(missing_ok=True)
            state = dict(state, status='error', error=str(exc) if isinstance(exc, ValueError) else 'Gagal membuat ABD CAD. Periksa log server.')
        finally:
            try:
                remove_output(output)
                write_status(folder, dict(state, finished=time.time()))
            finally:
                lock.close()

    @app.get('/api/abd-cad/jobs/<job_id>')
    def abd_cad_status(job_id):
        folder, state = owned_job(job_id)
        if folder is None:
            return json_response({'error': 'Pekerjaan ABD CAD tidak ditemukan.'}, 404)
        return json_response({key: state[key] for key in ('status', 'error') if key in state})

    @app.get('/api/abd-cad/jobs/<job_id>/download')
    def abd_cad_download(job_id):
        folder, state = owned_job(job_id)
        if folder is None:
            return json_response({'error': 'Pekerjaan ABD CAD tidak ditemukan.'}, 404)
        if state['status'] != 'done':
            return json_response({'error': state.get('error', 'ABD CAD belum selesai.')}, 409)
        archive = folder / 'ABD_CAD.zip'
        try:
            if archive.is_symlink() or not archive.is_file():
                return json_response({'error': 'Pekerjaan ABD CAD tidak ditemukan.'}, 404)
            payload = io.BytesIO(archive.read_bytes())
        except OSError:
            return json_response({'error': 'Pekerjaan ABD CAD tidak ditemukan.'}, 404)
        response = send_file(payload, as_attachment=True,
                             download_name='ABD_CAD.zip', mimetype='application/zip')
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.get('/abd-cad')
    def abd_cad_page():
        return render_template('abd_cad.html')

    @app.get('/api/abd-cad/token')
    def abd_cad_token():
        if not session.get('abd_cad_csrf_token'):
            session['abd_cad_csrf_token'] = secrets.token_urlsafe(32)
        response = jsonify({'csrf_token': session['abd_cad_csrf_token']})
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @app.post('/api/abd-cad/generate')
    def abd_cad_generate():
        expected = session.get('abd_cad_csrf_token', '')
        actual = request.headers.get('X-ABD-CAD-CSRF', '')
        if not expected or not actual or not secrets.compare_digest(expected.encode(), actual.encode()):
            return jsonify({'error': 'Sesi ABD CAD tidak valid. Muat ulang halaman.'}), 403
        if request.mimetype != 'application/json':
            return jsonify({'error': 'Kirim data JSON.'}), 415
        if request.content_length is None or request.content_length > MAX_JSON_BYTES:
            return jsonify({'error': 'Input terlalu besar.'}), 413
        spec = request.get_json(silent=True)
        if not isinstance(spec, dict):
            return jsonify({'error': 'Format input tidak valid.'}), 400
        if not isinstance(spec.get('fdts'), list) or not 1 <= len(spec['fdts']) <= 10:
            return jsonify({'error': 'Pilih 1–10 FDT per proses.'}), 400
        try:
            _validate(spec)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if not template_path.is_file():
            LOG.error('ABD CAD source template missing: %s', template_path)
            return jsonify({'error': 'Template ABD CAD belum tersedia.'}), 503
        lock = None
        job_folder = None
        try:
            lock = acquire_lock()
            if lock is None:
                return json_response({'error': 'ABD CAD sedang diproses. Tunggu hingga selesai lalu coba lagi.'}, 409)
            cleanup_jobs()
            if request.args.get('async') == '1':
                job_id = uuid.uuid4().hex
                job_folder = job_root / job_id
                job_folder.mkdir(mode=0o700)
                state = {'status': 'running', 'owner': owner(), 'created': time.time()}
                write_status(job_folder, state)
                response = json_response({'job_id': job_id, 'status': 'running',
                                          'status_url': f'/api/abd-cad/jobs/{job_id}',
                                          'download_url': f'/api/abd-cad/jobs/{job_id}/download'}, 202)
                # ponytail: one Linux/file-backed job; restart reports failure, no durable queue.
                threading.Thread(target=run_job, args=(job_folder, copy.deepcopy(spec), state, lock), daemon=True).start()
                lock = None  # Ownership transferred to the background thread.
                return response
            with tempfile.TemporaryDirectory(prefix='eva-abd-cad-') as folder:
                output = Path(folder).resolve()
                archive = Path(generate(spec, template_path, output)).resolve()
                if output.is_symlink() or not archive.is_file() or not archive.is_relative_to(output):
                    raise RuntimeError('Generator produced no archive inside output directory')
                payload = io.BytesIO(archive.read_bytes())
                payload.seek(0)
            response = send_file(payload, as_attachment=True, download_name='ABD_CAD.zip', mimetype='application/zip')
            response.headers['Cache-Control'] = 'private, no-store'
            return response
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception:
            LOG.exception('ABD CAD generation failed')
            return json_response({'error': 'Gagal membuat ABD CAD. Periksa log server.'}, 500)
        finally:
            if lock is not None:
                try:
                    if job_folder is not None:
                        shutil.rmtree(job_folder, ignore_errors=True)
                finally:
                    lock.close()
