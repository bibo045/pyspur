"""HTTP walk-through of the Definition of Done, using the real routes and database."""
import json
import re



def _post_json(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


def test_full_workflow_create_finalize_send_download(client, smtp):
    client.post("/settings", data={"business_name": "Acme Studio", "business_email": "hi@acme.example",
                                   "bank_iban": "BE68 5390 0754 7034", "invoice_prefix": "INV-",
                                   "invoice_next_number": "1", "invoice_pad": "4", "currency": "EUR"})
    # 1. client with email (quick-add API used by the editor modal)
    r = _post_json(client, "/api/clients", {"name": "Orion Analytics", "email": "ap@orion.example",
                                             "billing_address": "1 Main St"})
    assert r.status_code == 200
    cid = r.get_json()["client"]["id"]

    # 2. reusable service with price and VAT
    r = client.post("/services/new", data={"name": "Audit", "unit_price": "250", "vat_rate": "21",
                                           "unit": "hours", "description": "Review"})
    assert r.status_code == 302

    # 3. create invoice -> redirected into the editor
    r = client.post("/invoices/new", data={"key": "k-1"})
    assert r.status_code == 302
    edit_url = r.headers["Location"]
    inv_id = int(re.search(r"/invoices/(\d+)/edit", edit_url).group(1))

    # 4. autosave a draft: two lines, totals computed by the server
    payload = {"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-31", "template": "corporate",
               "items": [{"description": "Audit", "quantity": "2", "unit": "hours", "unit_price": "250",
                          "discount_pct": "10", "vat_rate": "21"},
                         {"description": "Travel", "quantity": "1", "unit": "", "unit_price": "40.005",
                          "discount_pct": "0", "vat_rate": "0"}]}
    r = _post_json(client, f"/api/invoices/{inv_id}/draft", {"payload": payload})
    assert r.status_code == 200, r.get_json()
    t = r.get_json()["totals"]
    # Audit: 500.00 gross, 50.00 discount, 450.00 net, 94.50 VAT. Travel: 40.01 (rounded half-up), 0% VAT.
    assert t["subtotal"] == "490.01"
    assert t["vat_total"] == "94.50"
    assert t["total"] == "584.51"

    # 5. the live preview is a real PDF
    r = client.get(f"/invoices/{inv_id}/preview.pdf")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"

    # 6. validation: an empty line is rejected with a field error, nothing saved
    bad = dict(payload, items=[{"description": "", "quantity": "1", "unit_price": "5", "vat_rate": "21"}])
    r = _post_json(client, f"/api/invoices/{inv_id}/draft", {"payload": bad})
    assert r.status_code == 422 and r.get_json()["errors"]

    # 7. Finalize & send
    r = _post_json(client, f"/api/invoices/{inv_id}/finalize-send", {"payload": payload,
                                                                   "subject": "Invoice", "body": "Hi {client_name}"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"], body
    assert body["number"] == "INV-0001"
    assert len(smtp["messages"]) == 1
    assert smtp["messages"][0].rcpt_tos == ["ap@orion.example"]

    # 8. double-click / second request must not send again
    r = _post_json(client, f"/api/invoices/{inv_id}/finalize-send", {"payload": payload})
    assert r.status_code == 409
    assert len(smtp["messages"]) == 1

    # 9. dashboard and list show it, with the right status
    assert b"INV-0001" in client.get("/").data
    assert b"Sent" in client.get("/invoices").data

    # 10. download the stored PDF of the finalized invoice
    r = client.get(f"/invoices/{inv_id}/download")
    assert r.status_code == 200 and r.data[:5] == b"%PDF-"

    # 11. editing a finalized invoice is refused
    r = client.get(f"/invoices/{inv_id}/edit")
    assert r.status_code == 302

    # 12. payment: sending alone never marks paid
    r = client.post(f"/invoices/{inv_id}/payment", data={"amount": "584.51", "paid_date": "2026-10-05"})
    assert r.status_code == 302


def test_finalize_blocked_when_email_not_configured(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    client.post("/settings", data={"business_name": "Acme", "bank_iban": "X", "invoice_prefix": "INV-",
                                   "invoice_next_number": "1", "invoice_pad": "4", "currency": "EUR"})
    cid = _post_json(client, "/api/clients", {"name": "A", "email": "a@example.com"}).get_json()["client"]["id"]
    inv_id = int(re.search(r"/invoices/(\d+)", client.post("/invoices/new").headers["Location"]).group(1))
    payload = {"client_id": cid, "issue_date": "2026-10-01", "due_date": "2026-10-31", "template": "minimal",
               "items": [{"description": "X", "quantity": "1", "unit_price": "10", "discount_pct": "0", "vat_rate": "21"}]}
    r = _post_json(client, f"/api/invoices/{inv_id}/finalize-send", {"payload": payload})
    assert r.status_code == 409
    assert "not configured" in r.get_json()["error"]
    # Finalized (number assigned) but unsent: the invoice page offers Resend and shows the failure.
    page = client.get(f"/invoices/{inv_id}")
    assert page.status_code == 200
    assert b"Email is not connected" in page.data


def test_all_templates_render_pdf(client):
    for key in ("minimal", "corporate", "elegant", "creative", "bw"):
        r = client.get(f"/templates/sample.pdf?template={key}&accent=%231F4E79&font=Helvetica")
        assert r.status_code == 200, key
        assert r.data[:5] == b"%PDF-", key
        assert len(r.data) > 1000, key


def test_pages_load(client):
    for url in ["/", "/invoices", "/invoices?status=overdue&sort=total&dir=asc", "/clients", "/clients?archived=1",
                "/services", "/templates", "/settings", "/clients/new", "/services/new"]:
        assert client.get(url).status_code == 200, url


def test_demo_data_is_labelled_and_removable(client):
    client.post("/settings/demo", data={"action": "add"})
    assert b"DEMO-0001" in client.get("/invoices").data
    client.post("/settings/demo", data={"action": "clear"})
    assert b"DEMO-0001" not in client.get("/invoices").data
