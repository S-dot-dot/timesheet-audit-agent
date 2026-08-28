# Timesheet Audit &amp; Comparison AI Agent

An automated web application that ingests two timesheet periods (Excel or CSV),
auto-maps their columns even when headers differ, and produces a full audit:
headcount changes, project/sales-rep reassignments, and hour-recording
anomalies — plus an executive dashboard, exportable reports, and a
natural-language query box.

```
timesheet-audit-agent/
├── backend/
│   ├── main.py                # FastAPI app & REST endpoints
│   ├── parser.py               # Column auto-mapping + file loading (pandas/openpyxl)
│   ├── comparison_engine.py    # Diff engine: headcount, reassignments, anomalies
│   ├── query_engine.py         # Rule-based NL query box (+ optional Claude fallback)
│   ├── export_utils.py         # CSV + PDF report generation (reportlab)
│   └── requirements.txt
├── frontend/
│   ├── index.html              # Single-file dashboard (Tailwind + vanilla JS)
│   └── tailwind.css            # Pre-compiled Tailwind stylesheet (no CDN dependency)
├── sample_data/
│   ├── period1_timesheet.csv   # Sample baseline period (with intentional anomalies)
│   └── period2_timesheet.xlsx  # Sample current period
└── README.md
```

## 1. How it works

1. **Upload** two timesheets — a baseline ("Period A") and a current period
   ("Period B") — as `.csv` or `.xlsx`.
2. **`parser.py`** auto-detects which column is which (Employee Name, Hours,
   Project, Sales Rep, Date, PTO) using an alias dictionary + fuzzy matching,
   so "Hrs Worked" and "Total Hours" both resolve to the same internal field.
   If a file is missing a required column, you get a clear error instead of
   a crash.
3. **`comparison_engine.py`** aggregates both periods per employee and computes:
   - **New hires** (in Period B, not in Period A)
   - **Departures** (in Period A, not in Period B)
   - **Reassignments** (project or sales-rep set changed for a retained employee)
   - **Anomalies**: hours outside a configurable range, single-day overloads,
     duplicate/conflicting time entries, missing project codes, and
     week-over-week hour spikes/drops beyond a configurable % threshold.
4. The **dashboard** (`frontend/index.html`) shows an executive summary,
   searchable/sortable tables per category, CSV/PDF export, and a
   **query box** ("Ask the Ledger") that answers plain-English questions
   about the audit using the computed report — no external AI API key required.

## 2. Setup

### Requirements
- Python 3.10+
- Node.js is **not** required to run the app — `tailwind.css` is already
  pre-compiled and committed. (It's only needed if you want to edit the
  frontend's Tailwind classes and rebuild the stylesheet — see §5.)

### Install & run the backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

The backend also serves the frontend directly, so once it's running, open:

```
http://localhost:8000
```

That's it — one server, one URL, no separate frontend build step required.

### Running the frontend separately (optional)

If you'd rather serve the frontend from a different origin (e.g. a static
host), you can open `frontend/index.html` directly or serve it with any
static file server, then set the API base before the app's script runs:

```html
<script>window.API_BASE = "http://localhost:8000";</script>
```

Make sure CORS is acceptable for your deployment (the backend currently
allows all origins for ease of local development — restrict
`allow_origins` in `main.py` for production).

## 3. Try it instantly

Click **"Try with sample data"** on the intake screen to run the full audit
against the bundled `sample_data/` files without uploading anything. Those
files intentionally contain a new hire, a departure, a project reassignment,
duplicate/conflicting entries, a missing project code, and a sudden hours
drop — so you can see every detection type fire immediately.

## 4. API reference

| Method | Endpoint                                   | Purpose                                   |
|--------|---------------------------------------------|--------------------------------------------|
| POST   | `/api/upload`                              | Upload both periods, run the audit         |
| GET    | `/api/report/{session_id}`                 | Retrieve the full report again             |
| GET    | `/api/table/{session_id}/{table_name}`     | Paginated/searchable/sortable table data   |
| GET    | `/api/export/csv/{session_id}?table=...`   | CSV export of one table                    |
| GET    | `/api/export/pdf/{session_id}`             | Full formatted PDF audit report            |
| POST   | `/api/query`                               | Ask a natural-language question            |
| DELETE | `/api/session/{session_id}`                | Free server memory for that session        |

`table_name` / `table` accepts: `new_hires`, `departures`, `reassignments`,
`anomalies`, `employees_curr`, `employees_prev`.

Interactive API docs (Swagger UI) are auto-available at `/docs` once the
server is running.

### Tuning audit thresholds

`/api/upload` accepts optional form fields to tune sensitivity:

| Field                        | Default | Meaning                                             |
|-------------------------------|---------|------------------------------------------------------|
| `max_reasonable_hours`        | 60      | Flag a period total above this                       |
| `min_reasonable_hours`        | 10      | Flag a period total below this (net of PTO)          |
| `max_single_day_hours`        | 16      | Flag any single day above this                       |
| `spike_drop_pct_threshold`    | 40      | % change vs. the prior period that triggers a flag   |

These are also exposed in the UI under "Audit thresholds (advanced)".

## 5. Rebuilding the frontend stylesheet (optional)

The frontend uses Tailwind CSS, pre-compiled to `frontend/tailwind.css` so
the app has **no runtime dependency on any CDN**. If you change class names
in `index.html` and need to regenerate the stylesheet:

```bash
npm install -D tailwindcss@3
npx tailwindcss -i input.css -o frontend/tailwind.css --minify \
  --content frontend/index.html
```

Where `input.css` contains:
```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

## 6. Optional: enabling true LLM-powered Q&A

The query box works fully out of the box using deterministic, rule-based
matching against the computed audit report (`backend/query_engine.py`).
If you'd like more open-ended questions answered by Claude directly, set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

before starting the backend. When set, questions the rule engine can't
confidently classify are automatically handed off to Claude, grounded in
the same JSON audit report (never external data), and the fallback fails
silently (reverting to the standard guidance message) if the call errors.

## 7. Production notes

This build is fully functional for real use but makes a few deliberate
simplifications worth knowing about before a large-scale deployment:

- **Session storage is in-memory** (`SESSIONS` dict in `main.py`), so
  reports don't survive a server restart and won't scale across multiple
  worker processes. Swap in Redis or a database-backed store for
  production, keyed the same way by `session_id`.
- **Sessions expire after 4 hours** (`SESSION_TTL_SECONDS`) and are cleaned
  up lazily on the next request.
- **CORS is wide open** (`allow_origins=["*"]`) for local development —
  restrict this to your actual frontend origin(s) before deploying.
- No authentication layer is included — add one before exposing this to
  the public internet, since uploaded timesheet data is sensitive.

## 8. Troubleshooting

| Symptom                                        | Likely cause / fix                                                        |
|--------------------------------------------------|----------------------------------------------------------------------------|
| "missing required column(s)" error on upload    | Your file has no recognizable Employee Name or Hours column — rename it to something like "Employee Name" / "Hours" or check `parser.py`'s `STANDARD_FIELDS` for supported aliases. |
| Dashboard loads but styling looks broken        | `tailwind.css` failed to load — confirm the backend is serving it at `/tailwind.css` (visit that URL directly), or rebuild it per §5. |
| "Session not found or has expired"              | Sessions live in server memory and reset on restart / after 4 hours — just re-upload. |
| PDF export looks sparse for very large datasets | The PDF report caps each table at 40 rows for readability — use the CSV export for the full dataset. |
