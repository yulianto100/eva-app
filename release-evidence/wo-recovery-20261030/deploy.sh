set -eu
cd /opt/eva
backup=/home/ubuntu/eva-backups/wo-recovery-20261030-180737
stage=/home/ubuntu/eva-backups/wo-recovery-candidate
(cd "$backup" && sha256sum -c SHA256SUMS)
for file in static/js/app.js templates/index.html; do cmp "$backup/$file" "$file"; done
(cd "$stage" && sha256sum -c <<'HASHES'
da5db1b5150b803d9c3f8b0a26a1e9251809797b5ad320d318dd2d4e23d6750d  static/js/app.js
3c09ce98c88d4586f0c000e2abc0fc2e2f65143dd1dec4d2724cc09359ac40a9  templates/index.html
HASHES
)
for file in static/js/app.js templates/index.html; do
 cp -p "$file" "$file.wo-recovery-next"
 cat "$stage/$file" > "$file.wo-recovery-next"
 cmp "$stage/$file" "$file.wo-recovery-next"
done
# Directory binds make atomic replacement visible without recreating services.
mv static/js/app.js.wo-recovery-next static/js/app.js
mv templates/index.html.wo-recovery-next templates/index.html
for file in static/js/app.js templates/index.html; do cmp "$stage/$file" "$file"; done
sha256sum static/js/app.js templates/index.html > "$stage/deployed-sha256.txt"
# Graceful web-only reload: refresh cached Jinja templates; workers/jobs unchanged.
docker exec eva-converter python3 -B -c '
import os, signal
masters=[]
for p in os.listdir("/proc"):
    if not p.isdigit(): continue
    try:
        fields=open("/proc/"+p+"/stat").read().split()
        command=open("/proc/"+p+"/cmdline").read()
        if fields[3]=="1" and "gunicorn" in command: masters.append(int(p))
    except (OSError,IndexError): pass
assert len(masters)==1, "Expected exactly one gunicorn master"
os.kill(masters[0], signal.SIGHUP)
print("WEB_HUP", masters[0])
' > "$stage/reload-receipt.txt"
cat "$stage/reload-receipt.txt"
date -Iseconds > "$stage/deployed-at.txt"
printf 'DEPLOY_OK backup=%s\n' "$backup"
