"""Invoice business rules: validation, totals, numbering, finalization, sending, payments."""
import datetime as dt
import json
import os
import uuid

from . import db as dbmod
from .money import MoneyError, cents, dec_str, percent, to_decimal
from .mailer import MailError, send_email
from . import pdf as pdfmod

STATUSES = ("draft", "finalized", "sent", "paid", "cancelled")
SEND_LOCK_SECONDS = 120
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "pdfs")


class ValidationFailed(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()) or "Invalid invoice")
        self.errors = errors


class BusinessError(Exception):
    """A rule was broken (wrong status, missing client, etc.). Message is shown to the user."""


def today():
    return dt.date.today()


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log_activity(conn, invoice_id, kind, detail=""):
    conn.execute("INSERT INTO activity(invoice_id, kind, detail, at) VALUES (?,?,?,?)",
                 (invoice_id, kind, detail[:300], _now()))


def ensure_share_token(conn, invoice_id):
    """Random, unguessable token for the public PDF link. Created once per invoice."""
    row = conn.execute("SELECT share_token, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        raise BusinessError("Invoice not found.")
    if row["status"] == "draft":
        raise BusinessError("Finalize the invoice before sharing it.")
    if row["share_token"]:
        return row["share_token"]
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    with dbmod.transaction(conn):
        conn.execute("UPDATE invoices SET share_token=? WHERE id=? AND share_token IS NULL", (token, invoice_id))
    return conn.execute("SELECT share_token FROM invoices WHERE id=?", (invoice_id,)).fetchone()[0]


def whatsapp_link(phone, message):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        raise BusinessError("Add the client's phone number (with country code) to share on WhatsApp.")
    from urllib.parse import quote
    return f"https://wa.me/{digits}?text={quote(message)}"


def parse_date(value, field):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationFailed({field: f"{field.replace('_', ' ').capitalize()} must be a valid date."})


def compute_line(item):
    qty = to_decimal(item["quantity"], "Quantity")
    if qty <= 0:
        raise MoneyError("Quantity must be greater than zero.")
    price = to_decimal(item["unit_price"], "Unit price", allow_negative=False)
    disc = percent(item.get("discount_pct") or "0", "Discount")
    vat = percent(item["vat_rate"], "VAT rate")
    gross = cents(qty * price)
    discount = cents(gross * disc / 100)
    net = gross - discount
    vat_amt = cents(net * vat / 100)
    return {
        "quantity": str(qty.normalize()) if qty == qty.to_integral() else str(qty),
        "unit_price": dec_str(price),
        "discount_pct": str(disc.normalize()),
        "vat_rate": str(vat.normalize()),
        "line_net": dec_str(net),
        "line_discount": dec_str(discount),
        "line_vat": dec_str(vat_amt),
        "line_total": dec_str(net + vat_amt),
    }


def compute_totals(lines):
    subtotal = sum((to_decimal(l["line_net"], allow_negative=True) for l in lines), cents(0))
    discount = sum((to_decimal(l["line_discount"], allow_negative=True) for l in lines), cents(0))
    vat = sum((to_decimal(l["line_vat"], allow_negative=True) for l in lines), cents(0))
    return {"subtotal": dec_str(subtotal), "discount_total": dec_str(discount),
            "vat_total": dec_str(vat), "total": dec_str(subtotal + vat)}


def vat_breakdown(lines):
    """VAT grouped by rate, for the totals block and legal requirements."""
    groups = {}
    for l in lines:
        g = groups.setdefault(l["vat_rate"], {"net": cents(0), "vat": cents(0)})
        g["net"] += to_decimal(l["line_net"], allow_negative=True)
        g["vat"] += to_decimal(l["line_vat"], allow_negative=True)
    return [{"rate": rate, "net": dec_str(v["net"]), "vat": dec_str(v["vat"])}
            for rate, v in sorted(groups.items(), key=lambda kv: float(kv[0]))]


def normalize_payload(payload, settings):
    """Validate form input and compute all money. Returns a clean dict or raises ValidationFailed."""
    errors = {}
    client_id = payload.get("client_id")
    try:
        client_id = int(client_id) if client_id not in (None, "", "None") else None
    except (TypeError, ValueError):
        client_id = None

    issue = due = None
    try:
        issue = parse_date(payload.get("issue_date") or today().isoformat(), "issue_date")
        due = parse_date(payload.get("due_date") or (issue + dt.timedelta(days=int(settings["payment_terms_days"]))).isoformat(), "due_date")
    except ValidationFailed as e:
        errors.update(e.errors)
    if issue and due and due < issue:
        errors["due_date"] = "Due date cannot be before the issue date."

    template = payload.get("template") or settings["default_template"]
    if template not in pdfmod.TEMPLATES:
        errors["template"] = "Choose a valid template."

    raw_items = payload.get("items") or []
    lines = []
    for idx, raw in enumerate(raw_items, start=1):
        desc = (raw.get("description") or "").strip()
        if not desc:
            errors[f"items.{idx}.description"] = f"Line {idx}: description is required."
            continue
        if len(desc) > 500:
            errors[f"items.{idx}.description"] = f"Line {idx}: description is too long."
            continue
        try:
            computed = compute_line({
                "quantity": raw.get("quantity", "1"),
                "unit_price": raw.get("unit_price", ""),
                "discount_pct": raw.get("discount_pct") or "0",
                "vat_rate": raw.get("vat_rate", settings["default_vat_rate"]),
            })
        except MoneyError as e:
            errors[f"items.{idx}"] = f"Line {idx}: {e}"
            continue
        sid = raw.get("service_id")
        lines.append({
            "service_id": int(sid) if str(sid or "").isdigit() else None,
            "description": desc,
            "unit": (raw.get("unit") or "").strip()[:40],
            **computed,
        })
    if not raw_items:
        errors["items"] = "Add at least one service or custom line."

    totals = compute_totals(lines) if lines else {k: "0.00" for k in ("subtotal", "discount_total", "vat_total", "total")}
    return {
        "client_id": client_id,
        "issue_date": issue.isoformat() if issue else None,
        "due_date": due.isoformat() if due else None,
        "template": template,
        "payment_terms": (payload.get("payment_terms") or "").strip()[:200],
        "payment_instructions": (payload.get("payment_instructions") or "").strip()[:1000],
        "notes": (payload.get("notes") or "").strip()[:1000],
        "items": lines,
        "totals": totals,
        "errors": errors,
    }


def _client_row(conn, client_id):
    if client_id is None:
        return None
    return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def _invoice_row(conn, invoice_id):
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise BusinessError("Invoice not found.")
    return row


def create_draft(conn, settings, idempotency_key=None):
    """Create an empty draft. The same key returns the same draft, so double submits are harmless."""
    key = idempotency_key or uuid.uuid4().hex
    existing = conn.execute("SELECT id FROM invoices WHERE idempotency_key = ?", (key,)).fetchone()
    if existing:
        return existing["id"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    issue = today()
    due = issue + dt.timedelta(days=int(settings["payment_terms_days"]))
    with dbmod.transaction(conn):
        cur = conn.execute(
            """INSERT INTO invoices (status, issue_date, due_date, currency, template, template_options,
               payment_terms, payment_instructions, idempotency_key, created_at, updated_at)
               VALUES ('draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (issue.isoformat(), due.isoformat(), settings["currency"], settings["default_template"],
             json.dumps(settings["template_options"]),
             f"Payment due within {settings['payment_terms_days']} days",
             settings["payment_instructions"], key, now, now),
        )
    return cur.lastrowid


def save_draft(conn, invoice_id, payload, settings):
    """Persist a draft. Raises ValidationFailed (nothing written) when the input is invalid."""
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] != "draft":
        raise BusinessError("Only drafts can be edited. Issued invoices are locked.")
    clean = normalize_payload(payload, settings)
    if clean["errors"]:
        raise ValidationFailed(clean["errors"])
    client = _client_row(conn, clean["client_id"])
    if client is None or client["archived"]:
        raise ValidationFailed({"client_id": "Select an active client."})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    t = clean["totals"]
    with dbmod.transaction(conn):
        conn.execute(
            """UPDATE invoices SET client_id=?, issue_date=?, due_date=?, template=?, payment_terms=?,
               payment_instructions=?, notes=?, subtotal=?, discount_total=?, vat_total=?, total=?, updated_at=?
               WHERE id=? AND status='draft'""",
            (client["id"], clean["issue_date"], clean["due_date"], clean["template"], clean["payment_terms"],
             clean["payment_instructions"], clean["notes"], t["subtotal"], t["discount_total"],
             t["vat_total"], t["total"], now, invoice_id),
        )
        conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        for pos, line in enumerate(clean["items"], start=1):
            conn.execute(
                """INSERT INTO invoice_items (invoice_id, position, service_id, description, quantity, unit,
                   unit_price, discount_pct, vat_rate, line_net, line_discount, line_vat, line_total)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invoice_id, pos, line["service_id"], line["description"], line["quantity"], line["unit"],
                 line["unit_price"], line["discount_pct"], line["vat_rate"], line["line_net"],
                 line["line_discount"], line["line_vat"], line["line_total"]),
            )
    return invoice_id


def load_render_data(conn, invoice_id, settings):
    """Everything the PDF needs. Finalized invoices use frozen snapshots, never live records."""
    inv = _invoice_row(conn, invoice_id)
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position", (invoice_id,)).fetchall()]
    if inv["status"] == "draft":
        client = _client_row(conn, inv["client_id"])
        client_info = dict(client) if client else {}
        seller = {k: settings[k] for k in ("business_name", "business_email", "business_phone",
                                           "business_address", "registration_no", "vat_number")}
        number = "DRAFT"
        logo = settings["logo_path"]
    else:
        client_info = json.loads(inv["client_snapshot"])
        seller = json.loads(inv["seller_snapshot"])
        number = inv["number"]
        logo = seller.get("logo_path", "")
    return {
        "number": number,
        "status": inv["status"],
        "issue_date": inv["issue_date"],
        "due_date": inv["due_date"],
        "currency": inv["currency"],
        "payment_terms": inv["payment_terms"],
        "payment_instructions": inv["payment_instructions"],
        "bank": {k: settings[k] for k in ("bank_name", "bank_iban", "bank_bic")},
        "notes": inv["notes"],
        "client": client_info,
        "seller": seller,
        "logo_path": logo,
        "items": items,
        "vat_breakdown": vat_breakdown(items),
        "totals": {k: inv[k] for k in ("subtotal", "discount_total", "vat_total", "total")},
        "legal_footer": settings["legal_footer"],
    }


def _pdf_path(invoice_id):
    os.makedirs(PDF_DIR, exist_ok=True)
    return os.path.join(PDF_DIR, f"invoice-{invoice_id}.pdf")


def finalize(conn, invoice_id, settings):
    """Assign the next number, freeze snapshots and write the PDF. All or nothing."""
    with dbmod.transaction(conn):
        inv = _invoice_row(conn, invoice_id)
        if inv["status"] != "draft":
            raise BusinessError("This invoice is already finalized.")
        client = _client_row(conn, inv["client_id"])
        if client is None or client["archived"]:
            raise BusinessError("Select an active client before finalizing.")
        if not (client["email"] or "").strip():
            raise BusinessError("This client has no email address. Add one before sending.")
        n_items = conn.execute("SELECT COUNT(*) FROM invoice_items WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
        if n_items == 0:
            raise BusinessError("Add at least one line item.")
        if not settings["business_name"]:
            raise BusinessError("Set your business name in Business Settings before finalizing.")

        number_int = int(settings["invoice_next_number"])
        number = f"{settings['invoice_prefix']}{str(number_int).zfill(int(settings['invoice_pad']))}"
        conn.execute("UPDATE settings SET value=? WHERE key='invoice_next_number'", (str(number_int + 1),))

        seller = {k: settings[k] for k in ("business_name", "business_email", "business_phone",
                                           "business_address", "registration_no", "vat_number")}
        seller["logo_path"] = settings["logo_path"]
        client_snapshot = {k: client[k] for k in ("name", "email", "billing_address", "registration_no", "vat_number")}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE invoices SET number=?, status='finalized', client_snapshot=?, seller_snapshot=?,
               finalized_at=?, updated_at=? WHERE id=?""",
            (number, json.dumps(client_snapshot), json.dumps(seller), now, now, invoice_id),
        )
        data = load_render_data(conn, invoice_id, settings)
        pdf_bytes = pdfmod.render(data, inv["template"], json.loads(inv["template_options"] or "{}"))
        path = _pdf_path(invoice_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(pdf_bytes)
        os.replace(tmp, path)
        conn.execute("UPDATE invoices SET pdf_path=? WHERE id=?", (path, invoice_id))
        log_activity(conn, invoice_id, "finalized", number)
    return number


def get_pdf_bytes(conn, invoice_id, settings):
    """Stored PDF for finalized invoices, a fresh render for drafts (preview only)."""
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] != "draft" and inv["pdf_path"] and os.path.exists(inv["pdf_path"]):
        with open(inv["pdf_path"], "rb") as fh:
            return fh.read()
    data = load_render_data(conn, invoice_id, settings)
    return pdfmod.render(data, inv["template"], json.loads(inv["template_options"] or "{}"))


def render_email(settings, conn, invoice_id, subject=None, body=None):
    inv = _invoice_row(conn, invoice_id)
    client = json.loads(inv["client_snapshot"]) if inv["client_snapshot"] not in (None, "{}") else {}
    ctx = {
        "client_name": client.get("name", ""), "number": inv["number"] or "",
        "total": f"{cents(inv['total']):,.2f} {inv['currency']}", "due_date": inv["due_date"],
        "business_name": json.loads(inv["seller_snapshot"]).get("business_name", "") if inv["seller_snapshot"] != "{}" else settings["business_name"],
    }
    subject = (subject or settings["email_subject"]).format(**ctx)
    body = (body or settings["email_body"]).format(**ctx)
    return subject, body


def send(conn, invoice_id, settings, subject=None, body=None):
    """Email the stored PDF to the client. Status changes to 'sent' only after the provider accepts it."""
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    stale = (now_dt - dt.timedelta(seconds=SEND_LOCK_SECONDS)).isoformat(timespec="seconds")
    with dbmod.transaction(conn):
        inv = _invoice_row(conn, invoice_id)
        if inv["status"] not in ("finalized", "sent"):
            raise BusinessError("Only finalized invoices can be sent.")
        locked = conn.execute(
            """UPDATE invoices SET send_locked_at=? WHERE id=?
               AND (send_locked_at IS NULL OR send_locked_at < ?)""",
            (now, invoice_id, stale, )).rowcount
        if not locked:
            raise BusinessError("This invoice is already being sent. Wait a moment and check the history.")

    recipient = json.loads(inv["client_snapshot"]).get("email", "")
    error = None
    message_id = None
    subject_used = subject or ""
    try:
        subject_used, body_text = render_email(settings, conn, invoice_id, subject, body)
        pdf_path = inv["pdf_path"]
        if not pdf_path or not os.path.exists(pdf_path):
            raise MailError("The finalized PDF is missing on disk. It cannot be sent.")
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
        message_id = send_email(
            to=recipient, subject=subject_used, body=body_text,
            attachment=(f"{inv['number']}.pdf", pdf_bytes),
        )
    except (MailError, KeyError, ValueError) as exc:
        error = str(exc)
    finally:
        with dbmod.transaction(conn):
            conn.execute("UPDATE invoices SET send_locked_at=NULL WHERE id=?", (invoice_id,))
            conn.execute(
                "INSERT INTO email_log(invoice_id, recipient, subject, status, error, attempted_at) VALUES (?,?,?,?,?,?)",
                (invoice_id, recipient, subject_used or "(not rendered)", "failed" if error else "sent", error, now),
            )
            if not error:
                conn.execute("UPDATE invoices SET status='sent', updated_at=? WHERE id=? AND status IN ('finalized','sent')",
                             (now, invoice_id))
    with dbmod.transaction(conn):
        log_activity(conn, invoice_id, "email_failed" if error else "email_sent", error or recipient)
    if error:
        raise BusinessError(f"Email was not sent: {error}")
    return {"recipient": recipient, "message_id": message_id, "sent_at": now}


def effective_status(inv, on=None):
    on = on or today()
    if inv["status"] in ("finalized", "sent") and inv["due_date"] < on.isoformat():
        return "overdue"
    return inv["status"]


def mark_paid(conn, invoice_id, amount, paid_date):
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] not in ("finalized", "sent"):
        raise BusinessError("Only issued, unpaid invoices can be marked as paid.")
    amt = to_decimal(amount, "Paid amount")
    if amt != to_decimal(inv["total"]):
        raise BusinessError("Partial payments are not supported yet. Enter the full invoice total.")
    try:
        parsed = dt.date.fromisoformat(paid_date)
    except (TypeError, ValueError):
        raise BusinessError("Enter a valid payment date.")
    with dbmod.transaction(conn):
        conn.execute("UPDATE invoices SET status='paid', paid_amount=?, paid_date=?, updated_at=? WHERE id=?",
                     (dec_str(amt), parsed.isoformat(), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), invoice_id))
        log_activity(conn, invoice_id, "paid", parsed.isoformat())


def mark_unpaid(conn, invoice_id):
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] != "paid":
        raise BusinessError("Only paid invoices can be reopened.")
    emailed = conn.execute("SELECT 1 FROM email_log WHERE invoice_id=? AND status='sent'", (invoice_id,)).fetchone()
    new_status = "sent" if emailed else "finalized"
    with dbmod.transaction(conn):
        conn.execute("UPDATE invoices SET status=?, paid_amount=NULL, paid_date=NULL, updated_at=? WHERE id=?",
                     (new_status, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), invoice_id))
        log_activity(conn, invoice_id, "reopened", new_status)


def cancel(conn, invoice_id, reason):
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] not in ("finalized", "sent"):
        raise BusinessError("Only unpaid issued invoices can be cancelled. For a paid invoice, issue a correction.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise BusinessError("Enter a short reason for the cancellation.")
    with dbmod.transaction(conn):
        conn.execute("UPDATE invoices SET status='cancelled', cancel_reason=?, updated_at=? WHERE id=?",
                     (reason[:300], dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), invoice_id))
        log_activity(conn, invoice_id, "cancelled", reason[:300])


def duplicate(conn, invoice_id, settings):
    """New draft with the same client and line items, fresh dates, no number."""
    src = _invoice_row(conn, invoice_id)
    new_id = create_draft(conn, settings)
    items = conn.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position", (invoice_id,)).fetchall()
    payload = {
        "client_id": src["client_id"],
        "template": src["template"],
        "payment_terms": src["payment_terms"],
        "payment_instructions": src["payment_instructions"],
        "notes": src["notes"],
        "items": [{"service_id": r["service_id"], "description": r["description"], "unit": r["unit"],
                   "quantity": r["quantity"], "unit_price": r["unit_price"],
                   "discount_pct": r["discount_pct"], "vat_rate": r["vat_rate"]} for r in items],
    }
    save_draft(conn, new_id, payload, settings)
    return new_id


def delete_draft(conn, invoice_id):
    inv = _invoice_row(conn, invoice_id)
    if inv["status"] != "draft":
        raise BusinessError("Only drafts can be deleted. Cancel issued invoices instead.")
    with dbmod.transaction(conn):
        conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
