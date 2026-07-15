import io
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
from queue_store import (  # noqa: E402
    add_printed_part,
    delete_printed_part,
    list_printed_parts,
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
    return jsonify(list_printed_parts())


def _queue_xlsx(rows):
    sheet_rows = [
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Barcode</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Quantity</t></is></c></row>'
    ]
    for row_number, item in enumerate(rows, 2):
        sheet_rows.append(
            f'<row r="{row_number}">'
            f'<c r="A{row_number}" t="inlineStr"><is><t>{escape(str(item.get("part_code", "")))}</t></is></c>'
            f'<c r="B{row_number}"><v>{int(item.get("quantity") or 0)}</v></c>'
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
    rows = list_printed_parts()["items"]
    return send_file(
        _queue_xlsx(rows),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="print-queue.xlsx",
        max_age=0,
    )


@app.post("/api/print-queue")
def add_print_queue_item():
    payload = request.get_json(silent=True) or {}
    try:
        item = add_printed_part(
            str(payload.get("part_code", "")),
            payload.get("quantity", 1),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(item=item), 201


@app.put("/api/print-queue/<int:item_id>")
def update_print_queue_item(item_id):
    payload = request.get_json(silent=True) or {}
    try:
        item = update_printed_part(
            item_id,
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
