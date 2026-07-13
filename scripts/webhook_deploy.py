#!/usr/bin/env python3
"""
GitHub Webhook Deploy Listener
Listens on port 9876, receives GitHub push events,
runs git pull + docker compose rebuild in background.
"""
import hashlib
import hmac
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Load /opt/eva/.env if exists (PM2 doesn't source env by default)
ENV_FILE = os.environ.get("EVA_ENV_FILE", "/opt/eva/.env")
if os.path.isfile(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

PORT = int(os.environ.get("WEBHOOK_PORT", 9876))
SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
REPO_DIR = os.environ.get("EVA_REPO_DIR", "/opt/eva")
LOG_FILE = os.path.join(REPO_DIR, "logs", "webhook_deploy.log")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def log(msg):
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def verify_signature(payload: bytes, signature: str) -> bool:
    if not SECRET:
        log("WARNING: No WEBHOOK_SECRET set — skipping verification")
        return True
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def run_deploy():
    """Run git pull + docker compose rebuild in background."""
    log("=== DEPLOY START ===")
    cmds = [
        f"cd {REPO_DIR} && git pull origin main",
        f"cd {REPO_DIR} && docker compose build --no-cache web worker",
        f"cd {REPO_DIR} && docker compose up -d web worker",
    ]
    for cmd in cmds:
        log(f">>> {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=600
            )
            if result.stdout:
                log(result.stdout.strip()[-500:])
            if result.returncode != 0:
                log(f"ERROR (exit {result.returncode}): {result.stderr.strip()[-500:]}")
                log("=== DEPLOY FAILED ===")
                return
        except subprocess.TimeoutExpired:
            log(f"TIMEOUT: {cmd}")
            log("=== DEPLOY FAILED (timeout) ===")
            return
        except Exception as e:
            log(f"EXCEPTION: {e}")
            log("=== DEPLOY FAILED ===")
            return
    log("=== DEPLOY SUCCESS ===")


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"EVA Webhook Deploy Listener - OK\n")

    def do_POST(self):
        if self.path not in ("/webhook", "/webhook/github", "/"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify GitHub signature
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, sig):
            log(f"REJECTED: Invalid signature")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        # Check event type
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            log("PING received from GitHub")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Pong")
            return

        if event != "push":
            log(f"SKIP: event={event}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK (ignored event)")
            return

        # Check branch
        import json
        try:
            payload = json.loads(body)
            ref = payload.get("ref", "")
            if ref != "refs/heads/main":
                log(f"SKIP: push to {ref} (not main)")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK (not main branch)")
                return
            pusher = payload.get("pusher", {}).get("name", "unknown")
        except Exception:
            pusher = "unknown"

        log(f"DEPLOY TRIGGERED by {pusher}")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Deploy started")

        # Run deploy in background thread
        t = threading.Thread(target=run_deploy, daemon=True)
        t.start()

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs


def main():
    if not SECRET:
        log("WARNING: WEBHOOK_SECRET not set! GitHub signatures won't be verified.")
    server = HTTPServer(("127.0.0.1", PORT), WebhookHandler)
    log(f"Webhook listener started on 127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
