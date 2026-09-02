"""
comparison_engine.py
---------------------
The "AI comparison & diff engine" described in the product spec. Pure pandas
logic (no external LLM calls needed) that compares two standardized
timesheet periods and produces:

  - Headcount changes (new hires / departures)
  - Project & sales-rep reassignment flags
  - Hour anomalies: outliers, duplicates/overlaps, spikes/drops, missing codes
  - An executive summary of everything above

All thresholds are configurable via AuditConfig so the tool can be tuned to
a company's specific pay-period length and tolerance for variance.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class AuditConfig:
    max_reasonable_hours: float = 80.0     # flag totals above this (per period) without PTO covering the gap
    min_reasonable_hours: float = 10.0     # flag totals below this (per period) without PTO covering the gap
    max_single_day_hours: float = 16.0     # flag any single day/row above this
    spike_drop_pct_threshold: float = 40.0  # % change vs prior period that triggers a flag
    min_hours_for_spike_check: float = 5.0  # ignore tiny baselines to avoid noisy %s
    regular_hours_cap: float = 40.0        # a "full" week of regular time, for the OT check below
    min_overtime_hours_for_flag: float = 1.0  # flag if OT exceeds this while regular hours are still under the cap


class TimesheetAuditEngine:
    """
    Wraps two standardized DataFrames (previous period & current period) and
    exposes methods to compute every comparison the product spec calls for.
    """

    def __init__(self, df_prev: pd.DataFrame, df_curr: pd.DataFrame,
                 label_prev: str = "Period 1", label_curr: str = "Period 2",
                 config: AuditConfig | None = None):
        self.df_prev = df_prev
        self.df_curr = df_curr
        self.label_prev = label_prev
        self.label_curr = label_curr
        self.config = config or AuditConfig()

        self.agg_prev = self._aggregate_by_employee(df_prev)
        self.agg_curr = self._aggregate_by_employee(df_curr)

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate_by_employee(df: pd.DataFrame) -> pd.DataFrame:
        """Roll raw row-level entries up to one summary row per employee."""
        if df.empty:
            return pd.DataFrame(columns=[
                "employee_name", "total_hours", "total_pto", "regular_hours",
                "overtime_hours", "projects", "sales_reps", "entry_count",
                "primary_project", "primary_sales_rep",
            ])

        def _clean_set(series: pd.Series) -> list[str]:
            vals = sorted({v for v in series if v and str(v).strip() and str(v).lower() != "nan"})
            return vals

        # regular_hours/overtime_hours only exist for formats that actually
        # carry a regular-vs-overtime split in the source data (see
        # parser.py); everywhere else they're NA, which should come through
        # as "no OT data available" (None) rather than blow up.
        grouped = df.groupby("employee_name", dropna=False)
        rows = []
        for name, g in grouped:
            projects = _clean_set(g["project_id"])
            reps = _clean_set(g["sales_rep"])
            g_reg = pd.to_numeric(g["regular_hours"], errors="coerce") if "regular_hours" in g.columns else pd.Series(dtype=float)
            g_ot = pd.to_numeric(g["overtime_hours"], errors="coerce") if "overtime_hours" in g.columns else pd.Series(dtype=float)
            rows.append({
                "employee_name": name,
                "total_hours": round(float(g["hours_worked"].sum()), 2),
                "total_pto": round(float(g["pto_hours"].sum()), 2),
                "regular_hours": round(float(g_reg.sum()), 2) if g_reg.notna().any() else None,
                "overtime_hours": round(float(g_ot.sum()), 2) if g_ot.notna().any() else None,
                "projects": projects,
                "sales_reps": reps,
                "entry_count": int(len(g)),
                "primary_project": projects[0] if projects else "",
                "primary_sales_rep": reps[0] if reps else "",
            })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 1. Headcount changes
    # ------------------------------------------------------------------
    def compute_headcount_changes(self) -> dict:
        prev_names = set(self.agg_prev["employee_name"]) if not self.agg_prev.empty else set()
        curr_names = set(self.agg_curr["employee_name"]) if not self.agg_curr.empty else set()

        new_hire_names = curr_names - prev_names
        departure_names = prev_names - curr_names
        retained_names = prev_names & curr_names

        new_hires = self.agg_curr[self.agg_curr["employee_name"].isin(new_hire_names)].copy()
        departures = self.agg_prev[self.agg_prev["employee_name"].isin(departure_names)].copy()

        new_hires = new_hires.rename(columns={"total_hours": "hours_in_first_period"})
        departures = departures.rename(columns={"total_hours": "hours_in_final_period"})

        return {
            "new_hires": new_hires.to_dict(orient="records"),
            "departures": departures.to_dict(orient="records"),
            "retained_count": len(retained_names),
            "new_hire_count": len(new_hire_names),
            "departure_count": len(departure_names),
        }

    # ------------------------------------------------------------------
    # 2. Project & sales rep reassignment tracking
    # ------------------------------------------------------------------
    def compute_reassignments(self) -> list[dict]:
        flags = []
        prev_lookup = self.agg_prev.set_index("employee_name") if not self.agg_prev.empty else pd.DataFrame()
        curr_lookup = self.agg_curr.set_index("employee_name") if not self.agg_curr.empty else pd.DataFrame()

        common = set(prev_lookup.index) & set(curr_lookup.index) if not prev_lookup.empty and not curr_lookup.empty else set()

        for name in sorted(common):
            prev_row = prev_lookup.loc[name]
            curr_row = curr_lookup.loc[name]

            prev_projects = set(prev_row["projects"])
            curr_projects = set(curr_row["projects"])
            prev_reps = set(prev_row["sales_reps"])
            curr_reps = set(curr_row["sales_reps"])

            project_changed = prev_projects != curr_projects
            rep_changed = prev_reps != curr_reps

            if project_changed or rep_changed:
                flags.append({
                    "employee_name": name,
                    "project_changed": project_changed,
                    "previous_projects": sorted(prev_projects),
                    "current_projects": sorted(curr_projects),
                    "sales_rep_changed": rep_changed,
                    "previous_sales_reps": sorted(prev_reps),
                    "current_sales_reps": sorted(curr_reps),
                })
        return flags

    # ------------------------------------------------------------------
    # 3. Hour anomalies
    # ------------------------------------------------------------------
    def detect_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []

        anomalies += self._detect_period_outliers(self.agg_curr, self.label_curr)
        anomalies += self._detect_single_day_outliers(self.df_curr, self.label_curr)
        anomalies += self._detect_duplicate_entries(self.df_curr, self.label_curr)
        anomalies += self._detect_low_regular_high_overtime(self.agg_curr, self.label_curr)
        # Per Darren's 9/2/2026 feedback, three rules are disabled for now
        # (methods kept below in case they're wanted back later):
        #   - "conflicting entries" (_detect_conflicting_entries)
        #   - "missing project code" (_detect_missing_project_codes)
        #   - "big change in hours" (_detect_spikes_and_drops)

        # Sort most severe / most recent first for readability
        severity_rank = {"high": 0, "medium": 1, "low": 2}
        anomalies.sort(key=lambda a: severity_rank.get(a.get("severity", "low"), 3))
        return anomalies

    def _detect_period_outliers(self, agg: pd.DataFrame, label: str) -> list[dict]:
        cfg = self.config
        out = []
        for _, row in agg.iterrows():
            effective_hours = row["total_hours"]  # PTO already excluded from hours_worked upstream
            covered_low = effective_hours + row["total_pto"]
            if effective_hours > cfg.max_reasonable_hours:
                out.append({
                    "type": "excessive_hours",
                    "severity": "high",
                    "employee_name": row["employee_name"],
                    "period": label,
                    "detail": f"Logged {effective_hours} hours in {label}, above the "
                              f"{cfg.max_reasonable_hours}-hour threshold.",
                    "value": effective_hours,
                })
            elif covered_low < cfg.min_reasonable_hours:
                out.append({
                    "type": "insufficient_hours",
                    "severity": "medium",
                    "employee_name": row["employee_name"],
                    "period": label,
                    "detail": f"Only {effective_hours} hours logged in {label} "
                              f"(with {row['total_pto']} PTO hours), below the "
                              f"{cfg.min_reasonable_hours}-hour threshold. Verify time was recorded.",
                    "value": effective_hours,
                })
        return out

    def _detect_single_day_outliers(self, df: pd.DataFrame, label: str) -> list[dict]:
        cfg = self.config
        out = []
        if df.empty or "date" not in df.columns or df["date"].isna().all():
            return out  # no date granularity available in this dataset

        daily = (
            df.dropna(subset=["date"])
              .groupby(["employee_name", "date"], dropna=False)["hours_worked"]
              .sum()
              .reset_index()
        )
        flagged = daily[daily["hours_worked"] > cfg.max_single_day_hours]
        for _, row in flagged.iterrows():
            out.append({
                "type": "single_day_overload",
                "severity": "high",
                "employee_name": row["employee_name"],
                "period": label,
                "detail": f"Logged {row['hours_worked']} hours on "
                          f"{row['date'].date() if pd.notna(row['date']) else 'an entry'} "
                          f"in a single day (threshold: {cfg.max_single_day_hours}h).",
                "value": float(row["hours_worked"]),
            })
        return out

    def _detect_duplicate_entries(self, df: pd.DataFrame, label: str) -> list[dict]:
        out = []
        if df.empty:
            return out

        key_cols = [c for c in ["employee_name", "date", "project_id", "activity_type"] if c in df.columns]
        if "date" not in key_cols or df["date"].isna().all():
            return out  # can't meaningfully detect duplicate day entries without dates

        # Exact duplicate rows (same employee/date/project/hours) -> likely double-entered
        dup_exact_cols = key_cols + ["hours_worked"]
        exact_dupes = df[df.duplicated(subset=dup_exact_cols, keep=False)]
        seen = set()
        for _, row in exact_dupes.iterrows():
            sig = tuple(row[c] for c in dup_exact_cols)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "type": "duplicate_entry",
                "severity": "medium",
                "employee_name": row["employee_name"],
                "period": label,
                "detail": f"Identical entry appears more than once for project "
                          f"'{row.get('project_id', '')}' on "
                          f"{row['date'].date() if pd.notna(row['date']) else 'the same date'} "
                          f"({row['hours_worked']}h each occurrence).",
                "value": float(row["hours_worked"]),
            })

        return out

    def _detect_conflicting_entries(self, df: pd.DataFrame, label: str) -> list[dict]:
        """
        Disabled per Darren's 9/2/2026 feedback (not called from
        detect_anomalies) — kept here in case the rule is wanted back.
        Flags cases where the same employee has different hour amounts
        entered for the same project on the same day.
        """
        out = []
        if df.empty:
            return out

        key_cols = [c for c in ["employee_name", "date", "project_id", "activity_type"] if c in df.columns]
        if "date" not in key_cols or df["date"].isna().all():
            return out

        grouped = df.groupby(key_cols, dropna=False)["hours_worked"].nunique().reset_index()
        conflicting_keys = grouped[grouped["hours_worked"] > 1]
        for _, key_row in conflicting_keys.iterrows():
            mask = np.logical_and.reduce([df[c] == key_row[c] for c in key_cols])
            conflicting_rows = df[mask]
            total_conflicting = conflicting_rows["hours_worked"].sum()
            date_val = key_row["date"]
            out.append({
                "type": "overlapping_conflicting_entry",
                "severity": "high",
                "employee_name": key_row["employee_name"],
                "period": label,
                "detail": f"Multiple different hour values logged for project "
                          f"'{key_row.get('project_id', '')}' on "
                          f"{date_val.date() if pd.notna(date_val) else 'the same date'} "
                          f"(entries: {list(conflicting_rows['hours_worked'])}, summing to {total_conflicting}h). "
                          f"Possible overlapping/conflicting time entries.",
                "value": float(total_conflicting),
            })

        return out

    def _detect_low_regular_high_overtime(self, agg: pd.DataFrame, label: str) -> list[dict]:
        """
        Added per Darren's 9/2/2026 feedback: flag employees who logged
        under a full week of regular time but still have meaningful
        overtime — a red flag for OT being applied somewhere it shouldn't
        (e.g. daily-OT rules kicking in on a short week). Only fires for
        formats where the source data actually distinguishes regular vs.
        overtime hours (see parser.py) — everywhere else, regular_hours/
        overtime_hours come back as None and this check is skipped rather
        than guessed at.
        """
        cfg = self.config
        out = []
        if agg.empty or "regular_hours" not in agg.columns or "overtime_hours" not in agg.columns:
            return out

        for _, row in agg.iterrows():
            regular = row.get("regular_hours")
            overtime = row.get("overtime_hours")
            if regular is None or overtime is None or pd.isna(regular) or pd.isna(overtime):
                continue  # this format doesn't carry a regular/OT split
            if regular < cfg.regular_hours_cap and overtime > cfg.min_overtime_hours_for_flag:
                out.append({
                    "type": "low_regular_high_overtime",
                    "severity": "medium",
                    "employee_name": row["employee_name"],
                    "period": label,
                    "detail": f"Only {regular}h of regular time logged in {label}, but "
                              f"{overtime}h of overtime — worth checking why OT was applied "
                              f"on a below-full week.",
                    "value": float(overtime),
                })
        return out

    def _detect_missing_project_codes(self, df: pd.DataFrame, label: str) -> list[dict]:
        out = []
        if df.empty:
            return out
        missing = df[(df["project_id"].isna()) | (df["project_id"].astype(str).str.strip() == "")]
        missing = missing[missing["hours_worked"] > 0]
        grouped = missing.groupby("employee_name")["hours_worked"].sum().reset_index()
        for _, row in grouped.iterrows():
            out.append({
                "type": "missing_project_code",
                "severity": "medium",
                "employee_name": row["employee_name"],
                "period": label,
                "detail": f"{row['hours_worked']}h logged with no project code assigned in {label}.",
                "value": float(row["hours_worked"]),
            })
        return out

    def _detect_spikes_and_drops(self) -> list[dict]:
        cfg = self.config
        out = []
        if self.agg_prev.empty or self.agg_curr.empty:
            return out

        prev_lookup = self.agg_prev.set_index("employee_name")["total_hours"]
        curr_lookup = self.agg_curr.set_index("employee_name")["total_hours"]
        common = set(prev_lookup.index) & set(curr_lookup.index)

        for name in sorted(common):
            prev_hours = prev_lookup[name]
            curr_hours = curr_lookup[name]
            if prev_hours < cfg.min_hours_for_spike_check:
                continue
            pct_change = ((curr_hours - prev_hours) / prev_hours) * 100 if prev_hours else 0
            if abs(pct_change) >= cfg.spike_drop_pct_threshold:
                direction = "spike" if pct_change > 0 else "drop"
                out.append({
                    "type": f"hours_{direction}",
                    "severity": "high" if abs(pct_change) >= 60 else "medium",
                    "employee_name": name,
                    "period": f"{self.label_prev} -> {self.label_curr}",
                    "detail": f"Hours went from {prev_hours}h to {curr_hours}h "
                              f"({pct_change:+.1f}%), a notable {direction} vs. baseline.",
                    "value": round(pct_change, 1),
                })
        return out

    # ------------------------------------------------------------------
    # 4. Executive summary
    # ------------------------------------------------------------------
    def build_summary(self) -> dict:
        headcount = self.compute_headcount_changes()
        reassignments = self.compute_reassignments()
        anomalies = self.detect_anomalies()

        total_hours_prev = round(float(self.agg_prev["total_hours"].sum()), 2) if not self.agg_prev.empty else 0.0
        total_hours_curr = round(float(self.agg_curr["total_hours"].sum()), 2) if not self.agg_curr.empty else 0.0
        hours_delta = round(total_hours_curr - total_hours_prev, 2)
        hours_delta_pct = round((hours_delta / total_hours_prev) * 100, 1) if total_hours_prev else 0.0

        severity_counts = {"high": 0, "medium": 0, "low": 0}
        for a in anomalies:
            severity_counts[a.get("severity", "low")] = severity_counts.get(a.get("severity", "low"), 0) + 1

        return {
            "label_prev": self.label_prev,
            "label_curr": self.label_curr,
            "total_hours_prev": total_hours_prev,
            "total_hours_curr": total_hours_curr,
            "hours_delta": hours_delta,
            "hours_delta_pct": hours_delta_pct,
            "active_headcount_prev": int(len(self.agg_prev)),
            "active_headcount_curr": int(len(self.agg_curr)),
            "new_hire_count": headcount["new_hire_count"],
            "departure_count": headcount["departure_count"],
            "retained_count": headcount["retained_count"],
            "reassignment_count": len(reassignments),
            "anomaly_count": len(anomalies),
            "anomaly_severity_counts": severity_counts,
        }

    # ------------------------------------------------------------------
    # Full report bundle used by the API layer
    # ------------------------------------------------------------------
    def build_full_report(self) -> dict:
        headcount = self.compute_headcount_changes()

        # Attach each employee's prior-period hours alongside their current
        # total so the dashboard can show "this week" vs. "last week"
        # side-by-side, instead of requiring a trip to a different tab.
        prev_hours_by_name = (
            self.agg_prev.set_index("employee_name")["total_hours"]
            if not self.agg_prev.empty else pd.Series(dtype=float)
        )
        employees_curr = self.agg_curr.to_dict(orient="records")
        for emp in employees_curr:
            prev_hours = prev_hours_by_name.get(emp["employee_name"])
            emp["hours_prev"] = None if prev_hours is None or pd.isna(prev_hours) else float(prev_hours)

        return {
            "summary": self.build_summary(),
            "new_hires": headcount["new_hires"],
            "departures": headcount["departures"],
            "reassignments": self.compute_reassignments(),
            "anomalies": self.detect_anomalies(),
            "employees_curr": employees_curr,
            "employees_prev": self.agg_prev.to_dict(orient="records"),
        }
