"""Run unchanged frozen check.py with only publisher post-reload assertion removed in memory."""
import hashlib, importlib.util, runpy, sys
from pathlib import Path
ROOT=Path('/tmp/opencode/docs-refresh/release-v3')
source=(ROOT/'publisher.py').read_text()
assert hashlib.sha256(source.encode()).hexdigest()=='f7fd51ee87a1ead1894e7803b73d2ed430bc2c545cebc03dcb18eb2143a31225'
line="  for name,p in FILES.items(): assert (root/p).read_bytes()==(candidate/name).read_bytes(), 'Foreign target after reload'\n"
assert source.count(line)==1
# Keep line numbering intact; mutate one exact assertion only.
mutant=source.replace(line,'\n')
real=importlib.util.spec_from_file_location
class MutantLoader:
 def create_module(self,spec): return None
 def exec_module(self,module):
  print('MUTATION_APPLIED: removed only publisher final candidate-byte assertion',flush=True)
  exec(compile(mutant,str(ROOT/'publisher.py'),'exec'),module.__dict__)
def spec(name,location,*args,**kwargs):
 result=real(name,location,*args,**kwargs)
 if Path(location)==ROOT/'publisher.py': result.loader=MutantLoader()
 return result
importlib.util.spec_from_file_location=spec
sys.excepthook=sys.__excepthook__
runpy.run_path(str(ROOT/'check.py'),run_name='__main__')
