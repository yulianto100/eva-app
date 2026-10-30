import hashlib, importlib.util, json, os, re, tempfile
from pathlib import Path
ROOT=Path('/tmp/opencode/docs-refresh/release-v2')
old=(ROOT/'baseline/docs.html').read_text(); new=(ROOT/'candidate/docs.html').read_text()
sections=lambda text:dict(re.findall(r'(<section id="([^"]+)"[\s\S]*?</section>)',text))
# Keyed by section ID, compare complete original section bytes, not just heading presence.
def sections(text): return {ident:body for body,ident in re.findall(r'(<section id="([^"]+)"[\s\S]*?</section>)',text)}
a=sections(old); b=sections(new)
assert len(a)==9 and set(b)==set(a)|{'bantuan'}
for ident,body in a.items():
 target=b[ident]
 if ident=='sumber': target=re.sub(r'          <details><summary>Checklist folder dari Bantuan</summary>.*?</details>\n','',target)
 if ident=='hasil': target=target.replace('buka dokumentasi bantuan','bantuan singkat')
 if ident=='istilah':
  target=re.sub(r'            <dt>NRO Cluster</dt>.*?</dd>\n','',target)
  target=re.sub(r'            <dt>ASTRI</dt>.*?</dd>\n','',target)
 assert target==body,ident
print('PASS all nine original docs sections byte-retained after explicit additive help/checklist/glossary and shortcut-copy normalization.')
from html.parser import HTMLParser
class Tags(HTMLParser):
 def __init__(self,text): super().__init__(); self.ids=[];self.links=[];self.feed(text)
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if 'id' in attrs:self.ids.append(attrs['id'])
  if tag=='a' and attrs.get('href','').startswith('#'):self.links.append(attrs['href'][1:])
p=Tags(new); assert len(p.ids)==len(set(p.ids));assert all(x in p.ids for x in p.links)
print('PASS unique docs IDs and all fragment destinations.')
spec=importlib.util.spec_from_file_location('publisher_b',ROOT/'publisher.py');pub=importlib.util.module_from_spec(spec);spec.loader.exec_module(pub)
authority=json.loads((ROOT/'authority.json').read_text())
for fail in [False,True]:
 with tempfile.TemporaryDirectory(prefix='review-b-publisher-',dir='/tmp/opencode') as tmp:
  root=Path(tmp); meta={'files':{}}
  for name,p in pub.FILES.items():
   f=root/p;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes((ROOT/'baseline'/name).read_bytes());f.chmod(authority['files'][p]['mode'])
   st=f.stat();meta['files'][p]=dict(sha256=pub.sha(f.read_bytes()),uid=st.st_uid,gid=st.st_gid,mode=st.st_mode&4095)
  calls=[]
  def event(phase):
   if fail and phase=='static/js/app.js':raise RuntimeError('B injected before HUP')
  mask=os.umask(0o077)
  try:
   try:pub.publish(root,ROOT/'candidate',meta,lambda:None,lambda:calls.append('web HUP'),event)
   except RuntimeError:assert fail
   for name,p in pub.FILES.items():
    st=(root/p).stat();m=meta['files'][p]
    assert (st.st_uid,st.st_gid,st.st_mode&4095)==(m['uid'],m['gid'],m['mode'])
    assert (root/p).read_bytes()==(ROOT/('baseline' if fail else 'candidate')/name).read_bytes()
   assert calls==([] if fail else ['web HUP'])
  finally:os.umask(mask)
 print('PASS B publisher actual authority modes 0644/0664 + uid/gid under umask 077; '+('recovery without HUP' if fail else 'single web reload'))
manifest=ROOT/'manifest.json';assert hashlib.sha256(manifest.read_bytes()).hexdigest()=='9cb83e8acca3c021fc01081a4905cd39bf4dea5a7555a13e2b0f36a765052d7f'
for p,h in json.loads(manifest.read_text()).items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
print('PASS final manifest and all 15 entries unchanged.')
