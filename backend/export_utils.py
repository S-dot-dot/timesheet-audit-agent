"""
export_utils.py
----------------
CSV and PDF export helpers for the audit report.
"""
from __future__ import annotations

import io

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)


TABLE_LABELS = {
    "new_hires": "New Hires",
    "departures": "Departures",
    "reassignments": "Project / Sales Rep Reassignments",
    "anomalies": "Flagged Anomalies",
    "employees_curr": "Current Period — Employee Summary",
    "employees_prev": "Previous Period — Employee Summary",
}


def table_to_csv_bytes(rows: list[dict]) -> bytes:
    """Flatten a list-of-dicts (which may contain list values) into CSV bytes."""
    if not rows:
        return b"No data available.\n"
    df = pd.DataFrame(rows)
    # Flatten list/set columns (e.g. projects, sales_reps) into readable strings
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (list, set))).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, (list, set)) else v)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


def _rows_to_table_data(rows: list[dict], max_rows: int = 40) -> list[list[str]]:
    if not rows:
        return [["No records."]]
    df = pd.DataFrame(rows)
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (list, set))).any():
            df[col] = df[col].apply(lambda v: ", ".join(v) if isinstance(v, (list, set)) else v)
    df = df.head(max_rows)
    header = list(df.columns)
    data = [header] + df.astype(str).values.tolist()
    return data


def build_pdf_report(report: dict) -> bytes:
    """Render the full audit report (summary + all tables) as a polished PDF."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], textColor=colors.HexColor("#1e293b"))
    heading_style = ParagraphStyle("HeadingStyle", parent=styles["Heading2"], textColor=colors.HexColor("#334155"),
                                   spaceBefore=14, spaceAfter=8)
    normal_style = styles["Normal"]

    elements = []
    summary = report.get("summary", {})

    elements.append(Paragraph("Timesheet Audit &amp; Comparison Report", title_style))
    elements.append(Paragraph(
        f"{summary.get('label_prev', 'Period 1')} vs. {summary.get('label_curr', 'Period 2')}",
        normal_style,
    ))
    elements.append(Spacer(1, 16))

    # --- Executive summary table ---
    summary_rows = [
        ["Metric", "Value"],
        ["Total Hours (Previous)", summary.get("total_hours_prev")],
        ["Total Hours (Current)", summary.get("total_hours_curr")],
        ["Hours Delta", f"{summary.get('hours_delta')} ({summary.get('hours_delta_pct')}%)"],
        ["Active Headcount (Previous)", summary.get("active_headcount_prev")],
        ["Active Headcount (Current)", summary.get("active_headcount_curr")],
        ["New Hires", summary.get("new_hire_count")],
        ["Departures", summary.get("departure_count")],
        ["Reassignments", summary.get("reassignment_count")],
        ["Flagged Anomalies", summary.get("anomaly_count")],
    ]
    t = Table(summary_rows, colWidths=[3 * inch, 3 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(t)
    elements.append(PageBreak())

    for key in ["new_hires", "departures", "reassignments", "anomalies"]:
        rows = report.get(key, [])
        elements.append(Paragraph(f"{TABLE_LABELS.get(key, key)} ({len(rows)})", heading_style))
        data = _rows_to_table_data(rows)
        col_count = len(data[0])
        col_width = (7.3 * inch) / max(col_count, 1)
        table = Table(data, colWidths=[col_width] * col_count, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d4ed8")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 14))

    doc.build(elements)
    return buf.getvalue()
