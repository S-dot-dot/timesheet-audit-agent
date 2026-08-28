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
        "employee name", "employee", "name", "emp name", "staff name",
        "worker", "full name", "team member", "resource", "resource name",
    ],
    "employee_id": [
        "employee id", "emp id", "employee number", "staff id", "id",
        "worker id", "badge id", "personnel id",
    ],
    "hours_worked": [
        "hours worked", "hours", "total hours", "hrs", "hours logged",
        "worked hours", "time worked", "hrs worked", "duration", "hours total",
    ],
    "project_id": [
        "project id", "project", "project code", "project name", "proj id",
        "proj", "job code", "cost center", "task", "wbs",
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
    return re.sub(r"[^a-z0-9]", "", str(text).lower().strip())


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

        # 2. Fallback: substring match (e.g. "Employee Full Name" contains "name")
        if not match:
            for norm_col, original_col in normalized_lookup.items():
                if original_col in used_source_cols:
                    continue
                for norm_alias in normalized_aliases:
                    if len(norm_alias) >= 3 and (norm_alias in norm_col or norm_col in norm_alias):
                        match = original_col
                        break
                if match:
                    break

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

    if mapping.missing_required:
        raise TimesheetParseError(
            f"'{source_label}' is missing required column(s): "
            f"{', '.join(mapping.missing_required)}. "
            f"Detected columns were: {', '.join(df.columns)}"
        )

    rename_map = {source_col: std_field for std_field, source_col in mapping.mapping.items()}
    std_df = df.rename(columns=rename_map).copy()

    # Ensure every standard field exists, even if not present in source
    for std_field in STANDARD_FIELDS:
        if std_field not in std_df.columns:
            std_df[std_field] = pd.NA

    # --- Type coercion & cleanup ---
    std_df["employee_name"] = std_df["employee_name"].astype(str).str.strip()
    std_df = std_df[std_df["employee_name"].str.len() > 0]
    std_df = std_df[~std_df["employee_name"].str.lower().isin(["nan", "none", ""])]

    std_df["hours_worked"] = pd.to_numeric(std_df["hours_worked"], errors="coerce").fillna(0.0)
    std_df["pto_hours"] = pd.to_numeric(std_df["pto_hours"], errors="coerce").fillna(0.0)

    if "date" in std_df.columns:
        std_df["date"] = pd.to_datetime(std_df["date"], errors="coerce")

    for col in ["project_id", "sales_rep", "employee_id", "notes"]:
        std_df[col] = std_df[col].astype(str).str.strip()
        std_df[col] = std_df[col].replace({"nan": "", "None": "", "<NA>": ""})

    std_df["source_file"] = source_label
    std_df = std_df.reset_index(drop=True)
    return std_df, mapping


def load_and_standardize(file_bytes: bytes, filename: str, source_label: str) -> tuple[pd.DataFrame, ColumnMapping]:
    """Convenience wrapper: load raw bytes straight to a standardized DataFrame."""
    raw_df = load_raw_dataframe(file_bytes, filename)
    return standardize_dataframe(raw_df, source_label)
