"""
excel_exporter.py
=================
Salary export (OpenPyXL). Two tabs:

  * "Salary Statement"  -> Sl.No | Employee ID | Name | Salary | Present Days |
        Leaves | Late Days | Leave Deduction | Late Penalty | Total Deduction |
        Net Salary
     Total Deduction and Net Salary are live Excel formulas, so a manual
     correction to a deduction cell recalculates the row and the totals.
  * "Daily Attendance"  -> every day's punch behind each deduction.
"""

from __future__ import annotations

from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_NAVY = "1F3A5F"
_HEAD_FILL = PatternFill("solid", fgColor=_NAVY)
_HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
_TITLE = Font(color=_NAVY, bold=True, size=14)
_SUB = Font(color="555555", size=9, italic=True)
_MONEY = "#,##0"
_RED = Font(color="B00020", bold=True)
_GREEN = Font(color="1B7F3B", bold=True)
_THIN = Side(style="thin", color="D0D0D0")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_C = Alignment(horizontal="center", vertical="center")
_L = Alignment(horizontal="left", vertical="center")
_R = Alignment(horizontal="right", vertical="center")

HEADERS = ["Sl.No", "Employee ID", "Name", "Salary", "Present Days", "Leaves",
           "Late Days", "Leave Deduction", "Late Penalty", "Total Deduction",
           "Net Salary"]


def _header(ws, row, headers):
    for col, text in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=text)
        c.fill, c.font, c.alignment, c.border = _HEAD_FILL, _HEAD_FONT, _C, _BORDER


def _autofit(ws, max_width=36):
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=0)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 9), max_width)


def policy_text(cfg: dict) -> str:
    return (f"Policy: leaves ≤{cfg['leave_tier1_max']} → ₹{cfg['leave_tier1_rate']}/day · "
            f"≤{cfg['leave_tier2_max']} → ₹{cfg['leave_tier2_rate']}/day · "
            f">{cfg['leave_tier2_max']} → salary/{cfg['leave_tier3_divisor']} per day · "
            f"every {cfg['late_block_days']} late days → ₹{cfg['late_block_penalty']}")


def export_payout(path, rows, month, year, daily_logs=None, cfg=None,
                  org_name="Bethune Nursing Home", cutoff_day=None):
    """rows: EmployeePayroll.to_summary() dicts. daily_logs: {employee_id: [day rows]}."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Salary Statement"
    ws.sheet_view.showGridLines = False

    period = datetime(year, month, 1).strftime("%B %Y")
    if cutoff_day:
        period += f" (days 1–{cutoff_day})"
    ws.cell(row=1, column=1, value=org_name).font = _TITLE
    ws.cell(row=2, column=1,
            value=f"Salary Statement — {period}   ·   generated {datetime.now():%d-%m-%Y %H:%M}").font = _SUB
    if cfg:
        ws.cell(row=3, column=1, value=policy_text(cfg)).font = _SUB

    H = 5
    _header(ws, H, HEADERS)
    first = H + 1
    r = H
    for i, row in enumerate(rows, start=1):
        r += 1
        values = [i, row["employee_id"], row["name"], row["salary"], row["present_days"],
                  row["leaves"], row["late_count"], row["leave_deduction"], row["late_penalty"]]
        for col, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=col, value=v)
            c.border = _BORDER
            c.alignment = _L if col == 3 else (_R if col in (4, 8, 9) else _C)
            if col in (4, 8, 9):
                c.number_format = _MONEY
            if col in (8, 9) and (v or 0) > 0:
                c.font = _RED
        tot = ws.cell(row=r, column=10, value=f"=H{r}+I{r}")          # Total Deduction
        net = ws.cell(row=r, column=11, value=f"=MAX(0,D{r}-J{r})")   # Net Salary
        for c, font in ((tot, Font(bold=True)), (net, _GREEN)):
            c.number_format, c.alignment, c.border, c.font = _MONEY, _R, _BORDER, font
    last = r

    r += 1
    ws.cell(row=r, column=3, value="TOTAL").font = Font(bold=True)
    for col in (4, 8, 9, 10, 11):
        L = get_column_letter(col)
        c = ws.cell(row=r, column=col, value=f"=SUM({L}{first}:{L}{max(last, first)})")
        c.number_format, c.font, c.alignment, c.border = _MONEY, Font(bold=True), _R, _BORDER
    _autofit(ws)
    ws.column_dimensions["C"].width = 28
    ws.freeze_panes = ws.cell(row=first, column=1)

    # ---- Daily Attendance ----
    ws2 = wb.create_sheet("Daily Attendance")
    ws2.sheet_view.showGridLines = False
    ws2.cell(row=1, column=1, value=f"{org_name} — Daily Attendance — {period}").font = _TITLE
    _header(ws2, 3, ["Employee ID", "Name", "Date", "Shift", "Check-In",
                     "Check-Out", "Minutes Late", "Status"])
    names = {x["employee_id"]: x["name"] for x in rows}
    ar = 3
    for eid, logs in (daily_logs or {}).items():
        for d in logs:
            ar += 1
            vals = [eid, names.get(eid, ""), d["date"], d["shift"], d["check_in"],
                    d["check_out"], d["late_minutes"], d["status"]]
            for col, v in enumerate(vals, start=1):
                c = ws2.cell(row=ar, column=col, value=v)
                c.border, c.alignment = _BORDER, (_L if col == 2 else _C)
                if col == 8 and v == "Late":
                    c.font = _RED
    _autofit(ws2)
    ws2.freeze_panes = ws2.cell(row=4, column=1)

    wb.save(path)
    return path


if __name__ == "__main__":
    import os, tempfile
    from openpyxl import load_workbook
    from payroll_engine import merge_config
    rows = [{"employee_id": "53", "name": "G DRAKSHA VALLI", "salary": 15000,
             "present_days": 20, "leaves": 3, "late_count": 4,
             "leave_deduction": 300, "late_penalty": 50}]
    logs = {"53": [{"date": "2026-11-02", "shift": "Morning", "check_in": "09:40",
                    "check_out": "", "late_minutes": 15, "status": "Late"}]}
    out = os.path.join(tempfile.gettempdir(), "_bnh_export_test.xlsx")
    export_payout(out, rows, 11, 2026, logs, merge_config(None), cutoff_day=16)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Salary Statement", "Daily Attendance"]
    assert wb["Salary Statement"]["K6"].value == "=MAX(0,D6-J6)"
    print("excel_exporter self-test OK ->", out)
