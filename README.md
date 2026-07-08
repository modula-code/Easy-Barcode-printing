# Odoo Part Code Lookup

A small Flask application that accepts:

- one purchase order number (`purchase.order.name`)
- one or two component/product codes (`product.product.default_code`), with no format restriction
- one PDF document to search for the resulting part code

For each SM code, it follows:

`mrp.bom.line.product_id → mrp.bom.line.bom_id → mrp.bom.product_tmpl_id`

It then returns purchase-order lines whose
`purchase.order.line.product_id.product_tmpl_id` matches the finished BOM
product template.

Results are consolidated by `product_template_id`. If multiple purchase order
lines or both SM codes resolve to the same product template, the UI displays one
entry and keeps the underlying purchase line IDs as supporting detail.

For each consolidated result, the app searches the uploaded PDF and reports all
matching label page numbers. It can print either the first matching label page
or all unique matching label pages. If the code appears multiple times on one
label page, that page is printed once. Text-based PDFs are supported; scanned
PDFs require OCR before upload.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in the Odoo values in `.env`, then run:

```bash
python app.py
```

Open <http://127.0.0.1:5000>.

## Coolify deployment

Deploy this repository as a Coolify Application using the **Dockerfile** build
pack.

- Dockerfile location: `/Dockerfile`
- Exposed port: `8000`
- Health-check path: `/healthz`

Set these environment variables in Coolify:

```text
ODOO_URL
ODOO_DB
ODOO_USERNAME
ODOO_PASSWORD
ODOO_TIMEOUT=20
ODOO_CACHE_TTL=300
MAX_UPLOAD_SIZE_MB=20
PORT=8000
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=120
```

Keep one Gunicorn worker because generated print artifacts are held in process
memory. The supplied configuration uses one worker and four threads.

## API

`POST /api/lookup`

```json
{
  "po_number": "P00001",
  "sm_codes": ["SM-1234", "SM-5678"]
}
```

The Odoo user must have read access to `purchase.order`, `purchase.order.line`,
`mrp.bom`, `mrp.bom.line`, `product.product`, and `product.template`.
