from pathlib import Path
import subprocess,json,hashlib,urllib.request,urllib.error
r=Path('/tmp/opencode/docs-refresh'); release=r/'release-v3'; out=r/'installed'
remote='''from pathlib import Path
import json,hashlib,subprocess
root=Path('/opt/eva'); release=Path('/home/ubuntu/eva-backups/docs-help-release-v3/release-v3')
receipt=json.loads((release/'deployed.json').read_text()); assert receipt['status']=='deployed'
for n,p in {'docs.html':'templates/docs.html','index.html':'templates/index.html','app.js':'static/js/app.js'}.items():
 b=(root/p).read_bytes(); assert b==(release/'candidate'/n).read_bytes()
 mounted=subprocess.check_output(['docker','exec','eva-converter','cat','/app/'+p]); assert mounted==b
print(json.dumps(receipt))
'''
p=subprocess.run(['ssh','eva','python3 -B -'],input=remote,text=True,capture_output=True,check=True)
(out/'deployed.json').write_text(p.stdout)
for n,p in {'docs.html':'templates/docs.html','index.html':'templates/index.html','app.js':'static/js/app.js'}.items():
 b=subprocess.check_output(['ssh','eva','cat','/opt/eva/'+p]); assert b==(release/'candidate'/n).read_bytes(); (out/n).write_bytes(b)
code='from pathlib import Path\nfrom jinja2 import Environment\ne=Environment(autoescape=True)\nprint(e.from_string(Path("/app/templates/docs.html").read_text()).render(eva_account=None,url_for=lambda endpoint,**kwargs:"/static/"+kwargs["filename"]))\n'
p=subprocess.run(['ssh','eva','docker exec -i eva-converter python3 -B -'],input=code,text=True,capture_output=True,check=True); (out/'docs-rendered.html').write_text(p.stdout)
public=urllib.request.urlopen('https://eva.dyena.id/static/js/app.js?v=docs-help-20261030-v1',timeout=20).read(); assert public==(out/'app.js').read_bytes(); (out/'public-app.js').write_bytes(public)
health=json.load(urllib.request.urlopen('https://eva.dyena.id/healthz',timeout=15)); assert health['status']=='ok'
try: urllib.request.urlopen('https://eva.dyena.id/docs',timeout=15); raise AssertionError('Anonymous docs unexpectedly allowed')
except urllib.error.HTTPError as e: assert e.code==401
(out/'public-proof.json').write_text(json.dumps(dict(public_sha256=hashlib.sha256(public).hexdigest(),health=health,anonymous_docs=401,authenticated_http='not tested'),indent=2)+'\n')
subprocess.run(['/home/ubuntu/.hermes/tools/node-26.7.0-linux-x64/bin/node',str(r/'installed-ui.js')],check=True)
print('PASS host/container/public hashes, metadata receipt, health, anonymous auth denial, installed-byte browser')
