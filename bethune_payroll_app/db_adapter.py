"""
db_adapter.py
=============
Read-only adapter for the eSSL eTimeTrackLite v12 MS Access (.mdb) database.

Two backends, selected automatically by platform:
  * Windows  -> pyodbc + "Microsoft Access Driver (*.mdb, *.accdb)"
  * macOS/Linux (development) -> mdbtools (`mdb-tables`, `mdb-export`)

Stability measures required by the PRD:
  * Non-blocking temp-copy reads: the live .mdb is copied to the OS temp dir
    before every read, so we never collide with eTimeTrackLite's own .ldb lock
    while it is downloading punches from the biometric terminals.
  * Raw punch ingestion: we read `DeviceLogs_<MM>_<YYYY>` (fallback
    `DeviceLogs_Processed`) rather than the pre-calculated `AttendanceLogs`.

A `DemoAdapter` with the identical interface is provided so the full app
(engine, UI, export) is runnable on a machine without the real .mdb.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime


# --------------------------------------------------------------------------- #
# Column-name tolerance: eTimeTrackLite builds vary slightly between versions.
# --------------------------------------------------------------------------- #
_EMP_CODE_KEYS = ["EmployeeCode", "EmployeeID", "EmployeeId", "Emp_Code", "Code"]
_EMP_DEVICE_KEYS = ["EmployeeCodeInDevice", "EmployeeCodeInDevice", "UserId", "DeviceUserId"]
_EMP_NAME_KEYS = ["EmployeeName", "Name", "FullName"]
_EMP_DEPTID_KEYS = ["DepartmentId", "DepartmentID", "DeptId"]
_EMP_DESIG_KEYS = ["Designation", "Category", "CategoryName", "Role"]
_EMP_STATUS_KEYS = ["Status", "EmployeeStatus", "IsActive"]
_LOG_USER_KEYS = ["UserId", "UserID", "EmployeeCodeInDevice", "EmployeeCode"]
_LOG_DATE_KEYS = ["LogDate", "PunchTime", "LogDateTime", "AttDateTime"]
_LOG_DIR_KEYS = ["Direction", "InOutMode", "C1"]

_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


def _first(d: dict, keys: list[str]):
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] not in (None, ""):
            return low[k.lower()]
    return None


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Real eSSL adapter
# --------------------------------------------------------------------------- #

class ESSLDatabaseAdapter:
    def __init__(self, mdb_path: str, password: str | None = None):
        self.mdb_path = mdb_path
        self.password = password or None
        self.is_windows = sys.platform.startswith("win")
        self.is_demo = False

    # -- temp copy ------------------------------------------------------- #
    def _temp_copy(self) -> str:
        if not os.path.exists(self.mdb_path):
            raise FileNotFoundError(f".mdb not found: {self.mdb_path}")
        tmp = os.path.join(
            tempfile.gettempdir(),
            f"bnh_essl_{os.getpid()}_{os.path.basename(self.mdb_path)}",
        )
        shutil.copy2(self.mdb_path, tmp)
        return tmp

    # -- connection test ------------------------------------------------- #
    def test_connection(self) -> dict:
        try:
            tables = self.list_tables()
            log_tables = [t for t in tables if t.lower().startswith("devicelogs")]
            return {
                "ok": True,
                "backend": "pyodbc" if self.is_windows else "mdbtools",
                "message": f"Connected. {len(tables)} tables found.",
                "has_employees": any(t.lower() == "employees" for t in tables),
                "device_log_tables": sorted(log_tables),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": "pyodbc" if self.is_windows else "mdbtools",
                    "message": str(exc)}

    # -- table listing --------------------------------------------------- #
    def list_tables(self) -> list[str]:
        if self.is_windows:
            return self._win_list_tables()
        return self._mdbtools_list_tables()

    # -- high-level reads ------------------------------------------------ #
    def get_departments(self) -> dict[str, str]:
        rows = self._read_table("Departments")
        out = {}
        for r in rows:
            dept_id = _first(r, _EMP_DEPTID_KEYS) or _first(r, ["DepartmentId"])
            # eTimeTrackLite v12 names the column DepartmentFName (not DepartmentName)
            name = _first(r, ["DepartmentFName", "DepartmentName", "DepartmentSName", "Name"])
            if dept_id is not None:
                out[str(dept_id)] = name or ""
        return out

    def get_employees(self) -> list[dict]:
        departments = {}
        try:
            departments = self.get_departments()
        except Exception:  # noqa: BLE001
            pass
        rows = self._read_table("Employees")
        employees = []
        for r in rows:
            # eTimeTrackLite soft-deletes by setting RecordStatus=0 and prefixing
            # the code/name with "del_" while leaving Status="Working". Skip those.
            record_status = str(_first(r, ["RecordStatus"]) or "").strip()
            if record_status == "0":
                continue
            status = str(_first(r, _EMP_STATUS_KEYS) or "").strip().lower()
            if status in ("resigned", "inactive", "left", "0", "false", "no"):
                continue
            dept_id = _first(r, _EMP_DEPTID_KEYS)
            code = _first(r, _EMP_CODE_KEYS)
            if code is None:
                continue
            if str(code).lower().startswith("del_") or \
               str(_first(r, _EMP_NAME_KEYS) or "").lower().startswith("del_"):
                continue
            employees.append({
                "employee_code": str(code),
                "device_code": str(_first(r, _EMP_DEVICE_KEYS) or code),
                "employee_name": _first(r, _EMP_NAME_KEYS) or "",
                "department": departments.get(str(dept_id), "") if dept_id is not None else "",
                "role": _first(r, _EMP_DESIG_KEYS) or "",
            })
        return employees

    def get_device_logs(self, month: int, year: int) -> dict[str, list[datetime]]:
        """Return {device_user_id: [punch datetimes...]} for the given month."""
        table = f"DeviceLogs_{month}_{year}"
        tables = {t.lower(): t for t in self.list_tables()}
        rows: list[dict] = []
        if table.lower() in tables:
            rows = self._read_table(tables[table.lower()])
        # zero-padded variant
        if not rows:
            alt = f"DeviceLogs_{month:02d}_{year}"
            if alt.lower() in tables:
                rows = self._read_table(tables[alt.lower()])
        # fallback to the processed/aggregate table, filtered by month/year.
        # eTimeTrackLite v12 stores a plain `DeviceLogs` table (no `_Processed`
        # suffix in this build), so try both names.
        if not rows:
            for fb in ("devicelogs_processed", "devicelogs"):
                if fb in tables:
                    rows = self._read_table(tables[fb])
                    if rows:
                        break

        punches: dict[str, list[datetime]] = {}
        for r in rows:
            dt = _parse_dt(_first(r, _LOG_DATE_KEYS))
            if dt is None:
                continue
            if dt.month != month or dt.year != year:
                # allow next-month early-morning night check-outs
                nxt = (year + 1, 1) if month == 12 else (year, month + 1)
                if (dt.year, dt.month) != nxt:
                    continue
            uid = _first(r, _LOG_USER_KEYS)
            if uid is None:
                continue
            punches.setdefault(str(uid), []).append(dt)
        for v in punches.values():
            v.sort()
        return punches

    # ================================================================== #
    # Windows backend (pyodbc)
    # ================================================================== #
    def _win_connect(self):
        import pyodbc  # imported lazily so macOS dev doesn't need it
        src = self._temp_copy()
        conn_str = (
            r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
            rf"DBQ={src};"
        )
        if self.password:
            conn_str += rf"PWD={self.password};"
        return pyodbc.connect(conn_str, autocommit=True), src

    def _win_list_tables(self) -> list[str]:
        conn, src = self._win_connect()
        try:
            cur = conn.cursor()
            return [row.table_name for row in cur.tables(tableType="TABLE")]
        finally:
            conn.close()
            _safe_remove(src)

    def _win_read_table(self, name: str) -> list[dict]:
        conn, src = self._win_connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM [{name}]")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
            _safe_remove(src)

    # ================================================================== #
    # macOS / Linux backend (mdbtools)
    # ================================================================== #
    def _mdbtools_list_tables(self) -> list[str]:
        src = self._temp_copy()
        try:
            out = subprocess.run(
                ["mdb-tables", "-1", src],
                capture_output=True, text=True, check=True,
            ).stdout
            return [t for t in out.splitlines() if t.strip()]
        except FileNotFoundError as exc:
            raise RuntimeError(
                "mdbtools is not installed. On macOS run: brew install mdbtools"
            ) from exc
        finally:
            _safe_remove(src)

    def _mdbtools_read_table(self, name: str) -> list[dict]:
        src = self._temp_copy()
        try:
            out = subprocess.run(
                ["mdb-export", "-D", "%Y-%m-%d %H:%M:%S", src, name],
                capture_output=True, text=True, check=True,
            ).stdout
            reader = csv.DictReader(io.StringIO(out))
            return list(reader)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"mdb-export failed for {name}: {exc.stderr}") from exc
        finally:
            _safe_remove(src)

    # -- dispatch -------------------------------------------------------- #
    def _read_table(self, name: str) -> list[dict]:
        if self.is_windows:
            return self._win_read_table(name)
        return self._mdbtools_read_table(name)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Demo adapter (synthetic data so the app runs without a real .mdb)
# --------------------------------------------------------------------------- #

class DemoAdapter:
    """Drop-in replacement generating realistic hospital punch data."""

    def __init__(self, *_, **__):
        self.is_demo = True

    def test_connection(self) -> dict:
        return {"ok": True, "backend": "demo",
                "message": "DEMO DATA (no .mdb configured)",
                "has_employees": True, "device_log_tables": ["DeviceLogs (synthetic)"]}

    def list_tables(self) -> list[str]:
        return ["Employees", "Departments", "DeviceLogs_demo"]

    def get_departments(self) -> dict[str, str]:
        return {"1": "ICU", "2": "General Ward", "3": "Laboratory",
                "4": "Reception", "5": "Pharmacy"}

    def get_employees(self) -> list[dict]:
        roster = [
            ("N001", "Lakshmi Devi", "ICU", "Nurse", "Auto"),
            ("N002", "Sita Rani", "ICU", "Nurse", "Night"),
            ("N003", "Padma Kumari", "General Ward", "Nurse", "Morning"),
            ("N004", "Anjali Reddy", "General Ward", "Nurse", "Auto"),
            ("W001", "Ramesh Yadav", "General Ward", "Ward Boy", "Morning"),
            ("W002", "Suresh Babu", "ICU", "Ward Boy", "Night"),
            ("L001", "Kiran Kumar", "Laboratory", "Lab Tech", "Morning"),
            ("R001", "Divya Sharma", "Reception", "Reception", "Morning"),
            ("P001", "Venkat Rao", "Pharmacy", "Pharmacist", "Morning"),
            ("D001", "Dr. Subbarao", "ICU", "Doctor", "Morning"),
        ]
        return [
            {"employee_code": c, "device_code": c, "employee_name": n,
             "department": d, "role": r, "_demo_mode": m}
            for (c, n, d, r, m) in roster
        ]

    def get_device_logs(self, month: int, year: int) -> dict[str, list[datetime]]:
        import calendar
        import random
        from datetime import timedelta
        rng = random.Random(f"{month}-{year}")  # deterministic per month
        ndays = calendar.monthrange(year, month)[1]
        today = datetime.now()
        cutoff = today.day if (today.month == month and today.year == year) else ndays

        punches: dict[str, list[datetime]] = {}
        for emp in self.get_employees():
            code = emp["employee_code"]
            mode = emp.get("_demo_mode", "Auto")
            punches[code] = []
            # doctors: sparse attendance (often excluded from payroll)
            attend_prob = 0.55 if emp["role"] == "Doctor" else 0.9
            for day in range(1, cutoff + 1):
                d = datetime(year, month, day)
                if d.weekday() == 6 and rng.random() < 0.7:  # some Sunday offs
                    continue
                if rng.random() > attend_prob:  # absent
                    continue
                shift = mode
                if mode == "Auto":
                    shift = "Night" if rng.random() < 0.4 else "Morning"
                if shift == "Morning":
                    base = d.replace(hour=9, minute=25)
                    late = rng.choice([0, 0, 0, 3, 8, 20, 35])  # occasional lates
                    cin = base + timedelta(minutes=late)
                    cout = d.replace(hour=17, minute=rng.randint(0, 40))
                    punches[code] += [cin, cout]
                else:  # Night, crosses midnight
                    base = d.replace(hour=21, minute=0)
                    late = rng.choice([0, 0, 0, 5, 12, 25])
                    cin = base + timedelta(minutes=late)
                    cout = (d + timedelta(days=1)).replace(hour=6, minute=rng.randint(10, 50))
                    punches[code] += [cin, cout]
        for v in punches.values():
            v.sort()
        return punches


# --------------------------------------------------------------------------- #
def get_adapter(cfg: dict):
    """Return the real adapter when the .mdb exists, else the demo adapter."""
    path = cfg.get("db_path", "")
    if path and os.path.exists(path):
        return ESSLDatabaseAdapter(path, cfg.get("db_password"))
    return DemoAdapter()


if __name__ == "__main__":
    a = DemoAdapter()
    print("tables:", a.list_tables())
    emps = a.get_employees()
    print(f"employees: {len(emps)}")
    logs = a.get_device_logs(datetime.now().month, datetime.now().year)
    total = sum(len(v) for v in logs.values())
    print(f"punches this month: {total}")
    print("OK")
