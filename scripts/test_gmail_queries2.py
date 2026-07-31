import sys
sys.path.insert(0,'/opt/eva')
import gmail_rfs_ingest as g
svc=g._gmail_service()
areas=['BEKASI','JAKARTA SELATAN','SUKABUMI']
for area in areas:
 qs=[
  f'from:planning@morarepublic.co.id subject:New subject:RFS subject:{area.replace(" ", "")}',
  f'from:planning@morarepublic.co.id subject:New subject:RFS subject:{area.split()[0]}',
  f'from:planning@morarepublic.co.id subject:{area.split()[0]}',
 ]
 print('\nAREA',area)
 for q in qs:
  r=svc.users().messages().list(userId='me',q=q,maxResults=100).execute()
  print('Q',q,'estimate',r.get('resultSizeEstimate'),'returned',len(r.get('messages',[])or[]),'next',bool(r.get('nextPageToken')))
  for x in (r.get('messages',[])or[])[:2]:
   m=svc.users().messages().get(userId='me',id=x['id'],format='metadata',metadataHeaders=['Subject']).execute()
   h=(m.get('payload')or{}).get('headers',[])
   print(' ',g._header(h,'Subject'))
