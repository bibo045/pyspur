"""Realistic demo data, clearly labelled. Every demo row has is_demo=1 so it can be removed in one click."""
import datetime as dt
import json

from . import db as dbmod
from . import invoices as inv


DEMO_CLIENTS = [
    ("Northwind Studio Ltd", "accounts@northwind.example", "14 Harbour Road\n1000 Brussels", "BE0123456789", "BE0123456789"),
    ("Blue Fern Cafés BV", "finance@bluefern.example", "Kerkstraat 7\n2000 Antwerp", "", "BE0987654321"),
    ("Orion Analytics Inc.", "ap@orion.example", "880 Market Street\nSuite 400\nSan Francisco CA", "", ""),
]

DEMO_SERVICES = [
    ("Strategy workshop", "Half-day facilitated workshop with agenda and summary.", "120.00", "hours", "21"),
    ("Monthly retainer", "Ongoing advisory and reporting, billed monthly.", "950.00", "fixed fee", "21"),
    ("Website maintenance", "Updates, backups and security patches.", "65.00", "hours", "21"),
]


def clear_demo(conn):
    with dbmod.transaction(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE client_id IN "
                                          "(SELECT id FROM clients WHERE is_demo=1)").fetchall()]
        for iid in ids:
            conn.execute("DELETE FROM email_log WHERE invoice_id=?", (iid,))
            conn.execute("DELETE FROM invoices WHERE id=?", (iid,))
        conn.execute("DELETE FROM clients WHERE is_demo=1")
        conn.execute("DELETE FROM services WHERE is_demo=1")


def seed_demo(conn, settings):
    """Demo clients, services and a few invoices. Demo invoices carry a DEMO- number and never send email."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if conn.execute("SELECT 1 FROM clients WHERE is_demo=1").fetchone():
        return
    with dbmod.transaction(conn):
        client_ids = []
        for name, email, addr, reg, vat in DEMO_CLIENTS:
            cur = conn.execute("INSERT INTO clients(name,email,billing_address,registration_no,vat_number,is_demo,created_at) "
                               "VALUES (?,?,?,?,?,1,?)", (name, email, addr, reg, vat, now))
            client_ids.append(cur.lastrowid)
        for name, desc, price, unit, vat in DEMO_SERVICES:
            conn.execute("INSERT INTO services(name,description,unit_price,unit,vat_rate,is_demo,created_at) "
                         "VALUES (?,?,?,?,?,1,?)", (name, desc, price, unit, vat, now))

    today = dt.date.today()
    samples = [
        (client_ids[0], "DEMO-0001", "paid", today - dt.timedelta(days=40), [("Strategy workshop", "4", "hours", "120.00", "0", "21")]),
        (client_ids[1], "DEMO-0002", "sent", today - dt.timedelta(days=12), [("Monthly retainer", "1", "fixed fee", "950.00", "10", "21"),
                                                                              ("Website maintenance", "6", "hours", "65.00", "0", "21")]),
        (client_ids[2], "DEMO-0003", "sent", today - dt.timedelta(days=75), [("Strategy workshop", "2", "hours", "120.00", "0", "0")]),
    ]
    for cid, number, status, issue, lines in samples:
        due = issue + dt.timedelta(days=30)
        items = []
        for desc, qty, unit, price, disc, vat in lines:
            items.append(inv.compute_line({"quantity": qty, "unit_price": price, "discount_pct": disc, "vat_rate": vat})
                         | {"description": desc, "unit": unit})
        totals = inv.compute_totals(items)
        client = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        snap = {k: client[k] for k in ("name", "email", "billing_address", "registration_no", "vat_number")}
        seller = {k: settings[k] for k in ("business_name", "business_email", "business_phone",
                                           "business_address", "registration_no", "vat_number")}
        with dbmod.transaction(conn):
            cur = conn.execute(
                """INSERT INTO invoices (number, status, client_id, client_snapshot, seller_snapshot, issue_date, due_date,
                   currency, template, template_options, payment_terms, payment_instructions, subtotal, discount_total,
                   vat_total, total, paid_amount, paid_date, finalized_at, created_at, updated_at, idempotency_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (number, status, cid, json.dumps(snap), json.dumps(seller), issue.isoformat(), due.isoformat(),
                 settings["currency"], settings["default_template"], json.dumps(settings["template_options"]),
                 "Payment due within 30 days", settings["payment_instructions"], totals["subtotal"],
                 totals["discount_total"], totals["vat_total"], totals["total"],
                 totals["total"] if status == "paid" else None,
                 (issue + dt.timedelta(days=20)).isoformat() if status == "paid" else None,
                 now, now, now, f"demo-{number}"),
            )
            iid = cur.lastrowid
            for pos, it in enumerate(items, start=1):
                conn.execute(
                    """INSERT INTO invoice_items (invoice_id, position, description, quantity, unit, unit_price,
                       discount_pct, vat_rate, line_net, line_discount, line_vat, line_total)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (iid, pos, it["description"], it["quantity"], it["unit"], it["unit_price"], it["discount_pct"],
                     it["vat_rate"], it["line_net"], it["line_discount"], it["line_vat"], it["line_total"]))
