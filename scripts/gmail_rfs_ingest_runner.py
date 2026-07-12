import sys, json, traceback
sys.path.insert(0, '/app')
try:
    import gmail_rfs_ingest as g
    print(json.dumps(g.ingest(max_results=50), ensure_ascii=False))
except Exception:
    traceback.print_exc()
    raise
