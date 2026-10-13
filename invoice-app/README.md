# Invoicing app

Create a client invoice, pick services, choose a template, finalize it, and email the PDF to the client.
Built with Flask, SQLite, Python `Decimal` for all money, ReportLab for PDFs, and SMTP for email.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                       # http://127.0.0.1:5000
```

Data is stored in `data/invoices.sqlite3`, PDFs in `data/pdfs/`, and logos in `data/uploads/`.
Set `INVOICE_DB` to use a different database file.

## Connect email (required before finalizing)

Credentials live only in server environment variables. They are never sent to the browser.

```bash
export SMTP_HOST=smtp.yourprovider.com
export SMTP_PORT=587
export SMTP_SECURITY=starttls        # starttls | ssl | none
export SMTP_USERNAME=...
export SMTP_PASSWORD=...
export SMTP_FROM="Your Business <billing@yourdomain.com>"
```

Any SMTP provider works: Amazon SES, Postmark, SendGrid, Mailgun, Resend, or Gmail with an app password.
Then open **Business settings → Send a test email** to confirm the provider accepts messages.

If these variables are missing, Finalize is disabled and the app says so. It never reports an email as sent unless the provider accepted it.

## The workflow

1. **Client**: search saved clients, or add one in the quick-add dialog without leaving the editor.
2. **Services**: add catalog services, or custom one-time lines. Quantity, unit price, discount and VAT are editable per line.
3. **Details**: issue date, due date, payment terms, notes.
4. **Template**: five styles (minimal, corporate, elegant, creative, black & white). The preview is the real PDF and updates after each save.
5. **Review & send**: totals, editable email subject and message, then **Finalize & send**.

Finalize runs in order: validate → assign the next invoice number → freeze client and seller snapshots → write the PDF → email it → record the result. Each step's outcome is shown.

## Rules the code enforces

- **Money**: every amount is a `Decimal`, rounded to cents half-up, per line. Totals are sums of line values, so they always reconcile.
- **Numbering**: numbers are assigned at finalization, inside a write-locked transaction. Drafts never leave gaps. The sequence never moves backwards once invoices exist.
- **Immutability**: finalized invoices cannot be edited. Corrections go through cancellation (with a required reason, unpaid invoices only). Duplicate the invoice to reissue it.
- **Snapshots**: client and seller details are frozen at finalization, so later edits never change history. Each invoice's PDF is stored and served back exactly as issued.
- **No double sends**: the Finalize button disables itself, a single request can finalize only a draft, and a short send lock blocks concurrent sends. Resend is a separate, explicit action.
- **Payment status is never inferred from email**: only "Mark as paid" (full amount and a date) sets Paid.
- **Overdue** is derived from the due date and unpaid status, so it updates on its own.

## Demo data

Business settings → **Add demo data** creates sample clients, services and invoices marked `DEMO`. They never send email.
**Remove demo data** deletes only rows marked as demo. Your records are not touched.

## Tests

```bash
python -m pytest -q
```

The suite covers money rounding, numbering, finalize/send, the send lock, email failure handling, and the HTTP workflow.
The email tests use a real SMTP server on localhost, so they check the actual message and attachment.

## Limits and decisions to know about

- **No login.** This is a single-user app. Put it behind your own authentication (SSO, a reverse proxy with basic auth, or a login layer) before exposing it to the internet.
- **Partial payments** are not supported yet. Payments must equal the invoice total.
- **Corrections** are cancel-and-reissue. Credit notes are not implemented.
- **Delivery status** means the provider accepted the message. Bounces and opens are not tracked; a provider webhook would be needed for that.
- **PDF text** uses ReportLab's built-in fonts (Latin / Western European characters). Non-Latin scripts need a registered TTF font, which is a small change in `app/pdf.py`.
- **Preview** uses your browser's PDF viewer. Browsers without one (some headless or embedded setups) will show an empty preview; the downloaded PDF is unaffected.
- **Print** opens the PDF in a new tab so the browser's print dialog can be used.
- **Legal wording** (VAT reverse charge, company registration requirements, e-invoicing rules) depends on your country. Set it in Business settings → Legal footer, and check it with an accountant before relying on it.

## Layout

```
run.py                  start the server
app/
  __init__.py           app factory
  web.py                routes and JSON endpoints
  invoices.py           invoice rules: validation, totals, numbering, send, payment
  pdf.py                ReportLab templates
  mailer.py             SMTP delivery
  money.py              Decimal helpers
  db.py                 SQLite schema and settings
  seed.py               labelled demo data
  templates/            HTML pages
  static/               CSS (design tokens) and editor.js
tests/                  pytest suite
```
