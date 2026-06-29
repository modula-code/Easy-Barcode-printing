import io
import os
import xmlrpc.client

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
    get_partner_ref,
    lookup_part_codes,
)
from pdf_service import (  # noqa: E402
    PDFSearchError,
    get_print_artifact,
    prepare_matching_pages,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.getenv("MAX_UPLOAD_SIZE_MB", "20")
) * 1024 * 1024


@app.get("/")
def index():
    return render_template("index.html")


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
                    "first_token": None,
                    "all_token": None,
                },
            ).copy()
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


@app.get("/print/<token>")
def print_page(token):
    artifact = get_print_artifact(token)
    if artifact is None:
        abort(404)
    if not artifact.png_pages:
        return redirect(url_for("print_page_pdf", token=token))

    pages = [
        {
            "image_url": url_for(
                "print_page_image",
                token=token,
                page_index=index,
            ),
            "source_page_number": source_page_number,
            "width_pt": page_size[0],
            "height_pt": page_size[1],
        }
        for index, (source_page_number, page_size) in enumerate(
            zip(artifact.page_numbers, artifact.page_sizes_pt, strict=True)
        )
    ]
    return render_template(
        "print_page.html",
        token=token,
        pages=pages,
    )


@app.get("/api/print-pages/<token>/<int:page_index>.png")
def print_page_image(token, page_index):
    artifact = get_print_artifact(token)
    if (
        artifact is None
        or page_index < 0
        or page_index >= len(artifact.png_pages)
    ):
        abort(404)
    return send_file(
        io.BytesIO(artifact.png_pages[page_index]),
        mimetype="image/png",
        max_age=0,
    )


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
