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


def standardize_dataframe(df: pd.DataFrame, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """
    Rename a raw DataFrame's columns to the standard schema and coerce types.
    Returns the standardized DataFrame plus the ColumnMapping used (for
    transparency in the UI / audit trail).
    """
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

    std_df["source_file"] = source_label
    std_df = std_df.reset_index(drop=True)
    return std_df, mapping


def load_and_standardize(file_bytes: bytes, filename: str, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """Convenience wrapper: load raw bytes straight to a standardized DataFrame."""
    raw_df = load_raw_dataframe(file_bytes, filename)
    std_df, mapping = standardize_dataframe(raw_df, source_label)

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

    return std_df, mapping


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
