"""Pluggable notifications: SMTP mail, webhook and/or ntfy push. Off unless NOTIFY_METHODS is set."""
import json
import logging
import os
import smtplib
import urllib.request
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_smtp(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = os.environ["SMTP_TO"]
    with smtplib.SMTP(host, port, timeout=30) as s:
        if os.environ.get("SMTP_STARTTLS", "1") == "1":
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.sendmail(msg["From"], [a.strip() for a in msg["To"].split(",")], msg.as_string())


def send_webhook(subject, body):
    url = os.environ["WEBHOOK_URL"]
    data = json.dumps({"text": "%s\n%s" % (subject, body)}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def send_ntfy(subject, body):
    """Push to a phone through ntfy (https://ntfy.sh or a self-hosted server).

    NTFY_TOPIC is effectively a password on the public server: anyone who knows
    it can read the messages, so use a long random name and keep it in .env.
    """
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": subject, "message": body,
               "priority": int(os.environ.get("NTFY_PRIORITY", "4")), "tags": ["warning"]}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


SENDERS = {"smtp": send_smtp, "webhook": send_webhook, "ntfy": send_ntfy}


class Notifier:
    def __init__(self, methods):
        self.methods = [m for m in methods if m in SENDERS]
        unknown = [m for m in methods if m not in SENDERS]
        if unknown:
            log.warning("unknown notify methods ignored: %s", unknown)

    @property
    def enabled(self):
        return bool(self.methods)

    def send(self, subject, body):
        """Returns True if at least one method succeeded."""
        ok = False
        for m in self.methods:
            try:
                SENDERS[m](subject, body)
                ok = True
                log.info("notified via %s: %s", m, subject)
            except Exception as e:  # never log credentials, only the error type/message
                log.error("notify via %s failed: %s", m, type(e).__name__)
        return ok
