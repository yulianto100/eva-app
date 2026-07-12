#!/usr/bin/env bash
set -euo pipefail
cd /opt/eva
LOG=/opt/eva/logs/gmail-rfs-ingest.log
mkdir -p /opt/eva/logs
{
  echo "===== $(date -Is) gmail-rfs-ingest ====="
  docker cp /opt/eva/scripts/gmail_rfs_ingest_runner.py eva-converter:/tmp/gmail_rfs_ingest_runner.py
  docker exec eva-converter python3 /tmp/gmail_rfs_ingest_runner.py
} >> "$LOG" 2>&1
