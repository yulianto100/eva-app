import hashlib, importlib.util, json, os, subprocess, tempfile
from pathlib import Path
ROOT=Path('/tmp/opencode/docs-refresh/release-v2')
EXPECTED='9cb83e8acca3c021fc01081a4905cd39bf4dea5a7555a13e2b0f36a765052d7f'
def integrity():
 assert hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest()==EXPECTED
 for name,h in json.loads((ROOT/'manifest.json').read_text()).items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h,name
integrity()
spec=importlib.util.spec_from_file_location('publisher_b_v2',ROOT/'publisher.py');pub=importlib.util.module_from_spec(spec);spec.loader.exec_module(pub)
authority=json.loads((ROOT/'authority.json').read_text())
for target in pub.FILES.values():
 with tempfile.TemporaryDirectory(prefix='review-b-v2-',dir='/tmp/opencode') as tmp:
  root=Path(tmp);meta={'files':{}};reloads=[]
  for name,p in pub.FILES.items():
   f=root/p;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes((ROOT/'baseline'/name).read_bytes());f.chmod(authority['files'][p]['mode']);s=f.stat()
   meta['files'][p]=dict(sha256=pub.sha(f.read_bytes()),uid=s.st_uid,gid=s.st_gid,mode=s.st_mode&4095)
  def reload_web():reloads.append('web HUP');(root/target).write_bytes(b'B foreign during reload')
  caught=None
  try:pub.publish(root,ROOT/'candidate',meta,lambda:None,reload_web)
  except AssertionError as e:caught=str(e)
  assert caught=='Foreign target after reload',(target,caught)
  assert reloads==['web HUP'],reloads
  for name,p in pub.FILES.items():
   assert (root/p).read_bytes()==(b'B foreign during reload' if p==target else (ROOT/'candidate'/name).read_bytes())
   s=(root/p).stat();m=meta['files'][p];assert (s.st_uid,s.st_gid,s.st_mode&4095)==(m['uid'],m['gid'],m['mode'])
  print('PASS independent reload-foreign '+target+': exact rejection, foreign/candidate files retained, metadata preserved, exactly one reload')
# Re-execute B-owned checks against v2 bytes, without editing old evidence or the freeze.
for name in ['review-b-retention.py','review-b-supplement.js']:
 old=Path('/tmp/opencode/docs-refresh')/name
 text=old.read_text().replace('/release-v1','/release-v2')
 if name.endswith('.py'):
  text=text.replace('5a3aa657a10f03a802348a37993302c37cb92072a3aacd88f81af068fcf48159',EXPECTED).replace('all 13 entries','all 15 entries')
 dst=Path('/tmp/opencode/docs-refresh')/name.replace('review-b-','review-b-v2-')
 assert not dst.exists();dst.write_text(text)
 subprocess.run((['python3','-B'] if name.endswith('.py') else ['node'])+[str(dst)],check=True)
for name in ['publisher.py','check.py','verify-installed.py','backup.py']:compile((ROOT/name).read_text(),str(ROOT/name),'exec')
subprocess.run(['node','--check',str(ROOT/'installed-ui.js')],check=True)
print('PASS installed proof scripts syntax; inspected only, not run against production')
integrity();print('PASS final v2 manifest and 15 entries unchanged')
