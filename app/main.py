"""
Employee Attendance & Analytics API - Production Implementation
Contract: Candidate Kit v2.0 (FastAPI + PyMongo + MongoDB 6.0+)

Single-file implementation following all requirements in:
- openapi.yaml
- DATA_MODEL.md
- PROBLEM_STATEMENT.docx
"""
from __future__ import annotations

import calendar
import os
import re
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Configuration & Database Connection
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Idempotently create required indexes at startup."""
    # employees collection
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

    # attendance_logs collection
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_att_emp_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_att_date_desc_emp_asc"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_att_emp_punch_in"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_att_date_status"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure indexes are in place
    init_indexes()
    yield
    # Shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Precision Math & Business Rule Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float | Decimal, decimals: int) -> float:
    """R8: Decimal half-up rounding (rates: 4 decimals, hours/days: 2 decimals)."""
    d = Decimal(str(value))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before calculations and persistence."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime with whole-second truncation."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return truncate_to_seconds(dt)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def parse_hh_mm(hh_mm_str: str) -> tuple[int, int]:
    h, m = map(int, hh_mm_str.split(":"))
    return h, m


def resolve_attendance_date(punched_at_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start):
      a punch-in earlier than shift_end belongs to the shift that started the previous calendar day.
    """
    dt_ist = punched_at_dt.astimezone(IST)
    h_start, m_start = parse_hh_mm(shift_start)
    h_end, m_end = parse_hh_mm(shift_end)

    is_overnight = (h_end, m_end) <= (h_start, m_start)
    if is_overnight:
        punch_time = (dt_ist.hour, dt_ist.minute, dt_ist.second)
        if punch_time < (h_end, m_end, 0):
            # Belongs to shift that started previous day
            return (dt_ist.date() - timedelta(days=1)).isoformat()

    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Late. Grace period 10 minutes.
    Late only if the punch-in is strictly more than 10:00 after shift_start (> 600s).
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    h, m = parse_hh_mm(shift_start)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return max(0, int(elapsed_seconds // 60))
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    d_hours = Decimal(seconds) / Decimal(3600)
    return round_half_up(d_hours, 2)


def compute_overtime_minutes(
    punch_out_dt: datetime, shift_end: str, shift_start: str, attendance_date_str: str
) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    h_start, m_start = parse_hh_mm(shift_start)
    h_end, m_end = parse_hh_mm(shift_end)

    end_date = att_date
    if (h_end, m_end) <= (h_start, m_start):
        end_date = att_date + timedelta(days=1)

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, h_end, m_end, 0, tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()

    if elapsed_seconds >= 1800:  # >= 30 minutes
        return int(elapsed_seconds // 60)
    return 0


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when the rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Mongo history entry to API HistoryEntry schema."""
    changes = {}
    raw_changes = entry.get("changes", {})
    for k, v in raw_changes.items():
        c_from = v.get("from")
        c_to = v.get("to")
        if isinstance(c_from, datetime):
            c_from = datetime_to_epoch_ms(c_from)
        if isinstance(c_to, datetime):
            c_to = datetime_to_epoch_ms(c_to)
        changes[k] = {"from": c_from, "to": c_to}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = datetime_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes MongoDB attendance record to OpenAPI AttendanceRecord schema."""
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serializes MongoDB employee document to OpenAPI Employee schema."""
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at = datetime_to_epoch_ms(created_at)

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at,
    }


def count_working_days(start_date: date, end_date: date) -> int:
    """R7: Mon-Fri working days between start_date and end_date inclusive."""
    if start_date > end_date:
        return 0
    cur = start_date
    count = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts_differ(cls, v: str, info):
        start = info.data.get("shift_start")
        if start and v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System & Employee Endpoints
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = resolve_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the employee's most recent attendance record with a punch_in
    target_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )

    if not target_rec:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_dt = target_rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    elapsed_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86400:
        raise HTTPException(
            status_code=422, detail="punch_out must be after punch_in and within 24 hours"
        )

    if target_rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime_minutes(
        punch_out_dt, emp["shift_end"], emp["shift_start"], target_rec["date"]
    )
    half_day = is_half_day(work_hours)

    # Atomic conditional update for concurrency safety
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": target_rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was already punched out by another request")

    return serialize_attendance_doc(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Current stored values
    old_status = rec["status"]
    old_punch_in = rec.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = rec.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)
    old_history = list(rec.get("history", []))

    # Evaluate intended target state
    target_status = body.status if body.status is not None else old_status

    # Setting ABSENT or LEAVE clears both punch times
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status: PRESENT, WFH, ON_DUTY
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            target_punch_in = old_punch_in

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # Validate punch_in stays on record's attendance date (R1)
        resolved_date = resolve_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if resolved_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {resolved_date}, expected {date}",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            target_punch_out = old_punch_out

        if target_punch_out is not None:
            elapsed = (target_punch_out - target_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(
                    status_code=422, detail="punch_out must be after punch_in and within 24 hours"
                )

    # Recompute derived fields from final state
    if target_status in ("ABSENT", "LEAVE"):
        target_late_minutes = 0
        target_work_hours = None
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime_minutes(
                target_punch_out, emp["shift_end"], emp["shift_start"], date
            )
            target_half_day = is_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Build changes dictionary of fields that actually changed
    changes: Dict[str, Any] = {}

    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != target_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if old_work_hours != target_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": target_work_hours}
    if old_late_minutes != target_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": target_late_minutes}
    if old_overtime_minutes != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": target_overtime_minutes}
    if old_half_day != target_half_day:
        changes["half_day"] = {"from": old_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change any values")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    new_history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length so concurrent edits do not lose history
    updated_rec = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": {"$size": len(old_history)},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": new_history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_rec:
        raise HTTPException(status_code=409, detail="Concurrent modification detected, please retry")

    return serialize_attendance_doc(updated_rec)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    regex_pattern = f"^{month}"
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": regex_pattern},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                                "timezone": "+05:30",
                            }
                        },
                        "timezone": "+05:30",
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon-Fri (1 is Sun, 7 is Sat)
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start_date = date(year, m, 1)
    month_end_date = date(year, m, num_days)

    emp_joined = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start_date, emp_joined)
    working_days = count_working_days(effective_start, month_end_date)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = round_half_up(agg.get("present_days", 0.0), 2)
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late = int(agg.get("total_late_minutes", 0))
        total_overtime = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100.0, 4)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_end_str = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "date": {
                                                                "$dateFromString": {
                                                                    "dateString": "$$l.date",
                                                                    "format": "%Y-%m-%d",
                                                                    "timezone": "+05:30",
                                                                }
                                                            },
                                                            "timezone": "+05:30",
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        headcount = int(doc["headcount"])
        if headcount == 0:
            continue
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)

        items.append(
            {
                "department": doc["department"],
                "headcount": headcount,
                "present_days": round_half_up(doc.get("present_days", 0.0), 2),
                "avg_work_hours": avg_wh,
                "late_count": int(doc.get("late_count", 0)),
                "total_late_minutes": int(doc.get("total_late_minutes", 0)),
                "leave_count": int(doc.get("leave_count", 0)),
                "on_duty_count": int(doc.get("on_duty_count", 0)),
            }
        )

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list[dict]:
    match_attendance: Dict[str, Any] = {
        "date": {"$regex": f"^{month}"},
        "late_minutes": {"$gt": 0},
    }

    pipeline: list[dict] = [
        {"$match": match_attendance},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking: ties share rank, next rank skipped (1, 2, 2, 4)
    # Primary sort total_late_minutes DESC, emp_code ASC
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    # We join logs for employees in the department across the date range
    return [
        {
            "$match": {
                "department": department,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"_id": {"$ne": None}}},
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to < from_:
        raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")

    start_d = date.fromisoformat(from_)
    end_d = date.fromisoformat(to)
    num_days = (end_d - start_d).days + 1

    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if department has no employees
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))
    if not dept_employees:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_, to)
    agg_res = list(db.employees.aggregate(pipeline))
    log_map = {doc["_id"]: doc for doc in agg_res}

    items = []
    rates_window = []  # non-null rates for moving average calculation

    cur = start_d
    while cur <= end_d:
        d_str = cur.isoformat()
        is_weekday = cur.weekday() < 5  # Mon-Fri

        # Headcount on that day: department employees with joined_on <= cur
        headcount = sum(1 for emp in dept_employees if emp["joined_on"] <= d_str)

        day_data = log_map.get(d_str, {})
        present_count = round_half_up(day_data.get("present_count", 0.0), 2)
        late_count = int(day_data.get("late_count", 0))

        # attendance_rate: only on working days with headcount > 0, else null
        if is_weekday and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
        else:
            att_rate = None

        rates_window.append(att_rate)

        # 7-day moving average: mean of non-null rates in current row and up to 6 preceding rows
        window_slice = [r for r in rates_window[-7:] if r is not None]
        if window_slice:
            moving_avg = round_half_up(sum(window_slice) / len(window_slice), 4)
        else:
            moving_avg = None

        items.append(
            {
                "date": d_str,
                "is_working_day": is_weekday,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": att_rate,
                "moving_avg_7d": moving_avg,
            }
        )
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def run_explain_aggregate(collection_name: str, pipeline: list[dict]) -> dict:
    cmd = {
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    }
    raw = db.command(cmd)
    return json.loads(json_util.dumps(raw))


def run_explain_find(
    collection_name: str, filter_dict: dict, sort_spec: list, skip: int, limit: int
) -> dict:
    cmd = {
        "explain": {
            "find": collection_name,
            "filter": filter_dict,
            "sort": dict(sort_spec),
            "skip": skip,
            "limit": limit,
        },
        "verbosity": "executionStats",
    }
    raw = db.command(cmd)
    return json.loads(json_util.dumps(raw))


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$"),
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_res = run_explain_find(
            "attendance_logs",
            filter_dict=q,
            sort_spec=[("date", -1), ("emp_code", 1)],
            skip=skip,
            limit=page_size,
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = run_explain_aggregate("attendance_logs", pipeline)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = run_explain_aggregate("employees", pipeline)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = run_explain_aggregate("attendance_logs", pipeline)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        if to < from_:
            raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")
        pipeline = build_department_trend_pipeline(department, from_, to)
        explain_res = run_explain_aggregate("employees", pipeline)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    raise HTTPException(status_code=422, detail="Unknown endpoint")
