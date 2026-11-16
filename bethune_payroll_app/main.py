"""
main.py
=======
Backend API for the Bethune Nursing Home Attendance & Payroll app, plus the
desktop (PyWebView) entry point.

Data flow:
  * Employees (ID, Name, Salary), waivers and policy -> SQLite (local_store.py)
  * Punches                                          -> eSSL .mdb, read-only (db_adapter.py)
  * Calculation                                      -> payroll_engine.py

Run as a desktop window:   python main.py
Run in the browser:        python server.py   (see server.py)
"""

from __future__ import annotations

import base64
import calendar
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

from db_adapter import get_adapter
from employee_import import read_employee_file
from excel_exporter import export_payout
from local_store import LocalStore
from payroll_engine import build_kpis, compute_employee_payroll

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
ORG_NAME = "Bethune Nursing Home"


def _api_safe(fn):
    """Return {"ok": True, "data": ...} or {"ok": False, "error": "..."}."""
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": fn(*args, **kwargs)}
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, ValueError):
                traceback.print_exc()
            return {"ok": False, "error": str(exc)}
    wrapper.__name__ = fn.__name__
    return wrapper


def _punch_key(x) -> str:
    """Normalise an ID so '053', '53' and 53.0 all match."""
    s = str(x).strip()
    try:
        return str(int(float(s)))
    except ValueError:
        return s.upper()


class Api:
    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()
        self._window = None

    def set_window(self, window):
        self._window = window

    # ------------------------------------------------------------------ #
    # Status / policy
    # ------------------------------------------------------------------ #
    @_api_safe
    def get_status(self):
        cfg = self.store.get_config()
        adapter = get_adapter(cfg)
        status = adapter.test_connection()
        now = datetime.now()
        status.update({
            "demo": getattr(adapter, "is_demo", False),
            "employees": len(self.store.list_employees()),
            "store_path": self.store.path,
            "current_month": now.month, "current_year": now.year,
        })
        return status

    @_api_safe
    def get_config(self):
        return self.store.get_config()

    @_api_safe
    def save_config(self, cfg):
        return self.store.save_config(cfg or {})

    @_api_safe
    def test_connection(self, path=None, password=None):
        cfg = self.store.get_config()
        if path:
            cfg["db_path"] = path
        if password is not None:
            cfg["db_password"] = password
        if not os.path.exists(cfg["db_path"]):
            return {"ok": False, "message": f"File not found: {cfg['db_path']}"}
        return get_adapter(cfg).test_connection()

    # ------------------------------------------------------------------ #
    # Employees (SQLite)
    # ------------------------------------------------------------------ #
    @_api_safe
    def list_employees(self):
        return self.store.list_employees()

    @_api_safe
    def add_employee(self, emp):
        emp = emp or {}
        return self.store.add_employee(emp.get("employee_id"), emp.get("name"), emp.get("salary"))

    @_api_safe
    def update_employee(self, original_id, emp):
        emp = emp or {}
        return self.store.update_employee(
            original_id, emp.get("employee_id"), emp.get("name"), emp.get("salary"))

    @_api_safe
    def delete_employee(self, employee_id):
        self.store.delete_employee(employee_id)
        return {"deleted": str(employee_id)}

    @_api_safe
    def import_from_essl(self):
        """Copy employee IDs + names from the eSSL .mdb into SQLite (new IDs only)."""
        cfg = self.store.get_config()
        adapter = get_adapter(cfg)
        if getattr(adapter, "is_demo", False):
            raise ValueError(f"eSSL database not found at {cfg.get('db_path')}. "
                             "Set the .mdb path in the Policy tab first.")
        rows = [{"employee_id": e["employee_code"], "name": e["employee_name"], "salary": 0}
                for e in adapter.get_employees()]
        result = self.store.add_missing_employees(rows)
        result["in_essl"] = len(rows)
        return result

    @_api_safe
    def save_employees(self, changes):
        """Save inline edits. changes: [{original_id, employee_id, name, salary}].
        Each row is saved on its own so one bad row doesn't block the rest."""
        saved, errors = 0, []
        for c in changes or []:
            try:
                self.store.update_employee(c.get("original_id"), c.get("employee_id"),
                                           c.get("name"), c.get("salary"))
                saved += 1
            except ValueError as exc:
                errors.append({"original_id": c.get("original_id"), "error": str(exc)})
        return {"saved": saved, "errors": errors}

    @_api_safe
    def import_employees_file(self, filename, data_b64):
        """Import an uploaded Excel/CSV (sent as base64) into SQLite."""
        ext = os.path.splitext(filename or "")[1].lower()
        tmpdir = tempfile.mkdtemp(prefix="bnh_import_")
        try:
            path = os.path.join(tmpdir, "upload" + ext)
            with open(path, "wb") as f:
                f.write(base64.b64decode(data_b64))
            parsed = read_employee_file(path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        result = self.store.upsert_employees(parsed["rows"])
        result.update({"sheet": parsed["sheet"], "rows_read": len(parsed["rows"])})
        return result

    # ------------------------------------------------------------------ #
    # Attendance & deductions
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cutoff(month, year, cutoff_day):
        ndays = calendar.monthrange(year, month)[1]
        if not cutoff_day:
            now = datetime.now()
            if (year, month) == (now.year, now.month):
                cutoff_day = now.day
            elif (year, month) > (now.year, now.month):
                cutoff_day = 1
            else:
                cutoff_day = ndays
        return max(1, min(int(cutoff_day), ndays)), ndays

    def _compute(self, month, year, cutoff_day):
        cfg = self.store.get_config()
        logs = get_adapter(cfg).get_device_logs(month, year)
        punches_by_id: dict[str, list] = {}
        for uid, plist in logs.items():
            punches_by_id.setdefault(_punch_key(uid), []).extend(plist)
        waivers = self.store.get_waivers()
        results = []
        for emp in self.store.list_employees():
            results.append(compute_employee_payroll(
                emp, punches_by_id.get(_punch_key(emp["employee_id"]), []),
                cfg, month, year, cutoff_day,
                waived_dates=waivers.get(emp["employee_id"], set()),
            ))
        return results, cfg

    @_api_safe
    def get_payroll(self, month, year, cutoff_day=None):
        month, year = int(month), int(year)
        cutoff_day, ndays = self._cutoff(month, year, cutoff_day)
        results, _ = self._compute(month, year, cutoff_day)
        return {
            "rows": [r.to_summary() for r in results],
            "kpis": build_kpis(results),
            "month": month, "year": year,
            "cutoff_day": cutoff_day, "days_in_month": ndays,
        }

    @_api_safe
    def get_employee_daily(self, employee_id, month, year, cutoff_day=None):
        month, year = int(month), int(year)
        cutoff_day, _ = self._cutoff(month, year, cutoff_day)
        results, _ = self._compute(month, year, cutoff_day)
        for r in results:
            if r.employee_id == str(employee_id):
                return {"employee_id": r.employee_id, "name": r.name, "rows": r.daily_rows()}
        return {"employee_id": str(employee_id), "name": "", "rows": []}

    @_api_safe
    def set_waiver(self, employee_id, duty_date, waived):
        self.store.set_waiver(str(employee_id), str(duty_date), bool(waived))
        return {"employee_id": employee_id, "duty_date": duty_date, "waived": bool(waived)}

    @_api_safe
    def export_payout_sheet(self, month, year, cutoff_day=None):
        month, year = int(month), int(year)
        cutoff_day, _ = self._cutoff(month, year, cutoff_day)
        results, cfg = self._compute(month, year, cutoff_day)
        path = self._save_dialog(f"BNH_Salary_{year}_{month:02d}.xlsx")
        if not path:
            return {"cancelled": True}
        export_payout(path, [r.to_summary() for r in results], month, year,
                      {r.employee_id: r.daily_rows() for r in results}, cfg,
                      ORG_NAME, cutoff_day)
        return {"path": path}

    # ------------------------------------------------------------------ #
    # File dialogs (desktop mode; server.py overrides these for the browser)
    # ------------------------------------------------------------------ #
    @_api_safe
    def pick_mdb(self):
        paths = self._open_dialog(("Access Database (*.mdb;*.accdb)", "All files (*.*)"))
        return {"path": paths[0] if paths else None}

    def _save_dialog(self, default_name):
        import webview
        win = self._window or (webview.windows[0] if webview.windows else None)
        if win is None:
            return os.path.join(os.path.expanduser("~"), default_name)
        result = win.create_file_dialog(webview.SAVE_DIALOG,
                                        directory=os.path.expanduser("~"),
                                        save_filename=default_name)
        if isinstance(result, (list, tuple)):
            return result[0] if result else None
        return result

    def _open_dialog(self, file_types=("All files (*.*)",)):
        import webview
        win = self._window or (webview.windows[0] if webview.windows else None)
        if win is None:
            return None
        result = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                        file_types=file_types)
        return list(result) if result else None


def main():
    import webview
    api = Api()
    window = webview.create_window(
        title=f"{ORG_NAME} — Attendance & Payroll",
        url=os.path.join(UI_DIR, "index.html"),
        js_api=api, width=1280, height=820, min_size=(1120, 720),
    )
    api.set_window(window)
    webview.start(debug=("--debug" in sys.argv))


if __name__ == "__main__":
    main()
