import hashlib, importlib.util, json, os, re, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location('publisher',ROOT/'publisher.py'); pub=importlib.util.module_from_spec(spec); spec.loader.exec_module(pub)
base=ROOT/'baseline'; candidate=ROOT/'candidate'
js=(candidate/'app.js').read_text(); old=(base/'app.js').read_text()
assert js.replace('    window.open("/docs#bantuan", "_blank", "noopener");','    populateFolderChecklist();\n    openModal("helpModal");')==old
index=(candidate/'index.html').read_text(); original=(base/'index.html').read_text()
expected=re.sub(r'            <button id="header-help".*?</button>\n','',original,count=1,flags=re.S)
expected=re.sub(r'  <!-- Help modal -->.*?(?=  <!-- Activity monitor drawer -->)','',expected,count=1,flags=re.S)
expected=expected.replace('data-tooltip="Dokumentasi"','data-tooltip="Dokumentasi & bantuan"').replace('aria-label="Dokumentasi"','aria-label="Dokumentasi & bantuan"').replace('?v=wo-recovery-20261030-v1','?v=docs-help-20261030-v1')
assert index==expected
assert 'id="header-help"' not in index and 'id="helpModal"' not in index
assert 'e.key === "?"' in js
html=(candidate/'docs.html').read_text()
for id in re.findall(r'<section id="([^"]+)"',(base/'docs.html').read_text()): assert f'id="{id}"' in html,id
for variable in ('requiredKmlFolders','optionalKmlFolders'):
 block=re.search(r'const '+variable+r' = \[(.*?)\];',old,re.S).group(1)
 for folder in re.findall(r'"([^"]+)"',block): assert '<code>'+folder+'</code>' in html,folder
for term in ('NRO Cluster','ASTRI','TWEY'): assert term in html
print('PASS scope: exact JS-only openHelp delta, exact header/modal/token delta, nine sections, full folders/glossary')
# Real files, restrictive umask, actual publisher; only external effects stubbed.
for fail in [None,*pub.FILES.values(),'reloaded','reload','reload-foreign','foreign','atomic','guard']:
 with tempfile.TemporaryDirectory(prefix='docs-check-') as tmp:
  root=Path(tmp); authority={'files':{}}; reloads=[]; calls=[0]
  for name,p in pub.FILES.items():
   f=root/p; f.parent.mkdir(parents=True,exist_ok=True); f.write_bytes((base/name).read_bytes()); f.chmod(0o640)
   s=f.stat(); authority['files'][p]=dict(sha256=pub.sha(f.read_bytes()),uid=s.st_uid,gid=s.st_gid,mode=0o640)
  def event(phase):
   if fail=='foreign' and phase=='templates/docs.html':
    (root/phase).write_bytes(b'foreign'); raise RuntimeError('injected foreign')
   if phase==fail: raise RuntimeError('injected phase')
  def guard():
   calls[0]+=1
   if fail=='guard' and calls[0]==3: raise RuntimeError('injected guard')
  real=pub.atomic
  def atomic(*args):
   real(*args)
   if fail=='atomic' and not getattr(atomic,'failed',False):
    atomic.failed=True; raise OSError('injected postwrite fsync')
  pub.atomic=atomic
  mask=os.umask(0o077)
  try:
   rejected=False
   try:
    def reload_web():
     reloads.append(True)
     if fail=='reload': raise RuntimeError('uncertain signal delivery')
     if fail=='reload-foreign': (root/'templates/docs.html').write_bytes(b'foreign')
    result=pub.publish(root,candidate,authority,guard,reload_web,event)
   except (RuntimeError,OSError,AssertionError):
    rejected=True
   assert rejected == (fail is not None), ('Expected rejection',fail)
   assert len(reloads)==(1 if fail in (None,'reloaded','reload','reload-foreign') else 0)
   for name,p in pub.FILES.items():
    f=root/p; assert f.stat().st_mode&0o777==0o640
    if fail in ('foreign','reload-foreign') and p=='templates/docs.html': assert f.read_bytes()==b'foreign'
    elif fail is None or fail in ('reloaded','reload','reload-foreign'): assert f.read_bytes()==(candidate/name).read_bytes()
    else: assert f.read_bytes()==(base/name).read_bytes(),(fail,p)
   if fail not in (None,'reloaded','reload','reload-foreign'): assert not reloads
  finally: os.umask(mask); pub.atomic=real
 print('PASS publisher',fail)
# Existing WO tests run from exact frozen copies, no ASTRI/network.
node='/home/ubuntu/.hermes/tools/node-26.7.0-linux-x64/bin/node'
subprocess.run([node,'--check',str(candidate/'app.js')],check=True)
for script in ('check_ui.js','schedule_check.js'):
 subprocess.run([node,str(ROOT/'tests'/script),str(candidate/'app.js')],check=True)
print('PASS docs-help final gate')
