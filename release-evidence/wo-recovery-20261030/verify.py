"""Read-only runtime verification; no application imports or upstream writes."""
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

STAGE = Path('/home/ubuntu/eva-backups/wo-recovery-candidate')
receipt = STAGE / 'runtime-receipt.json'
receipt.unlink(missing_ok=True)
manifest = {
    'static/js/app.js': 'da5db1b5150b803d9c3f8b0a26a1e9251809797b5ad320d318dd2d4e23d6750d',
    'templates/index.html': '3c09ce98c88d4586f0c000e2abc0fc2e2f65143dd1dec4d2724cc09359ac40a9',
}
receipts = []
for name in ('static/js/app.js', 'templates/index.html'):
    expected = manifest[name]
    host = hashlib.sha256((Path('/opt/eva') / name).read_bytes()).hexdigest()
    runtime = subprocess.check_output(['docker', 'exec', 'eva-converter', 'sha256sum', '/app/' + name], text=True).split()[0]
    assert host == runtime == expected, name
    receipts.append({'file': name, 'host_runtime_sha256': host})
asset_url = 'https://eva.dyena.id/static/js/app.js?v=wo-recovery-20261030-v1'
with urllib.request.urlopen(asset_url, timeout=30) as response:
    public = response.read()
assert hashlib.sha256(public).hexdigest() == manifest['static/js/app.js']
receipts.append({'public_asset': asset_url, 'sha256': manifest['static/js/app.js']})
for attempt in range(5):
    try:
        with urllib.request.urlopen('https://eva.dyena.id/healthz', timeout=8) as response:
            health = json.load(response)
        assert health.get('status') == 'ok'
        break
    except Exception:
        if attempt == 4:
            raise
        time.sleep(2)
receipts.append({'health': health})
raw = subprocess.check_output(['docker', 'inspect', 'eva-converter', 'eva-worker', 'eva-worker2', '--format', '{{.Name}} {{.Image}} {{.State.StartedAt}} {{.RestartCount}}'], text=True)
pre = Path('/home/ubuntu/eva-backups/wo-recovery-20261030-180737/runtime.txt').read_text()
assert raw == pre, 'Container images/start/restart counters changed'
receipts.append({'containers_unchanged': raw.splitlines()})
# Anonymous endpoint must not expose status; no authenticated or upstream probe.
request = urllib.request.Request('https://eva.dyena.id/api/astri/work-order/status?scope=subfeeder&subfeeder_code=BKS004962&work_order_number=WO%2FALL%2F2026%2FDOCU%2F16%2F85333')
try:
    urllib.request.urlopen(request, timeout=8)
except urllib.error.HTTPError as error:
    assert error.code == 401
    receipts.append({'anonymous_wo_status_http': error.code})
else:
    raise AssertionError('Anonymous WO status unexpectedly allowed')
temporary = STAGE / 'runtime-receipt.json.next'
temporary.write_text(json.dumps(receipts, indent=2) + '\n')
temporary.replace(receipt)
print(json.dumps(receipts, indent=2))
