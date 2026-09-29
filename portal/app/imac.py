"""Fill IMAC: a portal-native record of Install/Add/Change work done against an asset, replacing the paper
ONGC-AMT-4.03 form. Pressed from an asset's detail page - everything on the form is either the current date, the
asset's own identity fields (type, make, model, serial no., Asset ID), or the signed-in engineer's own name. The
one interactive part is the requester (CPF/name/mobile): pulled from the asset's own registered user when it has
one, or found by searching a CPF number or name when it does not. The generated PDF is kept (not just the field
values) so a copy can always be reprinted exactly as it was on the day.
"""
import datetime as dt
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import auth, db, edit
from .export import LOGO

DDL = ["""CREATE TABLE IF NOT EXISTS imac_record (
    imac_id BIGSERIAL PRIMARY KEY, asset_key TEXT NOT NULL,
    requester_cpf TEXT, requester_name TEXT, requester_mobile TEXT,
    asset_type TEXT, make TEXT, model TEXT, serial_no TEXT,
    call_closure_date DATE NOT NULL DEFAULT current_date, engineer_name TEXT NOT NULL,
    pdf BYTEA NOT NULL, created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
       "CREATE INDEX IF NOT EXISTS ix_imac_asset ON imac_record (asset_key, created_at DESC)"]


def _asset(key):
    return db.one("SELECT asset_key, asset_type, make, model, serial_no, engineer_name, cpf_no FROM asset "
                  "WHERE asset_key = %s AND is_current = 1 AND record_level = 'ASSET'", [key])


def for_asset(key):
    return db.query("""SELECT imac_id AS id, call_closure_date, requester_name, requester_cpf, engineer_name, created_by, created_at
                       FROM imac_record WHERE asset_key = %s ORDER BY created_at DESC LIMIT 25""", [key])


def person(cpf):
    """One employee's name + mobile by CPF number - used both to show the asset's own registered user and to
    resolve whoever was found through the requester search. `cpf_no` is stored as an integer on both `asset` and
    `employee` (unlike most other keys in this database), so a non-numeric or blank value is simply "not found"
    rather than a database error. -> {cpf, name, mobile} or None."""
    try:
        cpf_int = int(str(cpf).strip())
    except (TypeError, ValueError):
        return None
    r = db.one("SELECT cpf_no AS cpf, employee_name AS name, mobile_no AS mobile FROM employee WHERE cpf_no = %s AND record_status = 'ACTIVE'", [cpf_int])
    return {"cpf": str(r["cpf"]), "name": r["name"], "mobile": r["mobile"]} if r else None


def context(user, key):
    """Starter payload for the Fill IMAC dialog: the asset's own snapshot fields, plus its registered user's
    contact details (looked up fresh, not trusted from the browser) when it has one on file."""
    row = _asset(key)
    if not row:
        raise edit.NotFound(f"{key} is not a current asset.")
    auth.check_imac(user, key)
    return {"asset_type": row["asset_type"], "make": row["make"], "model": row["model"], "serial_no": row["serial_no"],
            "requester": person(row["cpf_no"]), "engineer_name": user["display_name"], "date": dt.date.today().isoformat()}


def create(user, asset_key, body, ip):
    row = _asset(asset_key)
    if not row:
        raise edit.NotFound(f"{asset_key} is not a current asset.")
    auth.check_imac(user, asset_key)

    req = person(body.get("requester_cpf")) or {}
    today = dt.date.today()
    rec = {
        "asset_key": row["asset_key"], "requester_cpf": req.get("cpf"), "requester_name": req.get("name"), "requester_mobile": req.get("mobile"),
        "asset_type": row["asset_type"], "make": row["make"], "model": row["model"], "serial_no": row["serial_no"],
        "call_closure_date": today, "engineer_name": user["display_name"], "date": today,
    }
    pdf = _pdf(rec, user)
    with db.write() as con:
        result = con.execute("""INSERT INTO imac_record (asset_key, requester_cpf, requester_name, requester_mobile, asset_type, make, model,
                                serial_no, call_closure_date, engineer_name, pdf, created_by)
                                VALUES (%(asset_key)s,%(requester_cpf)s,%(requester_name)s,%(requester_mobile)s,%(asset_type)s,%(make)s,%(model)s,
                                %(serial_no)s,%(call_closure_date)s,%(engineer_name)s,%(pdf)s,%(created_by)s) RETURNING imac_id""",
                             {**rec, "pdf": pdf, "created_by": user["username"]}).fetchone()
    return {"id": result[0], "asset_key": row["asset_key"]}


def get_pdf(user, imac_id):
    r = db.one("SELECT pdf, asset_key, created_by FROM imac_record WHERE imac_id = %s", [imac_id])
    if not r:
        raise edit.NotFound("IMAC record not found.")
    if not (auth.is_admin(user) or r["created_by"] == user["username"]):
        auth.check_imac(user, r["asset_key"])
    return bytes(r["pdf"]), f"IMAC_{r['asset_key']}_{imac_id}.pdf"


# ---------------------------------------------------------------- PDF
_NAVY, _GREY, _BORDER = colors.HexColor("#1F3864"), colors.HexColor("#5F6B7A"), colors.HexColor("#DDE1E6")
_MARGIN, _CW = 12 * mm, A4[0] - 24 * mm
_LBL = ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=_GREY)
_VAL = ParagraphStyle("val", fontName="Helvetica", fontSize=10.5, leading=13, textColor=colors.black)
_SEC = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=colors.white)


def _esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;") if v not in (None, "") else "&#160;"


def _field(label, value=""):
    return Table([[Paragraph(label.upper(), _LBL)], [Paragraph(_esc(value), _VAL)]], style=TableStyle([
        ("TOPPADDING", (0, 0), (-1, 0), 2), ("BOTTOMPADDING", (0, 0), (-1, 0), 1), ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 1), (-1, 1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("LINEBELOW", (0, 1), (0, 1), 0.6, colors.HexColor("#A9B3BF"))]))


def _section(title):
    t = Table([[Paragraph(title, _SEC)]], colWidths=[_CW])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _grid(cells, widths):
    t = Table([cells], colWidths=widths)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("BOX", (0, 0), (-1, -1), 0.75, _BORDER), ("INNERGRID", (0, 0), (-1, -1), 0.75, _BORDER),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return t


def _pdf(rec, user):
    story = []
    logo_w, logo_h = 26 * mm, 26 * mm / (400 / 176)
    hdr_cells = [Image(str(LOGO), width=logo_w, height=logo_h)] if LOGO.exists() else [Paragraph("", _VAL)]
    hdr = Table([[*hdr_cells, Paragraph("ASSET IMAC FORM<br/><font size=8.5 color='#5F6B7A'>Install / Add / Change &#183; ONGC Ankleshwar Asset &#183; Doc. No: ONGC.AMT.4.03</font>",
                                        ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=_NAVY))]], colWidths=[logo_w + 6 * mm, _CW - logo_w - 6 * mm])
    hdr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    story += [hdr, Spacer(1, 4), Table([[""]], colWidths=[_CW], rowHeights=[1.4], style=[("LINEBELOW", (0, 0), (-1, -1), 1.4, _NAVY)]), Spacer(1, 10)]

    cw2, cw3, cw4 = _CW / 2, _CW / 3, _CW / 4
    story += [_section("DATE"), Spacer(1, 3)]
    story.append(_grid([_field("Date", rec["date"].strftime("%d %b %Y")), _field("Call closure date", rec["call_closure_date"].strftime("%d %b %Y"))], [cw2, cw2]))
    story.append(Spacer(1, 8))

    story += [_section("REQUESTER"), Spacer(1, 3)]
    story.append(_grid([_field("CPF no.", rec["requester_cpf"]), _field("User name", rec["requester_name"]), _field("Mobile no.", rec["requester_mobile"])], [cw3] * 3))
    story.append(Spacer(1, 8))

    story += [_section("ASSET DETAILS"), Spacer(1, 3)]
    story.append(_grid([_field("Asset type", rec["asset_type"]), _field("Model", " ".join(filter(None, [rec["make"], rec["model"]]))),
                        _field("Asset ID", rec["asset_key"]), _field("Serial no.", rec["serial_no"])], [cw4] * 4))
    story.append(Spacer(1, 8))

    story += [_section("ENGINEER"), Spacer(1, 3)]
    story.append(_grid([_field("Engineer name", rec["engineer_name"])], [_CW]))

    stamp = dt.datetime.now().strftime("%d %b %Y %I:%M %p")

    def deco(c, doc):
        c.saveState()
        c.setFont("Helvetica", 7)
        c.setFillColor(_GREY)
        c.drawString(_MARGIN, 8 * mm, f"{user['username']} - {stamp}")
        c.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN,
                            title=f"IMAC {rec['asset_key']}", author="ITAM Portal")
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return out.getvalue()
