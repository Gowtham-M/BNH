"""
payroll_engine.py
=================
Attendance + salary-deduction calculator for Bethune Nursing Home.
Pure functions only (no I/O), so everything here is unit-testable.

ATTENDANCE (single-punch model)
-------------------------------
Most BNH staff punch the biometric terminal once, on arrival. So:
  * Present  = the employee punched at all that day.
  * Late     = time of the day's first punch vs the shift entry time (+ grace).
  * Shift    = Morning if the day has a daytime punch (06:00-16:59),
               otherwise Evening if it has a punch at/after 17:00.
Punches are never paired across midnight, so a next-morning punch can't turn
into a false "late" or "absent".

DEDUCTION POLICY (all values editable in the Policy tab)
--------------------------------------------------------
Leaves = working days in the period with no punch
         (minus paid weekly offs and management-waived days).

  Leave deduction (one slab applies to ALL leave days in the month):
    leaves <= 4          -> leaves x Rs 100
    leaves 5 .. 7        -> leaves x Rs 200
    leaves  > 7          -> leaves x (salary / 30)

  Late penalty:
    every 3 late days    -> Rs 50     i.e. floor(late_days / 3) x 50

  Net salary = Salary - Leave deduction - Late penalty   (never below 0)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Iterable


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    # Database
    "db_path": r"C:\Program Files (x86)\eSSL\eTimeTrackLite\eTimeTrackLite.mdb",
    "db_password": "",

    # Shift entry times (HH:MM, 24h)
    "morning_entry": "09:25",
    "evening_entry": "21:00",

    # Clock windows used to classify a day's punches
    "morning_window": ["06:00", "17:00"],   # daytime punch -> Morning shift
    "evening_window": ["17:00", "23:59"],   # punch >= 17:00 -> Evening shift

    # Attendance
    "grace_minutes": 0,          # minutes after entry time still counted on time
    "debounce_minutes": 5,       # ignore repeat taps within N minutes
    "paid_weekly_offs": 1,       # paid days off per 7 days (not counted as leave)

    # Leave deduction slabs
    "leave_tier1_max": 4,        # up to 4 leaves ...
    "leave_tier1_rate": 100,     #   ... Rs 100 per leave day
    "leave_tier2_max": 7,        # 5 to 7 leaves ...
    "leave_tier2_rate": 200,     #   ... Rs 200 per leave day
    "leave_tier3_divisor": 30,   # more than 7 -> salary / 30 per leave day

    # Late penalty
    "late_block_days": 3,        # every 3 late days ...
    "late_block_penalty": 50,    #   ... Rs 50
}


def merge_config(user_cfg: dict | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if user_cfg:
        cfg.update({k: v for k, v in user_cfg.items() if v is not None})
    return cfg


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def _hhmm_to_minutes(hhmm: str) -> int:
    h, m = str(hhmm).strip().split(":")[:2]
    return int(h) * 60 + int(m)


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else ""


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class DaySession:
    """One resolved duty for one employee on one calendar day."""
    duty_date: date
    shift: str                     # "Morning" | "Evening"
    check_in: datetime | None
    check_out: datetime | None
    late_minutes: int = 0
    is_late: bool = False
    waived: bool = False           # management "Excuse / Waive"

    def status(self) -> str:
        if self.waived:
            return "Excused"
        return "Late" if self.is_late else "On Time"

    def to_row(self) -> dict:
        return {
            "date": self.duty_date.isoformat(),
            "shift": self.shift,
            "check_in": _fmt_time(self.check_in),
            "check_out": _fmt_time(self.check_out),
            "late_minutes": self.late_minutes,
            "is_late": bool(self.is_late and not self.waived),
            "waived": self.waived,
            "status": self.status(),
        }


@dataclass
class EmployeePayroll:
    employee_id: str
    name: str
    salary: float
    present_days: int
    leaves: int
    late_count: int
    waived_days: int
    leave_rate: float              # Rs per leave day under the slab that applied
    leave_deduction: float
    late_penalty: float
    total_deduction: float
    net_salary: float
    sessions: list[DaySession] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "sessions"}

    def daily_rows(self) -> list[dict]:
        return [s.to_row() for s in sorted(self.sessions, key=lambda s: s.duty_date)]


# --------------------------------------------------------------------------- #
# Punch -> daily sessions
# --------------------------------------------------------------------------- #

def _debounce(punches: list[datetime], minutes: int) -> list[datetime]:
    """Drop punches within `minutes` of the previously kept punch."""
    if not punches:
        return []
    kept = [punches[0]]
    gap = timedelta(minutes=minutes)
    for p in punches[1:]:
        if p - kept[-1] >= gap:
            kept.append(p)
    return kept


def detect_sessions(punches: Iterable[datetime], cfg: dict) -> list[DaySession]:
    """Resolve raw punches into one session per calendar day."""
    morning_entry = _hhmm_to_minutes(cfg["morning_entry"])
    evening_entry = _hhmm_to_minutes(cfg["evening_entry"])
    grace = int(cfg.get("grace_minutes", 0) or 0)
    evening_start = _hhmm_to_minutes(cfg["evening_window"][0])   # 17:00
    morning_start = _hhmm_to_minutes(cfg["morning_window"][0])   # 06:00

    punches = _debounce(sorted(set(punches)), int(cfg.get("debounce_minutes", 5) or 0))

    by_date: dict[date, list[datetime]] = {}
    for p in punches:
        by_date.setdefault(p.date(), []).append(p)

    sessions: list[DaySession] = []
    for d, day in by_date.items():
        day.sort()
        daytime = [p for p in day if _minutes_of_day(p) < evening_start]
        evening = [p for p in day if _minutes_of_day(p) >= evening_start]

        if daytime:
            shift, entry = "Morning", morning_entry
            in_window = [p for p in daytime if _minutes_of_day(p) >= morning_start]
            check_in = min(in_window or daytime)
            # a pre-dawn-only punch (e.g. 05:30) is on time, not "20 hours late"
            late = max(0, _minutes_of_day(check_in) - entry) if in_window else 0
        else:
            shift, entry = "Evening", evening_entry
            check_in = min(evening)
            late = max(0, _minutes_of_day(check_in) - entry)

        last = max(day)
        sessions.append(DaySession(
            duty_date=d, shift=shift,
            check_in=check_in, check_out=last if last > check_in else None,
            late_minutes=late, is_late=late > grace,
        ))

    sessions.sort(key=lambda s: s.duty_date)
    return sessions


# --------------------------------------------------------------------------- #
# Deduction rules
# --------------------------------------------------------------------------- #

def leave_rate(leaves: int, salary: float, cfg: dict) -> float:
    """Rs per leave day under the slab that this many leaves falls into."""
    if leaves <= 0:
        return 0.0
    if leaves <= int(cfg["leave_tier1_max"]):
        return float(cfg["leave_tier1_rate"])
    if leaves <= int(cfg["leave_tier2_max"]):
        return float(cfg["leave_tier2_rate"])
    divisor = float(cfg.get("leave_tier3_divisor") or 30)
    return float(salary) / divisor


def late_penalty(late_count: int, cfg: dict) -> float:
    block = max(1, int(cfg["late_block_days"]))
    return float((late_count // block) * float(cfg["late_block_penalty"]))


# --------------------------------------------------------------------------- #
# Monthly payroll
# --------------------------------------------------------------------------- #

def compute_employee_payroll(
    employee: dict,
    punches: list[datetime],
    cfg: dict,
    month: int,
    year: int,
    cutoff_day: int,
    waived_dates: set[str] | None = None,
) -> EmployeePayroll:
    """
    employee: {employee_id, name, salary}
    punches:  this employee's raw punch datetimes (any range; filtered here)
    Period:   day 1 .. cutoff_day of month/year.
    """
    waived_dates = waived_dates or set()
    salary = float(employee.get("salary") or 0)
    start, cutoff = date(year, month, 1), date(year, month, cutoff_day)

    month_punches = [p for p in punches if start <= p.date() <= cutoff]
    sessions = detect_sessions(month_punches, cfg)
    for s in sessions:
        if s.duty_date.isoformat() in waived_dates:
            s.waived = True

    present_dates = {s.duty_date for s in sessions}
    present_days = len(present_dates)
    late_count = sum(1 for s in sessions if s.is_late and not s.waived)

    # waived days with no punch = excused absences
    waived_absent = 0
    for iso in waived_dates:
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        if start <= d <= cutoff and d not in present_dates:
            waived_absent += 1

    weekly_offs = (cutoff_day // 7) * int(cfg.get("paid_weekly_offs", 0) or 0)
    leaves = max(0, cutoff_day - present_days - weekly_offs - waived_absent)

    rate = leave_rate(leaves, salary, cfg)
    leave_ded = round(leaves * rate, 2)
    late_pen = round(late_penalty(late_count, cfg), 2)
    total = round(leave_ded + late_pen, 2)
    net = max(0.0, round(salary - total, 2))

    return EmployeePayroll(
        employee_id=str(employee.get("employee_id", "")),
        name=employee.get("name", ""),
        salary=round(salary, 2),
        present_days=present_days,
        leaves=leaves,
        late_count=late_count,
        waived_days=waived_absent,
        leave_rate=round(rate, 2),
        leave_deduction=leave_ded,
        late_penalty=late_pen,
        total_deduction=total,
        net_salary=net,
        sessions=sessions,
    )


def build_kpis(rows: list[EmployeePayroll]) -> dict:
    return {
        "total_staff": len(rows),
        "staff_with_penalties": sum(1 for r in rows if r.total_deduction > 0),
        "total_salary": round(sum(r.salary for r in rows), 2),
        "total_deductions": round(sum(r.total_deduction for r in rows), 2),
        "net_total_payable": round(sum(r.net_salary for r in rows), 2),
    }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    cfg = merge_config(None)

    # slabs: salary 30000
    assert leave_rate(0, 30000, cfg) == 0
    assert leave_rate(3, 30000, cfg) == 100      # <=4  -> 100/day
    assert leave_rate(4, 30000, cfg) == 100
    assert leave_rate(5, 30000, cfg) == 200      # 5-7  -> 200/day
    assert leave_rate(7, 30000, cfg) == 200
    assert leave_rate(8, 30000, cfg) == 1000     # >7   -> 30000/30
    # late: every 3 -> 50
    assert late_penalty(2, cfg) == 0
    assert late_penalty(3, cfg) == 50
    assert late_penalty(7, cfg) == 100

    # shifts
    D = lambda h, m, day=1: datetime(2026, 11, day, h, m)  # noqa: E731
    s = detect_sessions([D(9, 40)], cfg)[0]
    assert s.shift == "Morning" and s.is_late and s.late_minutes == 15
    s = detect_sessions([D(21, 10)], cfg)[0]
    assert s.shift == "Evening" and s.is_late
    s = detect_sessions([D(5, 30)], cfg)[0]
    assert s.shift == "Morning" and not s.is_late

    # full month: 10 days elapsed, punched 6 days, 3 of them late
    emp = {"employee_id": "53", "name": "Test", "salary": 30000}
    punches = [D(10, 0, d) for d in (1, 2, 3)] + [D(9, 0, d) for d in (4, 5, 6)]
    pr = compute_employee_payroll(emp, punches, cfg, 11, 2026, 10)
    # elapsed 10, present 6, weekly offs 1 -> leaves 3 -> 3 x 100 = 300; 3 lates -> 50
    assert pr.leaves == 3 and pr.leave_deduction == 300, pr
    assert pr.late_penalty == 50 and pr.net_salary == 29650, pr
    print("payroll_engine self-test OK:", pr.to_summary())
