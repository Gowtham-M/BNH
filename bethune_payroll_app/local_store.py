"""
local_store.py
==============
SQLite storage for everything the app owns (the eSSL .mdb is only ever READ,
for punches):

  employees  -> Employee ID, Name, Salary  (add / edit incl. ID / delete / import)
  waivers    -> days management has excused, per employee
  config     -> Policy tab settings (shift times, leave slabs, late penalty, DB path)

The database is a single file, created automatically on first run:
  Windows: %APPDATA%\\BethunePayroll\\bethune_store.sqlite
  macOS:   ~/.config/BethunePayroll/bethune_store.sqlite
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, datetime

from payroll_engine import merge_config


def default_store_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "BethunePayroll")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "bethune_store.sqlite")


def _id_sort_key(emp_id: str):
    s = str(emp_id)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def _clean(employee_id, name, salary) -> tuple[str, str, float]:
    """Validate one employee record; raises ValueError with a readable message."""
    eid = str(employee_id if employee_id is not None else "").strip()
    if eid.endswith(".0") and eid[:-2].isdigit():      # 53.0 from Excel -> 53
        eid = eid[:-2]
    nm = " ".join(str(name if name is not None else "").split())
    if not eid:
        raise ValueError("Employee ID is required")
    if not nm:
        raise ValueError(f"Name is required (Employee ID {eid})")
    try:
        sal = float(str(salary).replace(",", "").strip()) if salary not in (None, "") else 0.0
    except ValueError:
        raise ValueError(f"Salary must be a number (Employee ID {eid})") from None
    if sal < 0:
        raise ValueError(f"Salary cannot be negative (Employee ID {eid})")
    return eid, nm, round(sal, 2)


class LocalStore:
    def __init__(self, path: str | None = None):
        self.path = path or default_store_path()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    salary      REAL NOT NULL DEFAULT 0,
                    created_at  TEXT,
                    updated_at  TEXT
                );
                CREATE TABLE IF NOT EXISTS waivers (
                    employee_code TEXT NOT NULL,
                    duty_date     TEXT NOT NULL,
                    PRIMARY KEY (employee_code, duty_date)
                );
                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ #
    # Employees
    # ------------------------------------------------------------------ #
    def list_employees(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT employee_id, name, salary FROM employees").fetchall()
        out = [dict(r) for r in rows]
        out.sort(key=lambda r: _id_sort_key(r["employee_id"]))
        return out

    def _exists(self, eid: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM employees WHERE employee_id = ?", (eid,)).fetchone() is not None

    def add_employee(self, employee_id, name, salary) -> dict:
        eid, nm, sal = _clean(employee_id, name, salary)
        with self._lock, self._conn:
            if self._exists(eid):
                raise ValueError(f"Employee ID {eid} already exists")
            now = self._now()
            self._conn.execute(
                "INSERT INTO employees (employee_id, name, salary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (eid, nm, sal, now, now))
        return {"employee_id": eid, "name": nm, "salary": sal}

    def update_employee(self, original_id, employee_id, name, salary) -> dict:
        """Update a record; the Employee ID itself may change."""
        old = str(original_id).strip()
        eid, nm, sal = _clean(employee_id, name, salary)
        with self._lock, self._conn:
            if not self._exists(old):
                raise ValueError(f"Employee ID {old} not found")
            if eid != old and self._exists(eid):
                raise ValueError(f"Employee ID {eid} already exists")
            self._conn.execute(
                "UPDATE employees SET employee_id=?, name=?, salary=?, updated_at=? "
                "WHERE employee_id=?", (eid, nm, sal, self._now(), old))
            if eid != old:   # keep this person's waivers attached to the new ID
                self._conn.execute(
                    "UPDATE OR REPLACE waivers SET employee_code=? WHERE employee_code=?",
                    (eid, old))
        return {"employee_id": eid, "name": nm, "salary": sal}

    def delete_employee(self, employee_id) -> None:
        eid = str(employee_id).strip()
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM employees WHERE employee_id=?", (eid,))
            if cur.rowcount == 0:
                raise ValueError(f"Employee ID {eid} not found")
            self._conn.execute("DELETE FROM waivers WHERE employee_code=?", (eid,))

    def upsert_employees(self, rows: list[dict]) -> dict:
        """Bulk import: add new IDs, update existing ones; nothing is deleted.
        Returns counts plus any rows that were skipped and why."""
        added = updated = 0
        skipped: list[dict] = []
        seen: set[str] = set()
        with self._lock, self._conn:
            now = self._now()
            for r in rows:
                try:
                    eid, nm, sal = _clean(r.get("employee_id"), r.get("name"), r.get("salary"))
                except ValueError as exc:
                    skipped.append({"row": r.get("_row"), "reason": str(exc)})
                    continue
                if eid in seen:
                    skipped.append({"row": r.get("_row"),
                                    "reason": f"Duplicate Employee ID {eid} in file"})
                    continue
                seen.add(eid)
                if self._exists(eid):
                    self._conn.execute(
                        "UPDATE employees SET name=?, salary=?, updated_at=? WHERE employee_id=?",
                        (nm, sal, now, eid))
                    updated += 1
                else:
                    self._conn.execute(
                        "INSERT INTO employees (employee_id, name, salary, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)", (eid, nm, sal, now, now))
                    added += 1
        return {"added": added, "updated": updated, "skipped": skipped}

    def add_missing_employees(self, rows: list[dict]) -> dict:
        """Add only IDs that aren't in SQLite yet; existing records are left
        exactly as edited. rows: [{employee_id, name, salary?}]"""
        added, existing = [], 0
        with self._lock, self._conn:
            now = self._now()
            for r in rows:
                eid = str(r.get("employee_id", "")).strip()
                if not eid:
                    continue
                if self._exists(eid):
                    existing += 1
                    continue
                name = " ".join(str(r.get("name") or "").split()) or f"Employee {eid}"
                self._conn.execute(
                    "INSERT INTO employees (employee_id, name, salary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)", (eid, name, float(r.get("salary") or 0), now, now))
                added.append(eid)
        return {"added": len(added), "existing": existing, "added_ids": added}

    # ------------------------------------------------------------------ #
    # Waivers
    # ------------------------------------------------------------------ #
    def get_waivers(self) -> dict[str, set[str]]:
        with self._lock:
            rows = self._conn.execute("SELECT employee_code, duty_date FROM waivers").fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["employee_code"], set()).add(r["duty_date"])
        return out

    def set_waiver(self, employee_id: str, duty_date: str, waived: bool) -> None:
        date.fromisoformat(duty_date)  # validate
        with self._lock, self._conn:
            if waived:
                self._conn.execute(
                    "INSERT OR IGNORE INTO waivers (employee_code, duty_date) VALUES (?, ?)",
                    (employee_id, duty_date))
            else:
                self._conn.execute(
                    "DELETE FROM waivers WHERE employee_code=? AND duty_date=?",
                    (employee_id, duty_date))

    # ------------------------------------------------------------------ #
    # Config / policy
    # ------------------------------------------------------------------ #
    def get_config(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM config").fetchall()
        return merge_config({r["key"]: json.loads(r["value"]) for r in rows})

    def save_config(self, cfg: dict) -> dict:
        merged = merge_config({**self.get_config(), **(cfg or {})})
        with self._lock, self._conn:
            for key, value in merged.items():
                self._conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)))
        return merged

    def close(self) -> None:
        with self._lock:
            self._conn.close()


if __name__ == "__main__":
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "_bnh_store_selftest.sqlite")
    if os.path.exists(p):
        os.remove(p)
    s = LocalStore(p)
    s.add_employee("53", "G Draksha Valli", 15000)
    s.add_employee("4", "Ganta Prasad", "19,200")
    try:
        s.add_employee("53", "Dup", 1)
        raise AssertionError("duplicate allowed")
    except ValueError:
        pass
    s.set_waiver("53", "2026-11-02", True)
    s.update_employee("53", "153", "G Draksha Valli", 15500)       # ID change
    assert s.get_waivers() == {"153": {"2026-11-02"}}               # waiver followed
    res = s.upsert_employees([
        {"employee_id": "4", "name": "Ganta Prasad", "salary": 20000, "_row": 2},
        {"employee_id": "70", "name": "New Person", "salary": 12000, "_row": 3},
        {"employee_id": "", "name": "No ID", "salary": 1, "_row": 4},
    ])
    assert res["added"] == 1 and res["updated"] == 1 and len(res["skipped"]) == 1, res
    s.delete_employee("70")
    emps = s.list_employees()
    assert [e["employee_id"] for e in emps] == ["4", "153"], emps
    assert emps[0]["salary"] == 20000
    s.save_config({"late_block_penalty": 75})
    assert s.get_config()["late_block_penalty"] == 75
    print("local_store self-test OK:", emps)
