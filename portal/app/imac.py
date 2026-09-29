"""Fill IMAC: a portal-native record of Install / Add / Change work done against an asset, replacing the paper
ONGC-AMT-4.03 form. Started from an asset's detail page (Fill IMAC button); the asset's own identity fields
(type, make, model, serial no.) are pulled in automatically and never re-typed. A "Change" record may name a
different Asset (CI) - the one actually left in place - so the history is genuinely old CI -> new CI, not just
a note about it: `imac_record.asset_key` is always the CI the work left behind, `prev_asset_key` the one it
replaced (NULL for a record that did not change identity). The generated PDF is kept (not just the field
values) so a copy can always be reprinted exactly as it was on the day.

This module does not itself alter the asset register - an IMAC record is documentary. Retiring the old CI and
activating the new one, if the change warrants it, remains a separate edit/archive action by an administrator.
"""
import datetime as dt
import io
import json

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import auth, db, edit
from .export import LOGO

DDL = ["""CREATE TABLE IF NOT EXISTS imac_record (
    imac_id BIGSERIAL PRIMARY KEY,
    asset_key TEXT NOT NULL,                 -- the CI this record leaves in place (the new one, for a CHANGE that swapped assets)
    prev_asset_key TEXT,                     -- the CI this replaced - set only when a CHANGE record actually swapped the asset
    change_type TEXT NOT NULL CHECK (change_type IN ('INSTALLATION', 'ADDITION', 'CHANGE')),
    ticket_no TEXT, imac_date DATE NOT NULL DEFAULT current_date, ongc_section TEXT, location TEXT, room_no TEXT,
    requester_cpf TEXT, requester_name TEXT, requester_mobile TEXT,
    asset_type TEXT, make TEXT, model TEXT, serial_no TEXT,          -- snapshot of the asset at the time - not re-read later
    feasible BOOLEAN NOT NULL DEFAULT TRUE, infeasible_reason TEXT,
    part_name TEXT, part_qty TEXT, part_serial_no TEXT, demo_given BOOLEAN NOT NULL DEFAULT FALSE,
    remarks TEXT, pdf BYTEA NOT NULL,
    created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
       "CREATE INDEX IF NOT EXISTS ix_imac_asset ON imac_record (asset_key, created_at DESC)",
       "CREATE INDEX IF NOT EXISTS ix_imac_prev_asset ON imac_record (prev_asset_key) WHERE prev_asset_key IS NOT NULL"]

CHANGE_TYPES = ("INSTALLATION", "ADDITION", "CHANGE")


def _asset(key):
    return db.one("SELECT asset_key, asset_type, make, model, serial_no, engineer_name, user_name, cpf_no FROM asset "
                  "WHERE asset_key = %s AND is_current = 1 AND record_level = 'ASSET'", [key])


def for_asset(key):
    """Every IMAC record naming this asset either as the CI it left in place, or the one it replaced - so the record
    shows on both the old and the new asset's detail page. -> rows for the generic related-records table in detail.js."""
    return db.query("""SELECT imac_id AS id, imac_date, change_type, ticket_no, asset_key, prev_asset_key, requester_name, created_by
                       FROM imac_record WHERE asset_key = %s OR prev_asset_key = %s ORDER BY imac_date DESC, imac_id DESC LIMIT 25""", [key, key])


def create(user, asset_key, body, ip):
    """`asset_key`: the asset whose detail page the Fill IMAC button was pressed on - governs the permission check
    regardless of which asset the finished record ends up naming."""
    origin = _asset(asset_key)
    if not origin:
        raise edit.NotFound(f"{asset_key} is not a current asset.")
    auth.check_imac(user, origin)

    change_type = str(body.get("change_type") or "").upper()
    if change_type not in CHANGE_TYPES:
        raise edit.Invalid("Choose Installation, Addition or Change.", {"change_type": "required"})

    new_key = str(body.get("new_asset_key") or "").strip().upper()
    target, prev_key = origin, None
    if change_type == "CHANGE" and new_key and new_key != asset_key:
        target = _asset(new_key)
        if not target:
            raise edit.Invalid(f"{new_key} is not a current asset.", {"new_asset_key": "not found"})
        prev_key = asset_key

    feasible = bool(body.get("feasible", True))
    reason = str(body.get("infeasible_reason") or "").strip()[:500] or None
    if not feasible and not reason:
        raise edit.Invalid("Give a reason when the work is not feasible.", {"infeasible_reason": "required"})

    try:
        imac_date = dt.date.fromisoformat(str(body.get("imac_date") or dt.date.today())[:10])
    except ValueError:
        raise edit.Invalid("That date is not valid.", {"imac_date": "invalid"})

    rec = {
        "asset_key": target["asset_key"], "prev_asset_key": prev_key, "change_type": change_type,
        "ticket_no": str(body.get("ticket_no") or "").strip()[:60] or None, "imac_date": imac_date,
        "ongc_section": str(body.get("ongc_section") or "").strip()[:120] or None,
        "location": str(body.get("location") or "").strip()[:160] or None, "room_no": str(body.get("room_no") or "").strip()[:40] or None,
        "requester_cpf": str(body.get("requester_cpf") or "").strip()[:20] or None,
        "requester_name": str(body.get("requester_name") or "").strip()[:120] or None,
        "requester_mobile": str(body.get("requester_mobile") or "").strip()[:20] or None,
        "asset_type": target["asset_type"], "make": target["make"], "model": target["model"], "serial_no": target["serial_no"],
        "feasible": feasible, "infeasible_reason": reason,
        "part_name": str(body.get("part_name") or "").strip()[:160] or None, "part_qty": str(body.get("part_qty") or "").strip()[:20] or None,
        "part_serial_no": str(body.get("part_serial_no") or "").strip()[:80] or None, "demo_given": bool(body.get("demo_given")),
        "remarks": str(body.get("remarks") or "").strip()[:1000] or None,
    }
    pdf = _pdf(rec, user)
    with db.write() as con:
        row = con.execute("""INSERT INTO imac_record (asset_key, prev_asset_key, change_type, ticket_no, imac_date, ongc_section, location, room_no,
                             requester_cpf, requester_name, requester_mobile, asset_type, make, model, serial_no, feasible, infeasible_reason,
                             part_name, part_qty, part_serial_no, demo_given, remarks, pdf, created_by)
                             VALUES (%(asset_key)s,%(prev_asset_key)s,%(change_type)s,%(ticket_no)s,%(imac_date)s,%(ongc_section)s,%(location)s,%(room_no)s,
                             %(requester_cpf)s,%(requester_name)s,%(requester_mobile)s,%(asset_type)s,%(make)s,%(model)s,%(serial_no)s,%(feasible)s,%(infeasible_reason)s,
                             %(part_name)s,%(part_qty)s,%(part_serial_no)s,%(demo_given)s,%(remarks)s,%(pdf)s,%(created_by)s)
                             RETURNING imac_id""", {**rec, "pdf": pdf, "created_by": user["username"]}).fetchone()
        imac_id = row[0]
        if prev_key:
            con.execute("INSERT INTO portal_audit (editor, client_ip, dataset, record_key, action, changes, reason) VALUES (%s,%s,'assets',%s,'EVENT',%s::jsonb,%s)",
                        (user["username"], ip, target["asset_key"], json.dumps({"imac_change": {"old": prev_key, "new": target["asset_key"]}}),
                         f"IMAC {change_type.title()} - ticket {rec['ticket_no'] or 'n/a'}"))
    return {"id": imac_id, "asset_key": target["asset_key"], "prev_asset_key": prev_key}


def get_pdf(user, imac_id):
    r = db.one("SELECT pdf, asset_key, prev_asset_key, created_by, ticket_no FROM imac_record WHERE imac_id = %s", [imac_id])
    if not r:
        raise edit.NotFound("IMAC record not found.")
    if not (auth.is_admin(user) or r["created_by"] == user["username"]):
        asset = _asset(r["asset_key"]) or _asset(r["prev_asset_key"] or "")
        auth.check_imac(user, asset)
    name = f"IMAC_{(r['ticket_no'] or r['asset_key']).replace(' ', '_')}_{imac_id}.pdf"
    return bytes(r["pdf"]), name


# ---------------------------------------------------------------- PDF
_NAVY, _GREY, _LIGHT, _BORDER = colors.HexColor("#1F3864"), colors.HexColor("#5F6B7A"), colors.HexColor("#F1F3F5"), colors.HexColor("#DDE1E6")
_MARGIN, _CW = 8 * mm, A4[0] - 16 * mm
_LBL = ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=_GREY)
_VAL = ParagraphStyle("val", fontName="Helvetica", fontSize=10, leading=13, textColor=colors.black)
_SEC = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=colors.white)
_SIG = ParagraphStyle("sig", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.black, alignment=TA_CENTER)


def _esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;") if v not in (None, "") else "&#160;"


def _field(label, value=""):
    body = _esc(value) if value not in (None, "") else "&#160;"
    return Table([[Paragraph(label.upper(), _LBL)], [Paragraph(body, _VAL)]], style=TableStyle([
        ("TOPPADDING", (0, 0), (-1, 0), 2), ("BOTTOMPADDING", (0, 0), (-1, 0), 1), ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 1), (-1, 1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("LINEBELOW", (0, 1), (0, 1), 0.6, colors.HexColor("#A9B3BF"))]))


def _section(title):
    t = Table([[Paragraph(title, _SEC)]], colWidths=[_CW])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _grid(cells, widths, pad=6):
    t = Table([cells], colWidths=widths)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("BOX", (0, 0), (-1, -1), 0.75, _BORDER), ("INNERGRID", (0, 0), (-1, -1), 0.75, _BORDER),
                           ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _pdf(rec, user):
    story = []
    logo_w, logo_h = 24 * mm, 24 * mm / (400 / 176)
    hdr_cells = [Image(str(LOGO), width=logo_w, height=logo_h)] if LOGO.exists() else [Paragraph("", _VAL)]
    hdr = Table([[*hdr_cells, Paragraph(f"ASSET IMAC FORM<br/><font size=8.5 color='#5F6B7A'>{rec['change_type'].title()} &#183; ONGC Ankleshwar Asset &#183; Doc. No: ONGC.AMT.4.03</font>",
                                        ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=_NAVY))]], colWidths=[logo_w + 6 * mm, _CW - logo_w - 6 * mm])
    hdr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    story += [hdr, Spacer(1, 2), Table([[""]], colWidths=[_CW], rowHeights=[1.4], style=[("LINEBELOW", (0, 0), (-1, -1), 1.4, _NAVY)]), Spacer(1, 4)]

    cw4, cw2 = _CW / 4, _CW / 2
    story += [_section("CALL &amp; REQUESTER DETAILS"), Spacer(1, 2)]
    story.append(_grid([_field("Call ticket no.", rec["ticket_no"]), _field("Date", rec["imac_date"].strftime("%d %b %Y")),
                        _field("ONGC section", rec["ongc_section"]), _field("CPF no.", rec["requester_cpf"])], [cw4] * 4))
    story.append(Spacer(1, 2))
    story.append(_grid([_field("Location", rec["location"]), _field("User name", rec["requester_name"]),
                        _field("Room no.", rec["room_no"]), _field("Mobile no.", rec["requester_mobile"])], [cw4] * 4))
    story.append(Spacer(1, 5))

    story += [_section("ASSET &amp; CHANGE DETAILS"), Spacer(1, 2)]
    if rec["prev_asset_key"]:
        note = Paragraph(f"Asset (CI) changed: <font color='#5F6B7A'><strike>{_esc(rec['prev_asset_key'])}</strike></font> &#8594; <b>{_esc(rec['asset_key'])}</b>", _VAL)
        story.append(Table([[note]], colWidths=[_CW], style=TableStyle([("BOX", (0, 0), (-1, -1), 0.75, _BORDER), ("BACKGROUND", (0, 0), (-1, -1), _LIGHT),
                                                                        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)])))
        story.append(Spacer(1, 2))
    story.append(_grid([_field("Asset type", rec["asset_type"]), _field("Make / model", " ".join(filter(None, [rec["make"], rec["model"]]))),
                        _field("Asset ID", rec["asset_key"]), _field("Serial no.", rec["serial_no"])], [cw4] * 4))
    story.append(Spacer(1, 2))
    story.append(_grid([_field("Feasible?", "Yes" if rec["feasible"] else "No"), _field("If no, give reason", rec["infeasible_reason"])], [_CW * 0.25, _CW * 0.75]))
    story.append(Spacer(1, 5))

    story += [_section("PART / SOFTWARE USED"), Spacer(1, 2)]
    story.append(_grid([_field("Name", rec["part_name"]), _field("Quantity", rec["part_qty"]), _field("Part serial no.", rec["part_serial_no"]),
                        _field("Demo given", "Yes" if rec["demo_given"] else "No")], [cw4] * 4))
    story.append(Spacer(1, 5))

    story += [_section("REMARKS"), Spacer(1, 2)]
    remarks_body = _esc(rec["remarks"]).replace("\n", "<br/>") if rec["remarks"] else "<br/>".join(["&#160;"] * 3)
    story.append(Table([[Paragraph(remarks_body, _VAL)]], colWidths=[_CW],
                        style=TableStyle([("BOX", (0, 0), (-1, -1), 0.75, _BORDER), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5)])))

    stamp = dt.datetime.now().strftime("%d %b %Y %I:%M %p")

    def deco(c, doc):
        c.saveState()
        c.setFont("Helvetica", 7)
        c.setFillColor(_GREY)
        c.drawString(_MARGIN, 8 * mm, f"{user['username']} - {stamp}")
        c.restoreState()

    class _NumberedCanvas(pdfcanvas.Canvas):
        def __init__(self, *a, **kw):
            pdfcanvas.Canvas.__init__(self, *a, **kw)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                if total > 1:
                    self.saveState()
                    self.setFont("Helvetica", 7)
                    self.setFillColor(_GREY)
                    self.drawCentredString(A4[0] / 2, 8 * mm, f"PAGE {self._pageNumber} OF {total}")
                    self.restoreState()
                pdfcanvas.Canvas.showPage(self)
            pdfcanvas.Canvas.save(self)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN + 4,
                            title=f"IMAC {rec['asset_key']}", author="ITAM Portal")
    doc.build(story, onFirstPage=deco, onLaterPages=deco, canvasmaker=_NumberedCanvas)
    return out.getvalue()
