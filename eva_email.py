"""Transactional email helper for EVA using Resend."""
import json
import logging
import threading
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger("eva.email")

def _send_welcome_worker(email: str, name: str):
    try:
        env_file = Path("/app/.secrets/resend.env")
        if not env_file.exists():
            env_file = Path("/opt/eva/.secrets/resend.env")
        if not env_file.exists():
            logger.warning("Resend env file not found, skipping welcome email")
            return

        env_data = dict(line.strip().split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
        api_key = env_data.get("RESEND_API_KEY")
        if not api_key:
            logger.warning("RESEND_API_KEY missing, skipping welcome email")
            return

        from_email = env_data.get("RESEND_FROM", "EVA by DYENA <hello@eva.dyena.id>")

        html_path = Path("/app/data/welcome_email.html")
        if not html_path.exists():
            html_path = Path("/opt/eva/data/welcome_email.html")
        text_path = Path("/app/data/welcome_email.txt")
        if not text_path.exists():
            text_path = Path("/opt/eva/data/welcome_email.txt")

        if not html_path.exists() or not text_path.exists():
            logger.warning("Welcome email template not found")
            return

        html_tpl = html_path.read_text(encoding="utf-8")
        text_tpl = text_path.read_text(encoding="utf-8")

        html_content = html_tpl.replace("{{ name }}", name).replace("{{ email }}", email)
        text_content = text_tpl.replace("{{ name }}", name).replace("{{ email }}", email)

        payload = {
            "from": from_email,
            "to": [email],
            "subject": "Selamat datang di EVA",
            "html": html_content,
            "text": text_content
        }

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "EVA-Transactional/1.0"
            }
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            logger.info("Welcome email sent to %s (id: %s)", email, res.get("id"))
    except Exception as exc:
        logger.error("Failed to send welcome email to %s: %s", email, exc)

def send_welcome_email_async(email: str, name: str):
    """Fire and forget welcome email in a background thread."""
    if not email:
        return
    t = threading.Thread(target=_send_welcome_worker, args=(email, name or email), daemon=True)
    t.start()


def _send_plan_activated_worker(email: str, name: str, plan: str, until: str, amount: str, invoice: str):
    try:
        env_file = Path("/app/.secrets/resend.env")
        if not env_file.exists():
            env_file = Path("/opt/eva/.secrets/resend.env")
        if not env_file.exists():
            logger.warning("Resend env file not found, skipping plan email")
            return
        env_data = dict(line.strip().split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
        api_key = env_data.get("RESEND_API_KEY")
        if not api_key:
            logger.warning("RESEND_API_KEY missing, skipping plan email")
            return
        from_email = env_data.get("RESEND_FROM", "EVA by DYENA <hello@eva.dyena.id>")
        html_path = Path("/app/data/plan_activated.html")
        if not html_path.exists():
            html_path = Path("/opt/eva/data/plan_activated.html")
        text_path = Path("/app/data/plan_activated.txt")
        if not text_path.exists():
            text_path = Path("/opt/eva/data/plan_activated.txt")
        if not html_path.exists() or not text_path.exists():
            logger.warning("Plan email template not found")
            return
        fields = {"name": name or email, "plan": plan, "until": until, "amount": amount, "invoice": invoice}
        def render(tpl: str) -> str:
            for key, value in fields.items():
                tpl = tpl.replace("{{ " + key + " }}", value)
            return tpl
        payload = {
            "from": from_email,
            "to": [email],
            "subject": "Paket " + plan + " aktif — EVA by DYENA",
            "html": render(html_path.read_text(encoding="utf-8")),
            "text": render(text_path.read_text(encoding="utf-8")),
        }
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "User-Agent": "EVA-Transactional/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            logger.info("Plan-activated email sent to %s (id: %s)", email, res.get("id"))
    except Exception as exc:
        logger.error("Failed to send plan email to %s: %s", email, exc)


def send_plan_activated_async(email: str, name: str, plan: str, until: str, amount: str, invoice: str):
    """Fire and forget plan-activation email in a background thread."""
    if not email:
        return
    t = threading.Thread(target=_send_plan_activated_worker,
                         args=(email, name or email, plan, until, amount, invoice), daemon=True)
    t.start()



_REMINDER_TEMPLATES = {
    "expiring": "plan_expiring",
    "expired": "plan_expired",
    "trial": "plan_trial_ending",
}


def _send_plan_reminder_worker(email: str, name: str, kind: str, plan: str, until: str, when: str):
    try:
        env_file = Path("/app/.secrets/resend.env")
        if not env_file.exists():
            env_file = Path("/opt/eva/.secrets/resend.env")
        if not env_file.exists():
            logger.warning("Resend env file not found, skipping reminder email")
            return
        env_data = dict(line.strip().split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
        api_key = env_data.get("RESEND_API_KEY")
        if not api_key:
            logger.warning("RESEND_API_KEY missing, skipping reminder email")
            return
        from_email = env_data.get("RESEND_FROM", "EVA by DYENA <hello@eva.dyena.id>")
        base = _REMINDER_TEMPLATES.get(kind)
        if not base:
            logger.warning("Unknown reminder kind: %s", kind)
            return
        html_path = Path("/app/data/" + base + ".html")
        if not html_path.exists():
            html_path = Path("/opt/eva/data/" + base + ".html")
        text_path = Path("/app/data/" + base + ".txt")
        if not text_path.exists():
            text_path = Path("/opt/eva/data/" + base + ".txt")
        if not html_path.exists() or not text_path.exists():
            logger.warning("Reminder template not found: %s", base)
            return
        fields = {"name": name or email, "plan": plan, "until": until, "when": when}
        def render(tpl: str) -> str:
            for key, value in fields.items():
                tpl = tpl.replace("{{ " + key + " }}", value)
            return tpl
        if kind == "expiring":
            subject = "Paket " + plan + " berakhir " + when + " — EVA by DYENA"
        elif kind == "expired":
            subject = "Paket " + plan + " sudah berakhir — EVA by DYENA"
        else:
            subject = "Uji coba Premium berakhir " + when + " — EVA by DYENA"
        payload = {
            "from": from_email,
            "to": [email],
            "subject": subject,
            "html": render(html_path.read_text(encoding="utf-8")),
            "text": render(text_path.read_text(encoding="utf-8")),
        }
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "User-Agent": "EVA-Transactional/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            logger.info("Plan-reminder email (%s) sent to %s (id: %s)", kind, email, res.get("id"))
    except Exception as exc:
        logger.error("Failed to send reminder email (%s) to %s: %s", kind, email, exc)


def send_plan_reminder_async(email: str, name: str, kind: str, plan: str, until: str, when: str):
    """Fire and forget plan reminder email in a background thread."""
    if not email:
        return
    t = threading.Thread(target=_send_plan_reminder_worker,
                         args=(email, name or email, kind, plan, until, when), daemon=True)
    t.start()
