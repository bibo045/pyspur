"""End-to-end business rules through the real database. Email uses a fake SMTP server."""
import datetime as dt
import json
import os

import pytest

from app import db as dbmod
from app import invoices as inv


def _seed_business(conn):
    dbmod.save_settings(conn, {"business_name": "Acme Studio", "business_email": "hi@acme.example",
                               "bank_iban": "BE68 5390 0754 7034", "invoice_next_number": "1",
                               "invoice_prefix": "INV-", "invoice_pad": "4"})


def _client(conn, email="client@example.com"):
    cur = conn.execute("INSERT INTO clients(name,email,billing_address,created_at) VALUES ('Northwind Ltd', ?, 'Street 1', 'now')", (email,))
    return cur.lastrowid


def _payload(client_id, price="120.00", qty="4"):
    return {"client_id": client_id, "items": [{"description": "Workshop", "quantity": qty, "unit": "hours",
                                               "unit_price": price, "discount_pct": "0", "vat_rate": "21"}]}


def _settings(conn):
    return dbmod.get_settings(conn)


def test_draft_then_finalize_assigns_number_and_stores_pdf(conn):
    _seed_business(conn)
    cid = _client(conn)
    draft = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, draft, _payload(cid), _settings(conn))
    number = inv.finalize(conn, draft, _settings(conn))
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (draft,)).fetchone()
    assert number == "INV-0001"
    assert row["status"] == "finalized"
    assert row["total"] == "580.80"
    assert os.path.exists(row["pdf_path"])
    with open(row["pdf_path"], "rb") as fh:
        assert fh.read(5) == b"%PDF-"
    assert _settings(conn)["invoice_next_number"] == "2"


def test_numbers_are_unique_and_sequential(conn):
    _seed_business(conn)
    cid = _client(conn)
    numbers = []
    for _ in range(3):
        d = inv.create_draft(conn, _settings(conn))
        inv.save_draft(conn, d, _payload(cid), _settings(conn))
        numbers.append(inv.finalize(conn, d, _settings(conn)))
    assert numbers == ["INV-0001", "INV-0002", "INV-0003"]


def test_finalized_invoice_cannot_be_edited(conn):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    with pytest.raises(inv.BusinessError):
        inv.save_draft(conn, d, _payload(cid, price="1"), _settings(conn))


def test_finalize_refuses_without_client_email_and_consumes_no_number(conn):
    _seed_business(conn)
    cid = conn.execute("INSERT INTO clients(name,email,created_at) VALUES ('NoMail', '', 'now')").lastrowid
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    with pytest.raises(inv.BusinessError, match="no email"):
        inv.finalize(conn, d, _settings(conn))
    assert _settings(conn)["invoice_next_number"] == "1"
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "draft"


def test_snapshot_survives_client_edit(conn):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    conn.execute("UPDATE clients SET name='Renamed Corp', email='new@example.com' WHERE id=?", (cid,))
    snap = json.loads(conn.execute("SELECT client_snapshot FROM invoices WHERE id=?", (d,)).fetchone()[0])
    assert snap["name"] == "Northwind Ltd"
    assert snap["email"] == "client@example.com"


def test_send_success_marks_sent_and_logs(conn, smtp):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    result = inv.send(conn, d, _settings(conn))
    assert result["recipient"] == "client@example.com"
    assert len(smtp["messages"]) == 1
    assert b"INV-0001.pdf" in smtp["messages"][0].content
    row = conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()
    assert row[0] == "sent"
    log = conn.execute("SELECT status, error FROM email_log WHERE invoice_id=?", (d,)).fetchone()
    assert (log[0], log[1]) == ("sent", None)


def test_send_failure_keeps_invoice_finalized_and_records_error(conn, smtp):
    smtp["refuse"] = True
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    with pytest.raises(inv.BusinessError):
        inv.send(conn, d, _settings(conn))
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "finalized"
    log = conn.execute("SELECT status, error FROM email_log WHERE invoice_id=?", (d,)).fetchone()
    assert log[0] == "failed" and log[1]


def test_unconfigured_email_never_pretends_to_send(conn, monkeypatch):
    for name in ("SMTP_HOST", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    with pytest.raises(inv.BusinessError, match="not configured"):
        inv.send(conn, d, _settings(conn))
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "finalized"


def test_double_send_is_blocked_by_lock(conn, smtp):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    conn.execute("UPDATE invoices SET send_locked_at=? WHERE id=?", (dt.datetime.now(dt.timezone.utc).isoformat(), d))
    with pytest.raises(inv.BusinessError, match="already being sent"):
        inv.send(conn, d, _settings(conn))
    assert smtp["messages"] == []


def test_cannot_send_a_draft(conn, smtp):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    with pytest.raises(inv.BusinessError):
        inv.send(conn, d, _settings(conn))


def test_create_draft_is_idempotent_on_key(conn):
    _seed_business(conn)
    a = inv.create_draft(conn, _settings(conn), "same-key")
    b = inv.create_draft(conn, _settings(conn), "same-key")
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1


def test_payment_requires_full_amount_and_is_not_set_by_sending(conn, smtp):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    inv.send(conn, d, _settings(conn))
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "sent"
    with pytest.raises(inv.BusinessError, match="Partial"):
        inv.mark_paid(conn, d, "100.00", "2026-10-01")
    inv.mark_paid(conn, d, "580.80", "2026-10-02")
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "paid"


def test_duplicate_copies_lines_with_new_draft_and_fresh_dates(conn):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    new = inv.duplicate(conn, d, _settings(conn))
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (new,)).fetchone()
    assert row["status"] == "draft" and row["number"] is None
    assert conn.execute("SELECT COUNT(*) FROM invoice_items WHERE invoice_id=?", (new,)).fetchone()[0] == 1
    assert row["issue_date"] == dt.date.today().isoformat()


def test_cancel_requires_reason_and_only_unpaid(conn, smtp):
    _seed_business(conn)
    cid = _client(conn)
    d = inv.create_draft(conn, _settings(conn))
    inv.save_draft(conn, d, _payload(cid), _settings(conn))
    inv.finalize(conn, d, _settings(conn))
    with pytest.raises(inv.BusinessError):
        inv.cancel(conn, d, "")
    inv.cancel(conn, d, "Entered wrong client")
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (d,)).fetchone()[0] == "cancelled"


def test_effective_overdue_is_derived_from_due_date():
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert inv.effective_status({"status": "sent", "due_date": yesterday}) == "overdue"
    assert inv.effective_status({"status": "paid", "due_date": yesterday}) == "paid"
    assert inv.effective_status({"status": "draft", "due_date": yesterday}) == "draft"


def test_validation_collects_field_errors(conn):
    _seed_business(conn)
    clean = inv.normalize_payload({"items": [{"description": "", "quantity": "1", "unit_price": "x", "vat_rate": "21"}],
                                   "issue_date": "2026-10-10", "due_date": "2026-09-01"}, _settings(conn))
    assert "due_date" in clean["errors"]
    assert "items.1.description" in clean["errors"]
