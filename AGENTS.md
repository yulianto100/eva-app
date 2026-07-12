# EVA / ASTRI — Agent Operating Guide

Owner: Dwi Yulianto.
Scope: EVA, ASTRI, VPS-new only.

## Hard Rules

1. Never print secrets, tokens, passwords, cookies, private keys, service account JSON, or raw `.env` contents.
2. Read credentials only from environment variables, `.env`, mounted secret files, or `/opt/eva/.secrets/`.
3. If credential missing, ask user to add it to `.env`; do not ask user to paste secrets into chat.
4. Never delete old VPS / rollback data.
5. Never declare fixed without real verification: command output, test result, container log, HTTP response, or screenshot.
6. Before Docker rebuild touching baked Python files, run host-vs-container SHA256 + line-count check.
7. Verify inside running containers, not only host files.
8. Do not fabricate outputs. If blocked, state blocker and next exact command.

## App Location

Project root: `/opt/eva`

Production domains:
- Main: `https://eva-mrdesign.my.id`
- Dashboard: `https://dashboard.eva-mrdesign.my.id`

Docker containers:
- Web: `eva-converter`
- Worker: `eva-worker`

## Docker Reality

Core Python files are COPY-baked into Docker images. Changes usually require rebuild:

```bash
cd /opt/eva
python3 -m py_compile app.py converter.py kmz_standardizer.py boq.py fdt_coding_db.py
docker compose build --no-cache web worker
docker compose up -d web worker
```

Template/static/config/data changes may be bind-mounted. Usually only restart web:

```bash
docker restart eva-converter
```

## Mandatory Pre-Rebuild Integrity Check

Run before rebuild if touching Python logic:

```bash
cd /opt/eva
for f in app.py converter.py kmz_standardizer.py boq.py fdt_coding_db.py; do
  echo "== $f =="
  sha256sum "$f" 2>/dev/null || true
  wc -l "$f" 2>/dev/null || true
  docker exec eva-converter sh -lc "sha256sum /app/$f 2>/dev/null; wc -l /app/$f 2>/dev/null" || true
done
```

Known gotcha: host source was previously silently replaced by stub while container had real code. If host file much smaller than container file, recover first:

```bash
docker cp eva-converter:/app/<file> /opt/eva/<file>
python3 -m py_compile /opt/eva/<file>
```

## Useful Checks

```bash
cd /opt/eva
python3 codex_env_check.py

docker ps --format "table {{.Names}}	{{.Status}}	{{.Ports}}" | grep -E "eva-converter|eva-worker"
docker logs --tail=120 eva-converter
docker logs --tail=120 eva-worker
curl -I https://eva-mrdesign.my.id/
curl -I https://dashboard.eva-mrdesign.my.id/
```

## Environment Variables

Load `.env` without printing values:

```bash
cd /opt/eva
set -a
. ./.env
set +a
python3 codex_env_check.py
```

Expected env keys commonly used:
- `ASTRI_USER`
- `ASTRI_PASS`
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `EVA_AI_ASSIST`
- `EVA_AI_MODEL`
- `EVA_AI_CONFIDENCE_MIN`

Never show values. Only report present/missing.

## ASTRI Notes

Login:
- `POST /login-process`
- form-urlencoded fields: `user-identity`, `password`, empty `csrf`

Session file may exist inside container:
- `/tmp/eva_astri_sessions.json`

Search URLs:
- Cluster: `/rfs-document/rfs/?filter[global]=NAMA`
- Feeder: `/rfs-document/feeder/rfs/?filter[global]=AREA&limit=-1`

ASTRI clean-list export preference:
- Use `limit=-1`
- Group folders by Status Cluster
- Placemark name = Name
- Description = Cluster Code / Vendor / Status Permit
- Same-coordinate duplicates: keep newest only

## EVA UI Rules

GDR feedback uses centered EVA toast:
- background `#083127`
- text `#3ECF8E`
- auto-close after 5s
- manual button text `Tutup`
- no semantic red/blue/yellow difference for success/error

Non-GDR menus keep original inline/modal status unless user asks otherwise:
- ASTRI upload
- ManCore
- RFS
- Coding FDT
- KMZ tools

Avoid full-screen modals.

## Dashboard Access Rule

Write operations for dashboard-only features must be restricted to dashboard subdomain.
Use existing `is_dashboard_host()` pattern where applicable.
Do not expose GDR vendor management writes from main domain.

## KMZ Rules

General:
- No source placemarks/polygons should disappear after conversion.
- Source folder `AREA` may contain FAT boundary polygons and must route to `BOUNDARY FAT`, not be pruned.
- Folder selection should be hidden/auto-selected for eligible folders.
- Follow latest MORAREPUBLIC 2026 KMZ icon/color/folder standard globally unless user scopes narrowly.

FAT boundary:
- Each FAT placemark pairs with one unique nearest available BOUNDARY polygon.
- Matched polygon renamed to FAT code, e.g. `A01`.
- Matched polygon placed directly inside `LINE X - FDT Y / BOUNDARY FAT`.

MORAREPUBLIC 2026 durable rules:
- Cluster UG includes `SUBDUCT 40/34`, `SUBDUCT 32/28`, `SUBDUCT 3RD PARTY`, `PVC 4INCH`, `GALVANIS`, `DROP WIRE`, `ASPHALT`, `CUT TRACK`, `CROSSING REL KERETA`, `SUBMARINE`.
- `TALI PANCING` color `#AAAA7F`.
- `DROP WIRE` color `#3D3D3D`.
- `SUBDUCT 32/28` color `#FF00FF`.
- `SUBDUCT 40/34` color `#FF0000`.
- 36C cable layer should be `FO_36_CORE`.

Feeder UG / CCTV preference:
- Use standard feeder UG folders.
- Line/HDPE/Drop Wire goes into `SUBDUCT 32/28`.
- Do not create separate `SUBDUCT 32`.
- HH Pit goes into `NEW HH 20X20X20`.
- FAT placemarks directly in `FAT`, no nested `FAT/FAT`.

## GDR Notes

Spreadsheet `Nomor GDR 2026`:
- `Area` column G
- `PMO` column H

Spreadsheet `Nomor GDR NRO`:
- Column E `NOMOR PR` contains only PR numbers separated by `; `.
- Do not include cluster/site names in NOMOR PR.

## Coding / Style

- Prefer small targeted patches.
- Back up before risky geometry / KMZ restructuring experiments.
- Preserve working behavior unless explicitly changing it.
- Run `python3 -m py_compile` for touched Python files.
- For route/API fixes, use curl or browser/API check after restart/rebuild.
- Report changed files + verification output.

## Done Format

When done, report:

```text
Changed:
- file1
- file2

Verified:
- command => key output
- container/API/log evidence

Caveat:
- only if any
```
