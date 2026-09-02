"""
parser.py
---------
Handles ingestion of raw timesheet files (.xlsx, .xls, .csv) and normalizes
their columns to a standard internal schema, even when different periods
use slightly different headers (e.g. "Emp Name" vs "Employee" vs "Full Name").

This is the "dynamic column mapping" layer referenced in the product spec.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


class TimesheetParseError(Exception):
    """Raised when a file cannot be parsed or is missing required fields."""


# ---------------------------------------------------------------------------
# Standard schema definition
# ---------------------------------------------------------------------------
# Each standard field maps to a list of common real-world header variants.
# Matching is done on a normalized form (lowercased, punctuation stripped).
STANDARD_FIELDS: dict[str, list[str]] = {
    "employee_name": [
        "employee name", "employee full name", "name", "emp name",
        "staff name", "worker", "full name", "team member", "resource",
        "resource name",
    ],
    "employee_id": [
        "employee id", "emp id", "employee number", "staff id", "id",
        "worker id", "badge id", "personnel id", "personnel no",
        "personnel number", "personnel",
    ],
    "hours_worked": [
        "hours worked", "hours", "total hours", "hrs", "hours logged",
        "worked hours", "time worked", "hrs worked", "duration", "hours total",
    ],
    "project_id": [
        "project id", "project", "project code", "project name", "proj id",
        "proj", "job code", "cost center", "task", "wbs",
    ],
    "activity_type": [
        "activity type", "actvity type", "activity code", "act type",
        "charge type", "task type",
    ],
    "sales_rep": [
        "sales rep", "rep", "sales representative", "account rep",
        "salesperson", "account manager", "am", "assigned rep",
    ],
    "date": [
        "date", "work date", "entry date", "timesheet date", "day",
        "shift date", "log date",
    ],
    "pto_hours": [
        "pto", "pto hours", "paid time off", "vacation hours",
        "sick hours", "leave hours", "time off",
    ],
    "notes": ["notes", "comment", "comments", "remarks", "description"],
    # "rate_type" isn't itself a data point we report — it's a category flag
    # (e.g. "ST"/"OT"/"Lunch") some payroll exports use to split one
    # employee's week across several rows. When present, we use it below to
    # derive regular_hours/overtime_hours per row.
    "rate_type": ["rate type", "pay type", "pay code", "wage type"],
    "regular_hours": [
        "regular hours", "regular", "straight time hours", "straight time",
        "st hours",
    ],
    "overtime_hours": ["overtime hours", "overtime", "ot hours"],
}

REQUIRED_FIELDS = ["employee_name", "hours_worked"]


def _normalize(text: str) -> str:
    """Lowercase, strip, and remove non-alphanumeric characters for comparison."""
    text = str(text).lower().strip()
    # Spell out "#" as "number" *before* stripping punctuation, so a header
    # like "Employee #" normalizes to "employee number" (matching the
    # employee_id alias) instead of collapsing to just "employee" (which
    # would collide with the employee_name alias and overwrite the real
    # name with the employee number — see STANDARD_FIELDS below).
    text = text.replace("#", " number ")
    return re.sub(r"[^a-z0-9]", "", text)


@dataclass
class ColumnMapping:
    """Records how source columns were mapped to standard fields."""
    mapping: dict[str, str] = field(default_factory=dict)   # standard_field -> source_col
    unmapped_source_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)


def detect_column_mapping(columns: list[str]) -> ColumnMapping:
    """
    Given a list of raw column headers, figure out which standard field
    each one most likely represents.
    """
    result = ColumnMapping()
    normalized_lookup = {_normalize(c): c for c in columns}
    used_source_cols: set[str] = set()

    for std_field, aliases in STANDARD_FIELDS.items():
        normalized_aliases = {_normalize(a) for a in aliases} | {_normalize(std_field)}
        match = None

        # 1. Exact normalized match first
        for norm_alias in normalized_aliases:
            if norm_alias in normalized_lookup:
                match = normalized_lookup[norm_alias]
                break

        # 2. Fallback: substring match (e.g. "Employee Full Name" contains "name").
        # Score every candidate column and keep the LONGEST alias match, not
        # just the first column encountered in file order. Short generic
        # aliases like "name" can appear inside unrelated headers (e.g. a
        # "Job site Name" column) — without this, a real-world file could
        # have that unrelated column stolen ahead of the actual
        # "Employee Full Name" column simply because it came first.
        if not match:
            best_len = 0
            for norm_col, original_col in normalized_lookup.items():
                if original_col in used_source_cols:
                    continue
                for norm_alias in normalized_aliases:
                    if len(norm_alias) >= 3 and (norm_alias in norm_col or norm_col in norm_alias):
                        if len(norm_alias) > best_len:
                            best_len = len(norm_alias)
                            match = original_col

        if match:
            result.mapping[std_field] = match
            used_source_cols.add(match)

    result.unmapped_source_columns = [c for c in columns if c not in used_source_cols]
    result.missing_required = [f for f in REQUIRED_FIELDS if f not in result.mapping]
    return result


def load_raw_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Load a .csv/.xlsx/.xls file into a raw (un-mapped) pandas DataFrame."""
    lower = filename.lower()
    try:
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes))
        elif lower.endswith(".xlsx") or lower.endswith(".xlsm"):
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        elif lower.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(file_bytes))
        else:
            raise TimesheetParseError(
                f"Unsupported file type for '{filename}'. Please upload .csv, .xlsx, or .xls."
            )
    except TimesheetParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a friendly error to the API layer
        raise TimesheetParseError(f"Could not read '{filename}': {exc}") from exc

    if df.empty or len(df.columns) == 0:
        raise TimesheetParseError(f"'{filename}' appears to be empty.")

    # Drop fully-empty rows/columns which are common in exported spreadsheets
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _load_all_excel_sheets(file_bytes: bytes, filename: str) -> list[tuple[str, pd.DataFrame]]:
    """
    Load EVERY sheet in an Excel workbook, not just the first. Real payroll
    exports sometimes carry supplemental data on a second sheet (e.g. a
    "Manual Upload" tab for someone who worked under a special assignment
    that week) — if we only ever read sheet 1, that person is invisible to
    us on the week they're on the extra sheet, which then makes them look
    like a brand-new hire the following week when they show up normally.
    Sheets that don't contain usable data (empty, or no recognizable
    columns) are skipped rather than causing a hard failure.
    """
    engine = "openpyxl" if filename.lower().endswith((".xlsx", ".xlsm")) else None
    try:
        sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, engine=engine)
    except Exception as exc:  # noqa: BLE001
        raise TimesheetParseError(f"Could not read '{filename}': {exc}") from exc

    out: list[tuple[str, pd.DataFrame]] = []
    for name, df in sheets.items():
        df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
        if df.empty or len(df.columns) == 0:
            continue
        df.columns = [str(c).strip() for c in df.columns]
        out.append((name, df))

    if not out:
        raise TimesheetParseError(f"'{filename}' appears to be empty.")
    return out


# ---------------------------------------------------------------------------
# Shift-segment (clock in/out) format — e.g. TBL/TSMC-style time-tracking
# exports. One row per work SEGMENT rather than per day: an employee's shift
# gets split across multiple rows (work, paid lunch, unpaid lunch, time off),
# each with its own start/end time and an hours value. This shape can't be
# handled by the generic one-column-per-field mapper above because getting
# an accurate hours total requires business-rule judgment calls per row
# (e.g. an unpaid lunch segment must NOT count toward hours worked, while a
# *paid* lunch segment must). Detected by fingerprint and handled separately.
# ---------------------------------------------------------------------------
_SHIFT_SEGMENT_REQUIRED_COLUMNS = {
    "localdate", "localstarttime", "localendtime", "fname", "lname", "hours", "jobcode1",
}


def _is_shift_segment_export(columns: list[str]) -> bool:
    normalized = {_normalize(c) for c in columns}
    return _SHIFT_SEGMENT_REQUIRED_COLUMNS.issubset(normalized)


def _classify_shift_segment(jobcode_1: str) -> str:
    """
    Categorize a single shift-segment row so we know whether its "hours"
    value should count toward hours worked, paid time off, or neither.
    Verified against a real TSMC weekly PDF report: e.g. a day with
    7h work + 0.5h paid lunch + 2.5h work + 0.5h UNPAID lunch reports a
    10.00h total (the unpaid lunch is dropped entirely), and a day that's
    entirely "Time Off - UNPAID (APPROVED)" reports 0.00h.
    """
    text = (jobcode_1 or "").lower()
    if "unpaid" in text and "lunch" in text:
        return "exclude"
    if "time off" in text and "unpaid" in text:
        return "exclude"
    if "time off" in text:
        return "pto"
    return "work"


def _standardize_shift_segments(df: pd.DataFrame, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """Standardize a shift-segment (clock in/out) export into the standard schema."""
    col_lookup = {_normalize(c): c for c in df.columns}

    def col(name: str) -> str:
        return col_lookup[_normalize(name)]

    out = pd.DataFrame(index=df.index)
    fname = df[col("fname")].astype(str).str.strip()
    lname = df[col("lname")].astype(str).str.strip()
    out["employee_name"] = (fname + " " + lname).str.strip()

    # A username/email is a far more reliable unique identifier here than
    # name — names can repeat, and this format appends role tags like
    # "(T)" to last names — so prefer it as the employee_id when present.
    if "username" in col_lookup and df[col("username")].astype(str).str.strip().replace({"nan": ""}).ne("").any():
        out["employee_id"] = df[col("username")].astype(str).str.strip()
    elif "payroll_id" in col_lookup:
        out["employee_id"] = df[col("payroll_id")].astype(str).str.strip()
    else:
        out["employee_id"] = pd.NA

    out["date"] = pd.to_datetime(df[col("local_date")], errors="coerce")

    hours = pd.to_numeric(df[col("hours")], errors="coerce").fillna(0.0)
    jobcode_1 = df[col("jobcode_1")].astype(str)
    category = jobcode_1.apply(_classify_shift_segment)

    out["hours_worked"] = hours.where(category == "work", 0.0)
    out["pto_hours"] = hours.where(category == "pto", 0.0)

    if "jobcode_2" in col_lookup:
        jobcode_2 = df[col("jobcode_2")].astype(str).str.strip().replace({"nan": ""})
        out["project_id"] = jobcode_2.where(jobcode_2 != "", jobcode_1)
    else:
        out["project_id"] = jobcode_1

    out["sales_rep"] = pd.NA
    out["activity_type"] = jobcode_1.where(category == "work", pd.NA)
    out["notes"] = df[col("notes")].astype(str).str.strip() if "notes" in col_lookup else pd.NA

    for std_field in STANDARD_FIELDS:
        if std_field not in out.columns:
            out[std_field] = pd.NA

    out["employee_name"] = out["employee_name"].astype(str).str.strip()
    out = out[out["employee_name"].str.len() > 0]
    out = out[~out["employee_name"].str.lower().isin(["nan", "none", ""])]

    for c in ["project_id", "sales_rep", "employee_id", "notes", "activity_type"]:
        out[c] = out[c].astype(str).str.strip()
        out[c] = out[c].replace({"nan": "", "None": "", "<NA>": ""})

    out["source_file"] = source_label

    # This format naturally splits one continuous task into multiple rows
    # around a lunch break (e.g. 7h before lunch + 2.5h after, same
    # employee/day/task) — each half has a different "hours" value by
    # design, not because anything's wrong. Left as separate rows, the
    # duplicate/conflict checker misreads every ordinary split shift as a
    # conflicting entry (verified: 788 false flags out of ~3,300 rows on a
    # real file). Collapse same employee+day+project+activity segments into
    # one row, summing hours, before this ever reaches that check.
    group_cols = ["employee_name", "employee_id", "date", "project_id", "activity_type", "source_file"]
    agg = {
        "hours_worked": "sum",
        "pto_hours": "sum",
        "sales_rep": "first",
        "notes": "first",
    }
    out = out.groupby(group_cols, dropna=False, as_index=False).agg(agg)
    out = out.reset_index(drop=True)

    mapping = ColumnMapping(
        mapping={
            "employee_name": "fname + lname",
            "employee_id": "username" if "username" in col_lookup else "payroll_id",
            "hours_worked": "hours (work + paid-lunch segments, excl. unpaid breaks/time off)",
            "pto_hours": "hours (unpaid time-off segments excluded; paid time-off summed here)",
            "date": "local_date",
            "project_id": "jobcode_2 (falls back to jobcode_1)",
        },
        unmapped_source_columns=[c for c in df.columns if _normalize(c) not in _SHIFT_SEGMENT_REQUIRED_COLUMNS],
    )
    return out, mapping


def standardize_dataframe(df: pd.DataFrame, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """
    Rename a raw DataFrame's columns to the standard schema and coerce types.
    Returns the standardized DataFrame plus the ColumnMapping used (for
    transparency in the UI / audit trail).
    """
    if _is_shift_segment_export(list(df.columns)):
        return _standardize_shift_segments(df, source_label)

    mapping = detect_column_mapping(list(df.columns))

    # Some real-world exports (e.g. SAP-style payroll extracts) only carry a
    # personnel/employee ID column and no actual name column. Rather than
    # hard-failing on a file that's otherwise perfectly usable, fall back to
    # displaying the ID as the name — a human can still recognize/relabel
    # people by their ID, and every other check still works.
    missing_required = list(mapping.missing_required)
    fall_back_name_to_id = "employee_name" in missing_required and "employee_id" in mapping.mapping
    if fall_back_name_to_id:
        missing_required.remove("employee_name")

    if missing_required:
        raise TimesheetParseError(
            f"'{source_label}' is missing required column(s): "
            f"{', '.join(missing_required)}. "
            f"Detected columns were: {', '.join(df.columns)}"
        )

    rename_map = {source_col: std_field for std_field, source_col in mapping.mapping.items()}
    std_df = df.rename(columns=rename_map).copy()

    # Ensure every standard field exists, even if not present in source
    for std_field in STANDARD_FIELDS:
        if std_field not in std_df.columns:
            std_df[std_field] = pd.NA

    if fall_back_name_to_id:
        raw_id = std_df["employee_id"].astype(str).str.strip()
        raw_id = raw_id.replace({"nan": "", "None": "", "<NA>": ""})
        std_df["employee_name"] = "Personnel #" + raw_id

    # Some payroll exports embed a numeric employee ID directly inside the
    # name field itself, e.g. "Guanipa, JeanGuanipa0418 (111154420)". We've
    # confirmed against real files that this trailing ID can be silently
    # reassigned to the same person between pay periods (same name, same
    # everything else, different number in the parens) — which, left alone,
    # makes every headcount/new-hire comparison misread a routine re-number
    # as that person quitting one week and getting hired again the next.
    # When there's no separate ID column already mapped, pull that trailing
    # "(12345)" out into employee_id and key matching off the stable name
    # text instead.
    if "employee_id" not in mapping.mapping:
        extracted = std_df["employee_name"].astype(str).str.extract(
            r"^(?P<clean_name>.*?)\s*\((?P<embedded_id>\d+)\)\s*$"
        )
        has_embedded_id = extracted["embedded_id"].notna()
        if has_embedded_id.any():
            std_df.loc[has_embedded_id, "employee_id"] = extracted.loc[has_embedded_id, "embedded_id"]
            std_df.loc[has_embedded_id, "employee_name"] = extracted.loc[has_embedded_id, "clean_name"]

    # --- Type coercion & cleanup ---
    std_df["employee_name"] = std_df["employee_name"].astype(str).str.strip()
    std_df = std_df[std_df["employee_name"].str.len() > 0]
    std_df = std_df[~std_df["employee_name"].str.lower().isin(["nan", "none", ""])]

    std_df["hours_worked"] = pd.to_numeric(std_df["hours_worked"], errors="coerce").fillna(0.0)
    std_df["pto_hours"] = pd.to_numeric(std_df["pto_hours"], errors="coerce").fillna(0.0)

    if "date" in std_df.columns:
        std_df["date"] = pd.to_datetime(std_df["date"], errors="coerce")

    for col in ["project_id", "sales_rep", "employee_id", "notes", "activity_type"]:
        std_df[col] = std_df[col].astype(str).str.strip()
        std_df[col] = std_df[col].replace({"nan": "", "None": "", "<NA>": ""})

    # Some payroll exports split one employee's week across several rows by
    # a "Rate Type" (ST/OT/Lunch...) rather than reporting regular vs.
    # overtime hours directly. When that's the only source we have for it,
    # derive regular_hours/overtime_hours from it so anomaly checks that
    # depend on the regular/OT split (e.g. "under 40 regular hours but
    # already has overtime") have real data to work with instead of just
    # falling back to a flat >40-total-hours guess, which can't ever fire
    # that particular check.
    std_df["regular_hours"] = pd.to_numeric(std_df["regular_hours"], errors="coerce")
    std_df["overtime_hours"] = pd.to_numeric(std_df["overtime_hours"], errors="coerce")
    if (
        "rate_type" in mapping.mapping
        and "regular_hours" not in mapping.mapping
        and "overtime_hours" not in mapping.mapping
    ):
        rate_type_lower = std_df["rate_type"].astype(str).str.lower()
        is_overtime = rate_type_lower.str.contains("ot", na=False)
        is_break = rate_type_lower.str.contains("lunch", na=False) | rate_type_lower.str.contains("break", na=False)
        std_df["overtime_hours"] = std_df["hours_worked"].where(is_overtime, 0.0)
        std_df["regular_hours"] = std_df["hours_worked"].where(~is_overtime & ~is_break, 0.0)

    std_df["source_file"] = source_label
    std_df = std_df.reset_index(drop=True)
    return std_df, mapping


def load_and_standardize(file_bytes: bytes, filename: str, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """
    Convenience wrapper: load raw bytes straight to a standardized DataFrame.

    For Excel workbooks, every sheet is checked — not just the first — so a
    supplemental tab (e.g. "Manual Upload") isn't silently dropped. Sheets
    that don't contain usable timesheet columns are skipped rather than
    failing the whole upload.
    """
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm", ".xls")):
        raw_sheets = _load_all_excel_sheets(file_bytes, filename)
    else:
        raw_sheets = [(None, load_raw_dataframe(file_bytes, filename))]

    standardized_parts: list[pd.DataFrame] = []
    first_mapping: Optional[ColumnMapping] = None
    skip_errors: list[str] = []

    for sheet_name, raw_df in raw_sheets:
        try:
            std_df, mapping = standardize_dataframe(raw_df, source_label)
        except TimesheetParseError as exc:
            skip_errors.append(str(exc))
            continue

        # Some timesheet exports report one row per (employee, project, week)
        # with a separate column per weekday ("Monday", "Tuesday", ...) holding
        # that day's hours, alongside a week-ending date and a weekly total.
        # Everything above treats each row as a single day worked — which is
        # wrong for this layout, and badly confuses any per-day check (a
        # 56-hour week gets read as a 56-hour DAY). When we spot that shape,
        # explode each row into one row per real day actually worked instead.
        weekday_cols = _find_weekday_columns(raw_df.columns)
        if len(weekday_cols) >= 3:
            std_df = _expand_weekly_rollup_to_daily(std_df, weekday_cols)

        standardized_parts.append(std_df)
        if first_mapping is None:
            first_mapping = mapping

    if not standardized_parts:
        raise TimesheetParseError(
            skip_errors[0] if skip_errors else f"'{source_label}' has no usable timesheet data."
        )

    combined = (
        pd.concat(standardized_parts, ignore_index=True)
        if len(standardized_parts) > 1
        else standardized_parts[0]
    )
    return combined, first_mapping


WEEKDAY_COLUMN_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _find_weekday_columns(columns) -> dict[int, str]:
    """Map weekday index (Monday=0 ... Sunday=6) to the actual source column
    name, for files that lay a week out as one column per day."""
    targets = {_normalize(name): i for i, name in enumerate(WEEKDAY_COLUMN_NAMES)}
    found: dict[int, str] = {}
    for col in columns:
        idx = targets.get(_normalize(col))
        if idx is not None:
            found[idx] = col
    return found


def _expand_weekly_rollup_to_daily(std_df: pd.DataFrame, weekday_cols: dict[int, str]) -> pd.DataFrame:
    """
    Turn one row per (employee, project, week-ending date) with day-of-week
    columns into one row per real day worked, with that day's actual hours
    and actual calendar date. This lets day-level checks (single-day
    overload, duplicate entries, missing project codes) work correctly
    instead of comparing a whole week's hours against a one-day threshold.
    """
    if std_df.empty or "date" not in std_df.columns or std_df["date"].isna().all():
        return std_df  # no reliable week-ending date to anchor real days to

    day_col_names = set(weekday_cols.values())
    carry_cols = [c for c in std_df.columns if c not in day_col_names]
    expanded_rows: list[dict] = []

    for _, row in std_df.iterrows():
        week_end = row["date"]
        if pd.isna(week_end):
            continue
        # Anchor each weekday column to a real date relative to this row's
        # own week-ending date — don't assume "weekending" always lands on
        # a particular weekday, just use whichever day it actually is.
        week_end_dow = week_end.weekday()  # Monday=0 ... Sunday=6
        base = {c: row[c] for c in carry_cols}
        for weekday_idx, col_name in weekday_cols.items():
            hours = row.get(col_name)
            if pd.isna(hours):
                continue
            new_row = dict(base)
            new_row["date"] = week_end - pd.Timedelta(days=(week_end_dow - weekday_idx))
            new_row["hours_worked"] = float(hours)
            expanded_rows.append(new_row)

    if not expanded_rows:
        return std_df

    return pd.DataFrame(expanded_rows).reset_index(drop=True)
