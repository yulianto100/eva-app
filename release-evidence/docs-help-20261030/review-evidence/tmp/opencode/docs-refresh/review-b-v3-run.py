import hashlib,json,subprocess,tempfile,shutil
from pathlib import Path
ROOT=Path('/tmp/opencode/docs-refresh/release-v3');OUT=ROOT.parent
EXPECTED='c06491718aad1a4afe99333a918d5447f8289f759464bf7ff41293bd98b32dc8'
def integrity():
 assert hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest()==EXPECTED
 entries=json.loads((ROOT/'manifest.json').read_text())
 for name,h in entries.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h,name
 return len(entries)
assert integrity()==15
with (OUT/'review-b-v3-integrity.txt').open('x') as log:log.write('PASS before: expected manifest SHA256 and all 15 entries\n')
with (OUT/'review-b-v3-gate.txt').open('xb') as log:
 r=subprocess.run(['python3','-B',str(ROOT/'check.py')],stdout=log,stderr=subprocess.STDOUT)
 log.write(('\ngate_exit='+str(r.returncode)+'\n').encode())
assert r.returncode==0
with tempfile.TemporaryDirectory(prefix='review-b-v3-mutation-',dir='/tmp/opencode') as tmp:
 copy=Path(tmp)/'release';shutil.copytree(ROOT,copy)
 p=copy/'publisher.py';text=p.read_text();needle="for name,p in FILES.items(): assert (root/p).read_bytes()==(candidate/name).read_bytes(), 'Foreign target after reload'"
 lines=text.splitlines(True);assert sum(line.strip()==needle for line in lines)==1;p.chmod(0o600);p.write_text(''.join(line for line in lines if line.strip()!=needle))
 with (OUT/'review-b-v3-mutation.txt').open('xb') as log:
  log.write(b'Private temporary copy only: remove post-reload candidate assertion. Frozen release unchanged.\n');log.flush()
  m=subprocess.run(['python3','-B',str(copy/'check.py')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
  log.write(m.stdout);log.write(('\nmutation_gate_exit='+str(m.returncode)+'\n').encode())
 assert m.returncode==1
 assert b"AssertionError: ('Expected rejection', 'reload-foreign')" in m.stdout
print('PASS exact v3 gate exit 0; missing-assertion negative control exit 1 at expected reload-foreign assertion')
assert integrity()==15
with (OUT/'review-b-v3-integrity.txt').open('a') as log:log.write('PASS after: expected manifest SHA256 and all 15 entries unchanged\n')
