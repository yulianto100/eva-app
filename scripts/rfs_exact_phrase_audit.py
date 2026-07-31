import sys,json
from collections import Counter
sys.path.insert(0,'/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets as rs

AREAS=['BEKASI','JAKARTA SELATAN','SUKABUMI']

def get_all(svc,q):
 out=[]; token=None
 while True:
  kw={'userId':'me','q':q,'maxResults':100}
  if token: kw['pageToken']=token
  z=svc.users().messages().list(**kw).execute()
  out+=z.get('messages',[]) or []
  token=z.get('nextPageToken')
  if not token: return out

def main():
 svc=g._gmail_service()
 ws=rs.open_spreadsheet().worksheet(rs.DATA_SHEET_NAME)
 rows=rs._data_records(rs._all_values(ws))
 ids={x['message_id'] for x in rows if x.get('message_id')}
 fps={x['fingerprint'] for x in rows if x.get('fingerprint')}
 report={}
 for area in AREAS:
  q=f'from:planning@morarepublic.co.id "New RFS {area}"'
  listed=get_all(svc,q); valid=[]; missing=[]; fp=[]; fails=[]
  print(area, 'listed',len(listed),flush=True)
  for item in listed:
   msg=svc.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','Reply-To','Subject','Date']).execute()
   h=(msg.get('payload')or{}).get('headers',[])
   subj=g._header(h,'Subject'); date=g._header(h,'Date')
   p=g._parse_subject(subj)
   if not p or p['region']!=area:
    if area in subj.upper(): fails.append({'id':item['id'],'date':date,'subject':subj})
    continue
   if 'planning@morarepublic.co.id' not in g._header(h,'From').lower() or 'no-reply@myrepublic.net.id' not in g._header(h,'Reply-To').lower(): continue
   x={'id':item['id'],'date':date,'month':g._month_from_date(date),'subject':subj,**p}
   x['fingerprint']=rs.fingerprint_for(x['month'],x['region'],x['site'],x['hp'])
   valid.append(x)
   if x['id'] not in ids:
    (fp if x['fingerprint'] in fps else missing).append(x)
  report[area]={'query':q,'listed':len(listed),'valid':len(valid),'valid_months':dict(Counter(x['month'] for x in valid)),'sheet_records':sum(x['region'].upper()==area for x in rows),'missing_count':len(missing),'missing':missing,'fingerprint_only_count':len(fp),'fingerprint_only':fp,'parse_fail_count':len(fails),'parse_fail':fails}
 print(json.dumps(report,ensure_ascii=False,indent=2))
main()
