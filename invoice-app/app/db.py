"""SQLite persistence. Money is stored as TEXT so no float ever touches a total."""
import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    billing_address TEXT NOT NULL DEFAULT '',
    registration_no TEXT NOT NULL DEFAULT '',
    vat_number    TEXT NOT NULL DEFAULT '',
    is_demo       INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    unit_price  TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'fixed fee',
    vat_rate    TEXT NOT NULL,
    is_demo     INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT UNIQUE,                 -- assigned at finalization
    status          TEXT NOT NULL,               -- draft | finalized | sent | paid | cancelled
    client_id       INTEGER REFERENCES clients(id),
    client_snapshot TEXT NOT NULL DEFAULT '{}',  -- frozen at finalization
    seller_snapshot TEXT NOT NULL DEFAULT '{}',  -- frozen at finalization
    issue_date      TEXT NOT NULL,
    due_date        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    template        TEXT NOT NULL,
    template_options TEXT NOT NULL DEFAULT '{}',
    payment_terms   TEXT NOT NULL DEFAULT '',
    payment_instructions TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    subtotal        TEXT NOT NULL DEFAULT '0.00',   -- net after line discounts
    discount_total  TEXT NOT NULL DEFAULT '0.00',
    vat_total       TEXT NOT NULL DEFAULT '0.00',
    total           TEXT NOT NULL DEFAULT '0.00',
    paid_amount     TEXT,
    paid_date       TEXT,
    pdf_path        TEXT,                        -- stored PDF of the finalized invoice
    idempotency_key TEXT UNIQUE,
    cancel_reason   TEXT,
    send_locked_at  TEXT,                        -- short-lived lock against double sends
    created_at      TEXT NOT NULL,
    finalized_at    TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);
CREATE TABLE IF NOT EXISTS invoice_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    service_id  INTEGER REFERENCES services(id),
    description TEXT NOT NULL,
    quantity    TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '',
    unit_price  TEXT NOT NULL,
    discount_pct TEXT NOT NULL DEFAULT '0',
    vat_rate    TEXT NOT NULL,
    line_net    TEXT NOT NULL,
    line_discount TEXT NOT NULL,
    line_vat    TEXT NOT NULL,
    line_total  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
    recipient   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    status      TEXT NOT NULL,                  -- sent | failed
    error       TEXT,
    attempted_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "business_name": "",
    "business_email": "",
    "business_phone": "",
    "business_address": "",
    "registration_no": "",
    "vat_number": "",
    "logo_path": "",
    "currency": "EUR",
    "default_vat_rate": "21",
    "invoice_prefix": "INV-",
    "invoice_next_number": "1",
    "invoice_pad": "4",
    "payment_terms_days": "30",
    "bank_name": "",
    "bank_iban": "",
    "bank_bic": "",
    "payment_instructions": "Please pay by bank transfer quoting the invoice number.",
    "legal_footer": "",
    "email_subject": "Invoice {number} from {business_name}",
    "email_body": (
        "Dear {client_name},\n\n"
        "Please find attached invoice {number}.\n\n"
        "Total: {total}\nDue date: {due_date}\n\n"
        "Thank you for your business."
    ),
    "default_template": "minimal",
    "template_options": json.dumps({
        "accent": "#1F4E79", "font": "Helvetica", "footer": "", "show_logo": True,
    }),
}


def get_db_path():
    return os.environ.get("INVOICE_DB", os.path.join(os.path.dirname(__file__), "..", "data", "invoices.sqlite3"))


def connect():
    path = get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn, immediate=True):
    """BEGIN IMMEDIATE takes the write lock up front, so number allocation cannot race."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(conn=None):
    conn = conn or connect()
    conn.executescript(SCHEMA)
    with transaction(conn):
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    return conn


def get_settings(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    data["template_options"] = json.loads(data["template_options"] or "{}")
    return data


def save_settings(conn, updates):
    with transaction(conn):
        for key, value in updates.items():
            if key == "template_options":
                value = json.dumps(value)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
