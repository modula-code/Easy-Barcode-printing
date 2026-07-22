import io
import json
import os
import xmlrpc.client
import zipfile
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
    stage_production_event,
    update_printed_part,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.getenv("MAX_UPLOAD_SIZE_MB", "20")
) * 1024 * 1024


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/queue")
def queue_page():
    return render_template("queue.html")


@app.get("/history")
def history_page():
    return render_template("history.html")





@app.get("/healthz")
def health():
    return jsonify(status="ok")


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


def _queue_xlsx(rows):
    sheet_rows = [
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>PO Number</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>Barcode</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>Quantity</t></is></c></row>'
    ]
    for row_number, item in enumerate(rows, 2):
        sheet_rows.append(
            f'<row r="{row_number}">'
            f'<c r="A{row_number}" t="inlineStr"><is><t>{escape(str(item.get("work_date", "")))}</t></is></c>'
            f'<c r="B{row_number}" t="inlineStr"><is><t>{escape(str(item.get("po_number", "")))}</t></is></c>'
            f'<c r="C{row_number}" t="inlineStr"><is><t>{escape(str(item.get("part_code", "")))}</t></is></c>'
            f'<c r="D{row_number}"><v>{int(item.get("quantity") or 0)}</v></c>'
            "</row>"
        )

    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Queue" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>"""
        + "".join(sheet_rows)
        + "</sheetData></worksheet>",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as workbook:
        for name, content in files.items():
            workbook.writestr(name, content)
    out.seek(0)
    return out


@app.get("/api/print-queue/export.xlsx")
def export_print_queue_xlsx():
    try:
        queue = list_printed_parts(request.args.get("date"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return send_file(
        _queue_xlsx(queue["items"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"print-queue-{queue['date']}.xlsx",
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


@app.post("/api/print-queue")
def add_print_queue_item():
    payload = request.get_json(silent=True) or {}
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
    try:
        item, synced = _push_to_planner(event)
    except PlannerSyncError as exc:
        return jsonify(error=str(exc)), exc.status_code
    except ValueError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(item=item, sync=synced), 201


@app.post("/api/print-queue/<int:item_id>/reject")
def reject_print_queue_item(item_id):
    payload = request.get_json(silent=True) or {}
    try:
        item = get_printed_part(item_id)
        event = stage_production_event(
            str(payload.get("event_id", "")),
            "rejected",
            item["po_number"],
            item["so_number"],
            item["part_code"],
            payload.get("quantity", 1),
            item["work_date"],
            planner_plan_id=item["planner_plan_id"],
            target_row_id=item_id,
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
        return jsonify(list_production_events(request.args.get("date")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


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
