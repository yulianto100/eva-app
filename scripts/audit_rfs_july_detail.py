import sys,json
from collections import defaultdict
sys.path.insert(0,'/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets as rs
MONTH='2026-07'; AREAS=['BEKASI','JAKARTA SELATAN','SUKABUMI']
def getall(s,q):
 o=[]; t=None
 while True:
  a={'userId':'me','q':q,'maxResults':100}
  if t:a['pageToken']=t
  z=s.users().messages().list(**a).execute();o+=z.get('messages',[])or[];t=z.get('nextPageToken')
  if not t:return o
s=g._gmail_service(); w=rs.open_spreadsheet().worksheet(rs.DATA_SHEET_NAME); rows=rs._data_records(rs._all_values(w))
for area in AREAS:
 gmail=[]
 for it in getall(s,f'from:planning@morarepublic.co.id "New RFS {area}" after:2026/6/30 before:2026/8/1'):
  m=s.users().messages().get(userId='me',id=it['id'],format='metadata',metadataHeaders=['From','Reply-To','Subject','Date']).execute();h=(m.get('payload')or{}).get('headers',[]);sub=g._header(h,'Subject');date=g._header(h,'Date');p=g._parse_subject(sub)
  if p and p['region']==area and g._month_from_date(date)==MONTH:
   gmail.append({'id':it['id'],'site':p['site'],'hp':p['hp'],'date':date,'subject':sub,'fp':rs.fingerprint_for(MONTH,area,p['site'],p['hp'])})
 sheet=[{'id':x['message_id'],'site':x['site'],'hp':x['hp'],'at':x['recorded_at'],'source':x['source_chat'],'raw':x['raw_text'],'fp':x['fingerprint']} for x in rows if x['month']==MONTH and x['region'].upper()==area]
 print('\n===',area,'===');print('GMAIL',len(gmail),sum(x['hp'] for x in gmail),'SHEET',len(sheet),sum(x['hp'] for x in sheet))
 f=defaultdict(list)
 for x in gmail:f[x['fp']].append(x)
 for fp,v in f.items():
  if len(v)>1: print('GMAIL_DUP_FP',json.dumps(v,ensure_ascii=False))
 print('SHEET_ROWS',json.dumps(sheet,ensure_ascii=False))
