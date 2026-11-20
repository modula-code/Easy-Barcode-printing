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
POSTGRES_PASSWORD=a-strong-password
APP_TIMEZONE=Asia/Kolkata
PLANNER_API_URL=https://your-planner-api.example.com/api
BARCODE_SYNC_TOKEN=the-same-secret-configured-on-planner
PLANNER_SYNC_TIMEOUT=15
PLANNER_RETRY_INTERVAL=30
PORT=8000
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=360
```

Keep one Gunicorn worker because generated print artifacts are held in process
memory and the Planner retry thread runs once per process. The supplied
configuration uses one worker and four threads.

## Access

There is no sign-in. Anyone who can reach this service can read Odoo PO data
and record production in the Planner, so expose it only on the factory network
or behind the proxy's own access control, never on a public URL.

## Planner sync

With `PLANNER_SYNC_ENABLED=true` each print is written to `production_events`
first, then sent to the Planner (`POST /tracking/barcode-events`). The event id
never changes, so resending is safe: the Planner replays an event it already
has.

- A print the Planner did not accept stays in the queue as *Local* and is
  retried in the background, backing off from 1 to 30 minutes per event. The
  header shows how many are waiting; History lists the Planner's reason.
- "Only N remain" from the Planner means the PO is already fulfilled there. The
  print is kept local and not retried.
- When the Planner needs fewer panels than were printed, it takes what it
  needs; the rest stays in the queue as *Local*.
- Several plans carrying the same PO and part are filled in priority order
  (manual rank, then oldest plan).
- The print page warns when the typed PO is on no Planner plan, or only on a
  plan that is not in progress (`GET /tracking/barcode-po`).
- While a print is waiting, QC rejections and deletions also adjust the waiting
  event, so a later retry sends the corrected quantity. If the last attempt
  timed out (the Planner may have counted it), those edits are refused until
  the retry settles it.

Run the checks with `python -m unittest test_sync test_batch_labels` (needs a
local Postgres; override with `TEST_DATABASE_URL`).

## Print queue

When a part's label appears on more than one page of the uploaded PDF, its result
card shows **Labels to print at once (1–N)**, where N is the number of matching
label pages. **Print and record quantity** prints the first chosen pages in one PDF
and records that quantity. The server rejects a quantity above N, both for the print
and for the recorded event.

Printed barcodes are stored in Postgres at `DATABASE_URL` (tables
`printed_parts`, `production_events`, `shift_plans`, created automatically on
first run). The compose file runs a dedicated `db` service for this. Browser
refreshes do not clear this data. Queue rows are grouped by PO and date; a new
day starts with an empty active queue while older dates remain available from the
queue date picker for viewing and XLSX export.

Prints are recorded in the local queue by default. Set
`PLANNER_SYNC_ENABLED=true` only when this app should synchronously send
production and rejection events to a configured Planner API.

Deploy this directory's `docker-compose.yml` as its own Coolify resource. Set
`PLANNER_API_URL` and a `BARCODE_SYNC_TOKEN` matching the Planner.

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
