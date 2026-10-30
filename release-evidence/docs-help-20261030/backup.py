import hashlib, json, os, subprocess, tarfile, time
from pathlib import Path
ROOT=Path('/opt/eva')
DEST=Path('/home/ubuntu/eva-backups/docs-help-20261030')
FILES=['templates/docs.html','templates/index.html','static/js/app.js']
PINS=['6e6b573fe38ab61adcacd8b3c6616d0317e96e47edcd3c2ee3902622f765105e','3c09ce98c88d4586f0c000e2abc0fc2e2f65143dd1dec4d2724cc09359ac40a9','da5db1b5150b803d9c3f8b0a26a1e9251809797b5ad320d318dd2d4e23d6750d']
def git(*args, data=None, env=None):
 return subprocess.check_output(['git','-C',str(ROOT),*args],input=data,env=env).decode().strip()
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
for p,h in zip(FILES,PINS): assert sha(ROOT/p)==h,p
head=git('rev-parse','HEAD'); index=ROOT/git('rev-parse','--git-path','index'); index_sha=sha(index)
DEST.mkdir(mode=0o700)
import shutil
for p in FILES:
 d=DEST/p; d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(ROOT/p,d); assert sha(d)==sha(ROOT/p)
(DEST/'status.txt').write_text(git('status','--short'))
(DEST/'prepatch.diff').write_text(git('diff','--',*FILES))
env=dict(os.environ,GIT_INDEX_FILE=str(DEST/'backup.index'))
git('read-tree','--empty',env=env)
for p in FILES:
 blob=git('hash-object','-w','--stdin',data=(DEST/p).read_bytes())
 git('update-index','--add','--cacheinfo','100644',blob,p,env=env)
tree=git('write-tree',env=env)
commit=git('-c','user.name=EVA Deploy','-c','user.email=deploy@eva.dyena.id','commit-tree',tree,data=b'backup: documentation help production preimage\n')
ref='refs/heads/backup/docs-help-20261030'
assert not git('ls-remote','origin',ref),'Backup ref exists'
git('push','origin',commit+':'+ref)
assert git('ls-remote','origin',ref).split()[0]==commit
assert git('ls-tree','-r','--name-only',commit).splitlines()==sorted(FILES)
bare=DEST/'readback.git'
subprocess.check_call(['git','init','--bare','-q',str(bare)])
remote=git('remote','get-url','origin')
subprocess.check_call(['git','--git-dir',str(bare),'fetch','-q','--depth=1','--no-tags',remote,ref])
assert subprocess.check_output(['git','--git-dir',str(bare),'rev-parse','FETCH_HEAD']).decode().strip()==commit
for p,h in zip(FILES,PINS):
 b=subprocess.check_output(['git','--git-dir',str(bare),'show',commit+':'+p]); assert hashlib.sha256(b).hexdigest()==h
assert git('rev-parse','HEAD')==head and sha(index)==index_sha
receipt=dict(backup=str(DEST),ref=ref,commit=commit,hashes=dict(zip(FILES,PINS)),independent_fetch=True,head_unchanged=True,index_unchanged=True)
(DEST/'backup-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
with tarfile.open(DEST/'preimage.tar.gz','w:gz') as tar:
 for p in FILES+['backup-receipt.json','status.txt','prepatch.diff']: tar.add(DEST/p,arcname=p)
print(json.dumps(receipt,indent=2)); print('ARCHIVE_SHA256',sha(DEST/'preimage.tar.gz'))
