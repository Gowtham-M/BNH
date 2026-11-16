# Bethune Nursing Home — Attendance & Payroll

Reads biometric punches from the eSSL **eTimeTrackLite** database (`.mdb`,
read-only) and calculates monthly salary deductions for leaves and late days.
Employees, salaries, waivers and the policy live in a local **SQLite** file that
the app creates automatically.

## Run it

```bash
cd bethune_payroll_app
.venv/bin/python server.py        # opens http://127.0.0.1:8765 in your browser
```

First-time setup on a new machine:

```bash
brew install mdbtools                       # macOS only (reads the .mdb)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Desktop window instead of the browser: `.venv/bin/python main.py`.
Windows executable: run `build_windows.bat` on a Windows PC -> `dist\BethunePayroll.exe`.

## Where the data lives

| Data | Stored in |
|---|---|
| Employees (ID, Name, Salary) | SQLite `employees` table |
| Excused days (waivers) | SQLite `waivers` table |
| Policy settings + `.mdb` path | SQLite `config` table |
| Punches (timestamps) | eSSL `.mdb`, read-only |

SQLite file: `%APPDATA%\BethunePayroll\bethune_store.sqlite` (Windows) or
`~/.config/BethunePayroll/bethune_store.sqlite` (macOS). Back it up by copying
that one file.

## The three tabs

1. **Employees & Salaries** — Employee ID, Name, Salary.
   * **Import from Excel** (`.xlsx` or `.csv`): the sheet needs columns named
     `Employee ID`, `Name`, `Salary` (a title row above them and extra columns are
     fine). New IDs are added, existing IDs are updated, nothing is deleted.
     Rows with problems are listed with the reason.
   * **Add / Edit / Delete** any employee. Editing can change the ID itself.
   * The **Employee ID must be the eSSL Employee Code** — the number staff punch
     with — because that's how punches are matched to people.
2. **Attendance & Deductions** — pick month/year (and optionally "up to day").
   Columns: Sl.No, Employee ID, Name, Salary, Present, Leaves, Late Days,
   Leave Deduction, Late Penalty, Total Deduction, Net Salary. Click a row for
   daily punches; the **Waive** switch excuses a late or a leave for that day.
   **Download Salary Sheet** exports the same columns to Excel (plus a daily
   attendance tab).
3. **Policy Configuration** — morning and evening shift entry times, grace
   minutes, leave slabs, late penalty, paid weekly offs, `.mdb` path.

## Calculation

```
Present      = days with at least one punch
Late day     = first punch of the day after shift entry time + grace
               (Morning if the day has a punch before 17:00, otherwise Evening)
Leaves       = days elapsed - present days - paid weekly offs - waived absent days

Leave deduction (slab picked by the month's total leaves, applied to every leave day):
    leaves <= 4   ->  leaves x Rs 100
    leaves <= 7   ->  leaves x Rs 200
    leaves  > 7   ->  leaves x (salary / 30)

Late penalty   = floor(late days / 3) x Rs 50
Net salary     = salary - leave deduction - late penalty      (never below 0)
```

All the numbers above are editable in the Policy tab.

## Files

```
main.py             backend API (+ desktop window)
server.py           runs the same app in the browser
local_store.py      SQLite: employees, waivers, config
payroll_engine.py   attendance + deduction rules (pure logic)
employee_import.py  reads Employee ID / Name / Salary from Excel or CSV
db_adapter.py       reads punches from the eSSL .mdb (mdbtools on Mac, ODBC on Windows)
excel_exporter.py   salary sheet export
ui/                 index.html, styles.css, app.js
```
