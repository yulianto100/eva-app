from __future__ import annotations
import json, sys
from collections import Counter
sys.path.insert(0, '/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets

AREAS = ['BEKASI', 'JAKARTA SELATAN', 'SUKABUMI']
BASE_QUERY = 'from:planning@morarepublic.co.id subject:"New RFS"'

def list_all(svc, query):
    out=[]; page=None
    while True:
        kw={'userId':'me','q':query,'maxResults':100}
        if page: kw['pageToken']=page
        res=svc.users().messages().list(**kw).execute()
        out += res.get('messages',[]) or []
        page=res.get('nextPageToken')
        if not page: return out

def headers(msg):
    return (msg.get('payload') or {}).get('headers', [])

def main():
    svc=g._gmail_service()
    ss=rfs_sheets.open_spreadsheet()
    ws=ss.worksheet(rfs_sheets.DATA_SHEET_NAME)
    rows=rfs_sheets._data_records(rfs_sheets._all_values(ws))
    ids={r.get('message_id') for r in rows if r.get('message_id')}
    fps={r.get('fingerprint') for r in rows if r.get('fingerprint')}
    result={'sheet_records':len(rows),'sheet_by_area':dict(Counter(r['region'].upper() for r in rows if r['region'].upper() in AREAS)),'areas':{}}
    for area in AREAS:
        q=f'{BASE_QUERY} "{area}"'
        items=list_all(svc,q)
        print(f'{area}: gmail search returned {len(items)}', flush=True)
        accepted=[]; parse_fail=[]; rejected=[]
        for idx,item in enumerate(items,1):
            msg=svc.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','Reply-To','Subject','Date']).execute()
            hs=headers(msg)
            frm=g._header(hs,'From'); reply=g._header(hs,'Reply-To'); subj=g._header(hs,'Subject'); date=g._header(hs,'Date')
            parsed=g._parse_subject(subj)
            if not parsed or parsed['region'] != area:
                if area in subj.upper(): parse_fail.append({'id':item['id'],'date':date,'subject':subj})
                continue
            x={'id':item['id'],'date':date,'subject':subj,**parsed,'month':g._month_from_date(date)}
            x['fingerprint']=rfs_sheets.fingerprint_for(x['month'],x['region'],x['site'],x['hp'])
            if 'planning@morarepublic.co.id' not in frm.lower() or 'no-reply@myrepublic.net.id' not in reply.lower():
                x['from']=frm; x['reply_to']=reply; rejected.append(x); continue
            accepted.append(x)
            if idx % 50 == 0: print(f'{area}: processed {idx}', flush=True)
        missing=[x for x in accepted if x['id'] not in ids and x['fingerprint'] not in fps]
        fp_only=[x for x in accepted if x['id'] not in ids and x['fingerprint'] in fps]
        result['areas'][area]={'gmail_search_count':len(items),'accepted_count':len(accepted),'accepted_by_month':dict(Counter(x['month'] for x in accepted)),'missing_count':len(missing),'missing':missing,'represented_by_fingerprint_count':len(fp_only),'represented_by_fingerprint':fp_only,'rejected_count':len(rejected),'rejected':rejected,'parse_fail_count':len(parse_fail),'parse_fail':parse_fail}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
