"""Scoped account release. Bind-mounted Python must retain its inode; no image/worker changes."""
import hashlib,json,os,signal,stat,subprocess,tempfile,time
from pathlib import Path
sha=lambda data:hashlib.sha256(data).hexdigest()
def put(path,data,meta,inplace=False):
 if inplace:
  # Exact file bind: rename would leave the running container on the previous inode.
  with path.open('r+b') as f:
   assert (os.fstat(f.fileno()).st_uid,os.fstat(f.fileno()).st_gid,os.fstat(f.fileno()).st_mode&4095)==meta
   f.write(data);f.truncate();f.flush();os.fsync(f.fileno())
 else:
  fd,name=tempfile.mkstemp(prefix='.account-ui-',dir=path.parent)
  try:
   with os.fdopen(fd,'wb') as f:
    os.fchown(f.fileno(),meta[0],meta[1]);os.fchmod(f.fileno(),meta[2]);f.write(data);f.flush();os.fsync(f.fileno())
   os.replace(name,path)
  finally:
   if os.path.exists(name):os.unlink(name)
 fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def install(root,candidate,manifest,guard,reload_web):
 before={p:(root/p).read_bytes() for p in manifest['files']};meta={p:((root/p).stat().st_uid,(root/p).stat().st_gid,(root/p).stat().st_mode&4095) for p in before};applied=[];signaled=False
 for p,d in manifest['files'].items():
  assert sha(before[p])==d['before'],p
  assert sha((candidate/d['name']).read_bytes())==d['after'],p
 guard()
 try:
  for p,d in manifest['files'].items():
   guard();assert (root/p).read_bytes()==before[p],'Foreign bytes before install'
   applied.append(p);put(root/p,(candidate/d['name']).read_bytes(),meta[p],p=='eva_auth.py')
  guard();signaled=True;reload_web();guard()
  for p,d in manifest['files'].items():assert sha((root/p).read_bytes())==d['after'],'Foreign bytes after reload'
 except BaseException:
  if signaled:raise # Uncertain delivery: hold for investigation, never blindly rollback/reload.
  for p in reversed(applied):
   d=manifest['files'][p];assert sha((root/p).read_bytes()) in (d['before'],d['after']),'Foreign bytes during recovery'
   put(root/p,before[p],meta[p],p=='eva_auth.py')
  raise
 return meta
PROC='''import os,json,signal,sys
rows=[]
for p in os.listdir('/proc'):
 if not p.isdigit():continue
 try:
  s=open('/proc/'+p+'/stat').read().split();c=open('/proc/'+p+'/cmdline').read()
  if 'gunicorn' in c:rows.append(dict(pid=int(p),ppid=int(s[3]),start=s[21]))
 except OSError:pass
masters=[r for r in rows if r['ppid']==1];assert len(masters)==1
if len(sys.argv)>1:
 pin=json.loads(sys.argv[1]);assert masters[0]==pin;os.kill(pin['pid'],signal.SIGHUP)
print(json.dumps(dict(master=masters[0],children=[r for r in rows if r['ppid']==masters[0]['pid']])))
'''
def proc(pin=None):
 return json.loads(subprocess.check_output(['docker','exec','eva-converter','python3','-B','-c',PROC]+([json.dumps(pin)] if pin else [])))
def roles():
 rows=json.loads(subprocess.check_output(['docker','inspect','eva-converter','eva-worker','eva-worker2']))
 return [dict(Id=r['Id'],Image=r['Image'],Mounts=sorted(r['Mounts'],key=lambda m:m['Destination'])) for r in rows]
def main():
 import fcntl
 release=Path(__file__).parent;root=Path('/opt/eva');manifest=json.loads((release/'manifest.json').read_text());closure=json.loads((release/'closure.json').read_text())
 for p,h in closure.items():assert sha((release/p).read_bytes())==h,p
 for label in ['a','b']:
  review=json.loads((release/f'review-{label}.json').read_text());assert review['approved'] and review['closure_sha256']==sha((release/'closure.json').read_bytes())
 receipt=release/'deployed.json';assert not receipt.exists()
 backup=Path('/home/ubuntu/eva-backups/account-ui-20261030')
 for p,d in manifest['files'].items():assert sha((backup/p).read_bytes())==d['before']
 pins=roles();initial=proc();auth_inode=(root/'eva_auth.py').stat().st_ino
 def guard():
  assert roles()==pins,'Role drift'
  assert (root/'eva_auth.py').stat().st_ino==auth_inode,'Auth inode drift'
  for p,h in manifest['dependency'].items():assert sha((root/p).read_bytes())==h,p
 def reload_web():
  for p,d in manifest['files'].items():
   actual=subprocess.check_output(['docker','exec','eva-converter','sha256sum','/app/'+p]).decode().split()[0]
   assert actual==d['after'],('Runtime bind mismatch',p)
  # Compile syntax without module import or __pycache__ writes.
  subprocess.check_call(['docker','exec','eva-converter','python3','-B','-c',"compile(open('/app/eva_auth.py').read(),'/app/eva_auth.py','exec')"])
  assert proc()==initial,'Web process drift'
  proc(initial['master']);old={r['pid'] for r in initial['children']}
  for _ in range(90):
   current=proc();assert current['master']==initial['master']
   if current['children'] and old.isdisjoint({r['pid'] for r in current['children']}):return
   time.sleep(1)
  raise RuntimeError('Old children not retired; hold for investigation')
 with (release/'publish.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  metadata=install(root,release/'candidate',manifest,guard,reload_web)
  result=dict(status='deployed',files={p:sha((root/p).read_bytes()) for p in manifest['files']},metadata=metadata,process_before=initial,process_after=proc(),roles_unchanged=roles()==pins,auth_inode_preserved=(root/'eva_auth.py').stat().st_ino==auth_inode)
  with receipt.open('x') as f:json.dump(result,f,indent=2);f.flush();os.fsync(f.fileno())
  print(json.dumps(result,indent=2))
if __name__=='__main__':main()
