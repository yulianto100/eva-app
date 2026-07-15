#!/usr/bin/env python3
"""Patch astri_create_boq_link to paginate through drm/list/v3 pages."""

path = "/opt/eva/app.py"
with open(path, "r") as f:
    content = f.read()

old = '''def astri_create_boq_link(record: dict, match: dict) -> dict:
    response = record["session"].get(f"{ASTRI_BASE_URL}/material/cluster/drm/list/v3", timeout=60)
    response.raise_for_status()
    pattern = r'<a[^>]+/material/cluster/boq-drm/insert[^>]*' + re.escape(match["cluster_code"]) + r'[^>]*>.*?</a>'
    found = re.search(pattern, response.text, flags=re.I | re.S)
    if not found:
        return {"enabled": False, "error": "Create BOQ link tidak ditemukan di ASTRI."}
    tag = found.group(0)
    href_match = re.search(r'href=("|\\')([^"\\']+)\\1', tag, flags=re.I)
    href = html.unescape(href_match.group(2)) if href_match else ""
    enabled = "disabled" not in tag.lower()
    return {"enabled": enabled, "href": href, "url": f"{ASTRI_BASE_URL}{href}" if href.startswith("/") else href}'''

new = '''def astri_create_boq_link(record: dict, match: dict) -> dict:
    code = match.get("cluster_code", "").strip()
    if not code:
        return {"enabled": False, "error": "No cluster_code in match."}
    # Scan paginated drm/list/v3 pages — cluster may be beyond page 1
    for page in range(1, 31):
        url = f"{ASTRI_BASE_URL}/material/cluster/drm/list/v3/page-{page}"
        response = record["session"].get(url, timeout=60)
        response.raise_for_status()
        pattern = r'<a[^>]+/material/cluster/boq-drm/insert[^>]*' + re.escape(code) + r'[^>]*>.*?</a>'
        found = re.search(pattern, response.text, flags=re.I | re.S)
        if found:
            tag = found.group(0)
            href_match = re.search(r'href=("|\\')([^"\\']+)\\1', tag, flags=re.I)
            href = html.unescape(href_match.group(2)) if href_match else ""
            enabled = "disabled" not in tag.lower()
            return {"enabled": enabled, "href": href, "url": f"{ASTRI_BASE_URL}{href}" if href.startswith("/") else href}
        # Stop if page has no boq-drm/insert links at all
        if not re.search(r'boq-drm/insert', response.text):
            break
    return {"enabled": False, "error": f"BOQ link untuk {code} tidak ditemukan di halaman manapun."}'''

if old in content:
    content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    print("PATCH OK")
else:
    print("NOT FOUND")
