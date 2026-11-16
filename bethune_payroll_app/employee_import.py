"""
employee_import.py
==================
Read an employee list (Employee ID, Name, Salary) from an Excel (.xlsx) or CSV
file so it can be loaded into the SQLite `employees` table.

The header row is found automatically (within the first 10 rows of each sheet)
by matching column names, so extra columns, a title row above the header, or a
different column order are all fine. The first sheet that has all three
columns is used.
"""

from __future__ import annotations

import csv
import os
import re

_ALIASES = {
    "employee_id": ["employee id", "emp id", "empid", "employee code", "emp code",
                    "employee no", "emp no", "id", "code", "device id", "staff id"],
    "name": ["name", "employee name", "staff name", "emp name"],
    "salary": ["salary", "monthly salary", "gross salary", "gross", "basic salary"],
}


def _norm(v) -> str:
    s = str(v or "").strip().lower().replace(".", "").replace("_", " ")
    return re.sub(r"\s+", " ", s)


def _find_columns(row) -> dict[str, int]:
    cols: dict[str, int] = {}
    labels = [_norm(c) for c in row]
    for field, aliases in _ALIASES.items():
        for alias in aliases:                     # alias order = priority
            if alias in labels:
                cols[field] = labels.index(alias)
                break
    return cols


def _rows_to_records(rows: list[list]) -> tuple[list[dict], int] | None:
    """Locate the header and turn the rows below it into records."""
    for h, row in enumerate(rows[:10]):
        cols = _find_columns(row)
        if len(cols) == 3:
            out = []
            for i, r in enumerate(rows[h + 1:], start=h + 2):   # 1-based sheet row
                get = lambda f: r[cols[f]] if cols[f] < len(r) else None  # noqa: E731
                eid, name, sal = get("employee_id"), get("name"), get("salary")
                if all(v in (None, "") for v in (eid, name, sal)):
                    continue                                    # blank line
                if _norm(name) in ("total", "grand total"):
                    continue
                out.append({"employee_id": eid, "name": name, "salary": sal, "_row": i})
            return out, h + 1
    return None


def read_employee_file(path: str) -> dict:
    """Return {"sheet", "header_row", "rows": [{employee_id, name, salary, _row}]}."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [list(r) for r in csv.reader(f)]
        found = _rows_to_records(rows)
        if found:
            return {"sheet": "CSV", "header_row": found[1], "rows": found[0]}
    elif ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            for ws in wb.worksheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                found = _rows_to_records(rows)
                if found:
                    return {"sheet": ws.title, "header_row": found[1], "rows": found[0]}
        finally:
            wb.close()
    else:
        raise ValueError("Please choose an Excel (.xlsx) or CSV file.")

    raise ValueError(
        "Couldn't find the columns 'Employee ID', 'Name' and 'Salary' in this file. "
        "Make sure the header row has those three column names."
    )


if __name__ == "__main__":
    import tempfile
    import openpyxl
    p = os.path.join(tempfile.gettempdir(), "_bnh_import_test.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bethune Nursing Home staff list"])           # title row
    ws.append(["Sl.No", "Employee ID", "Name", "Salary"])     # header on row 2
    ws.append([1, 53, "G DRAKSHA VALLI", 15000])
    ws.append([2, 4.0, "GANTA PRASAD", "19,200"])
    ws.append([None, None, None, None])
    ws.append([None, None, "TOTAL", 34200])
    wb.save(p)
    res = read_employee_file(p)
    assert res["header_row"] == 2 and len(res["rows"]) == 2, res
    print("employee_import self-test OK:", res)
