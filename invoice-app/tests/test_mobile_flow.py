"""Tests for the phone flow: review and send screens, share links, WhatsApp and the activity log."""
import json
import re

from app import db as dbmod


def _setup(client):
    client.post("/settings", data={"business_name": "Mohamed Studio", "business_email": "hi@studio.example",
                                   "bank_iban": "BE68 5390 0754 7034", "invoice_prefix": "INV-",
                                   "invoice_next_number": "1", "invoice_pad": "4", "currency": "EUR"})
    r = client.post("/api/clients", data=json.dumps({"name": "Le Loft", "email": "leloft@example.com",
                                                     "phone": "+32 470 00 00 00"}), content_type="application/json")
    cid = r.get_json()["client"]["id"]
    r = client.post("/invoices/new")
    inv_id = int(re.search(r"/invoices/(\d+)", r.headers["Location"]).group(1))
    payload = {"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-31", "template": "modern",
               "items": [{"description": "Website", "quantity": "1", "unit_price": "2000", "discount_pct": "0", "vat_rate": "21"}]}
    client.post(f"/api/invoices/{inv_id}/draft", data=json.dumps({"payload": payload}), content_type="application/json")
    return inv_id


def _finalize(client, inv_id):
    """Finalize through the real endpoint, with the saved draft's payload, so the send step has an issued invoice."""
    conn = dbmod.connect()
    cid = conn.execute("SELECT client_id FROM invoices WHERE id=?", (inv_id,)).fetchone()[0]
    conn.close()
    payload = {"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-31", "template": "modern",
               "items": [{"description": "Website", "quantity": "1", "unit_price": "2000", "discount_pct": "0", "vat_rate": "21"}]}
    r = client.post(f"/api/invoices/{inv_id}/finalize-send",
                    data=json.dumps({"payload": payload, "send": False}), content_type="application/json")
    return r.get_json()


def test_phone_is_saved_and_listed(client):
    _setup(client)
    page = client.get("/clients")
    assert page.status_code == 200 and b"Le Loft" in page.data


def test_review_page_renders_document_in_chosen_template(client):
    inv_id = _setup(client)
    r = client.get(f"/invoices/{inv_id}/review")
    assert r.status_code == 200
    assert b"doc-modern" in r.data and b"Website" in r.data


def test_email_send_records_activity_and_confirmation(client, smtp):
    inv_id = _setup(client)
    assert _finalize(client, inv_id)["ok"]
    r = client.post(f"/invoices/{inv_id}/send/email", data={"subject": "Invoice", "body": "Please find it attached."})
    assert r.status_code == 302 and f"/invoices/{inv_id}/sent" in r.headers["Location"]
    assert len(smtp["messages"]) == 1
    page = client.get(r.headers["Location"])
    assert page.status_code == 200 and b"Invoice sent!" in page.data
    detail = client.get(f"/invoices/{inv_id}")
    assert b"Emailed to" in detail.data


def test_send_screen_offers_pdf_share_without_phone_number(client):
    inv_id = _setup(client)
    assert _finalize(client, inv_id)["ok"]
    page = client.get(f"/invoices/{inv_id}/send")
    assert page.status_code == 200
    assert b'id="share-pdf"' in page.data
    assert b"wa.me" not in page.data
    assert b"Add phone number" not in page.data


def test_shared_pdf_is_recorded_in_activity(client):
    inv_id = _setup(client)
    assert _finalize(client, inv_id)["ok"]
    r = client.post(f"/invoices/{inv_id}/send/shared", json={"channel": "share"})
    assert r.status_code == 200 and r.get_json()["ok"]
    detail = client.get(f"/invoices/{inv_id}")
    assert b"Shared PDF via phone share sheet" in detail.data


def test_share_log_refused_for_drafts(client):
    inv_id = _setup(client)
    r = client.post(f"/invoices/{inv_id}/send/shared", json={"channel": "share"})
    assert r.status_code == 409


def test_pdf_download_is_available_for_sharing(client):
    inv_id = _setup(client)
    assert _finalize(client, inv_id)["ok"]
    r = client.get(f"/invoices/{inv_id}/download")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"


def test_share_link_refused_for_drafts(client):
    inv_id = _setup(client)
    r = client.post(f"/invoices/{inv_id}/share", follow_redirects=True)
    assert b"Finalize the invoice before sharing" in r.data


def test_pages_render_on_phone_flow(client):
    for url in ["/welcome", "/more", "/templates", "/settings", "/clients/new", "/invoices?status=sent"]:
        assert client.get(url).status_code == 200, url


def test_finalize_without_sending_goes_to_send_screen(client, smtp):
    inv_id = _setup(client)
    body = _finalize(client, inv_id)
    assert body["ok"] and body["sent"] is False
    assert body["redirect"].endswith(f"/invoices/{inv_id}/send")
    assert smtp["messages"] == []  # nothing emailed when the user chose to finalize only
