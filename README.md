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
ODOO_REPORT_TIMEOUT=300
ODOO_CACHE_TTL=300
MAX_UPLOAD_SIZE_MB=20
DATABASE_URL=postgres://modula:PASSWORD@postgres:5432/modula_planner
APP_TIMEZONE=Asia/Kolkata
PLANNER_API_URL=https://your-planner-api.example.com/api
BARCODE_SYNC_TOKEN=the-same-secret-configured-on-planner
PLANNER_SYNC_TIMEOUT=15
PORT=8000
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=360
```

Keep one Gunicorn worker because generated print artifacts are held in process
memory. The supplied configuration uses one worker and four threads.

## Print queue

Today's PO label PDFs and printed barcodes are stored in Postgres at
`DATABASE_URL` — the same database the Planner uses (tables `printed_parts`,
`production_events`, `shift_plans`, created automatically on first run). Browser
refreshes do not clear this data. Queue rows are grouped by PO and date; a new
day starts with an empty active queue while older dates remain available from the
queue date picker for viewing and XLSX export. Add or replace today's PO label
PDFs on `/plans`; operators select the active PO from the sidebar on the main
print page.

Prints are recorded in the local queue by default. Set
`PLANNER_SYNC_ENABLED=true` only when this app should synchronously send
production and rejection events to a configured Planner API.

Deploy this directory's `docker-compose.yml` as its own Coolify resource (Base
Directory `/barcode-Printing`). It has no database of its own — point
`DATABASE_URL` at the Planner's Postgres and set `PLANNER_API_URL` and a
`BARCODE_SYNC_TOKEN` matching the Planner. See `../DEPLOY-COOLIFY.md`.

## API

`POST /api/lookup`

```json
{
  "po_number": "P00001",
  "sm_codes": ["SM-1234", "SM-5678"]
}
```

The Odoo user must have read access to `purchase.order`, `purchase.order.line`,
`mrp.bom`, `mrp.bom.line`, `product.product`, `product.template`, `sale.order`,
and `stock.picking`.
