import json, sys
from collections import defaultdict
sys.path.insert(0, '/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets as rs

MONTH='2026-07'
AREAS=['BEKASI','JAKARTA SELATAN','SUKABUMI']

def list_all(service, query):
    items=[]; token=None
    while True:
        args={'userId':'me','q':query,'maxResults':100}
        if token: args['pageToken']=token
        page=service.users().messages().list(**args).execute()
        items += page.get('messages',[]) or []
        token=page.get('nextPageToken')
        if not token: return items

def groups(rows):
    grouped=defaultdict(list)
    for row in rows:
        grouped[(row['area'],row['site'],row['hp'])].append(row)
    return [
        {'area':key[0],'site':key[1],'hp':key[2],'count':len(values),'items':values}
        for key,values in grouped.items() if len(values)>1
    ]

def main():
    svc=g._gmail_service()
    gmail=[]
    for area in AREAS:
        q=f'from:planning@morarepublic.co.id "New RFS {area}" after:2026/6/30 before:2026/8/1'
        for item in list_all(svc,q):
            msg=svc.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','Reply-To','Subject','Date']).execute()
            h=(msg.get('payload') or {}).get('headers',[])
            subject=g._header(h,'Subject'); date=g._header(h,'Date'); parsed=g._parse_subject(subject)
            if not parsed or parsed['region'] != area or g._month_from_date(date) != MONTH: continue
            if 'planning@morarepublic.co.id' not in g._header(h,'From').lower(): continue
            gmail.append({'area':area,'site':parsed['site'],'hp':parsed['hp'],'id':item['id'],'date':date,'subject':subject})

    ws=rs.open_spreadsheet().worksheet(rs.DATA_SHEET_NAME)
    records=rs._data_records(rs._all_values(ws))
    sheet=[{'area':r['region'].upper(),'site':r['site'],'hp':r['hp'],'id':r['message_id'],'date':r['recorded_at'],'source':r['source_chat'],'row':r['row_number']} for r in records if r['month']==MONTH and r['region'].upper() in AREAS]
    print(json.dumps({'gmail_duplicates':groups(gmail),'sheet_duplicates':groups(sheet),'gmail_rows':len(gmail),'sheet_rows':len(sheet)},ensure_ascii=False,indent=2))
main()
