"""
query_engine.py
----------------
Powers the "Interactive Query Box" from the PRD — lets a user type questions
like:
  - "Why did John Doe's hours drop?"
  - "Show me all employees assigned to Project P-100"
  - "Who are the new hires?"
  - "What anomalies were found for Grace Lin?"
  - "Total hours for Bob Smith"

This is a deterministic, rule-based NL layer built directly on the audit
report already computed by TimesheetAuditEngine — no external API key is
required for it to work out of the box.

If an ANTHROPIC_API_KEY environment variable IS present, `answer_question`
will transparently hand off to Claude for open-ended questions the rule
engine can't confidently classify, using the report JSON as grounding
context. This is optional and guarded so the app runs perfectly without it.
"""
from __future__ import annotations

import os
import re
from difflib import get_close_matches


def _find_employee(question: str, known_names: list[str]) -> str | None:
    """Fuzzy-match an employee name mentioned anywhere in the question."""
    q_lower = question.lower()

    # 1. Direct substring match (handles most real cases cleanly)
    matches = [n for n in known_names if n.lower() in q_lower]
    if matches:
        return max(matches, key=len)  # prefer the longest / most specific match

    # 2. Token-based fuzzy match as a fallback (typos, partial names)
    tokens = re.findall(r"[a-zA-Z']+", question)
    for n in known_names:
        name_tokens = n.lower().split()
        if any(get_close_matches(t.lower(), name_tokens, n=1, cutoff=0.85) for t in tokens):
            return n
    return None


def _find_project(question: str, known_projects: list[str]) -> str | None:
    q_lower = question.lower()
    matches = [p for p in known_projects if p and p.lower() in q_lower]
    if matches:
        return max(matches, key=len)
    return None


def answer_question(question: str, report: dict) -> dict:
    """
    Main entry point. Returns:
        {
          "answer": "<natural language answer>",
          "table": [ {...}, ... ]   # optional supporting rows for the UI
        }
    """
    q = question.strip()
    if not q:
        return {"answer": "Please type a question about the uploaded timesheets.", "table": []}

    q_lower = q.lower()
    employees_curr = report.get("employees_curr", [])
    employees_prev = report.get("employees_prev", [])
    anomalies = report.get("anomalies", [])
    summary = report.get("summary", {})

    known_names = sorted({e["employee_name"] for e in employees_curr + employees_prev})
    known_projects = sorted({p for e in employees_curr + employees_prev for p in e.get("projects", [])})

    employee = _find_employee(q, known_names)
    project = _find_project(q, known_projects)

    # ---------------- Intent: new hires ----------------
    if re.search(r"\bnew hire", q_lower) or ("who" in q_lower and "hire" in q_lower):
        hires = report.get("new_hires", [])
        if not hires:
            return {"answer": "No new hires were detected between the two periods.", "table": []}
        names = ", ".join(h["employee_name"] for h in hires)
        return {
            "answer": f"There {'is' if len(hires) == 1 else 'are'} {len(hires)} new hire(s) in the "
                      f"latest period: {names}.",
            "table": hires,
        }

    # ---------------- Intent: departures ----------------
    if re.search(r"\bdepart|\bleft\b|\bterminat|no longer", q_lower):
        departures = report.get("departures", [])
        if not departures:
            return {"answer": "No departures were detected between the two periods.", "table": []}
        names = ", ".join(d["employee_name"] for d in departures)
        return {
            "answer": f"{len(departures)} employee(s) left between the two periods: {names}.",
            "table": departures,
        }

    # ---------------- Intent: reassignments ----------------
    if re.search(r"reassign|switch(ed)? project|changed project|new rep", q_lower) and not employee:
        reassignments = report.get("reassignments", [])
        if not reassignments:
            return {"answer": "No project or sales rep reassignments were detected.", "table": []}
        names = ", ".join(r["employee_name"] for r in reassignments)
        return {
            "answer": f"{len(reassignments)} employee(s) had a project or sales rep change: {names}.",
            "table": reassignments,
        }

    # ---------------- Intent: employees on a specific project ----------------
    if project and re.search(r"show|list|who|assigned|working on|employees", q_lower):
        matches = [e for e in employees_curr if project in e.get("projects", [])]
        if not matches:
            return {"answer": f"No employees in the current period are assigned to project '{project}'.", "table": []}
        names = ", ".join(m["employee_name"] for m in matches)
        return {
            "answer": f"{len(matches)} employee(s) are currently assigned to project '{project}': {names}.",
            "table": matches,
        }

    # ---------------- Intent: why did hours change / anomalies for an employee ----------------
    if employee and re.search(r"why|drop|spike|increase|decrease|anomal|issue|flag|wrong|mistake", q_lower):
        emp_anomalies = [a for a in anomalies if a.get("employee_name") == employee]
        prev_e = next((e for e in employees_prev if e["employee_name"] == employee), None)
        curr_e = next((e for e in employees_curr if e["employee_name"] == employee), None)

        parts = []
        if prev_e and curr_e:
            delta = curr_e["total_hours"] - prev_e["total_hours"]
            pct = (delta / prev_e["total_hours"] * 100) if prev_e["total_hours"] else 0
            direction = "increased" if delta > 0 else "decreased" if delta < 0 else "stayed flat"
            parts.append(
                f"{employee}'s hours {direction} from {prev_e['total_hours']}h to {curr_e['total_hours']}h "
                f"({pct:+.1f}%)."
            )
        elif curr_e and not prev_e:
            parts.append(f"{employee} is a new hire this period with {curr_e['total_hours']}h logged.")
        elif prev_e and not curr_e:
            parts.append(f"{employee} does not appear in the current period (departed) — logged "
                          f"{prev_e['total_hours']}h previously.")

        if emp_anomalies:
            parts.append("Related flags: " + "; ".join(a["detail"] for a in emp_anomalies))
        elif prev_e and curr_e:
            parts.append("No specific anomaly flags were raised for this change — it may reflect a normal "
                          "schedule change, reduced project scope, or approved leave.")

        return {"answer": " ".join(parts), "table": emp_anomalies}

    # ---------------- Intent: total hours for an employee ----------------
    if employee and re.search(r"hours|total|how many|worked", q_lower):
        prev_e = next((e for e in employees_prev if e["employee_name"] == employee), None)
        curr_e = next((e for e in employees_curr if e["employee_name"] == employee), None)
        bits = []
        if curr_e:
            bits.append(f"{summary.get('label_curr', 'Current period')}: {curr_e['total_hours']}h "
                        f"across project(s) {', '.join(curr_e.get('projects', [])) or 'none logged'}.")
        if prev_e:
            bits.append(f"{summary.get('label_prev', 'Previous period')}: {prev_e['total_hours']}h.")
        if not bits:
            return {"answer": f"I couldn't find timesheet data for {employee}.", "table": []}
        return {"answer": f"{employee} — " + " ".join(bits), "table": [x for x in [curr_e, prev_e] if x]}

    # ---------------- Intent: general anomaly listing ----------------
    if re.search(r"anomal|flag|problem|issue|mistake|error", q_lower):
        if employee:
            emp_anomalies = [a for a in anomalies if a.get("employee_name") == employee]
            if not emp_anomalies:
                return {"answer": f"No anomalies were flagged for {employee}.", "table": []}
            return {
                "answer": f"{len(emp_anomalies)} anomaly flag(s) for {employee}: " +
                          " | ".join(a["detail"] for a in emp_anomalies),
                "table": emp_anomalies,
            }
        if not anomalies:
            return {"answer": "No anomalies were detected in this audit.", "table": []}
        return {
            "answer": f"{len(anomalies)} total anomalies were flagged "
                      f"({summary.get('anomaly_severity_counts', {})}). "
                      f"Top items: " + " | ".join(a["detail"] for a in anomalies[:3]),
            "table": anomalies,
        }

    # ---------------- Intent: general summary ----------------
    if re.search(r"summary|overview|how many employees|headcount|total hours", q_lower):
        return {
            "answer": (
                f"{summary.get('label_prev')}: {summary.get('active_headcount_prev')} employees, "
                f"{summary.get('total_hours_prev')}h total. "
                f"{summary.get('label_curr')}: {summary.get('active_headcount_curr')} employees, "
                f"{summary.get('total_hours_curr')}h total "
                f"({summary.get('hours_delta_pct')}% change). "
                f"{summary.get('new_hire_count')} new hire(s), {summary.get('departure_count')} departure(s), "
                f"{summary.get('anomaly_count')} anomaly flag(s)."
            ),
            "table": [],
        }

    # ---------------- Fallback: try an LLM if configured, else guide the user ----------------
    llm_answer = _try_llm_fallback(q, report)
    if llm_answer:
        return {"answer": llm_answer, "table": []}

    if employee:
        curr_e = next((e for e in employees_curr if e["employee_name"] == employee), None)
        prev_e = next((e for e in employees_prev if e["employee_name"] == employee), None)
        chosen = curr_e or prev_e
        return {
            "answer": f"Here's what I have on {employee}: "
                      f"{chosen['total_hours']}h, project(s): {', '.join(chosen.get('projects', [])) or 'none'}, "
                      f"sales rep(s): {', '.join(chosen.get('sales_reps', [])) or 'none'}.",
            "table": [chosen],
        }

    return {
        "answer": (
            "I'm not sure how to answer that yet. Try asking things like: "
            "\"Who are the new hires?\", \"Show employees on Project P-100\", "
            "\"Why did Jane Doe's hours drop?\", or \"Total hours for John Smith\"."
        ),
        "table": [],
    }


def _try_llm_fallback(question: str, report: dict) -> str | None:
    """
    Optional enhancement: if ANTHROPIC_API_KEY is set in the environment,
    use Claude to answer open-ended questions the rule engine couldn't
    classify, grounded in the computed audit report. Silently returns None
    on any failure so the rest of the app is unaffected.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import json
        import anthropic  # optional dependency, only needed for this fallback

        client = anthropic.Anthropic(api_key=api_key)
        context = json.dumps(report, default=str)[:12000]  # keep prompt bounded
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    "You are a timesheet audit assistant. Using ONLY the JSON audit report below, "
                    "answer the user's question concisely and factually. If the data doesn't contain "
                    "the answer, say so.\n\nAUDIT REPORT JSON:\n" + context +
                    "\n\nQUESTION: " + question
                ),
            }],
        )
        return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text").strip() or None
    except Exception:  # noqa: BLE001 - fallback must never crash the request
        return None
