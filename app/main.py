"""Employee Attendance & Analytics API
All-in-one FastAPI application implementing the HROne engineering assignment specification.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date, time
from decimal import Decimal, ROUND_HALF_UP
import calendar
import json
import os
import re
from typing import Optional, List, Dict, Any, Union

from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# 1. Environment & Database Configuration
# ---------------------------------------------------------------------------
# Load .env without overriding system environment variables (real env vars win)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezone-aware PyMongo client so datetimes returned from MongoDB are never naive
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

MIN_EPOCH_MS = 100_000_000_000      # 1973-03-03
MAX_EPOCH_MS = 4_102_444_800_000    # 2100-01-01


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotently ensure all database indexes exist at startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

        # attendance_logs collection
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True
        )
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING)])
    except Exception:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# ---------------------------------------------------------------------------
# 2. Time & Business Calculation Helpers (Rules R1 - R5, R8)
# ---------------------------------------------------------------------------

def validate_epoch_ms(val: Any) -> int:
    """Strictly validates an integer epoch millisecond value."""
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError("Instant must be an integer count of milliseconds")
    if val < MIN_EPOCH_MS or val > MAX_EPOCH_MS:
        raise ValueError(f"Epoch ms out of range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}]")
    return val


def truncate_to_seconds(epoch_ms: int) -> int:
    """R1: Truncate instants to whole seconds before any calculation."""
    return (epoch_ms // 1000) * 1000


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Converts epoch ms to timezone-aware UTC datetime."""
    truncated_ms = truncate_to_seconds(epoch_ms)
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON UTC datetime to epoch ms."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up_2(val: Union[float, Decimal]) -> float:
    """R8: 2 decimals half-up (financial rounding, not banker's rounding)."""
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Union[float, Decimal]) -> float:
    """R8: 4 decimals half-up for rates."""
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def parse_time_str(time_str: str) -> time:
    """Parses 'HH:MM' string into datetime.time."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date = IST calendar date of punch_in.
    Overnight exception: if shift_end <= shift_start, a punch-in whose IST time
    is strictly earlier than shift_end belongs to the previous day's shift.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    p_time = punch_in_ist.time()
    s_end = parse_time_str(shift_end)
    s_start = parse_time_str(shift_start)

    # Check if overnight shift
    if s_end <= s_start:
        if p_time < s_end:
            # Belongs to previous day's shift
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(attendance_date_str: str, punch_in_dt: datetime, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes.
    Late only if strictly > 10:00 (600s) after shift_start.
    When late: floor minutes elapsed since shift_start. Otherwise 0.
    """
    att_date = date.fromisoformat(attendance_date_str)
    s_start_time = parse_time_str(shift_start)
    shift_start_dt = datetime.combine(att_date, s_start_time, tzinfo=IST)

    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()

    # Strictly more than 10 minutes (600 seconds)
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime_minutes(
    attendance_date_str: str,
    punch_out_dt: datetime,
    shift_start: str,
    shift_end: str
) -> int:
    """
    R3: Overtime = floor minutes between shift_end and punch_out.
    Counted only when >= 30 minutes; otherwise 0.
    Overnight-aware: if shift_end <= shift_start, shift_end falls on next day.
    """
    att_date = date.fromisoformat(attendance_date_str)
    s_start_time = parse_time_str(shift_start)
    s_end_time = parse_time_str(shift_end)

    if s_end_time <= s_start_time:
        # Overnight: shift ends on next calendar day
        shift_end_dt = datetime.combine(att_date + timedelta(days=1), s_end_time, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(att_date, s_end_time, tzinfo=IST)

    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()

    diff_minutes = int(diff_seconds // 60)
    if diff_minutes >= 30:
        return diff_minutes
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_seconds = Decimal(str((punch_out_dt - punch_in_dt).total_seconds()))
    hours = diff_seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """
    R5: half_day = true when rounded work_hours < 4.50.
    """
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# 3. Serialization Helpers
# ---------------------------------------------------------------------------

def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an employee MongoDB document for API responses (removes _id)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an audit history entry with epoch ms for datetime fields."""
    changes_serialized: Dict[str, Any] = {}
    for field_name, change in entry.get("changes", {}).items():
        from_val = change.get("from")
        to_val = change.get("to")
        if isinstance(from_val, datetime):
            from_val = utc_dt_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = utc_dt_to_epoch_ms(to_val)
        changes_serialized[field_name] = {"from": from_val, "to": to_val}

    at_val = entry.get("at")
    if isinstance(at_val, datetime):
        at_val = utc_dt_to_epoch_ms(at_val)

    return {
        "at": at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_serialized,
    }


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an attendance document for API responses, handling legacy defaults."""
    raw_history = doc.get("history") or []
    history = [serialize_history_entry(h) for h in raw_history]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# ---------------------------------------------------------------------------
# 4. Pydantic Request Models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date format for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_ms(v)
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_ms(v)
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_instants(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            return validate_epoch_ms(v)
        return v


# ---------------------------------------------------------------------------
# 5. Core System & Employee Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 if MongoDB answers a ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MongoDB ping failed: {str(e)}"
        )


@app.post(
    "/employees",
    tags=["employees"],
    status_code=status.HTTP_201_CREATED,
    response_model=None
)
def create_employee(payload: EmployeeCreate):
    """
    Creates an employee. Unique emp_code is strictly enforced by the database
    unique index; concurrent duplicates yield 409 Conflict.
    """
    now_utc = datetime.now(timezone.utc)
    # Truncate to whole seconds per convention
    now_ms = truncate_to_seconds(int(now_utc.timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)

    doc = {
        "emp_code": payload.emp_code,
        "name": payload.name,
        "email": payload.email,
        "department": payload.department,
        "shift_start": payload.shift_start,
        "shift_end": payload.shift_end,
        "joined_on": payload.joined_on,
        "created_at": created_at_dt,
    }

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{payload.emp_code}' already exists"
        )

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Lists employees sorted by emp_code ascending.
    'total' counts the filtered set, not the whole collection.
    """
    filter_query: Dict[str, Any] = {}
    if department is not None:
        filter_query["department"] = department

    total = db.employees.count_documents(filter_query)
    skip = (page - 1) * page_size

    cursor = (
        db.employees.find(filter_query)
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_employee(emp) for emp in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 6. Core Attendance Endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/attendance/punch-in",
    tags=["attendance"],
    status_code=status.HTTP_201_CREATED,
    response_model=None
)
def punch_in(payload: PunchInRequest):
    """
    Records a punch-in event.
    Concurrency safe: uses DB unique index on (emp_code, date) to reject duplicates with 409.
    """
    emp = db.employees.find_one({"emp_code": payload.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{payload.emp_code}' not found"
        )

    if payload.punched_at is not None:
        punch_in_ms = truncate_to_seconds(payload.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        punch_in_ms = truncate_to_seconds(int(now_utc.timestamp() * 1000))

    punch_in_dt = datetime.fromtimestamp(punch_in_ms / 1000.0, tz=timezone.utc)
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(att_date, punch_in_dt, emp["shift_start"])

    record = {
        "emp_code": payload.emp_code,
        "date": att_date,
        "status": payload.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee '{payload.emp_code}' already punched in for attendance date '{att_date}'"
        )

    return serialize_attendance(record)


@app.post(
    "/attendance/punch-out",
    tags=["attendance"],
    status_code=status.HTTP_200_OK,
    response_model=None
)
def punch_out(payload: PunchOutRequest):
    """
    Records a punch-out event on the most recent punch-in record.
    Concurrency safe: uses atomic find_one_and_update with punch_out: null condition.
    """
    emp = db.employees.find_one({"emp_code": payload.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{payload.emp_code}' not found"
        )

    if payload.punched_at is not None:
        punch_out_ms = truncate_to_seconds(payload.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        punch_out_ms = truncate_to_seconds(int(now_utc.timestamp() * 1000))

    punch_out_dt = datetime.fromtimestamp(punch_out_ms / 1000.0, tz=timezone.utc)

    # Find the most recent record whose punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": payload.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No matching punch-in found for employee '{payload.emp_code}' at or before punched_at"
        )

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in"
        )

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out"
        )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime_minutes(record["date"], punch_out_dt, emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half,
            }
        },
        return_document=True
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record was concurrently punched out"
        )

    return serialize_attendance(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Lists attendance records sorted by date desc, then emp_code asc.
    Fast on ~100k records via indexed database queries.
    """
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be greater than date_to"
            )

    filter_query: Dict[str, Any] = {}
    if emp_code:
        filter_query["emp_code"] = emp_code
    if status:
        filter_query["status"] = status

    date_filter: Dict[str, Any] = {}
    if date_from:
        date_filter["$gte"] = date_from
    if date_to:
        date_filter["$lte"] = date_to
    if date_filter:
        filter_query["date"] = date_filter

    total = db.attendance_logs.count_documents(filter_query)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(filter_query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance(doc) for doc in cursor]
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
    payload: RegularizeRequest = ...
):
    """
    Manually regularize an attendance record.
    Recomputes derived fields, checks invariant rules, and appends an audit history entry.
    Concurrency safe: uses optimistic locking on history size to return 409 on race conditions.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found"
        )

    old_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No attendance record found for '{emp_code}' on '{date}'"
        )

    target_status = payload.status if payload.status is not None else old_record["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if payload.punch_in is not None or payload.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE"
            )
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if payload.punch_in is not None:
            new_punch_in_dt = epoch_ms_to_utc_dt(payload.punch_in)
            # punch_in must stay on the record's attendance date
            computed_date = get_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
            if computed_date != date:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"punch_in belongs to attendance date '{computed_date}', which does not match record date '{date}'"
                )
        else:
            new_punch_in_dt = old_record.get("punch_in")

        # A presence status requires a punch_in (supplied or already stored)
        if new_punch_in_dt is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A presence status requires a valid punch_in"
            )

        if payload.punch_out is not None:
            new_punch_out_dt = epoch_ms_to_utc_dt(payload.punch_out)
        else:
            new_punch_out_dt = old_record.get("punch_out")

        # Validate punch_out order and range
        if new_punch_out_dt is not None:
            if new_punch_out_dt <= new_punch_in_dt:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in"
                )
            if (new_punch_out_dt - new_punch_in_dt).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )

        new_late_minutes = compute_late_minutes(date, new_punch_in_dt, emp["shift_start"])
        if new_punch_out_dt is not None:
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime_minutes(
                date, new_punch_out_dt, emp["shift_start"], emp["shift_end"]
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute actual field changes
    changes: Dict[str, Dict[str, Any]] = {}
    if target_status != old_record.get("status"):
        changes["status"] = {"from": old_record.get("status"), "to": target_status}
    if new_punch_in_dt != old_record.get("punch_in"):
        changes["punch_in"] = {"from": old_record.get("punch_in"), "to": new_punch_in_dt}
    if new_punch_out_dt != old_record.get("punch_out"):
        changes["punch_out"] = {"from": old_record.get("punch_out"), "to": new_punch_out_dt}
    if new_work_hours != old_record.get("work_hours"):
        changes["work_hours"] = {"from": old_record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != old_record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": old_record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != old_record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": old_record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != old_record.get("half_day", False):
        changes["half_day"] = {"from": old_record.get("half_day", False), "to": new_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The regularize request produces no change to the record"
        )

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }

    # Optimistic locking: ensure history array length matches what we read
    old_history = old_record.get("history") or []
    update_res = db.attendance_logs.update_one(
        {
            "_id": old_record["_id"],
            "history": {"$size": len(old_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry}
        }
    )

    if update_res.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict: record was updated by another request"
        )

    updated_record = db.attendance_logs.find_one({"_id": old_record["_id"]})
    return serialize_attendance(updated_record)


# ---------------------------------------------------------------------------
# 7. Analytics Endpoints
# ---------------------------------------------------------------------------

def get_month_date_range(month_str: str) -> tuple[str, str]:
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}"


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    d_start = date.fromisoformat(start_date_str)
    d_end = date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    working_days = 0
    while cur <= d_end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[Dict[str, Any]]:
    start_date, end_date = get_month_date_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_weekday": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    "$is_weekday"
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                }
            }
        }
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found"
        )

    start_date, end_date = get_month_date_range(month)
    effective_start = max(start_date, emp["joined_on"])
    working_days = count_working_days(effective_start, end_date)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    cursor = list(db.attendance_logs.aggregate(pipeline))

    if cursor:
        res = cursor[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up_2((present_days / working_days) * 100.0)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[Dict[str, Any]]:
    start_date, end_date = get_month_date_range(month)
    match_filter: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$lte": [
                                                    {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}},
                                                    5
                                                ]
                                            }
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$log.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]}
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_list.work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    """Department summary for a month aggregated across employees and logs."""
    pipeline = build_department_summary_pipeline(month, department)
    cursor = list(db.employees.aggregate(pipeline))

    items = []
    for row in cursor:
        raw_avg = row.get("avg_work_hours")
        avg_wh = round_half_up_2(raw_avg) if raw_avg is not None else None
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
) -> list[Dict[str, Any]]:
    start_date, end_date = get_month_date_range(month)
    pipeline: list[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
                "_id": 0
            }
        }
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    """Ranks employees by total late_minutes using standard competition ranking ($rank)."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str
) -> list[Dict[str, Any]]:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    from_dt = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    to_dt_bound = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "emps": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on"
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "emps": 1,
                "date_dt": from_dt
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound]
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "emps": 1,
                "date_dt": 1,
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_dt"
                    }
                },
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "date_dt": 1,
                "date_str": 1,
                "is_working_day": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date_str"]}
                        }
                    }
                },
                "emp_codes": "$emps.emp_code"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date_str", "codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {"$in": ["$emp_code", "$$codes"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs"
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """Daily attendance trend with gap-filling ($densify) and 7-day moving average ($setWindowFields)."""
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found"
        )

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date cannot be earlier than 'from' date"
        )

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days"
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = list(db.employees.aggregate(pipeline))

    items = []
    for row in cursor:
        raw_rate = row.get("attendance_rate")
        rate = round_half_up_4(raw_rate) if raw_rate is not None else None

        raw_avg = row.get("moving_avg_7d")
        moving_avg = round_half_up_4(raw_avg) if raw_avg is not None else None

        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row.get("present_count", 0.0)),
            "late_count": int(row.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": moving_avg,
        })

    return {
        "department": department,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 8. Admin Explain Endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, verifying index usage (IXSCAN).
    """
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="date_from cannot be greater than date_to"
                )
            filter_query: Dict[str, Any] = {}
            if emp_code:
                filter_query["emp_code"] = emp_code
            if status:
                filter_query["status"] = status
            date_filter: Dict[str, Any] = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            if date_filter:
                filter_query["date"] = date_filter

            skip = (page - 1) * page_size
            cmd = {
                "find": "attendance_logs",
                "filter": filter_query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            }
            collection_name = "attendance_logs"
            explain_raw = db.command("explain", cmd, verbosity="executionStats")

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="emp_code and month are required query parameters for employee_monthly explain"
                )
            pipeline = build_employee_monthly_pipeline(emp_code, month)
            cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection_name = "attendance_logs"
            explain_raw = db.command("explain", cmd, verbosity="executionStats")

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="month is a required query parameter for department_summary explain"
                )
            pipeline = build_department_summary_pipeline(month, department)
            cmd = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection_name = "employees"
            explain_raw = db.command("explain", cmd, verbosity="executionStats")

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="month is a required query parameter for late_leaderboard explain"
                )
            pipeline = build_late_leaderboard_pipeline(month, limit, department)
            cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection_name = "attendance_logs"
            explain_raw = db.command("explain", cmd, verbosity="executionStats")

        elif endpoint == "department_trend":
            trend_from = from_date or date_from
            trend_to = to_date or date_to
            if not department or not trend_from or not trend_to:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="department, from, and to are required for department_trend explain"
                )
            d_f = date.fromisoformat(trend_from)
            d_t = date.fromisoformat(trend_to)
            if d_t < d_f:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="'to' date cannot be earlier than 'from' date"
                )
            if (d_t - d_f).days + 1 > 92:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Date range cannot exceed 92 days"
                )
            pipeline = build_department_trend_pipeline(department, trend_from, trend_to)
            cmd = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection_name = "employees"
            explain_raw = db.command("explain", cmd, verbosity="executionStats")

        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown endpoint '{endpoint}'"
            )

        # Convert any BSON types to JSON serializable objects
        explain_sanitized = json.loads(json_util.dumps(explain_raw))

        return {
            "endpoint": endpoint,
            "collection": collection_name,
            "explain": explain_sanitized,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to run explain command: {str(e)}"
        )



