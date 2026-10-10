"""Private R2 output mirror; source of truth for ownership remains EVA auth.

Docs: https://developers.cloudflare.com/r2/examples/aws/boto3/
      https://developers.cloudflare.com/r2/api/s3/presigned-urls/
"""
import hashlib
import json
import logging
from pathlib import Path
import re
import stat
import time

LOGGER = logging.getLogger(__name__)
SETTINGS_PATH = Path(__file__).resolve().parent / '.secrets' / 'r2.json'
OUTPUT_ROOT = Path(__file__).resolve().parent / 'outputs'
ENDPOINT = re.compile(r'https://[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com/?\Z')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z')
LINK_SECONDS = 300
CHUNK = 1024 * 1024
MAX_MIRROR_BYTES = 256 * CHUNK
MIRROR_SECONDS = 60
DOWNLOAD_SECONDS = 8


def _digest(stream, max_bytes=MAX_MIRROR_BYTES, deadline=None):
    sha = hashlib.sha256()
    size = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('R2 mirror deadline exceeded')
        data = stream.read(min(CHUNK, max_bytes - size + 1))
        if not data:
            break
        size += len(data)
        if size > max_bytes:
            raise ValueError('R2 object exceeds expected size')
        sha.update(data)
    return sha.hexdigest(), size


class R2Storage:
    def __init__(self, settings_path=SETTINGS_PATH, output_root=OUTPUT_ROOT, *, client=None, _inline=False):
        self.settings_path = Path(settings_path)
        self.output_root = Path(output_root).resolve()
        self._client_override = client
        self._inline = _inline

    def _bounded(self, operation, job, method='GET'):
        import subprocess
        import sys
        # A killed subprocess closes sockets too; a timed-out thread would keep uploading.
        fields = ('job_id', 'owner_user_id', 'output_path', 'download_name',
                  'download_mimetype', 'status', 'r2_upload_status', 'r2_output')
        payload = json.dumps({key: job[key] for key in fields if key in job})
        seconds = MIRROR_SECONDS if operation == 'publish' else DOWNLOAD_SECONDS
        result = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()), operation,
             str(self.settings_path), str(self.output_root), method],
            input=payload, capture_output=True, text=True, timeout=seconds,
        )
        if result.returncode != 0:
            raise RuntimeError('R2 subprocess failed')
        return json.loads(result.stdout)

    def settings(self):
        try:
            info = self.settings_path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('R2 configuration must be a private regular file')
        config = json.loads(self.settings_path.read_text(encoding='utf-8'))
        if config.get('enabled') is not True:
            return None
        if (not ENDPOINT.fullmatch(str(config.get('endpoint', '')))
                or not BUCKET.fullmatch(str(config.get('bucket', '')))
                or not config.get('access_key_id') or not config.get('secret_access_key')):
            raise ValueError('Invalid R2 configuration')
        return config

    def client(self, config):
        if self._client_override is not None:
            return self._client_override
        import boto3
        from botocore.config import Config
        return boto3.client(
            's3', endpoint_url=config['endpoint'], region_name='auto',
            aws_access_key_id=config['access_key_id'],
            aws_secret_access_key=config['secret_access_key'],
            config=Config(signature_version='s3v4', connect_timeout=3, read_timeout=15,
                          retries={'mode': 'standard', 'total_max_attempts': 2},
                          s3={'addressing_style': 'path'},
                          request_checksum_calculation='when_required',
                          response_checksum_validation='when_required'),
        )

    def identity(self, job):
        owner, job_id = job.get('owner_user_id'), job.get('job_id')
        if not isinstance(owner, str) or not IDENTIFIER.fullmatch(owner):
            raise ValueError('R2 output requires an immutable EVA creator')
        if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id):
            raise ValueError('Invalid job ID')
        return owner, job_id

    def output_path(self, job):
        path = Path(job['output_path']).resolve()
        if path == self.output_root or self.output_root not in path.parents:
            raise ValueError('Output is outside the EVA output directory')
        if not path.is_file():
            raise ValueError('Output is not a regular file')
        return path

    def publish(self, job):
        config = self.settings()
        if config is None:
            return {}
        if not job.get('owner_user_id'):
            return {'r2_upload_status': 'skipped_legacy'}
        owner, job_id = self.identity(job)
        path = self.output_path(job)
        size_before = path.stat().st_size
        if size_before > MAX_MIRROR_BYTES:
            raise ValueError('Output exceeds the R2 mirror size budget')
        if not self._inline and self._client_override is None:
            return self._bounded('publish', job)
        deadline = time.monotonic() + MIRROR_SECONDS
        with path.open('rb') as stream:
            digest, size = _digest(stream, max_bytes=size_before, deadline=deadline)
        key = f'outputs/{owner}/{job_id}/{digest}'
        sdk = self.client(config)
        old = job.get('r2_output') or {}
        if (job.get('r2_upload_status') == 'ready' and old.get('bucket') == config['bucket']
                and old.get('key') == key and old.get('sha256') == digest and old.get('size') == size):
            return {'r2_upload_status': 'ready', 'r2_output': old}
        from boto3.s3.transfer import TransferConfig
        sdk.upload_file(
            str(path), config['bucket'], key,
            ExtraArgs={'ContentType': job.get('download_mimetype') or 'application/octet-stream',
                       'Metadata': {'sha256': digest}},
            Config=TransferConfig(multipart_threshold=16 * CHUNK,
                                  multipart_chunksize=16 * CHUNK, use_threads=False),
        )
        head = sdk.head_object(Bucket=config['bucket'], Key=key)
        if head.get('ContentLength') != size or head.get('Metadata', {}).get('sha256') != digest:
            raise ValueError('R2 upload metadata does not match output')
        body = sdk.get_object(Bucket=config['bucket'], Key=key)['Body']
        try:
            remote_digest, remote_size = _digest(body, max_bytes=size, deadline=deadline)
        finally:
            body.close()
        if (remote_digest, remote_size) != (digest, size):
            raise ValueError('R2 read-back does not match output')
        # ponytail: mirror only; existing local retention stays unchanged.
        return {'r2_upload_status': 'ready', 'r2_output': {
            'bucket': config['bucket'], 'key': key, 'sha256': digest,
            'size': size, 'verified_at': time.time(),
        }}

    def download_url(self, job, method='GET'):
        if method not in {'GET', 'HEAD'}:
            raise ValueError('Unsupported R2 download method')
        config = self.settings()
        meta = job.get('r2_output')
        if config is None or job.get('r2_upload_status') != 'ready' or not isinstance(meta, dict):
            return None
        owner, job_id = self.identity(job)
        digest = meta.get('sha256', '')
        if (job.get('status') != 'done' or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or meta.get('bucket') != config['bucket']
                or meta.get('key') != f'outputs/{owner}/{job_id}/{digest}'
                or type(meta.get('size')) is not int or meta['size'] < 0):
            raise ValueError('R2 object identity mismatch')
        if not self._inline and self._client_override is None:
            return self._bounded('download', job, method)
        sdk = self.client(config)
        head = sdk.head_object(Bucket=config['bucket'], Key=meta['key'])
        if head.get('ContentLength') != meta['size'] or head.get('Metadata', {}).get('sha256') != digest:
            raise ValueError('R2 output is no longer valid')
        name = str(job.get('download_name') or f'{job_id}.bin')
        if '/' in name or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError('Invalid download filename')
        from urllib.parse import quote
        fallback = name.encode('ascii', 'ignore').decode().replace('\\', '\\\\').replace('"', '\\"') or 'download'
        disposition = f'attachment; filename="{fallback}"'
        if not name.isascii():
            disposition += "; filename*=UTF-8''" + quote(name, safe='')
        params = {'Bucket': config['bucket'], 'Key': meta['key']}
        if method == 'GET':
            params.update(ResponseContentDisposition=disposition,
                          ResponseContentType=job.get('download_mimetype') or 'application/octet-stream')
        url = sdk.generate_presigned_url(
            'get_object' if method == 'GET' else 'head_object', Params=params,
            ExpiresIn=LINK_SECONDS, HttpMethod=method,
        )
        return url


def publish_job_output(job):
    try:
        return R2Storage().publish(job)
    except Exception as exc:
        LOGGER.warning('r2_publish_failed job_id=%s error_type=%s local_output_preserved=true',
                       job.get('job_id'), type(exc).__name__)
        return {'r2_upload_status': 'failed'}


def job_download_url(job, method='GET'):
    try:
        return R2Storage().download_url(job, method=method)
    except Exception as exc:
        LOGGER.warning('r2_download_fallback job_id=%s error_type=%s',
                       job.get('job_id'), type(exc).__name__)
        return None


if __name__ == '__main__':
    import sys
    try:
        operation, settings, root, method = sys.argv[1:]
        import signal
        # Also terminate if the parent worker is cancelled while this child runs.
        signal.setitimer(signal.ITIMER_REAL, MIRROR_SECONDS if operation == 'publish' else DOWNLOAD_SECONDS)
        job = json.load(sys.stdin)
        storage = R2Storage(settings, root, _inline=True)
        if operation == 'publish':
            result = storage.publish(job)
        elif operation == 'download':
            result = storage.download_url(job, method=method)
        else:
            raise ValueError('Unknown R2 operation')
        sys.stdout.write(json.dumps(result))
    except Exception:
        # Provider exception text can contain signed URLs; do not emit it.
        raise SystemExit(1)
