"""HTTP layer: pages, JSON endpoints used by the editor, and PDF downloads."""
import datetime as dt
import json
import os
import re
from decimal import Decimal

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.utils import secure_filename

from . import db as dbmod
from . import invoices as inv
from . import mailer, pdf as pdfmod
from .money import MoneyError, cents, fmt, to_decimal
from .seed import clear_demo, seed_demo

bp = Blueprint("web", __name__)
PAGE_SIZE = 20
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "uploads")
ALLOWED_LOGO = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def settings():
    return dbmod.get_settings(g.db)


def _json_error(message, status=400, errors=None):
    return jsonify({"ok": False, "error": message, "errors": errors or {}}), status


def _pdf_response(data, filename, inline=True):
    return Response(data, mimetype="application/pdf", headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{filename}"',
        "Cache-Control": "no-store",
    })


@bp.app_template_filter("money")
def money_filter(value):
    return f"{cents(value):,.2f}"


@bp.app_template_filter("datetime")
def datetime_filter(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    return parsed.astimezone().strftime("%d %b %Y, %H:%M")


@bp.app_template_filter("date")
def date_filter(value):
    try:
        return dt.date.fromisoformat(value).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return value or ""


@bp.app_context_processor
def inject_globals():
    s = settings() if getattr(g, "db", None) else {}
    return {
        "today": dt.date.today(),
        "mail_ready": mailer.is_configured(),
        "currency": s.get("currency", "EUR"),
        "effective": inv.effective_status,
        "business_name": s.get("business_name", ""),
    }


# ---------------------------------------------------------------- dashboard
def _monthly_totals(rows, today, months=6):
    """Invoiced totals for the last N months, oldest first. Used for the sparkline."""
    buckets = []
    year, month = today.year, today.month
    for _ in range(months):
        buckets.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    buckets.reverse()
    totals = {b: Decimal("0") for b in buckets}
    for r in rows:
        if r["status"] == "cancelled":
            continue
        key = r["issue_date"][:7]
        if key in totals:
            totals[key] += to_decimal(r["total"], allow_negative=True)
    return [float(totals[b]) for b in buckets]


@bp.get("/welcome")
def welcome():
    return render_template("welcome.html")


@bp.get("/more")
def more():
    return render_template("more.html")


@bp.get("/sw.js")
def service_worker():
    """Served from the root so the worker controls the whole app. Caches static files only."""
    path = os.path.join(os.path.dirname(__file__), "static", "sw.js")
    resp = send_file(path, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/favicon.ico")
def favicon():
    return Response(status=204)


@bp.get("/")
def dashboard():
    today = dt.date.today()
    month_prefix = today.strftime("%Y-%m")
    rows = g.db.execute("""SELECT i.*, c.name AS client_name FROM invoices i
                           LEFT JOIN clients c ON c.id = i.client_id
                           WHERE i.status != 'draft' ORDER BY i.id DESC""").fetchall()
    month_total = Decimal("0")
    outstanding = Decimal("0")
    paid = Decimal("0")
    paid_count = overdue_count = 0
    overdue = Decimal("0")
    for r in rows:
        if r["status"] == "cancelled":
            continue
        total = to_decimal(r["total"], allow_negative=True)
        if r["issue_date"].startswith(month_prefix):
            month_total += total
        if r["status"] == "paid":
            paid += total
            paid_count += 1
        else:
            outstanding += total
            if inv.effective_status(r, today) == "overdue":
                overdue += total
                overdue_count += 1
    recent = rows[:8]
    outstanding_count = sum(1 for r in rows if r["status"] in ("finalized", "sent"))
    counts = {k: sum(1 for r in g.db.execute("SELECT status FROM invoices").fetchall() if r["status"] == k)
              for k in ("draft", "sent", "paid")}
    spark = _monthly_totals(rows, today, months=6)
    overdue_rows = [r for r in rows if inv.effective_status(r, today) == "overdue"][:5]
    return render_template("dashboard.html", month_total=month_total, outstanding=outstanding,
                           paid=paid, paid_count=paid_count, overdue=overdue,
                           overdue_count=overdue_count, recent=recent, overdue_rows=overdue_rows,
                           outstanding_count=outstanding_count, counts=counts, spark=spark,
                           has_demo=bool(g.db.execute("SELECT 1 FROM clients WHERE is_demo=1").fetchone()),
                           setup_ok=bool(settings()["business_name"]) and mailer.is_configured())


# ---------------------------------------------------------------- invoices list / detail
@bp.get("/invoices")
def invoice_list():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    sort = request.args.get("sort") or "number"
    direction = "ASC" if request.args.get("dir") == "asc" else "DESC"
    page = max(1, int(request.args.get("page", 1) or 1))
    sort_cols = {"number": "COALESCE(i.number, 'zzz'), i.id", "client": "c.name", "issue": "i.issue_date",
                 "due": "i.due_date", "total": "CAST(i.total AS REAL)"}
    order = sort_cols.get(sort, sort_cols["number"])
    sql = """SELECT i.*, c.name AS client_name FROM invoices i LEFT JOIN clients c ON c.id = i.client_id WHERE 1=1"""
    params = []
    if q:
        sql += " AND (i.number LIKE ? OR c.name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    rows = g.db.execute(f"{sql} ORDER BY {order} {direction}", params).fetchall()
    today = dt.date.today()
    if status:
        rows = [r for r in rows if inv.effective_status(r, today) == status]
    total_rows = len(rows)
    pages = max(1, -(-total_rows // PAGE_SIZE))
    page = min(page, pages)
    shown = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return render_template("invoices.html", rows=shown, q=q, status=status, sort=sort,
                           dir=direction.lower(), page=page, pages=pages, total_rows=total_rows,
                           statuses=["draft", "finalized", "sent", "overdue", "paid", "cancelled"])


@bp.post("/invoices/new")
def invoice_new():
    key = request.form.get("key") or None
    new_id = inv.create_draft(g.db, settings(), key)
    return redirect(url_for("web.invoice_edit", invoice_id=new_id))


@bp.get("/invoices/<int:invoice_id>")
def invoice_detail(invoice_id):
    row = g.db.execute("SELECT i.*, c.name AS client_name, c.email AS client_email FROM invoices i "
                       "LEFT JOIN clients c ON c.id = i.client_id WHERE i.id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if row["status"] == "draft":
        return redirect(url_for("web.invoice_edit", invoice_id=invoice_id))
    items = g.db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position", (invoice_id,)).fetchall()
    emails = g.db.execute("SELECT * FROM email_log WHERE invoice_id=? ORDER BY id DESC", (invoice_id,)).fetchall()
    activity = g.db.execute("SELECT * FROM activity WHERE invoice_id=? ORDER BY id DESC", (invoice_id,)).fetchall()
    subject, body = inv.render_email(settings(), g.db, invoice_id)
    return render_template("invoice_detail.html", inv=row, items=items, emails=emails, activity=activity,
                           effective_status=inv.effective_status(row), subject=subject, body=body,
                           client_snapshot=json.loads(row["client_snapshot"] or "{}"))


@bp.get("/invoices/<int:invoice_id>/edit")
def invoice_edit(invoice_id):
    row = g.db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if row["status"] != "draft":
        return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))
    s = settings()
    clients = g.db.execute("SELECT * FROM clients WHERE archived=0 ORDER BY name").fetchall()
    services = g.db.execute("SELECT * FROM services WHERE archived=0 ORDER BY name").fetchall()
    items = g.db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position", (invoice_id,)).fetchall()
    client = g.db.execute("SELECT * FROM clients WHERE id=?", (row["client_id"],)).fetchone() if row["client_id"] else None
    subject, body = s["email_subject"], s["email_body"]
    editor = {
        "invoice": invoice_id,
        "clientId": row["client_id"],
        "mailReady": mailer.is_configured(),
        "defaultVat": s["default_vat_rate"],
        "currency": s["currency"],
        "items": [dict(i) for i in items],
        "clients": [dict(c) for c in clients],
    }
    return render_template("invoice_edit.html", inv=row, items=items, clients=clients, services=services,
                           client=client, s=s, templates=pdfmod.TEMPLATES, editor=editor,
                           subject=subject, body=body)


def _payload_from_request():
    body = request.get_json(silent=True) or {}
    payload = body.get("payload", body)
    if not isinstance(payload, dict):
        raise MoneyError("Malformed request.")
    return payload


@bp.post("/api/invoices/<int:invoice_id>/draft")
def api_save_draft(invoice_id):
    try:
        inv.save_draft(g.db, invoice_id, _payload_from_request(), settings())
    except inv.ValidationFailed as e:
        return _json_error(str(e), 422, e.errors)
    except inv.BusinessError as e:
        return _json_error(str(e), 409)
    data = inv.load_render_data(g.db, invoice_id, settings())
    return jsonify({"ok": True, "totals": {k: fmt(v, "").strip() for k, v in data["totals"].items()},
                    "lines": [{"id": i["id"], "line_total": f"{cents(i['line_total']):,.2f}"} for i in data["items"]],
                    "saved_at": dt.datetime.now().strftime("%H:%M:%S")})


@bp.post("/api/invoices/<int:invoice_id>/finalize-send")
def api_finalize_send(invoice_id):
    """Validate, save the final draft, finalize, store PDF, then email it. Each step reports honestly."""
    body = request.get_json(silent=True) or {}
    payload = body.get("payload", {})
    subject = (body.get("subject") or "").strip() or None
    text = (body.get("body") or "").strip() or None
    row = g.db.execute("SELECT status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        return _json_error("Invoice not found.", 404)
    stage = "validate"
    try:
        if row["status"] == "draft":
            inv.save_draft(g.db, invoice_id, payload, settings())
            stage = "finalize"
            number = inv.finalize(g.db, invoice_id, settings())
        elif row["status"] == "finalized":
            number = g.db.execute("SELECT number FROM invoices WHERE id=?", (invoice_id,)).fetchone()["number"]
        else:
            return _json_error("This invoice has already been sent. Use Resend from the invoice page.", 409)
        if not body.get("send", True):
            # Finalize only: the invoice is issued and the user picks how to send it on the send screen.
            return jsonify({"ok": True, "number": number, "sent": False,
                            "redirect": url_for("web.invoice_send", invoice_id=invoice_id)})
        stage = "send"
        result = inv.send(g.db, invoice_id, settings(), subject, text)
        return jsonify({"ok": True, "number": number, "sent": True, "recipient": result["recipient"],
                        "redirect": url_for("web.invoice_sent", invoice_id=invoice_id, via="email", to=result["recipient"])})
    except inv.ValidationFailed as e:
        return _json_error(str(e), 422, e.errors)
    except inv.BusinessError as e:
        # Finalized but not sent is a valid, recoverable state: the invoice page offers Resend.
        current = g.db.execute("SELECT status, number FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return jsonify({"ok": False, "stage": stage, "error": str(e), "errors": {},
                        "number": current["number"], "status": current["status"],
                        "redirect": url_for("web.invoice_detail", invoice_id=invoice_id)}), (409 if stage == "send" else 422)
    except Exception:  # never let a PDF or DB fault look like a sent invoice
        return _json_error("Unexpected error while finalizing. Nothing was sent. Try again.", 500)


@bp.get("/invoices/<int:invoice_id>/preview.pdf")
def invoice_preview(invoice_id):
    if g.db.execute("SELECT 1 FROM invoices WHERE id=?", (invoice_id,)).fetchone() is None:
        abort(404)
    data = inv.get_pdf_bytes(g.db, invoice_id, settings())
    return _pdf_response(data, "preview.pdf")


@bp.get("/invoices/<int:invoice_id>/review")
def invoice_review(invoice_id):
    """The invoice as it will be exported: the real data rendered in the chosen template."""
    row = g.db.execute("SELECT i.*, c.name AS client_name, c.email AS client_email, c.phone AS client_phone "
                       "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id WHERE i.id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    s_ = settings()
    data = inv.load_render_data(g.db, invoice_id, s_)
    return render_template("invoice_review.html", inv=row, doc=data, tpl=pdfmod.TEMPLATES.get(row["template"]),
                           opts=json.loads(row["template_options"] or "{}"), templates=pdfmod.TEMPLATES)


@bp.get("/invoices/<int:invoice_id>/send")
def invoice_send(invoice_id):
    row = g.db.execute("SELECT i.*, c.name AS client_name, c.email AS client_email, c.phone AS client_phone "
                       "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id WHERE i.id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if row["status"] == "draft":
        return redirect(url_for("web.invoice_edit", invoice_id=invoice_id))
    subject, body = inv.render_email(settings(), g.db, invoice_id)
    return render_template("invoice_send.html", inv=row, subject=subject, body=body,
                           mail_ready=mailer.is_configured(),
                           public_base=os.environ.get("PUBLIC_BASE_URL", ""))


@bp.post("/invoices/<int:invoice_id>/send/email")
def invoice_send_email(invoice_id):
    subject = (request.form.get("subject") or "").strip() or None
    text = (request.form.get("body") or "").strip() or None
    try:
        result = inv.send(g.db, invoice_id, settings(), subject, text)
    except inv.BusinessError as e:
        flash(str(e), "error")
        return redirect(url_for("web.invoice_send", invoice_id=invoice_id))
    return redirect(url_for("web.invoice_sent", invoice_id=invoice_id, via="email", to=result["recipient"]))


@bp.post("/invoices/<int:invoice_id>/send/shared")
def invoice_send_shared(invoice_id):
    """Called by the send screen after the phone's share sheet finishes with the PDF.
    The app cannot see who the PDF went to, so the record says only that a share happened."""
    row = g.db.execute("SELECT id, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "Invoice not found."}), 404
    if row["status"] == "draft":
        return jsonify({"ok": False, "error": "Finalize the invoice before sharing it."}), 409
    channel = (request.get_json(silent=True) or {}).get("channel", "share")
    label = "Shared PDF via phone share sheet" if channel == "share" else "Downloaded PDF to share"
    with dbmod.transaction(g.db):
        inv.log_activity(g.db, invoice_id, "pdf_shared", label)
    return jsonify({"ok": True})


@bp.get("/invoices/<int:invoice_id>/sent")
def invoice_sent(invoice_id):
    row = g.db.execute("SELECT i.*, c.name AS client_name FROM invoices i LEFT JOIN clients c ON c.id = i.client_id WHERE i.id=?",
                       (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    via = request.args.get("via", "email")
    return render_template("invoice_sent.html", inv=row, via=via, to=request.args.get("to", ""))


@bp.get("/share/<token>.pdf")
def public_pdf(token):
    """Public read-only link to a finalized invoice PDF. Only the unguessable token grants access."""
    row = g.db.execute("SELECT id, number FROM invoices WHERE share_token=? AND status != 'draft'", (token,)).fetchone()
    if row is None:
        abort(404)
    return _pdf_response(inv.get_pdf_bytes(g.db, row["id"], settings()), f"{row['number']}.pdf", inline=True)


@bp.post("/invoices/<int:invoice_id>/share")
def invoice_share(invoice_id):
    try:
        token = inv.ensure_share_token(g.db, invoice_id)
    except inv.BusinessError as e:
        flash(str(e), "error")
        return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))
    link = url_for("web.public_pdf", token=token, _external=True)
    flash(f"Share link created: {link}", "success")
    return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))


@bp.get("/invoices/<int:invoice_id>/download")
def invoice_download(invoice_id):
    row = g.db.execute("SELECT number, status FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    name = f"{row['number'] or 'draft'}.pdf"
    return _pdf_response(inv.get_pdf_bytes(g.db, invoice_id, settings()), name, inline=True)


@bp.post("/invoices/<int:invoice_id>/resend")
def invoice_resend(invoice_id):
    subject = (request.form.get("subject") or "").strip() or None
    text = (request.form.get("body") or "").strip() or None
    try:
        result = inv.send(g.db, invoice_id, settings(), subject, text)
        flash(f"Invoice emailed to {result['recipient']}.", "success")
    except inv.BusinessError as e:
        flash(str(e), "error")
    return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))


@bp.post("/invoices/<int:invoice_id>/payment")
def invoice_payment(invoice_id):
    try:
        if request.form.get("action") == "unpaid":
            inv.mark_unpaid(g.db, invoice_id)
            flash("Invoice reopened as unpaid.", "success")
        else:
            inv.mark_paid(g.db, invoice_id, request.form.get("amount", ""), request.form.get("paid_date", ""))
            flash("Marked as paid.", "success")
    except (inv.BusinessError, MoneyError) as e:
        flash(str(e), "error")
    return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))


@bp.post("/invoices/<int:invoice_id>/cancel")
def invoice_cancel(invoice_id):
    try:
        inv.cancel(g.db, invoice_id, request.form.get("reason", ""))
        flash("Invoice cancelled. Its number is kept in the sequence.", "success")
    except inv.BusinessError as e:
        flash(str(e), "error")
    return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))


@bp.post("/invoices/<int:invoice_id>/duplicate")
def invoice_duplicate(invoice_id):
    try:
        new_id = inv.duplicate(g.db, invoice_id, settings())
    except inv.BusinessError as e:
        flash(str(e), "error")
        return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))
    return redirect(url_for("web.invoice_edit", invoice_id=new_id))


@bp.post("/invoices/<int:invoice_id>/delete")
def invoice_delete(invoice_id):
    try:
        inv.delete_draft(g.db, invoice_id)
        flash("Draft deleted.", "success")
    except inv.BusinessError as e:
        flash(str(e), "error")
        return redirect(url_for("web.invoice_detail", invoice_id=invoice_id))
    return redirect(url_for("web.invoice_list"))


# ---------------------------------------------------------------- clients
def _client_form_values(src):
    return {
        "name": (src.get("name") or "").strip()[:150],
        "email": (src.get("email") or "").strip()[:254],
        "phone": (src.get("phone") or "").strip()[:40],
        "billing_address": (src.get("billing_address") or "").strip()[:500],
        "registration_no": (src.get("registration_no") or "").strip()[:60],
        "vat_number": (src.get("vat_number") or "").strip()[:60],
    }


def _validate_client(values):
    errors = {}
    if not values["name"]:
        errors["name"] = "Name is required."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
        errors["email"] = "Enter a valid email address."
    return errors


@bp.get("/clients")
def clients():
    q = (request.args.get("q") or "").strip()
    show_archived = request.args.get("archived") == "1"
    rows = g.db.execute("SELECT * FROM clients WHERE archived=? AND (name LIKE ? OR email LIKE ?) ORDER BY name",
                        (1 if show_archived else 0, f"%{q}%", f"%{q}%")).fetchall()
    return render_template("clients.html", rows=rows, q=q, show_archived=show_archived)


@bp.route("/clients/new", methods=["GET", "POST"])
@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def client_form(client_id=None):
    client = None
    if client_id is not None:
        client = g.db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if client is None:
            abort(404)
    errors = {}
    values = dict(client) if client else {}
    if request.method == "POST":
        values = _client_form_values(request.form)
        errors = _validate_client(values)
        if not errors:
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            with dbmod.transaction(g.db):
                if client is None:
                    cur = g.db.execute("""INSERT INTO clients(name, email, billing_address, registration_no,
                                          vat_number, phone, created_at) VALUES (?,?,?,?,?,?,?)""",
                                       (values["name"], values["email"], values["billing_address"],
                                        values["registration_no"], values["vat_number"], values["phone"], now))
                    new_id = cur.lastrowid
                else:
                    g.db.execute("""UPDATE clients SET name=?, email=?, billing_address=?, registration_no=?,
                                    vat_number=?, phone=? WHERE id=?""",
                                 (values["name"], values["email"], values["billing_address"],
                                  values["registration_no"], values["vat_number"], values["phone"], client_id))
                    new_id = client_id
            flash("Client saved.", "success")
            return redirect(url_for("web.client_detail", client_id=new_id))
    return render_template("client_form.html", client=client, values=values, errors=errors)


@bp.get("/clients/<int:client_id>")
def client_detail(client_id):
    client = g.db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if client is None:
        abort(404)
    invoices_ = g.db.execute("SELECT * FROM invoices WHERE client_id=? AND status != 'draft' ORDER BY id DESC",
                             (client_id,)).fetchall()
    today = dt.date.today()
    outstanding = sum((to_decimal(r["total"]) for r in invoices_
                       if r["status"] in ("finalized", "sent")), Decimal("0"))
    return render_template("client_detail.html", client=client, invoices=invoices_, today=today,
                           outstanding=outstanding)


@bp.post("/clients/<int:client_id>/archive")
def client_archive(client_id):
    restore = request.form.get("restore") == "1"
    with dbmod.transaction(g.db):
        g.db.execute("UPDATE clients SET archived=? WHERE id=?", (0 if restore else 1, client_id))
    flash("Client restored." if restore else "Client archived. Past invoices are kept.", "success")
    return redirect(url_for("web.clients"))


@bp.post("/api/clients")
def api_client_quick_add():
    values = _client_form_values(request.get_json(silent=True) or {})
    errors = _validate_client(values)
    if errors:
        return _json_error("Check the highlighted fields.", 422, errors)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with dbmod.transaction(g.db):
        cur = g.db.execute("""INSERT INTO clients(name, email, billing_address, registration_no, vat_number, phone, created_at)
                              VALUES (?,?,?,?,?,?,?)""",
                           (values["name"], values["email"], values["billing_address"],
                            values["registration_no"], values["vat_number"], values["phone"], now))
    return jsonify({"ok": True, "client": {**values, "id": cur.lastrowid}})


# ---------------------------------------------------------------- services
def _service_values(src):
    try:
        price = to_decimal(src.get("unit_price"), "Unit price")
        vat = to_decimal(src.get("vat_rate"), "VAT rate")
        if vat > 100:
            raise MoneyError("VAT rate cannot exceed 100.")
    except MoneyError as e:
        return None, str(e)
    return {
        "name": (src.get("name") or "").strip()[:150],
        "description": (src.get("description") or "").strip()[:500],
        "unit_price": str(cents(price)),
        "unit": (src.get("unit") or "fixed fee").strip()[:40],
        "vat_rate": str(vat.normalize()),
    }, None


@bp.get("/services")
def services():
    show_archived = request.args.get("archived") == "1"
    rows = g.db.execute("SELECT * FROM services WHERE archived=? ORDER BY name", (1 if show_archived else 0,)).fetchall()
    return render_template("services.html", rows=rows, show_archived=show_archived)


@bp.route("/services/new", methods=["GET", "POST"])
@bp.route("/services/<int:service_id>/edit", methods=["GET", "POST"])
def service_form(service_id=None):
    svc = None
    if service_id is not None:
        svc = g.db.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        if svc is None:
            abort(404)
    errors = {}
    values = dict(svc) if svc else {"unit": "fixed fee", "vat_rate": settings()["default_vat_rate"]}
    if request.method == "POST":
        parsed, msg = _service_values(request.form)
        values = {**request.form.to_dict()}
        if msg:
            errors["price"] = msg
        if not (request.form.get("name") or "").strip():
            errors["name"] = "Name is required."
        if not errors:
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            with dbmod.transaction(g.db):
                if svc is None:
                    g.db.execute("""INSERT INTO services(name, description, unit_price, unit, vat_rate, created_at)
                                          VALUES (?,?,?,?,?,?)""",
                                       (parsed["name"], parsed["description"], parsed["unit_price"], parsed["unit"],
                                        parsed["vat_rate"], now))
                else:
                    g.db.execute("""UPDATE services SET name=?, description=?, unit_price=?, unit=?, vat_rate=?
                                    WHERE id=?""",
                                 (parsed["name"], parsed["description"], parsed["unit_price"], parsed["unit"],
                                  parsed["vat_rate"], service_id))
            flash("Service saved. Future invoices will use these defaults.", "success")
            return redirect(url_for("web.services"))
    return render_template("service_form.html", svc=svc, values=values, errors=errors)


@bp.post("/services/<int:service_id>/archive")
def service_archive(service_id):
    restore = request.form.get("restore") == "1"
    with dbmod.transaction(g.db):
        g.db.execute("UPDATE services SET archived=? WHERE id=?", (0 if restore else 1, service_id))
    flash("Service restored." if restore else "Service archived.", "success")
    return redirect(url_for("web.services"))


@bp.get("/api/catalog")
def api_catalog():
    rows = g.db.execute("SELECT * FROM services WHERE archived=0 ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- templates
@bp.get("/templates")
def templates_page():
    s = settings()
    return render_template("templates.html", templates=pdfmod.TEMPLATES, categories=pdfmod.CATEGORIES,
                           s=s, opts=s["template_options"], doc=sample_render_data(s))


@bp.post("/templates")
def templates_save():
    s = settings()
    chosen = request.form.get("default_template", s["default_template"])
    if chosen not in pdfmod.TEMPLATES:
        flash("Choose a valid template.", "error")
        return redirect(url_for("web.templates_page"))
    accent = request.form.get("accent", "").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
        accent = pdfmod.TEMPLATES[chosen]["accent_default"]
    font = request.form.get("font", "Helvetica")
    if font not in pdfmod.FONT_FAMILIES:
        font = "Helvetica"
    opts = {"accent": accent, "font": font, "footer": request.form.get("footer", "")[:200],
            "show_logo": request.form.get("show_logo") == "1"}
    dbmod.save_settings(g.db, {"default_template": chosen, "template_options": opts})
    flash("Template saved as your default.", "success")
    return redirect(url_for("web.templates_page"))


def sample_render_data(s):
    """A realistic sample invoice used by the template gallery and the sample PDF."""
    return {
        "number": "INV-0042", "status": "draft", "issue_date": dt.date.today().isoformat(),
        "due_date": (dt.date.today() + dt.timedelta(days=30)).isoformat(), "currency": s["currency"],
        "payment_terms": "Payment due within 30 days", "payment_instructions": s["payment_instructions"],
        "bank": {k: s[k] for k in ("bank_name", "bank_iban", "bank_bic")}, "notes": "",
        "client": {"name": "Sample Client Ltd", "email": "client@example.com",
                   "billing_address": "12 Example Street\n1000 Brussels", "registration_no": "", "vat_number": ""},
        "seller": {"business_name": s["business_name"] or "Your Business", "business_address": s["business_address"],
                   "business_email": s["business_email"], "business_phone": s["business_phone"],
                   "registration_no": s["registration_no"], "vat_number": s["vat_number"]},
        "logo_path": s["logo_path"],
        "items": [
            {"description": "Strategy workshop (half day)", "unit": "hours", "quantity": "4", "unit_price": "120.00",
             "discount_pct": "0", "vat_rate": "21", "line_net": "480.00", "line_discount": "0.00", "line_vat": "100.80", "line_total": "580.80"},
            {"description": "Monthly retainer, long description to show wrapping across the table cell", "unit": "fixed fee",
             "quantity": "1", "unit_price": "950.00", "discount_pct": "10", "vat_rate": "21", "line_net": "855.00", "line_discount": "95.00", "line_vat": "179.55", "line_total": "1034.55"},
        ],
        "vat_breakdown": [{"rate": "21", "net": "1335.00", "vat": "280.35"}],
        "totals": {"subtotal": "1335.00", "discount_total": "95.00", "vat_total": "280.35", "total": "1615.35"},
        "legal_footer": s["legal_footer"],
    }


@bp.get("/templates/sample.pdf")
def template_sample():
    s = settings()
    template = request.args.get("template", s["default_template"])
    if template not in pdfmod.TEMPLATES:
        abort(400)
    accent = request.args.get("accent", "")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", accent):
        accent = ""
    font = request.args.get("font", "Helvetica")
    opts = {"accent": accent or s["template_options"].get("accent"), "font": font,
            "footer": request.args.get("footer", s["template_options"].get("footer", ""))[:200],
            "show_logo": request.args.get("show_logo", "1") == "1"}
    data = sample_render_data(s)
    return _pdf_response(pdfmod.render(data, template, opts), "template-sample.pdf")


# ---------------------------------------------------------------- settings & setup
@bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    s = settings()
    if request.method == "POST":
        updates = {}
        for key in ("business_name", "business_email", "business_phone", "business_address",
                    "registration_no", "vat_number", "bank_name", "bank_iban", "bank_bic",
                    "payment_instructions", "legal_footer", "email_subject", "email_body", "payment_terms_days",
                    "invoice_prefix", "invoice_pad", "invoice_next_number", "currency", "default_vat_rate"):
            if key in request.form:
                updates[key] = request.form.get(key, "").strip()[:1000]
        try:
            updates["currency"] = updates.get("currency", "EUR").upper()
            if not re.fullmatch(r"[A-Z]{3}", updates["currency"]):
                raise MoneyError("Currency must be a three-letter code, e.g. EUR.")
            updates["default_vat_rate"] = str(to_decimal(updates.get("default_vat_rate", "21"), "Default VAT rate").normalize())
            if int(updates.get("payment_terms_days", "30")) < 0:
                raise MoneyError("Payment terms cannot be negative.")
            pad = int(updates.get("invoice_pad", "4"))
            if not 1 <= pad <= 10:
                raise MoneyError("Number padding must be between 1 and 10.")
            nxt = int(updates.get("invoice_next_number", s["invoice_next_number"]))
            if nxt < 1:
                raise MoneyError("Next invoice number must be at least 1.")
            if g.db.execute("SELECT COUNT(*) FROM invoices WHERE number IS NOT NULL").fetchone()[0] and nxt < int(s["invoice_next_number"]):
                raise MoneyError("The numbering sequence can only move forward once invoices exist.")
        except (MoneyError, ValueError) as e:
            flash(str(e), "error")
            return redirect(url_for("web.settings_page"))
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = secure_filename(logo.filename).rsplit(".", 1)[-1].lower()
            if ext not in ALLOWED_LOGO:
                flash("Logo must be a PNG or JPG file.", "error")
                return redirect(url_for("web.settings_page"))
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            path = os.path.abspath(os.path.join(UPLOAD_DIR, f"logo.{ext}"))
            logo.save(path)
            updates["logo_path"] = path
        dbmod.save_settings(g.db, updates)
        flash("Business settings saved.", "success")
        return redirect(url_for("web.settings_page"))
    return render_template("settings.html", s=s, mail=mailer.config_status(),
                           mail_ready=mailer.is_configured(),
                           has_demo=bool(g.db.execute("SELECT 1 FROM clients WHERE is_demo=1").fetchone()),
                           emails=g.db.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 10").fetchall())


@bp.post("/settings/test-email")
def settings_test_email():
    s = settings()
    to = s["business_email"]
    try:
        mailer.send_email(to, "Test email from your invoicing app",
                          "This is a test message confirming your email provider works.")
        flash(f"Test email accepted by your provider for {to}.", "success")
    except mailer.MailError as e:
        flash(f"Test email failed: {e}", "error")
    return redirect(url_for("web.settings_page"))


@bp.post("/settings/demo")
def settings_demo():
    if request.form.get("action") == "clear":
        clear_demo(g.db)
        flash("Demo data removed. Your records were not touched.", "success")
    else:
        seed_demo(g.db, settings())
        flash("Demo clients, services and invoices added. They are labelled Demo and can be removed here.", "success")
    return redirect(url_for("web.settings_page"))
