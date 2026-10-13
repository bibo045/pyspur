"""Email delivery over SMTP (works with SES, Postmark, SendGrid, Mailgun, Resend, Gmail app passwords...).

Credentials live only in environment variables on the server and are never rendered to the browser.
If the provider is not configured, sending fails loudly instead of pretending to succeed.
"""
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


class MailError(Exception):
    pass


REQUIRED = ("SMTP_HOST", "SMTP_FROM")


def config_status():
    """Safe-to-display summary: which variables are present, never their values."""
    return {name: bool(os.environ.get(name)) for name in
            ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY")}


def is_configured():
    return all(os.environ.get(v) for v in REQUIRED)


def _settings():
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        raise MailError(f"Email is not configured. Set {', '.join(missing)} on the server.")
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
    if security not in ("starttls", "ssl", "none"):
        raise MailError("SMTP_SECURITY must be starttls, ssl or none.")
    return {
        "host": os.environ["SMTP_HOST"],
        "port": int(os.environ.get("SMTP_PORT", "465" if security == "ssl" else "587")),
        "username": os.environ.get("SMTP_USERNAME", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": os.environ["SMTP_FROM"],
        "security": security,
    }


def send_email(to, subject, body, attachment=None):
    """Send one message. Returns the Message-ID on success; raises MailError otherwise."""
    if not to or "@" not in to:
        raise MailError("The client has no valid email address.")
    cfg = _settings()
    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = to
    msg["Subject"] = subject
    msg_id = make_msgid(domain=cfg["from"].split("@")[-1].strip(">"))
    msg["Message-ID"] = msg_id
    msg.set_content(body)
    if attachment:
        filename, data = attachment
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "application/pdf").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    try:
        context = ssl.create_default_context()
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=context)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        with server:
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            refused = server.send_message(msg)
            if refused:
                raise MailError(f"Provider refused the recipient: {refused}")
    except MailError:
        raise
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Email provider error: {exc}") from exc
    return msg_id
