import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICE_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    import app.invoices as invmod
    monkeypatch.setattr(invmod, "PDF_DIR", str(tmp_path / "pdfs"))
    from app import create_app
    flask_app = create_app({"TESTING": True})
    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    from app import db as dbmod
    c = dbmod.connect()
    yield c
    c.close()


@pytest.fixture()
def smtp(monkeypatch):
    pytest.importorskip("aiosmtpd.controller")
    from aiosmtpd.controller import Controller
    box = {"messages": [], "refuse": False}

    class H:
        async def handle_DATA(self, server, session, envelope):
            if box["refuse"]:
                return "554 rejected"
            box["messages"].append(envelope)
            return "250 OK"

    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ctl = Controller(H(), hostname="127.0.0.1", port=port)
    ctl.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(port))
    monkeypatch.setenv("SMTP_SECURITY", "none")
    monkeypatch.setenv("SMTP_FROM", "Billing <billing@example.com>")
    yield box
    ctl.stop()
