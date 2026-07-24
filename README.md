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
createdb barcode
```

Fill in the Odoo values in `.env`. The example `DATABASE_URL` connects to the
local `barcode` database as your current PostgreSQL user. Then run:

```bash
python app.py
```

Open <http://127.0.0.1:5000>. The tables are created on first use; there is no
separate migration step for a fresh database.

### Migrating an existing SQLite queue

Installations from before the Postgres switch keep their data in
`printed_parts.sqlite3`. Copy it across once, after `DATABASE_URL` points at the
new empty database:

```bash
python migrate_sqlite_to_pg.py printed_parts.sqlite3
```

Row IDs are preserved and the script is idempotent, so a partial run can simply
be repeated.

On an existing Coolify deployment the old queue lives in the `barcode_data`
volume, which this Compose file no longer mounts. Carry it across **before**
dropping that volume: temporarily add `barcode_data:/data` back to the `barcode`
service (and to the top-level `volumes:`), deploy, then run

```bash
python migrate_sqlite_to_pg.py /data/printed_parts.sqlite3
```

in the container. Confirm the queue and history pages look right, then remove
the two `barcode_data` lines and redeploy. Skipping this strands the old queue
in an unreferenced Docker volume.

## Coolify deployment

Deploy this repository as its own Coolify **Docker Compose** resource. Do not
add it to the Planner's Compose stack.

- Compose location: `/docker-compose.yml`
- Public service: `barcode`, internal port `8000`
- Health-check path: `/healthz`
- Persistent data: named volume `barcode_pgdata_v2` on the bundled `db` service

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
POSTGRES_USER=barcode
POSTGRES_PASSWORD=a-strong-password
POSTGRES_DB=barcode
DB_POOL_SIZE=5
APP_TIMEZONE=Asia/Kolkata
PLANNER_SYNC_ENABLED=true
PLANNER_API_URL=https://your-planner-api.example.com/api
BARCODE_SYNC_TOKEN=the-same-secret-configured-on-planner
PLANNER_SYNC_TIMEOUT=15
PORT=8000
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=360
```

To enable Planner sync, configure the same long random `BARCODE_SYNC_TOKEN`
in both Coolify resources:

```text
# Barcode resource
PLANNER_SYNC_ENABLED=true
PLANNER_API_URL=https://your-planner-api.example.com/api
BARCODE_SYNC_TOKEN=the-shared-secret

# Planner resource
BARCODE_SYNC_TOKEN=the-same-shared-secret
```

Keep `/api` at the end of `PLANNER_API_URL`. The Barcode app sends events to
`${PLANNER_API_URL}/tracking/barcode-events`.

Keep one Gunicorn worker because generated print artifacts are held in process
memory. The supplied configuration uses one worker and four threads.

`POSTGRES_PASSWORD` is only read when Postgres initialises its data directory.
Changing it later in Coolify does not change the password inside an existing
`barcode_pgdata_v2` volume, and the app will then fail to authenticate; change it
with `ALTER ROLE` in the running database instead.

## Print queue

Today's PO label PDFs and printed barcodes are stored in the Postgres database at
`DATABASE_URL`. Browser refreshes do not clear this data. Queue rows are
grouped by PO and date; a new day starts with an empty active queue while older
dates remain available from the queue date picker for viewing and XLSX export.
Add or replace today's PO label PDFs on `/plans`; operators select the active PO
from the sidebar on the main print page.

Prints are recorded in the local queue by default. Set
`PLANNER_SYNC_ENABLED=true` only when this app should synchronously send
production and rejection events to a configured Planner API.

The standalone `docker-compose.yml` runs its own `db` service backed by the
`barcode_pgdata_v2` volume, so redeploying the container preserves the queue and
history. To use a managed Postgres instead, drop the `db` service and set
`DATABASE_URL` to the managed connection string.

Queue writes are serialised by a Postgres advisory lock rather than an
in-process mutex, so correctness no longer depends on running a single worker.
The Gunicorn config still pins one worker because generated print artifacts are
cached in process memory.

To run the queue tests, point them at a scratch database — they truncate the
tables between cases:

```bash
createdb barcode_test
TEST_DATABASE_URL=postgresql:///barcode_test python -m unittest test_queue_pg
```

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
