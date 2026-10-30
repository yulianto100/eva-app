"""Three-file frontend publisher. No backend, worker, DB, Redis or image writes."""
import hashlib, json, os, signal, stat, subprocess, tempfile, time
from pathlib import Path
FILES={'docs.html':'templates/docs.html','app.js':'static/js/app.js','index.html':'templates/index.html'}
def sha(data): return hashlib.sha256(data).hexdigest()
def atomic(path, data, metadata):
 fd,name=tempfile.mkstemp(prefix='.docs-help-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as f:
   os.fchown(f.fileno(),metadata['uid'],metadata['gid']); os.fchmod(f.fileno(),metadata['mode'])
   f.write(data); f.flush(); os.fsync(f.fileno())
  os.replace(name,path)
  fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try: os.fsync(fd)
  finally: os.close(fd)
 finally:
  if os.path.exists(name): os.unlink(name)
def publish(root,candidate,authority,guard,reload_web,event=lambda phase:None):
 before={p:(root/p).read_bytes() for p in FILES.values()}; applied=[]; reload_started=False
 for name,p in FILES.items():
  assert sha(before[p])==authority['files'][p]['sha256'],p
 guard()
 try:
  for name,p in FILES.items():
   guard(); meta=authority['files'][p]
   assert (root/p).read_bytes()==before[p], 'Foreign target before publish'
   # Record ownership immediately after replacement, including a later fsync failure.
   applied.append((p,(candidate/name).read_bytes()))
   atomic(root/p,(candidate/name).read_bytes(),meta); event(p)
  for name,p in FILES.items(): assert (root/p).read_bytes()==(candidate/name).read_bytes()
  guard(); reload_started=True; reload_web(); event('reloaded'); guard()
  for name,p in FILES.items(): assert (root/p).read_bytes()==(candidate/name).read_bytes(), 'Foreign target after reload'
  return {'status':'deployed','files':{p:sha((root/p).read_bytes()) for p in FILES.values()}}
 except BaseException:
  # Signal transport failure is uncertain: never blindly reload or overwrite after it.
  if reload_started: raise
  for p,data in reversed(applied):
   current=(root/p).read_bytes()
   assert current in (before[p],data), 'Foreign target during recovery'
   if current==data: atomic(root/p,before[p],authority['files'][p])
  raise

def inspect_roles():
 rows=json.loads(subprocess.check_output(['docker','inspect','eva-converter','eva-worker','eva-worker2','eva-redis']))
 result=[]
 for d in rows:
  mounts=sorted(d['Mounts'],key=lambda m:m['Destination']); assert len({m['Destination'] for m in mounts})==len(mounts)
  result.append(dict(name=d['Name'],id=d['Id'],image=d['Image'],started=d['State']['StartedAt'],user=d['Config']['User'],mounts=mounts,config_sha256=sha(json.dumps(d['Config'],sort_keys=True).encode()),hostconfig_sha256=sha(json.dumps(d['HostConfig'],sort_keys=True).encode())))
 return result
PROC='''import os,json,signal,sys,time
pinned=json.loads(sys.argv[1]); rows=[]
for p in os.listdir('/proc'):
 if not p.isdigit(): continue
 try:
  s=open('/proc/'+p+'/stat').read().split(); c=open('/proc/'+p+'/cmdline').read()
  if 'gunicorn' in c: rows.append(dict(pid=int(p),ppid=int(s[3]),start=s[21]))
 except OSError: pass
masters=[r for r in rows if r['ppid']==1]
assert len(masters)==1
if pinned:
 assert masters[0]==pinned
 if sys.argv[2]=='signal': os.kill(pinned['pid'],signal.SIGHUP)
print(json.dumps(dict(master=masters[0],children=[r for r in rows if r['ppid']==masters[0]['pid']])))
'''
def process(pinned=None,action='read'):
 return json.loads(subprocess.check_output(['docker','exec','eva-converter','python3','-B','-c',PROC,json.dumps(pinned or {}),action]))
def main():
 import fcntl
 release=Path(__file__).parent; root=Path('/opt/eva'); authority=json.loads((release/'authority.json').read_text())
 manifest=json.loads((release/'manifest.json').read_text())
 for name,h in manifest.items(): assert sha((release/name).read_bytes())==h,name
 reviews=[json.loads((release/('review-'+r+'.json')).read_text()) for r in ('a','b')]
 for r in reviews:
  assert r['approved'] is True and r['manifest_sha256']==sha((release/'manifest.json').read_bytes()) and r['scope']=='docs-help-three-files-web-hup'
 assert reviews[0]['reviewer']!=reviews[1]['reviewer']
 receipt=release/'deployed.json'; assert not receipt.exists(),'Already applied'
 backup=Path('/home/ubuntu/eva-backups/docs-help-20261030')
 for p in FILES.values(): assert sha((backup/p).read_bytes())==authority['files'][p]['sha256']
 def guard():
  assert inspect_roles()==authority['roles'],'Runtime drift'
  for p,meta in authority['files'].items():
   f=root/p; s=f.lstat(); assert stat.S_ISREG(s.st_mode)
   assert (s.st_uid,s.st_gid,s.st_mode&4095)==(meta['uid'],meta['gid'],meta['mode']),p
   if p not in FILES.values(): assert sha(f.read_bytes())==meta['sha256'],p
 guard(); initial=process(); master=initial['master']
 assert initial['children'],'No gunicorn children'
 def reload_web():
  assert process(master)['children']==initial['children'],'Children drift before HUP'
  process(master,'signal')
  old={r['pid'] for r in initial['children']}
  for _ in range(90):
   current=process(master)
   if current['children'] and old.isdisjoint({r['pid'] for r in current['children']}): return
   time.sleep(1)
  raise RuntimeError('Old web workers not retired; publication held for investigation')
 with open(release/'publish.lock','a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  result=publish(root,release/'candidate',authority,guard,reload_web)
  result['process_before']=initial; result['process_after']=process(master)
  result['metadata']={p:dict(inode=(root/p).stat().st_ino,uid=(root/p).stat().st_uid,gid=(root/p).stat().st_gid,mode=(root/p).stat().st_mode&4095) for p in FILES.values()}
  # Exclusive durable success evidence; a failed write never triggers a second deployment.
  with receipt.open('x') as f:
   json.dump(result,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
  print(json.dumps(result,indent=2))
if __name__=='__main__': main()
