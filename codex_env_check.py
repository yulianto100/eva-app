#!/usr/bin/env python3
"""Check Codex/EVA environment without printing secret values."""
from __future__ import annotations
import hashlib, os
from pathlib import Path
ROOT = Path('/opt/eva')
ENV_PATH = ROOT / ".env"
REQUIRED = ["ASTRI_USER", "ASTRI_PASS", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"]
OPTIONAL = ["GOOGLE_APPLICATION_CREDENTIALS", "EVA_AI_ASSIST", "EVA_AI_MODEL", "EVA_AI_CONFIDENCE_MIN", "DXF_CONVERSION_ENABLED", "SEARXNG_URL"]

def load_dotenv(path: Path) -> None:
    if not path.exists(): return
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]

def status(key: str) -> str:
    value = os.environ.get(key, "")
    if not value: return "MISSING"
    return f"PRESENT sha256:{fp(value)} len:{len(value)}"

def main() -> int:
    load_dotenv(ENV_PATH)
    print(f"root={ROOT}")
    print(f"env_file={'PRESENT' if ENV_PATH.exists() else 'MISSING'}")
    print("\nRequired:")
    missing = []
    for key in REQUIRED:
        s = status(key); print(f"- {key}: {s}")
        if s == "MISSING": missing.append(key)
    print("\nOptional:")
    for key in OPTIONAL: print(f"- {key}: {status(key)}")
    gce = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gce:
        p = Path(gce)
        print(f"\nGOOGLE_APPLICATION_CREDENTIALS file={'PRESENT' if p.exists() else 'MISSING'} path={p}")
    if missing:
        print(f"\nFAIL missing={','.join(missing)}")
        return 1
    print("\nOK Codex can access required env without exposing secrets.")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
