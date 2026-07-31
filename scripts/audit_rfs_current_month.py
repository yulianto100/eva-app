import json, sys
from collections import Counter
sys.path.insert(0, '/opt/eva')
import gmail_rfs_ingest as g
import rfs_sheets as rs

MONTH = '2026-07'
AREAS = ['BEKASI', 'JAKARTA SELATAN', 'SUKABUMI']

def all_messages(svc, query):
    data, token = [], None
    while True:
        args = {'userId': 'me', 'q': query, 'maxResults': 100}
        if token:
            args['pageToken'] = token
        page = svc.users().messages().list(**args).execute()
        data.extend(page.get('messages', []) or [])
        token = page.get('nextPageToken')
        if not token:
            return data

def main():
    svc = g._gmail_service()
    ws = rs.open_spreadsheet().worksheet(rs.DATA_SHEET_NAME)
    rows = rs._data_records(rs._all_values(ws))
    ids = {r['message_id'] for r in rows if r.get('message_id')}
    fps = {r['fingerprint'] for r in rows if r.get('fingerprint')}
    report = {}
    for area in AREAS:
        query = f'from:planning@morarepublic.co.id "New RFS {area}" after:2026/6/30 before:2026/8/1'
        gmail = []
        for item in all_messages(svc, query):
            msg = svc.users().messages().get(userId='me', id=item['id'], format='metadata', metadataHeaders=['From','Reply-To','Subject','Date']).execute()
            h = (msg.get('payload') or {}).get('headers', [])
            subj, date = g._header(h,'Subject'), g._header(h,'Date')
            parsed = g._parse_subject(subj)
            if not parsed or parsed['region'] != area or g._month_from_date(date) != MONTH:
                continue
            if 'planning@morarepublic.co.id' not in g._header(h,'From').lower() or 'no-reply@myrepublic.net.id' not in g._header(h,'Reply-To').lower():
                continue
            x = {'id':item['id'], 'site':parsed['site'], 'hp':parsed['hp'], 'subject':subj, 'date':date}
            x['fingerprint'] = rs.fingerprint_for(MONTH, area, x['site'], x['hp'])
            gmail.append(x)
        sheet = [r for r in rows if r['month'] == MONTH and r['region'].upper() == area]
        missing = [x for x in gmail if x['id'] not in ids and x['fingerprint'] not in fps]
        report[area] = {
          'gmail_count':len(gmail), 'gmail_hp':sum(x['hp'] for x in gmail),
          'sheet_count':len(sheet), 'sheet_hp':sum(x['hp'] for x in sheet),
          'missing_count':len(missing), 'missing_hp':sum(x['hp'] for x in missing),
          'missing':missing,
          'sheet_without_gmail_id':[{k:r[k] for k in ('site','hp','source_chat','message_id','recorded_at')} for r in sheet if not r.get('message_id')],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
main()
