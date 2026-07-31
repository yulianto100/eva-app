import sys
sys.path.insert(0, '/opt/eva')
import gmail_rfs_ingest as g
svc=g._gmail_service()
queries = [
 'from:planning@morarepublic.co.id subject:"New RFS"',
 'from:planning@morarepublic.co.id subject:"New RFS BEKASI"',
 'from:planning@morarepublic.co.id subject:"New RFS JAKARTA SELATAN"',
 'from:planning@morarepublic.co.id subject:"New RFS SUKABUMI"',
]
for q in queries:
 r=svc.users().messages().list(userId='me',q=q,maxResults=5).execute()
 print('QUERY',q,'estimate',r.get('resultSizeEstimate'),'got',len(r.get('messages',[]) or []))
 for x in r.get('messages',[]) or []:
  m=svc.users().messages().get(userId='me',id=x['id'],format='metadata',metadataHeaders=['Subject','From']).execute()
  h=(m.get('payload') or {}).get('headers',[])
  print(x['id'], g._header(h,'From'), '|',g._header(h,'Subject'))
 print()
