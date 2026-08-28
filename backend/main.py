"""
main.py
-------
FastAPI backend for the Timesheet Audit & Comparison AI Agent.

Endpoints
---------
POST   /api/upload                 Upload Period 1 + Period 2 files, run the audit
GET    /api/report/{session_id}    Full audit report JSON
GET    /api/table/{session_id}/{table_name}   Paginated/searchable/sortable table data
GET    /api/export/csv/{session_id}           CSV export of a given table
GET    /api/export/pdf/{session_id}           Full PDF audit report
POST   /api/query                  Natural-language question answering over the report
DELETE /api/session/{session_id}   Free server memory for a session
GET    /                           Serves the frontend dashboard (single-file app)

Run with:  uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from parser import load_and_standardize, TimesheetParseError
from comparison_engine import TimesheetAuditEngine, AuditConfig
from query_engine import answer_question
from export_utils import table_to_csv_bytes, build_pdf_report

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Timesheet Audit & Comparison AI Agent",
    description="Upload two timesheet periods and get an automated audit: "
                "headcount changes, reassignments, and anomaly detection.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory session store.
# NOTE: For a production deployment, swap this for Redis / a database so
# reports survive server restarts and scale across multiple workers.
# ---------------------------------------------------------------------------
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 60 * 60 * 4  # 4 hours


def _cleanup_expired_sessions() -> None:
    now = time.time()
    expired = [sid for sid, s in SESSIONS.items() if now - s["created_at"] > SESSION_TTL_SECONDS]
    for sid in expired:
        SESSIONS.pop(sid, None)


def _get_session(session_id: str) -> dict:
    _cleanup_expired_sessions()
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or has expired. Please re-upload your files.")
    return session


TABLE_KEYS = {"new_hires", "departures", "reassignments", "anomalies", "employees_curr", "employees_prev"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class QueryRequest(BaseModel):
    session_id: str
    question: str


class ConfigOverrides(BaseModel):
    max_reasonable_hours: Optional[float] = None
    min_reasonable_hours: Optional[float] = None
    max_single_day_hours: Optional[float] = None
    spike_drop_pct_threshold: Optional[float] = None


# ---------------------------------------------------------------------------
# Upload & audit endpoint
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload_timesheets(
    period1_file: UploadFile = File(..., description="Baseline / previous period timesheet"),
    period2_file: UploadFile = File(..., description="Current / latest period timesheet"),
    period1_label: str = Form("Period 1"),
    period2_label: str = Form("Period 2"),
    max_reasonable_hours: float = Form(60.0),
    min_reasonable_hours: float = Form(10.0),
    max_single_day_hours: float = Form(16.0),
    spike_drop_pct_threshold: float = Form(40.0),
):
    """
    Accepts two timesheet files, standardizes their columns, runs the full
    audit/diff engine, and returns a session_id plus the complete report.
    """
    try:
        bytes1 = await period1_file.read()
        bytes2 = await period2_file.read()

        if not bytes1 or not bytes2:
            raise HTTPException(status_code=400, detail="One or both uploaded files are empty.")

        df1, mapping1 = load_and_standardize(bytes1, period1_file.filename or "period1", period1_label)
        df2, mapping2 = load_and_standardize(bytes2, period2_file.filename or "period2", period2_label)

        config = AuditConfig(
            max_reasonable_hours=max_reasonable_hours,
            min_reasonable_hours=min_reasonable_hours,
            max_single_day_hours=max_single_day_hours,
            spike_drop_pct_threshold=spike_drop_pct_threshold,
        )

        engine = TimesheetAuditEngine(df1, df2, period1_label, period2_label, config=config)
        report = engine.build_full_report()

        session_id = str(uuid.uuid4())
        SESSIONS[session_id] = {
            "created_at": time.time(),
            "report": report,
            "column_mapping": {
                "period1": mapping1.mapping,
                "period1_unmapped": mapping1.unmapped_source_columns,
                "period2": mapping2.mapping,
                "period2_unmapped": mapping2.unmapped_source_columns,
            },
        }

        return {
            "session_id": session_id,
            "column_mapping": SESSIONS[session_id]["column_mapping"],
            "report": report,
        }

    except TimesheetParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - always return a clean, friendly error
        raise HTTPException(status_code=500, detail=f"Unexpected error while processing files: {exc}") from exc


# ---------------------------------------------------------------------------
# Report retrieval
# ---------------------------------------------------------------------------
@app.get("/api/report/{session_id}")
async def get_report(session_id: str):
    session = _get_session(session_id)
    return {"session_id": session_id, "column_mapping": session["column_mapping"], "report": session["report"]}


# ---------------------------------------------------------------------------
# Table endpoint: search, sort, paginate any of the report's tables
# ---------------------------------------------------------------------------
@app.get("/api/table/{session_id}/{table_name}")
async def get_table(
    session_id: str,
    table_name: str,
    search: str = Query("", description="Case-insensitive search across all fields"),
    sort_by: str = Query("", description="Column name to sort by"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=500),
):
    if table_name not in TABLE_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown table '{table_name}'. Valid options: {sorted(TABLE_KEYS)}")

    session = _get_session(session_id)
    rows: list[dict] = session["report"].get(table_name, [])

    if search:
        needle = search.lower()
        def _matches(row: dict) -> bool:
            for v in row.values():
                if isinstance(v, (list, set)):
                    if any(needle in str(item).lower() for item in v):
                        return True
                elif needle in str(v).lower():
                    return True
            return False
        rows = [r for r in rows if _matches(r)]

    if sort_by:
        def _sort_key(row: dict):
            val = row.get(sort_by)
            if isinstance(val, (list, set)):
                return ",".join(str(v) for v in val)
            if val is None:
                return ""
            return val
        try:
            rows = sorted(rows, key=_sort_key, reverse=(sort_dir == "desc"))
        except TypeError:
            rows = sorted(rows, key=lambda r: str(_sort_key(r)), reverse=(sort_dir == "desc"))

    total = len(rows)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]

    return {
        "table_name": table_name,
        "rows": page_rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------
@app.get("/api/export/csv/{session_id}")
async def export_csv(session_id: str, table: str = Query(..., description="Table name to export")):
    if table not in TABLE_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown table '{table}'. Valid options: {sorted(TABLE_KEYS)}")
    session = _get_session(session_id)
    rows = session["report"].get(table, [])
    csv_bytes = table_to_csv_bytes(rows)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}_audit_export.csv"'},
    )


@app.get("/api/export/pdf/{session_id}")
async def export_pdf(session_id: str):
    session = _get_session(session_id)
    pdf_bytes = build_pdf_report(session["report"])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="timesheet_audit_report.pdf"'},
    )


# ---------------------------------------------------------------------------
# Natural language query endpoint
# ---------------------------------------------------------------------------
@app.post("/api/query")
async def query(request: QueryRequest):
    session = _get_session(request.session_id)
    result = answer_question(request.question, session["report"])
    return result


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------
@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"deleted": True}


@app.get("/api/health")
async def health():
    return {"status": "ok", "active_sessions": len(SESSIONS)}


# ---------------------------------------------------------------------------
# Serve the frontend as a single unified app (optional convenience).
# The frontend can also be opened directly as a static file / hosted separately.
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

if FRONTEND_DIR.exists():
    @app.get("/", response_class=HTMLResponse)
    async def serve_frontend():
        index_path = FRONTEND_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/tailwind.css")
    async def serve_tailwind_css():
        css_path = FRONTEND_DIR / "tailwind.css"
        if css_path.exists():
            return FileResponse(css_path, media_type="text/css")
        raise HTTPException(status_code=404, detail="tailwind.css not found")


# ---------------------------------------------------------------------------
# Local / fallback entrypoint.
#
# Cloud hosts normally start this app via an explicit command, e.g.:
#   uvicorn main:app --host 0.0.0.0 --port $PORT
# (see Procfile / render.yaml in the repo root). This block is just a
# convenience so `python main.py` also works and respects a PORT env var,
# which some platforms (and quick local tests) expect.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
