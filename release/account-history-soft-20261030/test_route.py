"""Native route extraction only; no EVA import, real storage or authentication."""
import ast
from pathlib import Path
from types import SimpleNamespace
r=Path(__file__).parent
mod=ast.parse((r/'candidate/eva_auth.py').read_text())
method=next(n for n in ast.walk(mod) if isinstance(n,ast.FunctionDef) and n.name=='account_page')
# Run with installed Flask through remote container, or the existing local Flask environment.
from flask import Flask,request,redirect,url_for
app=Flask(__name__)
ns=dict(request=request,redirect=redirect,url_for=url_for,render_template=lambda name:'ACCOUNT')
exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'native-account-route','exec'),ns)
gate=SimpleNamespace();gate.account_page=lambda:ns['account_page'](gate)
register=next(n for n in mod.body if isinstance(n,ast.FunctionDef) and any(isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute) and x.func.attr=='add_url_rule' for x in ast.walk(n)))
rules=[n for n in ast.walk(register) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='add_url_rule' and len(n.args)>1 and isinstance(n.args[1],ast.Constant) and n.args[1].value=='eva_account_page']
assert len(rules)==2
for node in rules:exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.Expr(value=node)],type_ignores=[])),'native-rule','exec'),dict(app=app,gate=gate))
with app.test_request_context('/'):assert url_for('eva_account_page')=='/akun'
client=app.test_client()
assert client.get('/akun').data==b'ACCOUNT'
assert client.head('/akun').status_code==200
for path in ['/account','/account?next=https://invalid.test']:
 response=client.get(path);assert response.status_code==302 and response.headers['Location']=='/akun'
for name in ['support.html','langganan.html']:
 html=(r/'candidate'/name).read_text();assert 'href="/account"' not in html and 'href="/akun"' in html
print('PASS: native /akun GET/HEAD, canonical url_for, fixed local legacy redirect, support/package links')
