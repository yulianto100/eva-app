import hashlib,importlib.util,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('publisher',Path(__file__).with_name('publish.py'));p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
sha=lambda b:hashlib.sha256(b).hexdigest()
for case in ['success','pre-reload-failure','uncertain-signal','foreign-after-reload']:
 with tempfile.TemporaryDirectory() as t:
  root=Path(t)/'root';candidate=Path(t)/'candidate';root.mkdir();candidate.mkdir();files={}
  for name in ['account.html','eva_auth.py']:
   (root/name).write_bytes(b'old');(candidate/name).write_bytes(b'new');files[name]=dict(name=name,before=sha(b'old'),after=sha(b'new'))
  inode=(root/'eva_auth.py').stat().st_ino;reloads=[];calls=[]
  def guard():
   calls.append(1)
   if case=='pre-reload-failure' and len(calls)==4:raise RuntimeError('before signal')
  def reload():
   reloads.append(1)
   if case=='uncertain-signal':raise RuntimeError('uncertain')
   if case=='foreign-after-reload':(root/'account.html').write_bytes(b'foreign')
  failed=False
  try:p.install(root,candidate,dict(files=files),guard,reload)
  except (RuntimeError,AssertionError):failed=True
  assert failed==(case!='success'),case
  assert (root/'eva_auth.py').stat().st_ino==inode,case
  assert len(reloads)==(0 if case=='pre-reload-failure' else 1),case
  if case=='pre-reload-failure':assert all((root/n).read_bytes()==b'old' for n in files)
  if case=='foreign-after-reload':assert (root/'account.html').read_bytes()==b'foreign'
print('PASS: install, pre-signal rollback, uncertain-signal hold, foreign-byte hold, bind inode preservation')
