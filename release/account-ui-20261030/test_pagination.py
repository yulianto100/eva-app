"""Run: python3 test_pagination.py. Extract only native handler; no app/Redis/DB imports."""
import ast, sys, types, time, stat
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
root=Path(__file__).parent
source=(root/'candidate/eva_auth.py').read_text()
module=ast.parse(source)
method=next(n for n in ast.walk(module) if isinstance(n,ast.FunctionDef) and n.name=='account_history')
method.decorator_list=[]
all_jobs=[dict(job_id=f'{i:032x}',created_at=1000-i,owner_user_id='self',status='done',scope_type='Cluster',original_filenames=[f'file-{i}.kmz']) for i in range(23)]
all_events=[dict(submission_id=str(i),operator_user_id='self',scope='cluster') for i in range(23)]
calls=[]
class Journal:
 def __init__(self,*a,**kw):pass
 def list_success_history(self,**kw):
  calls.append(kw);assert kw['user_id']=='self'
  events=[e for e in all_events if e['operator_user_id']==kw['user_id'] and (not kw['scope'] or e['scope']==kw['scope'])]
  p=kw['page'];return dict(history=events[(p-1)*20:p*20],page=p,page_size=20,total=len(events))
class DB:
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def execute(self,sql,args):assert args==('self',);return []
class Store:
 def user_active(self,uid):return uid=='self'
 def connect(self):return DB()
 def conversion_history(self,uid):assert uid=='self';return all_jobs
stubs={
 'astri_boq_submissions':dict(RedisBoqSubmissionStore=Journal),
 'config':dict(Config=SimpleNamespace(REDIS_URL='forbidden',JOB_STORE_KEY_PREFIX='forbidden',JOB_STORE_TTL_SECONDS=1)),
 'jobs':dict(list_jobs=lambda limit:[dict(job_id='f'*32,owner_user_id='foreign')]),
 'eva_billing':dict(user_plan=lambda user:dict(active=False,plan='free')),
}
for name,values in stubs.items():
 m=types.ModuleType(name);m.__dict__.update(values);sys.modules[name]=m
request=SimpleNamespace(args={})
ns=dict(g=SimpleNamespace(eva_user=dict(id='self')),request=request,jsonify=lambda value:value,_history_text=lambda v:v,_user_upload_files=lambda *a:[],time=time,stat=stat)
exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'native-account-history','exec'),ns)
with TemporaryDirectory() as temp:
 store=Store();store.path=Path(temp)/'never-created.db';gate=SimpleNamespace(store=store)
 for kind in ['convert','boq']:
  seen=[]
  for page in range(1,7):
   request.args=dict(kind=kind,page=str(page))
   result=ns['account_history'](gate)
   assert isinstance(result,dict),result
   assert result['page']==page and result['page_size']==5 and result['total']==23,result
   rows=result['jobs' if kind=='convert' else 'history']
   assert len(rows)==max(0,min(5,23-(page-1)*5)),(page,rows)
   seen += [r['job_id' if kind=='convert' else 'submission_id'] for r in rows]
  assert len(seen)==len(set(seen))==23,(kind,seen)
  request.args=dict(kind=kind,page='0');assert ns['account_history'](gate)[1]==400
 request.args=dict(kind='convert',page='1',scope='feeder');assert ns['account_history'](gate)['jobs']==[]
 request.args=dict(kind='convert',page='1',q='file-22');assert len(ns['account_history'](gate)['jobs'])==1
 ns['g'].eva_user=None;assert ns['account_history'](gate)[1]==401
assert [c['page'] for c in calls]==[1,1,1,1,2,2]
assert (root/'candidate/account.html').read_text().split('<body',1)[1]==(root/'baseline/account.html').read_text().split('<body',1)[1]
assert (root/'candidate/eva-account.css').read_text().startswith((root/'baseline/eva-account.css').read_text())
print('PASS: native Convert + BOQ pages 1–6, 23 unique records, max five, no gaps, filters/auth unchanged; no app imports/network/storage writes')
