import csv
import io
import json
import os
import re
import xmlrpc.client
import zipfile
from datetime import date, datetime, timezone
from html import escape

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

load_dotenv()

import eco_audit  # noqa: E402
from eco_tracker import EcoTrackerError, check_codes  # noqa: E402
from eco_tracker import is_enabled as eco_tracker_enabled  # noqa: E402
from odoo_client import (  # noqa: E402
    OdooConfigurationError,
    OdooLookupError,
    fetch_panel_label_pdf,
    get_partner_ref,
    lookup_part_codes,
    suggest_purchase_orders,
)
from pdf_service import (  # noqa: E402
    PDFSearchError,
    get_print_artifact,
    prepare_matching_pages,
)
from planner_client import PlannerSyncError, sync_production_event  # noqa: E402
from queue_store import (  # noqa: E402
    APP_TIMEZONE,
    QC_DISPOSITIONS,
    add_printed_part,
    clear_printed_parts,
    complete_production_event,
    delete_printed_part,
    fail_production_event,
    get_printed_part,
    get_production_event,
    list_history_dates,
    list_printed_parts,
    list_production_events,
    list_qc_records,
    record_local_production_event,
    record_local_rejection,
    skip_production_event,
    stage_production_event,
    update_printed_part,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.getenv("MAX_UPLOAD_SIZE_MB", "20")
) * 1024 * 1024
ECO_AUDIT_STATES = {"draft", "sent", "to approve", "purchase", "done", "cancel"}
PRODUCTION_REPORT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "DKP_AYENA_Line_Hourly_Production_Report.xlsx",
)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/queue")
def queue_page():
    return render_template("queue.html")


@app.get("/history")
def history_page():
    return render_template("history.html")


@app.get("/qc")
def qc_page():
    return render_template("qc.html", qc_dispositions=QC_DISPOSITIONS)


@app.get("/changelog")
def changelog_page():
    return render_template("changelog.html")


@app.get("/healthz")
def health():
    return jsonify(status="ok")


def _eco_audit_report():
    """Shared by the JSON and CSV endpoints. Raises on bad input or an Odoo failure."""
    vendor_id = int(request.args.get("vendor") or eco_audit.DEFAULT_VENDOR_ID)
    state = str(request.args.get("state", "done")).strip() or "done"
    if state not in ECO_AUDIT_STATES:
        raise ValueError(
            f"State must be one of: {', '.join(sorted(ECO_AUDIT_STATES))}."
        )
    return eco_audit.audit(vendor_id, state)


@app.get("/api/eco-audit")
def eco_audit_json():
    if not eco_tracker_enabled():
        return jsonify(
            enabled=False,
            error="The revision check is switched off (ECO_REVISION_CHECK_ENABLED).",
        ), 200

    try:
        report = _eco_audit_report()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except EcoTrackerError as exc:
        return jsonify(error=str(exc)), 502
    except OdooConfigurationError as exc:
        return jsonify(error=str(exc)), 500
    except (xmlrpc.client.Error, OSError, TimeoutError) as exc:
        app.logger.exception("Odoo request failed during the ECO audit")
        return jsonify(error=f"Could not query Odoo: {exc}"), 502

    report["enabled"] = True
    return jsonify(report)


@app.get("/api/eco-audit.csv")
def eco_audit_csv():
    try:
        report = _eco_audit_report()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except EcoTrackerError as exc:
        return jsonify(error=str(exc)), 502

    columns = [
        "po_number", "po_approved", "partner_ref", "panel",
        "revision", "drawing_code", "released_on", "description",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(report["findings"])
    return send_file(
        io.BytesIO(buffer.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"drawing-changes-{report['state']}-{date.today().isoformat()}.csv",
        max_age=0,
    )


@app.get("/api/partner-ref")
def partner_ref():
    po_number = str(request.args.get("po_number", "")).strip()
    if not po_number:
        return jsonify(error="PO number is required."), 400

    try:
        result = get_partner_ref(po_number)
    except OdooLookupError as exc:
        return jsonify(error=str(exc)), 404
    except OdooConfigurationError as exc:
        return jsonify(error=str(exc)), 500
    except (xmlrpc.client.Error, OSError, TimeoutError) as exc:
        app.logger.exception("Odoo request failed")
        return jsonify(error=f"Could not query Odoo: {exc}"), 502
    except Exception:
        app.logger.exception("Unexpected partner_ref lookup failure")
        return jsonify(error="The lookup failed unexpectedly."), 500

    return jsonify(result)


@app.get("/api/po-suggestions")
def po_suggestions():
    query = str(request.args.get("q", "")).strip()
    if not query:
        return jsonify(suggestions=[])

    try:
        suggestions = suggest_purchase_orders(query)
    except OdooConfigurationError as exc:
        return jsonify(error=str(exc)), 500
    except (xmlrpc.client.Error, OSError, TimeoutError) as exc:
        app.logger.exception("Odoo PO suggestion request failed")
        return jsonify(error=f"Could not query Odoo: {exc}"), 502
    except Exception:
        app.logger.exception("Unexpected PO suggestion failure")
        return jsonify(error="The PO suggestion lookup failed unexpectedly."), 500

    return jsonify(suggestions=suggestions)


@app.get("/api/panel-label")
def panel_label():
    po_number = str(request.args.get("po_number", "")).strip()
    if not po_number:
        return jsonify(error="PO number is required."), 400

    try:
        result = fetch_panel_label_pdf(po_number)
    except OdooLookupError as exc:
        return jsonify(error=str(exc)), 404
    except OdooConfigurationError as exc:
        return jsonify(error=str(exc)), 500
    except (xmlrpc.client.Error, OSError, TimeoutError) as exc:
        app.logger.exception("Odoo report download failed")
        return jsonify(error=f"Could not fetch the report from Odoo: {exc}"), 502
    except Exception:
        app.logger.exception("Unexpected panel label fetch failure")
        return jsonify(error="The report fetch failed unexpectedly."), 500

    response = send_file(
        io.BytesIO(result["pdf_bytes"]),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"panel-label-{result['po_number'].replace('/', '-')}.pdf",
        max_age=0,
    )
    response.headers["X-So-Number"] = result["so_number"]
    response.headers["X-Po-Number"] = result["po_number"]
    return response


@app.post("/api/lookup")
def lookup():
    payload = request.get_json(silent=True) or request.form
    po_number = str(payload.get("po_number", "")).strip()
    document = request.files.get("document")
    document_bytes = None

    if document and document.filename:
        if not document.filename.lower().endswith(".pdf"):
            return jsonify(error="Please upload a PDF document."), 400
        document_bytes = document.read()
        if not document_bytes.startswith(b"%PDF-"):
            return jsonify(error="The uploaded document is not a valid PDF."), 400

    supplied_codes = payload.get("sm_codes")
    if isinstance(supplied_codes, list):
        sm_codes = [
            str(code).strip().upper()
            for code in supplied_codes
            if str(code).strip()
        ]
    else:
        sm_codes = [
            code
            for code in (
                str(payload.get("code1", "")).strip().upper(),
                str(payload.get("code2", "")).strip().upper(),
            )
            if code
        ]

    if not po_number:
        return jsonify(error="PO number is required."), 400
    if not sm_codes:
        return jsonify(error="Enter at least one product code."), 400
    if len(sm_codes) > 2:
        return jsonify(error="A maximum of two product codes is allowed."), 400

    try:
        result = lookup_part_codes(po_number, sm_codes)
    except OdooLookupError as exc:
        return jsonify(error=str(exc)), 404
    except OdooConfigurationError as exc:
        return jsonify(error=str(exc)), 500
    except (xmlrpc.client.Error, OSError, TimeoutError) as exc:
        app.logger.exception("Odoo request failed")
        return jsonify(error=f"Could not query Odoo: {exc}"), 502
    except Exception:
        app.logger.exception("Unexpected lookup failure")
        return jsonify(error="The lookup failed unexpectedly."), 500

    # A tracker outage must never stop the floor printing, so this failure is a
    # banner, not an error response.
    try:
        result["revision_alerts"] = check_codes(
            [str(match.get("part_code") or "") for match in result["matches"]]
            + sm_codes,
            result.get("po_date"),
        )
    except EcoTrackerError as exc:
        app.logger.warning("ECO revision check unavailable: %s", exc)
        result["revision_alerts"] = []
        result["revision_check_error"] = str(exc)

    if document_bytes is not None and result["matches"]:
        try:
            document_results = prepare_matching_pages(
                document_bytes,
                [str(match.get("part_code") or "") for match in result["matches"]],
            )
        except PDFSearchError as exc:
            return jsonify(error=str(exc)), 400

        for match in result["matches"]:
            part_code = str(match.get("part_code") or "")
            document_match = document_results.get(
                part_code,
                {
                    "found": False,
                    "page_number": None,
                    "page_numbers": [],
                    "occurrence_count": 0,
                    "matching_page_count": 0,
                    "page_matches": [],
                    "first_token": None,
                    "all_token": None,
                },
            ).copy()
            page_matches = []
            for page_match in document_match.get("page_matches", []):
                page_match = page_match.copy()
                page_token = page_match.pop("token", None)
                if page_token:
                    page_match["print_url"] = url_for(
                        "print_page",
                        token=page_token,
                    )
                    page_match["pdf_url"] = url_for(
                        "print_page_pdf",
                        token=page_token,
                    )
                page_matches.append(page_match)
            document_match["page_matches"] = page_matches
            first_token = document_match.pop("first_token", None)
            all_token = document_match.pop("all_token", None)
            if first_token:
                document_match["print_url"] = url_for(
                    "print_page",
                    token=first_token,
                )
                document_match["pdf_url"] = url_for(
                    "print_page_pdf",
                    token=first_token,
                )
            if all_token:
                document_match["all_print_url"] = url_for(
                    "print_page",
                    token=all_token,
                )
                document_match["all_pdf_url"] = url_for(
                    "print_page_pdf",
                    token=all_token,
                )
            match["document_match"] = document_match

    return jsonify(result)





@app.get("/api/print-queue")
def print_queue():
    try:
        return jsonify(list_printed_parts(request.args.get("date")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/history")
def history_dates():
    return jsonify(dates=list_history_dates())


def _xlsx_file(sheet_name, sheet_xml, styles_xml=None):
    styles_override = (
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        if styles_xml
        else ""
    )
    styles_relationship = (
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        if styles_xml
        else ""
    )
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""".replace("</Types>", styles_override + "</Types>"),
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{escape(sheet_name, quote=True)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": (
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>"""
            + styles_relationship
            + "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    if styles_xml:
        files["xl/styles.xml"] = styles_xml
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as workbook:
        for name, content in files.items():
            workbook.writestr(name, content)
    out.seek(0)
    return out


def _xlsx_cell(reference, value="", style=0, *, number=False):
    if value == "":
        return f'<c r="{reference}" s="{style}"/>'
    if number:
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr"><is><t>'
        f"{escape(str(value))}</t></is></c>"
    )


def _local_hour(value):
    try:
        timestamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(APP_TIMEZONE).hour


def _production_plan_id(label):
    match = re.search(r"\bW(\d+)-A(\d+)", str(label or ""), re.IGNORECASE)
    return f"W{match.group(1)}-A{match.group(2)}" if match else ""


def _hourly_production_rows(queue_rows, event_rows):
    grouped = {}
    event_totals = {}
    plan_ids = {
        str(event.get("plan_id") or ""): plan_id
        for event in event_rows
        if (plan_id := _production_plan_id(event.get("plan_label")))
    }

    def add_quantity(key, quantity, created_at):
        row = grouped.setdefault(
            key,
            {
                "po_number": key[0],
                "plan_id": key[1],
                "part_code": key[2],
                "hours": [0] * 12,
                "outside_hours": 0,
                "unbucketed": 0,
                "first_at": created_at or "",
            },
        )
        if created_at and (not row["first_at"] or created_at < row["first_at"]):
            row["first_at"] = created_at
        hour = _local_hour(created_at)
        if hour is None:
            row["unbucketed"] += quantity
        elif 8 <= hour < 20:
            row["hours"][hour - 8] += quantity
        else:
            row["outside_hours"] += quantity

    completed_events = sorted(
        (
            event
            for event in event_rows
            if event.get("action") == "produced"
            and event.get("status") in {"synced", "skipped", "error"}
        ),
        key=lambda event: str(event.get("created_at") or ""),
    )
    for event in completed_events:
        key = (
            str(event.get("po_number") or ""),
            _production_plan_id(event.get("plan_label"))
            if event.get("status") == "synced"
            else "",
            str(event.get("part_code") or ""),
        )
        quantity = int(event.get("quantity") or 0)
        event_totals[key] = event_totals.get(key, 0) + quantity
        add_quantity(key, quantity, event.get("created_at"))

    legacy_totals = {}
    for item in queue_rows:
        key = (
            str(item.get("po_number") or ""),
            plan_ids.get(str(item.get("planner_plan_id") or ""), "")
            if item.get("status") == "synced"
            else "",
            str(item.get("part_code") or ""),
        )
        legacy = legacy_totals.setdefault(
            key,
            {"quantity": 0, "created_at": str(item.get("created_at") or "")},
        )
        legacy["quantity"] += int(item.get("quantity") or 0)
        created_at = str(item.get("created_at") or "")
        if created_at and (
            not legacy["created_at"] or created_at < legacy["created_at"]
        ):
            legacy["created_at"] = created_at

    for key, legacy in legacy_totals.items():
        missing = legacy["quantity"] - event_totals.get(key, 0)
        if missing > 0:
            add_quantity(key, missing, legacy["created_at"])

    return sorted(
        grouped.values(),
        key=lambda row: (
            row["first_at"],
            row["po_number"],
            row["plan_id"],
            row["part_code"],
        ),
    )

def _queue_pdf(queue_rows, event_rows, work_date):
    import io
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    
    report_rows = _hourly_production_rows(queue_rows, event_rows)
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10*mm,
        leftMargin=10*mm,
        topMargin=15*mm,
        bottomMargin=15*mm
    )
    
    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'TitleStyle',
        parent=styles['Heading1'],
        alignment=1,
        fontSize=16,
        spaceAfter=5
    )
    subtitle_style = ParagraphStyle(
        'SubtitleStyle',
        parent=styles['Normal'],
        alignment=1,
        fontSize=12,
        spaceAfter=15
    )
    
    elements.append(Paragraph("AYENA LINE DAILY PRODUCTION REPORT", title_style))
    elements.append(Paragraph("DKP | Hourly Production Tracking Sheet", subtitle_style))
    
    header_data = [
        [f"Date: {work_date}", "Shift: _____________", "Line: _____________", "Supervisor: _____________"]
    ]
    header_table = Table(header_data, colWidths=[60*mm, 60*mm, 60*mm, 60*mm])
    header_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    col_widths = [12*mm, 35*mm, 30*mm, 30*mm, 20*mm] + [12*mm]*12 + [15*mm, 25*mm]
    table_data = [
        [
            "Sr No", "PO Number", "Plan ID", "Part Code", "Man-power",
            "08:00\n09:00", "09:00\n10:00", "10:00\n11:00", "11:00\n12:00",
            "12:00\n13:00", "13:00\n14:00", "14:00\n15:00", "15:00\n16:00",
            "16:00\n17:00", "17:00\n18:00", "18:00\n19:00", "19:00\n20:00",
            "Total\nProduced", "Remarks"
        ]
    ]
    
    reserved_rows = max(15, len(report_rows))
    column_totals = [0] * 12
    extra_total = 0
    
    for i in range(reserved_rows):
        if i < len(report_rows):
            item = report_rows[i]
            row_total = sum(item["hours"]) + item["outside_hours"] + item["unbucketed"]
            for h in range(12):
                column_totals[h] += item["hours"][h]
            extra_total += item["outside_hours"] + item["unbucketed"]
            
            row = [
                str(i + 1),
                item["po_number"],
                item["plan_id"],
                item["part_code"],
                ""
            ]
            row.extend(str(h) for h in item["hours"])
            row.append(str(row_total))
            
            remarks = []
            if item["outside_hours"]: remarks.append(f"Outside: {item['outside_hours']}")
            if item["unbucketed"]: remarks.append(f"Unbucketed: {item['unbucketed']}")
            row.append("; ".join(remarks))
        else:
            row = [str(i + 1), "", "", "", ""] + [""]*12 + ["", ""]
        table_data.append(row)
        
    grand_total_row = ["GRAND TOTAL", "", "", "", ""]
    grand_total_row.extend(str(t) for t in column_totals)
    grand_total_row.append(str(sum(column_totals) + extra_total))
    grand_total_row.append("")
    table_data.append(grand_total_row)
    
    t = Table(table_data, colWidths=col_widths, rowHeights=[10*mm] + [8.5*mm]*reserved_rows + [8.5*mm])
    t.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 8),
        ('FONTSIZE', (0,1), (-1,-1), 9),
        ('SPAN', (0,-1), (4,-1)),
        ('ALIGN', (0,-1), (0,-1), 'RIGHT'),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
    ]))
    elements.append(t)
    
    elements.append(Spacer(1, 15*mm))
    
    sig_data = [
        ["Signature - Production Supervisor", "Signature - Shift In-charge", "Signature - Quality / QA"],
        ["Date: __________________", "Date: __________________", "Date: __________________"]
    ]
    sig_table = Table(sig_data, colWidths=[90*mm, 90*mm, 90*mm])
    sig_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('TOPPADDING', (0,1), (-1,1), 10),
    ]))
    elements.append(sig_table)
    
    doc.build(elements)
    buffer.seek(0)
    return buffer

def _queue_xlsx(queue_rows, event_rows, work_date):
    report_rows = _hourly_production_rows(queue_rows, event_rows)
    reserved_rows = max(15, len(report_rows))
    data_end = 8 + reserved_rows
    grand_total_row = data_end + 1
    signature_rows = (grand_total_row + 1, grand_total_row + 2, grand_total_row + 3)
    column_totals = [0] * 12
    extra_total = 0
    rendered_rows = []

    for index in range(reserved_rows):
        row_number = 9 + index
        if index >= len(report_rows):
            rendered_rows.append(
                f'<row r="{row_number}" spans="1:19" s="4" customFormat="1" '
                'ht="25" customHeight="1">'
                + "".join(
                    _xlsx_cell(f"{column}{row_number}", "", 11)
                    for column in "ABCDEFGHIJKLMNOPQRS"
                )
                + "</row>"
            )
            continue

        item = report_rows[index]
        hours = item["hours"]
        outside_hours = item["outside_hours"]
        unbucketed = item["unbucketed"]
        for hour_index, quantity in enumerate(hours):
            column_totals[hour_index] += quantity
        extra_total += outside_hours + unbucketed
        remarks = []
        if outside_hours:
            remarks.append(f"Outside 08:00-20:00: {outside_hours}")
        if unbucketed:
            remarks.append(f"Time unavailable: {unbucketed}")
        row_total = sum(hours) + outside_hours + unbucketed
        formula = f"SUM(F{row_number}:Q{row_number})"
        if outside_hours + unbucketed:
            formula += f"+{outside_hours + unbucketed}"
        cells = [
            _xlsx_cell(f"A{row_number}", index + 1, 11, number=True),
            _xlsx_cell(f"B{row_number}", item["po_number"], 11),
            _xlsx_cell(f"C{row_number}", item["plan_id"], 11),
            _xlsx_cell(f"D{row_number}", item["part_code"], 11),
            _xlsx_cell(f"E{row_number}", "", 11),
        ]
        cells.extend(
            _xlsx_cell(f"{column}{row_number}", quantity, 11, number=True)
            for column, quantity in zip("FGHIJKLMNOPQ", hours)
        )
        cells.extend(
            (
                f'<c r="R{row_number}" s="11"><f>{formula}</f>'
                f"<v>{row_total}</v></c>",
                _xlsx_cell(f"S{row_number}", "; ".join(remarks), 11),
            )
        )
        rendered_rows.append(
            f'<row r="{row_number}" spans="1:19" s="4" customFormat="1" '
            'ht="25" customHeight="1">'
            + "".join(cells)
            + "</row>"
        )

    grand_cells = [
        f'<c r="A{grand_total_row}" s="12" t="s"><v>32</v></c>',
        *(
            _xlsx_cell(f"{column}{grand_total_row}", "", 12)
            for column in "BCDE"
        ),
    ]
    grand_cells.extend(
        (
            f'<c r="{column}{grand_total_row}" s="13">'
            f"<f>SUM({column}9:{column}{data_end})</f><v>{total}</v></c>"
        )
        for column, total in zip("FGHIJKLMNOPQ", column_totals)
    )
    grand_formula = f"SUM(F{grand_total_row}:Q{grand_total_row})"
    if extra_total:
        grand_formula += f"+{extra_total}"
    grand_cells.extend(
        (
            f'<c r="R{grand_total_row}" s="13"><f>{grand_formula}</f>'
            f"<v>{sum(column_totals) + extra_total}</v></c>",
            _xlsx_cell(f"S{grand_total_row}", "", 13),
        )
    )
    rendered_rows.append(
        f'<row r="{grand_total_row}" spans="1:19" s="4" customFormat="1" '
        'ht="15" customHeight="1">'
        + "".join(grand_cells)
        + "</row>"
    )

    with zipfile.ZipFile(PRODUCTION_REPORT_TEMPLATE) as template:
        sheet = template.read("xl/worksheets/sheet1.xml").decode("utf-8")
        workbook = template.read("xl/workbook.xml").decode("utf-8")
        before, remainder = sheet.split("<sheetData>", 1)
        original_rows, after = remainder.split("</sheetData>", 1)
        header_rows = original_rows.split('<row r="9"', 1)[0]
        header_rows = header_rows.replace(
            '<c r="B6" s="8"/>',
            _xlsx_cell("B6", work_date, 8),
        )

        for source_row, target_row in zip((13, 14, 15), signature_rows):
            row_xml = re.search(
                rf'<row r="{source_row}".*?</row>',
                original_rows,
                re.DOTALL,
            ).group(0)
            row_xml = row_xml.replace(
                f'<row r="{source_row}"',
                f'<row r="{target_row}"',
            )
            row_xml = re.sub(
                rf'r="([A-S]){source_row}"',
                lambda match: f'r="{match.group(1)}{target_row}"',
                row_xml,
            )
            rendered_rows.append(row_xml)

        merge_refs = [
            f"A{signature_rows[2]}:E{signature_rows[2]}",
            f"A{signature_rows[0]}:E{signature_rows[0]}",
            f"F{signature_rows[0]}:L{signature_rows[0]}",
            f"M{signature_rows[0]}:R{signature_rows[0]}",
            f"M{signature_rows[1]}:R{signature_rows[1]}",
            f"M{signature_rows[2]}:R{signature_rows[2]}",
            f"F{signature_rows[1]}:L{signature_rows[1]}",
            f"F{signature_rows[2]}:L{signature_rows[2]}",
            f"A{grand_total_row}:E{grand_total_row}",
            f"A{signature_rows[1]}:E{signature_rows[1]}",
            "A1:B4",
            "C1:S2",
            "C3:S3",
            "B6:D6",
            "F6:H6",
            "J6:L6",
            "N6:S6",
        ]
        merge_cells = (
            f'<mergeCells count="{len(merge_refs)}">'
            + "".join(f'<mergeCell ref="{reference}"/>' for reference in merge_refs)
            + "</mergeCells>"
        )
        after = re.sub(
            r'<mergeCells count="\d+">.*?</mergeCells>',
            merge_cells,
            after,
            flags=re.DOTALL,
        )
        last_row = signature_rows[2]
        before = re.sub(
            r'<dimension ref="A1:S\d+"/>',
            f'<dimension ref="A1:S{last_row}"/>',
            before,
        ).replace(
            'activeCell="N12" sqref="N12"',
            f'activeCell="N{grand_total_row}" sqref="N{grand_total_row}"',
        )
        sheet = (
            before
            + "<sheetData>"
            + header_rows
            + "".join(rendered_rows)
            + "</sheetData>"
            + after
        )
        workbook = re.sub(
            r"('Hourly Production Report'!\$A\$1:\$S\$)\d+",
            rf"\g<1>{max(20, last_row)}",
            workbook,
        )
        workbook = workbook.replace(
            '<definedNames>',
            '<definedNames><definedName name="_xlnm.Print_Titles" localSheetId="0">\'Hourly Production Report\'!$A:$E</definedName>',
        )
        formula_cells = [
            *(f"R{9 + index}" for index in range(len(report_rows))),
            *(f"{column}{grand_total_row}" for column in "FGHIJKLMNOPQR"),
        ]
        calc_chain = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(
                '<c r="%s" i="1"%s/>'
                % (reference, ' l="1"' if index == 0 else "")
                for index, reference in enumerate(formula_cells)
            )
            + "</calcChain>"
        )

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as generated:
            for info in template.infolist():
                content = template.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    content = sheet.encode("utf-8")
                elif info.filename == "xl/workbook.xml":
                    content = workbook.encode("utf-8")
                elif info.filename == "xl/calcChain.xml":
                    content = calc_chain.encode("utf-8")
                generated.writestr(info, content)
    output.seek(0)
    return output


def _qc_xlsx(rows):
    reserved_rows = max(10, len(rows))
    data_end = reserved_rows + 2
    signature_row = data_end + 2
    sheet_rows = [
        '<row r="1" ht="32" customHeight="1">'
        + "".join(
            _xlsx_cell(f"{column}1", "Firewall NC Report" if column == "A" else "", 1)
            for column in "ABCDEF"
        )
        + "</row>",
        '<row r="2" ht="26" customHeight="1">'
        + "".join(
            _xlsx_cell(f"{column}2", heading, 2)
            for column, heading in zip(
                "ABCDEF",
                (
                    "Sr no.",
                    "Date",
                    "Panel no",
                    "Problem Description",
                    "Qty",
                    "Disposition",
                ),
            )
        )
        + "</row>",
    ]
    for index in range(reserved_rows):
        row_number = index + 3
        item = rows[index] if index < len(rows) else {}
        work_date = str(item.get("work_date") or "")
        problem_description = str(item.get("problem_description") or "")
        row_height = max(
            24,
            18 * max(1, (len(problem_description) + 32) // 33),
        )
        try:
            date_value = (date.fromisoformat(work_date) - date(1899, 12, 30)).days
            date_cell = _xlsx_cell(f"B{row_number}", date_value, 6, number=True)
        except ValueError:
            date_cell = _xlsx_cell(f"B{row_number}", work_date, 3)
        sheet_rows.append(
            f'<row r="{row_number}" ht="{row_height}" customHeight="1">'
            + _xlsx_cell(
                f"A{row_number}",
                index + 1 if item else "",
                5,
                number=bool(item),
            )
            + date_cell
            + _xlsx_cell(f"C{row_number}", item.get("part_code") or "", 3)
            + _xlsx_cell(
                f"D{row_number}",
                problem_description,
                4,
            )
            + _xlsx_cell(
                f"E{row_number}",
                int(item.get("quantity") or 0) if item else "",
                5,
                number=bool(item),
            )
            + _xlsx_cell(
                f"F{row_number}",
                item.get("disposition") or "",
                3,
            )
            + "</row>"
        )
    sheet_rows.extend(
        (
            f'<row r="{data_end + 1}" ht="38" customHeight="1">'
            + "".join(_xlsx_cell(f"{column}{data_end + 1}", "", 3) for column in "ABCDEF")
            + "</row>",
            f'<row r="{signature_row}" ht="30" customHeight="1">'
            + "".join(
                _xlsx_cell(f"{column}{signature_row}", label, 7)
                for column, label in zip(
                    "ABCDEF",
                    (
                        "DKP Quality",
                        "DKP Production",
                        "",
                        "Ayena SQE",
                        "",
                        "Ayena Operation",
                    ),
                )
            )
            + "</row>",
        )
    )
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:F{signature_row}"/>
<sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews>
<sheetFormatPr defaultRowHeight="24"/>
<cols>
<col min="1" max="1" width="10.5" customWidth="1"/>
<col min="2" max="2" width="14.5" customWidth="1"/>
<col min="3" max="3" width="11.5" customWidth="1"/>
<col min="4" max="4" width="22.5" customWidth="1"/>
<col min="5" max="5" width="5.5" customWidth="1"/>
<col min="6" max="6" width="18" customWidth="1"/>
</cols>
<sheetData>{"".join(sheet_rows)}</sheetData>
<mergeCells count="1"><mergeCell ref="A1:F1"/></mergeCells>
<dataValidations count="1">
<dataValidation type="list" allowBlank="1" showInputMessage="1" showErrorMessage="1"
 errorTitle="Choose a disposition" error="Select one of the listed options."
 promptTitle="Disposition" prompt="Select the QC decision." sqref="F3:F{data_end}">
<formula1>"{",".join(QC_DISPOSITIONS)}"</formula1>
</dataValidation>
</dataValidations>
<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>
<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/>
</worksheet>"""
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="dd-mm-yyyy"/></numFmts>
<fonts count="3">
<font><sz val="11"/><name val="Arial"/><family val="2"/></font>
<font><b/><sz val="18"/><name val="Arial"/><family val="2"/></font>
<font><b/><sz val="11"/><name val="Arial"/><family val="2"/></font>
</fonts>
<fills count="2">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border>
<left style="thin"><color rgb="FF000000"/></left>
<right style="thin"><color rgb="FF000000"/></right>
<top style="thin"><color rgb="FF000000"/></top>
<bottom style="thin"><color rgb="FF000000"/></bottom>
<diagonal/>
</border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center" wrapText="1"/></xf>
<xf numFmtId="1" fontId="0" fillId="0" borderId="1" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" shrinkToFit="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
    return _xlsx_file("QC Report", sheet_xml, styles_xml)


@app.get("/api/print-queue/export.xlsx")
def export_print_queue_xlsx():
    try:
        queue = list_printed_parts(request.args.get("date"))
        events = list_production_events(queue["date"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return send_file(
        _queue_xlsx(queue["items"], events["items"], queue["date"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"print-queue-{queue['date']}.xlsx",
        max_age=0,
    )

@app.get("/api/print-queue/export.pdf")
def export_print_queue_pdf():
    try:
        queue = list_printed_parts(request.args.get("date"))
        events = list_production_events(queue["date"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return send_file(
        _queue_pdf(queue["items"], events["items"], queue["date"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"print-queue-{queue['date']}.pdf",
        max_age=0,
    )


@app.get("/api/qc/export.xlsx")
def export_qc_xlsx():
    try:
        report = list_qc_records(request.args.get("date"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return send_file(
        _qc_xlsx(report["items"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"firewall-nc-report-{report['date']}.xlsx",
        max_age=0,
    )


def _push_to_planner(event):
    """Send a staged event to Planner and apply its answer to the local queue.

    Returns (item, synced). Raises PlannerSyncError / ValueError, already
    recorded on the event so the ledger can show and retry the failure."""
    try:
        if event["status"] == "synced" and event["planner_response"]:
            synced = json.loads(event["planner_response"])
        else:
            payload = {
                "eventId": event["event_id"],
                "poNumber": event["po_number"],
                "partCode": event["part_code"],
                "quantity": event["quantity"],
                "action": event["action"],
                "workDate": event["work_date"],
            }
            if event["planner_plan_id"]:
                payload["planId"] = event["planner_plan_id"]
            if event["so_number"]:
                payload["soNumber"] = event["so_number"]
            synced = sync_production_event(payload)
        return complete_production_event(event["event_id"], synced), synced
    except (PlannerSyncError, ValueError) as exc:
        fail_production_event(event["event_id"], str(exc))
        raise


def _planner_sync_enabled() -> bool:
    return os.getenv("PLANNER_SYNC_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _planner_quantity_fulfilled(error: PlannerSyncError) -> bool:
    message = str(error).lower()
    return error.status_code == 409 and (
        "fulfilled" in message
        or bool(re.search(r"\bonly\s+\d+(?:\.\d+)?\b.*\bremain\b.*\bpo\b", message))
    )


@app.post("/api/print-queue")
def add_print_queue_item():
    payload = request.get_json(silent=True) or {}
    if not _planner_sync_enabled():
        work_date = str(payload.get("work_date", "")).strip()
        if not work_date:
            return jsonify(error="work_date is required."), 400
        try:
            if str(payload.get("event_id", "")).strip():
                event = stage_production_event(
                    str(payload.get("event_id", "")),
                    "produced",
                    str(payload.get("po_number", "")),
                    "",
                    str(payload.get("part_code", "")),
                    payload.get("quantity", 1),
                    work_date,
                )
                item = skip_production_event(
                    event["event_id"],
                    "Planner sync is disabled.",
                )
            else:
                item = add_printed_part(
                    str(payload.get("part_code", "")),
                    payload.get("quantity", 1),
                    str(payload.get("po_number", "")),
                    work_date,
                )
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(item=item, sync=None), 201

    try:
        event = stage_production_event(
            str(payload.get("event_id", "")),
            "produced",
            str(payload.get("po_number", "")),
            "",
            str(payload.get("part_code", "")),
            payload.get("quantity", 1),
            str(payload.get("work_date", "")),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if event["status"] == "skipped":
        item = skip_production_event(event["event_id"], event["error"])
        return jsonify(item=item, sync=None, sync_status="skipped"), 201
    try:
        item, synced = _push_to_planner(event)
    except (PlannerSyncError, ValueError) as exc:
        if isinstance(exc, PlannerSyncError) and _planner_quantity_fulfilled(exc):
            try:
                item = skip_production_event(event["event_id"], str(exc))
            except ValueError as local_exc:
                return jsonify(error=str(local_exc)), 500
            return jsonify(item=item, sync=None, sync_status="skipped"), 201
        try:
            item = record_local_production_event(event["event_id"], str(exc))
        except ValueError as local_exc:
            return jsonify(error=str(local_exc)), 500
        return jsonify(
            item=item,
            sync=None,
            sync_status="error",
            warning=str(exc),
        ), 201
    return jsonify(item=item, sync=synced), 201


@app.post("/api/print-queue/<int:item_id>/reject")
def reject_print_queue_item(item_id):
    payload = request.get_json(silent=True) or {}
    event_id = str(payload.get("event_id", ""))
    problem_description = str(payload.get("problem_description", ""))
    disposition = str(payload.get("disposition", ""))
    try:
        item = get_printed_part(item_id)
        if item["status"] != "synced":
            return jsonify(
                item=record_local_rejection(
                    event_id,
                    item_id,
                    problem_description,
                    disposition,
                ),
                sync=None,
            )
        event = stage_production_event(
            event_id,
            "rejected",
            item["po_number"],
            item["so_number"],
            item["part_code"],
            1,
            item["work_date"],
            planner_plan_id=item["planner_plan_id"],
            target_row_id=item_id,
            problem_description=problem_description,
            disposition=disposition,
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        item, synced = _push_to_planner(event)
    except PlannerSyncError as exc:
        return jsonify(error=str(exc)), exc.status_code
    except ValueError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(item=item, sync=synced)


@app.get("/api/production-events")
def production_events():
    try:
        ledger = list_production_events(request.args.get("date"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    ledger["enabled"] = _planner_sync_enabled()
    ledger["configured"] = bool(
        os.getenv("PLANNER_API_URL", "").strip()
        and os.getenv("BARCODE_SYNC_TOKEN", "").strip()
    )
    return jsonify(ledger)


@app.post("/api/production-events/<event_id>/retry")
def retry_production_event(event_id):
    """Resend an event Planner never accepted. The event id is unchanged, so
    Planner replays it instead of double-counting if it did land."""
    try:
        event = get_production_event(event_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 404
    if event["status"] == "synced":
        return jsonify(error="That event is already synced with Planner."), 409
    if event["status"] == "skipped":
        return jsonify(error="Planner quantity was fulfilled; this event is local only."), 409
    try:
        item, synced = _push_to_planner(event)
    except PlannerSyncError as exc:
        return jsonify(error=str(exc)), exc.status_code
    except ValueError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(item=item, sync=synced)


@app.put("/api/print-queue/<int:item_id>")
def update_print_queue_item(item_id):
    payload = request.get_json(silent=True) or {}
    try:
        item = update_printed_part(
            item_id,
            str(payload.get("po_number", "")),
            str(payload.get("part_code", "")),
            payload.get("quantity", 1),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(item=item)


@app.delete("/api/print-queue/<int:item_id>")
def delete_print_queue_item(item_id):
    try:
        delete_printed_part(item_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@app.delete("/api/print-queue")
def clear_print_queue():
    return jsonify(ok=True, cleared=clear_printed_parts())


@app.get("/print/<token>")
def print_page(token):
    if get_print_artifact(token) is None:
        abort(404)
    return redirect(url_for("print_page_pdf", token=token))


@app.get("/api/print-pages/<token>.pdf")
def print_page_pdf(token):
    artifact = get_print_artifact(token)
    if artifact is None:
        abort(404)
    return send_file(
        io.BytesIO(artifact.pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="matching-pages.pdf",
        max_age=0,
    )


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    size_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify(error=f"The PDF must be {size_mb} MB or smaller."), 413


if __name__ == "__main__":
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("FLASK_PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes"},
    )
