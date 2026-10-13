"""PDF rendering with ReportLab (pure Python, no system dependencies).

Every template uses the same flowing platypus layout, so long descriptions wrap, tables
split cleanly across pages, and nothing is clipped or left as a blank page.
"""
import datetime as dt
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from .money import cents, fmt, to_decimal

# Each template has a gallery category (Modern, Minimal, Classic, Bold). The keys are stable: they are
# stored in settings and on invoices, so renaming a label is safe but changing a key is not.
TEMPLATES = {
    "modern": {"label": "Modern", "category": "Modern", "desc": "Sans-serif, navy accent, tinted totals panel.",
               "header": "left", "table": "hairline", "accent_default": "#1C2B4A", "font": "Helvetica"},
    "minimal": {"label": "Minimal", "category": "Minimal", "desc": "Airy layout, one accent, hairline rules.",
                "header": "left", "table": "lines", "accent_default": "#1F4E79", "font": "Helvetica"},
    "corporate": {"label": "Classic", "category": "Classic", "desc": "Solid header band and boxed totals.",
                  "header": "band", "table": "grid", "accent_default": "#1B3A5C", "font": "Helvetica"},
    "elegant": {"label": "Elegant", "category": "Classic", "desc": "Serif type, spaced capitals, fine rules.",
                "header": "centered", "table": "hairline", "accent_default": "#8A6D3B", "font": "Times-Roman"},
    "creative": {"label": "Bold", "category": "Bold", "desc": "Strong colour block and tinted rows.",
                 "header": "block", "table": "zebra", "accent_default": "#E4572E", "font": "Helvetica"},
    "bw": {"label": "Black & white", "category": "Minimal", "desc": "Monochrome, printer-friendly.",
           "header": "left", "table": "lines", "accent_default": "#000000", "font": "Helvetica"},
}
CATEGORIES = ["Modern", "Minimal", "Classic", "Bold"]

FONT_FAMILIES = {
    "Helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique"),
    "Times-Roman": ("Times-Roman", "Times-Bold", "Times-Italic"),
    "Courier": ("Courier", "Courier-Bold", "Courier-Oblique"),
}


def _hex(value, fallback="#1F4E79"):
    try:
        return colors.HexColor(value if str(value).startswith("#") and len(str(value)) == 7 else fallback)
    except ValueError:
        return colors.HexColor(fallback)


def _date(iso):
    try:
        return dt.date.fromisoformat(iso).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return iso or ""


def _p(text, style):
    return Paragraph(escape(text or "").replace("\n", "<br/>"), style)


def render(data, template, options=None):
    """Return the PDF bytes for one invoice. `data` comes from invoices.load_render_data()."""
    spec = TEMPLATES.get(template, TEMPLATES["minimal"])
    options = options or {}
    bw = template == "bw"
    accent = colors.black if bw else _hex(options.get("accent"), spec["accent_default"])
    family = options.get("font") if options.get("font") in FONT_FAMILIES else spec["font"]
    reg, bold, ital = FONT_FAMILIES[family]
    if template == "elegant":
        # Elegant is always serif to keep its character, even if the user picks another font.
        reg, bold, ital = FONT_FAMILIES["Times-Roman"]
    currency = data["currency"]
    light = colors.HexColor("#F3F4F6") if not bw else colors.HexColor("#EEEEEE")
    rule = colors.HexColor("#D1D5DB") if not bw else colors.black
    muted = colors.HexColor("#6B7280") if not bw else colors.HexColor("#333333")
    ink = colors.HexColor("#111827") if not bw else colors.black

    base = ParagraphStyle("base", fontName=reg, fontSize=9.5, leading=13, textColor=ink)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, leading=11.5, textColor=muted)
    bold_s = ParagraphStyle("bold", parent=base, fontName=bold)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    right_b = ParagraphStyle("rightb", parent=right, fontName=bold)
    title = ParagraphStyle("title", parent=base, fontName=bold, fontSize=24, leading=28,
                           textColor=accent, alignment=TA_RIGHT if spec["header"] != "centered" else 1)
    heading = ParagraphStyle("heading", parent=base, fontName=bold, fontSize=8, leading=10,
                             textColor=accent if not bw else colors.black)
    biz = ParagraphStyle("biz", parent=base, fontName=bold, fontSize=14, leading=18, textColor=ink)
    cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=12)
    cell_r = ParagraphStyle("cellr", parent=cell, alignment=TA_RIGHT)
    head_cell = ParagraphStyle("hcell", parent=cell, fontName=bold, textColor=(
        colors.white if spec["table"] == "grid" or spec["header"] == "block" else ink))

    buf = io.BytesIO()
    footer_text = (options.get("footer") or "").strip() or data.get("legal_footer", "")
    footer_text = (footer_text or "").strip()
    business = data["seller"].get("business_name", "")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont(reg, 8)
        canvas.setFillColor(muted)
        y_foot = 12 * mm
        canvas.setStrokeColor(rule)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, y_foot + 6 * mm, A4[0] - 18 * mm, y_foot + 6 * mm)
        if footer_text:
            canvas.drawString(18 * mm, y_foot, footer_text[:160])
        canvas.drawRightString(A4[0] - 18 * mm, y_foot, f"{business}  ·  Page {doc.page}" if business else f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=24 * mm,
                            title=f"Invoice {data['number']}", author=business or "Invoice")
    story = []

    # ---- header -------------------------------------------------------------
    logo = None
    show_logo = options.get("show_logo", True)
    logo_path = data.get("logo_path")
    if show_logo and logo_path and os.path.exists(logo_path):
        try:
            logo = Image(logo_path, width=40 * mm, height=18 * mm, kind="proportional", hAlign="LEFT")
        except Exception:  # unreadable logo must not break the invoice
            logo = None

    seller_lines = [data["seller"].get(k) for k in ("business_address", "business_email",
                                                     "business_phone", "registration_no", "vat_number")]
    seller_block = [Paragraph(escape(business or "Your business"), biz)]
    for line in seller_lines:
        if line:
            seller_block.append(_p(line, small))
    meta = [
        [_p("Invoice number", small), _p(data["number"], right_b)],
        [_p("Issue date", small), _p(_date(data["issue_date"]), right)],
        [_p("Due date", small), _p(_date(data["due_date"]), right)],
    ]
    meta_table = Table(meta, colWidths=[34 * mm, 42 * mm])
    meta_table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    if spec["header"] == "centered":
        if logo:
            story.append(Table([[logo]], colWidths=[174 * mm], style=[("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(Paragraph("INVOICE", title))
        story.append(Spacer(1, 4))
        story.append(Paragraph(escape(business), ParagraphStyle("c", parent=biz, alignment=1)))
        for line in seller_lines:
            if line:
                story.append(Paragraph(escape(line), ParagraphStyle("cs", parent=small, alignment=1)))
        story.append(Spacer(1, 4))
        story.append(Table([[""]], colWidths=[174 * mm], rowHeights=[0.6],
                           style=[("LINEABOVE", (0, 0), (-1, -1), 0.6, accent)]))
        story.append(Spacer(1, 10))
        story.append(Table([[_p(f"Invoice {data['number']}", ParagraphStyle("ec", parent=right_b, alignment=1)),
                             _p(f"Issued {_date(data['issue_date'])}", ParagraphStyle("ec2", parent=small, alignment=1)),
                             _p(f"Due {_date(data['due_date'])}", ParagraphStyle("ec3", parent=small, alignment=1))]],
                           colWidths=[58 * mm] * 3))
    else:
        left_cells = [[logo]] if logo else []
        left_cells += [[p] for p in seller_block]
        left = Table(left_cells, colWidths=[100 * mm], style=[("LEFTPADDING", (0, 0), (-1, -1), 0)])
        right_cells = [[Paragraph("INVOICE", title)], [meta_table]]
        right_col = Table(right_cells, colWidths=[76 * mm], style=[("LEFTPADDING", (0, 0), (-1, -1), 0),
                                                                    ("ALIGN", (0, 0), (-1, -1), "RIGHT")])
        header_style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]
        if spec["header"] == "band":
            band = Table([[Paragraph("INVOICE", ParagraphStyle("bt", parent=title, textColor=colors.white,
                                                              alignment=TA_LEFT)),
                           _p(data["number"], ParagraphStyle("bn", parent=right_b, textColor=colors.white,
                                                             alignment=TA_RIGHT))]],
                         colWidths=[87 * mm, 87 * mm], style=[
                             ("BACKGROUND", (0, 0), (-1, -1), accent), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
            story.append(band)
            story.append(Spacer(1, 8))
            story.append(Table([[left, meta_table]], colWidths=[100 * mm, 74 * mm], style=header_style))
        elif spec["header"] == "block":
            block = Table([[Paragraph("INVOICE", ParagraphStyle("bk", parent=title, textColor=colors.white,
                                                               alignment=TA_LEFT)), ""]],
                          colWidths=[100 * mm, 74 * mm], style=[("BACKGROUND", (0, 0), (0, 0), accent),
                                                               ("BACKGROUND", (1, 0), (1, 0), accent),
                                                               ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                                               ("TOPPADDING", (0, 0), (-1, -1), 12),
                                                               ("BOTTOMPADDING", (0, 0), (-1, -1), 12)])
            story.append(block)
            story.append(Spacer(1, 8))
            story.append(Table([[left, meta_table]], colWidths=[100 * mm, 74 * mm], style=header_style))
        else:
            story.append(Table([[left, right_col]], colWidths=[100 * mm, 74 * mm], style=header_style))

    story.append(Spacer(1, 14))

    # ---- bill to ------------------------------------------------------------
    client = data["client"]
    bill_lines = [client.get("name", "")] + [client.get("billing_address", "")]
    if client.get("registration_no"):
        bill_lines.append(f"Reg. no: {client['registration_no']}")
    if client.get("vat_number"):
        bill_lines.append(f"VAT no: {client['vat_number']}")
    bill_lines.append(client.get("email", ""))
    bill = [[Paragraph("BILL TO", heading)]] + [[_p(l, bold_s if i == 0 else base)] for i, l in enumerate(bill_lines) if l]
    story.append(Table(bill, colWidths=[174 * mm], style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(Spacer(1, 16))

    # ---- items --------------------------------------------------------------
    rows = [[Paragraph("Description", head_cell), Paragraph("Qty", head_cell),
             Paragraph("Unit price", ParagraphStyle("h2", parent=head_cell, alignment=TA_RIGHT)),
             Paragraph("Disc.", ParagraphStyle("h3", parent=head_cell, alignment=TA_RIGHT)),
             Paragraph("VAT", ParagraphStyle("h4", parent=head_cell, alignment=TA_RIGHT)),
             Paragraph("Net amount", ParagraphStyle("h5", parent=head_cell, alignment=TA_RIGHT))]]
    for it in data["items"]:
        desc = escape(it["description"])
        if it.get("unit"):
            desc += f' <font color="{"#333333" if bw else "#6B7280"}" size="8">({escape(it["unit"])})</font>'
        rows.append([
            Paragraph(desc, cell),
            Paragraph(escape(str(it["quantity"])), cell_r),
            Paragraph(f"{cents(it['unit_price']):,.2f}", cell_r),
            Paragraph(f"{to_decimal(it['discount_pct']):g}%" if to_decimal(it["discount_pct"]) else "–", cell_r),
            Paragraph(f"{to_decimal(it['vat_rate']):g}%", cell_r),
            Paragraph(f"{cents(it['line_net']):,.2f}", cell_r),
        ])
    items = Table(rows, colWidths=[72 * mm, 14 * mm, 24 * mm, 14 * mm, 14 * mm, 36 * mm], repeatRows=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, accent),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, rule),
    ]
    if spec["table"] == "grid":
        style = [("BACKGROUND", (0, 0), (-1, 0), accent)] + style
        style += [("GRID", (0, 0), (-1, -1), 0.4, rule)]
    if spec["table"] == "zebra":
        style = [("BACKGROUND", (0, 0), (-1, 0), accent)] + style
        for r in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), light))
    if spec["table"] == "hairline":
        style.append(("LINEBELOW", (0, 0), (-1, 0), 0.4, accent))
    items.setStyle(TableStyle(style))
    story.append(items)
    story.append(Spacer(1, 12))

    # ---- totals -------------------------------------------------------------
    t = data["totals"]
    gross = cents(to_decimal(t["subtotal"]) + to_decimal(t["discount_total"]))
    tot_rows = [[_p("Subtotal before discounts", base), _p(fmt(gross, currency).replace(f" {currency}", ""), right)]]
    if to_decimal(t["discount_total"]):
        tot_rows.append([_p("Discounts", base), _p("−" + fmt(t["discount_total"], "").strip(), right)])
        tot_rows.append([_p("Net excl. VAT", base), _p(fmt(t["subtotal"], "").strip(), right)])
    else:
        tot_rows[0][0] = _p("Subtotal excl. VAT", base)
    for vb in data["vat_breakdown"]:
        tot_rows.append([_p(f"VAT {to_decimal(vb['rate']):g}% on {fmt(vb['net'], '').strip()}", small),
                         _p(fmt(vb["vat"], "").strip(), right)])
    tot_rows.append([Paragraph("Total payable", ParagraphStyle("tl", parent=bold_s, textColor=accent if not bw else colors.black)),
                     Paragraph(fmt(t["total"], currency), right_b)])
    totals = Table(tot_rows, colWidths=[90 * mm, 50 * mm])
    tstyle = [("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
              ("LINEABOVE", (0, -1), (-1, -1), 0.8, accent)]
    if spec["table"] in ("grid", "zebra"):
        tstyle.append(("BOX", (0, -1), (-1, -1), 0.8, accent))
    totals.setStyle(TableStyle(tstyle))
    wrapper = Table([[Spacer(1, 1), totals]], colWidths=[20 * mm, 140 * mm],
                    style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)])
    story.append(KeepTogether([wrapper]))
    story.append(Spacer(1, 18))

    # ---- payment ------------------------------------------------------------
    pay = []
    pay.append(Paragraph("PAYMENT", heading))
    pay.append(Spacer(1, 3))
    if data.get("payment_terms"):
        pay.append(_p(data["payment_terms"], base))
    bank = data.get("bank", {})
    bank_lines = [("Bank", bank.get("bank_name")), ("IBAN", bank.get("bank_iban")), ("BIC", bank.get("bank_bic"))]
    for label, value in bank_lines:
        if value:
            pay.append(_p(f"{label}: {value}", base))
    if data.get("payment_instructions"):
        pay.append(Spacer(1, 3))
        pay.append(_p(data["payment_instructions"], small))
    if data.get("notes"):
        pay.append(Spacer(1, 8))
        pay.append(Paragraph("NOTES", heading))
        pay.append(Spacer(1, 3))
        pay.append(_p(data["notes"], small))
    story.append(KeepTogether(pay))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
