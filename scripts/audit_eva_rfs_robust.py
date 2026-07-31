from __future__ import annotations
import json, sys
from collections import Counter
sys.path.insert(0, '/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets

AREAS={'BEKASI','JAKARTA SELATAN','SUKABUMI'}
QUERY='from:planning@morarepublic.co.id subject:"New RFS"'

def list_unique(svc):
    all_items=[]; seen_ids=set(); seen_tokens=set(); page=None
    while True:
        if page in seen_tokens:
            print('STOP repeated token', flush=True); break
        if page: seen_tokens.add(page)
        kw={'userId':'me','q':QUERY,'maxResults':100}
        if page: kw['pageToken']=page
        res=svc.users().messages().list(**kw).execute()
        batch=res.get('messages',[]) or []
        new=[x for x in batch if x.get('id') not in seen_ids]
        seen_ids.update(x.get('id') for x in new)
        all_items.extend(new)
        print(f'PAGE total_unique={len(all_items)} batch={len(batch)} new={len(new)} estimate={res.get("resultSizeEstimate")}', flush=True)
        page=res.get('nextPageToken')
        if not page or not new: break
    return all_items

def main():
    svc=g._gmail_service()
    ss=rfs_sheets.open_spreadsheet(); ws=ss.worksheet(rfs_sheets.DATA_SHEET_NAME)
    rows=rfs_sheets._data_records(rfs_sheets._all_values(ws))
    ids={r['message_id'] for r in rows if r.get('message_id')}
    fps={r['fingerprint'] for r in rows if r.get('fingerprint')}
    items=list_unique(svc)
    out={'query':QUERY,'gmail_unique_result_count':len(items),'sheet_target_records':len([r for r in rows if r['region'].upper() in AREAS]),'areas':{a:{'valid':[],'missing':[],'fp_only':[],'invalid_filter':[],'parse_fail':[]} for a in sorted(AREAS)}}
    for pos,item in enumerate(items,1):
        m=svc.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','Reply-To','Subject','Date']).execute()
        h=(m.get('payload') or {}).get('headers',[])
        frm=g._header(h,'From'); reply=g._header(h,'Reply-To'); subj=g._header(h,'Subject'); date=g._header(h,'Date')
        parsed=g._parse_subject(subj)
        if not parsed or parsed['region'] not in AREAS:
            if any(a in subj.upper() for a in AREAS):
                target=next(a for a in AREAS if a in subj.upper())
                out['areas'][target]['parse_fail'].append({'id':item['id'],'date':date,'subject':subj})
            continue
        x={'id':item['id'],'date':date,'subject':subj,**parsed,'month':g._month_from_date(date)}
        x['fingerprint']=rfs_sheets.fingerprint_for(x['month'],x['region'],x['site'],x['hp'])
        bucket=out['areas'][x['region']]
        if 'planning@morarepublic.co.id' not in frm.lower() or 'no-reply@myrepublic.net.id' not in reply.lower():
            x['from']=frm; x['reply_to']=reply; bucket['invalid_filter'].append(x); continue
        bucket['valid'].append(x)
        if x['id'] not in ids:
            (bucket['fp_only'] if x['fingerprint'] in fps else bucket['missing']).append(x)
        if pos%25==0: print('METADATA',pos,flush=True)
    for area,b in out['areas'].items():
        valid=b['valid']
        b['counts']={
          'gmail_valid':len(valid),'gmail_valid_by_month':dict(sorted(Counter(x['month'] for x in valid).items())),
          'missing':len(b['missing']),'fp_only':len(b['fp_only']),'invalid_filter':len(b['invalid_filter']),'parse_fail':len(b['parse_fail']),
          'sheet_records':sum(1 for r in rows if r['region'].upper()==area),
        }
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
