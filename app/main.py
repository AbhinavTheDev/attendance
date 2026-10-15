"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware = True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)],unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


def parse_month_input(month: str) -> tuple[date, date, int]:
    date_str = month.strip()

    formats_with_year = [
        "%B %Y",  # "March 2026"
        "%b %Y",  # "Mar 2026"
        "%Y-%m",  # "2026-03"
        "%Y/%m",  # "2026/03"
        "%m-%Y",  # "03-2026"
    ]

    formats_month_only = [
        "%B",  # "March"
        "%b",  # "Mar"
    ]

    dt = None

    for fmt in formats_with_year:
        try:
            dt = datetime.strptime(date_str, fmt)
            year = dt.year
            month = dt.month
            break
        except ValueError:
            continue

    if dt is None:
        for fmt in formats_month_only:
            try:
                dt = datetime.strptime(date_str, fmt)
                year = datetime.now(tz=timezone.utc).year
                month = dt.month
                break
            except ValueError:
                continue

    if dt is None:
        raise ValueError(f"Unable to parse month input: '{date_str}'")

    start_date = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    end_date = date(year, month, last_day)

    return start_date, end_date, month


TWO_PLACES = Decimal("0.01")       # For numbers / currency
FOUR_PLACES = Decimal("0.0001")    # For rates / ratios


def round_number(val) -> Decimal:
    """Rounds to 2 decimal places using HALF_UP."""
    return Decimal(str(val)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def round_rate(val) -> Decimal:
    """Rounds to 4 decimal places using HALF_UP."""
    return Decimal(str(val)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutOut(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class Regularize(BaseModel):
    status: str = "PRESENT"
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise Exception("DB Connection Error Occured: ", e)


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    doc.update({"created_at": int(doc["created_at"].timestamp() * 1000)})
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    for doc in items:
        doc.update({"created_at": int(doc["created_at"].timestamp() * 1000)})
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "an unknown employee")
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(tz=timezone.utc)
    tsIST = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(tz=IST)
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(tsIST, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    db.attendance_logs.insert_one(doc)
    doc.pop("_id", None)
    doc.update({"punch_in": int(ts.timestamp() * 1000)})
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
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
    docs = list(db.attendance_logs.find(q, {"_id": 0}))
    for doc in docs:
        if doc.get("punch_in") is not None:
            doc["punch_in"] = int(doc["punch_in"].timestamp() * 1000)
        if doc.get("punch_out") is not None:
            doc["punch_out"] = int(doc["punch_out"].timestamp() * 1000)
        history = doc.get("history")
        if isinstance(history, list):
            for entry in history:
                if not isinstance(entry, dict):
                    continue

                if entry.get("at") is not None:
                    entry["at"] = int(entry["at"].timestamp() * 1000)

                changes = entry.get("changes")
                if isinstance(changes, dict):
                    for field in {"punch_in", "punch_out"}:
                        change_data = changes.get(field)

                        if isinstance(change_data, dict):
                            if change_data.get("from") is not None:
                                change_data["from"] = int(change_data["from"].timestamp() * 1000)
                            if change_data.get("to") is not None:
                                change_data["to"] = int(change_data["to"].timestamp() * 1000)

    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutOut):

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "an unknown employee")

    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(tz=timezone.utc)
    tsIST = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(tz=IST)
    d = ts.date().isoformat()
    attendance = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}, {"_id": 0})

    if attendance is None:
        raise HTTPException(404, "no record for that date")
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d, "punch_out": ts}):
        raise HTTPException(409, "already punched in for this date")

    db.attendance_logs.update_one({
        "emp_code": body.emp_code,
        "date": d
    }, {"$set": {
        "punch_out": ts,
        "work_hours": compute_work_hours(attendance["punch_in"], tsIST),
        "overtime_minutes": compute_overtime(tsIST, emp["shift_end"], d),
        "half_day": True if compute_work_hours(attendance["punch_in"], tsIST) < 4.50 else False,
        }}
    )
    updated_attendance = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}, {"_id": 0})
    updated_attendance["punch_in"] = int(updated_attendance["punch_in"].timestamp() * 1000)
    updated_attendance["punch_out"] = int(updated_attendance["punch_out"].timestamp() * 1000)
    return updated_attendance


@app.patch("/attendance/{emp_code}/{date}")
def update_attendance(emp_code, date, body: Regularize):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "an unknown employee")
    attendance = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if attendance is None:
        raise HTTPException(404, "no record for that date")
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Invalid Body")
    if body.status in ("PRESENT", "WFH", "ON_DUTY"):
        if body.punch_in is None and attendance["punch_in"] is None:
            raise HTTPException(status_code=422, detail="Invalid Body")

    q = {}
    c = {}

    if attendance["status"] != body.status:
        c["status"] = {
              "from": attendance["status"],
              "to": body.status
            }
        q["status"] = body.status
    if body.status in ("ABSENT", "LEAVE"):
        if attendance["punch_in"] != body.punch_in:
            c["punch_in"] = {
             "from": attendance["punch_in"],
             "to": body.punch_in
            }
            q["punch_in"] = body.punch_in
        if attendance["punch_out"] != body.punch_out:
            c["punch_out"] = {
             "from": attendance["punch_out"],
             "to": body.punch_out
            }
            q["punch_out"] = body.punch_out

        if attendance["work_hours"] != 0:
            c["work_hours"] = {
             "from": attendance["work_hours"],
             "to": 0
            }
            q["work_hours"] = 0
        if attendance["late_minutes"] != 0:
            c["late_minutes"] = {
             "from": attendance["late_minutes"],
             "to": 0
            }
            q["late_minutes"] = 0
        if attendance["overtime_minutes"] != 0:
            c["overtime_minutes"] = {
             "from": attendance["overtime_minutes"],
             "to": 0
            }
            q["overtime_minutes"] = 0
        if attendance["half_day"] != False:
            c["half_day"] = {
             "from": True,
             "to": False
            }
            q["half_day"] = False

    if body.status in ("PRESENT", "WFH", "ON_DUTY"):
        if body.punch_in is not None and attendance["punch_in"] != body.punch_in:
            c["punch_in"] = {
             "from": attendance["punch_in"],
             "to": body.punch_in
            }
            q["punch_in"] = body.punch_in
        if body.punch_out is not None and attendance["punch_out"] != body.punch_out:
            c["punch_out"] = {
             "from": attendance["punch_out"],
             "to": body.punch_out
            }
            q["punch_out"] = body.punch_out

        if body.punch_in is None:
            w_p_i = attendance["punch_in"]
        else:
            w_p_i = body.punch_in

        if body.punch_out is None:
            w_p_o = attendance["punch_out"]
        else:
            w_p_o = body.punch_out

        w_h = compute_work_hours(w_p_i, w_p_o)
        if attendance["work_hours"] != w_h:
            c["work_hours"] = {
             "from": attendance["work_hours"],
             "to": w_h
            }
            q["work_hours"] = w_h

            if w_h < 4.50 and attendance["half_day"] == False:
                c["half_day"] = {
                "from": attendance["half_day"],
                "to": True
                }
                q["half_day"] = True
            elif w_h > 4.50 and attendance["half_day"] == True:
                c["half_day"] = {
                "from": attendance["half_day"],
                "to": False
                }
                q["half_day"] = False


        l_m = compute_late_minutes(w_p_i, emp["shift_start"])
        if attendance["late_minutes"] != l_m:
            c["late_minutes"] = {
            "from": attendance["late_minutes"],
            "to": l_m
            }
            q["late_minutes"] = l_m

        o_m = compute_overtime(w_p_o, emp["shift_end"], date)
        if attendance["overtime_minutes"] != o_m:
            c["overtime_minutes"] = {
            "from": attendance["overtime_minutes"],
            "to": o_m
            }
            q["overtime_minutes"] = o_m

    if len(c) == 0:
        raise HTTPException(422, "No Changes Occur, Invalid Body")

    db.attendance_logs.update_one(
        {
         "emp_code": emp_code,
         "date": date
        },
        {
         "$set": q,
         "$push": {
           "history": {
            "at": datetime.now(tz=timezone.utc),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": c
            }}
        }
    )

    updated_attendance = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if updated_attendance.get("punch_in") is not None:
        updated_attendance["punch_in"] = int(updated_attendance["punch_in"].timestamp() * 1000)
    if updated_attendance.get("punch_out") is not None:
        updated_attendance["punch_out"] = int(updated_attendance["punch_out"].timestamp() * 1000)
    history = updated_attendance.get("history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue

            if entry.get("at") is not None:
                entry["at"] = int(entry["at"].timestamp() * 1000)

            changes = entry.get("changes")
            if isinstance(changes, dict):
                for field in ("punch_in", "punch_out"):
                    change_data = changes.get(field)

                    if isinstance(change_data, dict):
                        if change_data.get("from") is not None:
                            change_data["from"] = int(change_data["from"].timestamp() * 1000)
                        if change_data.get("to") is not None:
                            change_data["to"] = int(change_data["to"].timestamp() * 1000)
    return updated_attendance


@app.get("/analytics/employees/{emp_code}/monthly")
def monthly_attendance_summary_of_employee(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "an unknown employee")

    start_date, end_date, monthInt = parse_month_input(month)

    joined_on_date = date.fromisoformat(emp["joined_on"])
    if joined_on_date > start_date:
        start_date = joined_on_date

    total_days = (end_date - start_date).days + 1
    working_days = 0

    for i in range(total_days):
        current = start_date + timedelta(days=i)
        # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
        if current.weekday() < 5:
            working_days += 1

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat()
                }
            }
        }
    ]

    attendances = list(db.attendance_logs.aggregate(pipeline))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for i in attendances:
        status = i.get("status")
        log_date = date.fromisoformat(i["date"])
        is_weekday = log_date.weekday() < 5

        if status in ("PRESENT", "WFH", "ON_DUTY"):
            late_m = i.get("late_minutes", 0)
            if late_m > 0:
                late_count += 1
                total_late_minutes += late_m
            ot_m = i.get("overtime_minutes", 0)
            if ot_m > 0:
                total_overtime_minutes += ot_m
            if is_weekday:
                if i.get("half_day"):
                    present_days += 0.5
                else:
                    present_days += 1.0

        if status == "LEAVE":
            leave_days += 1

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(round_rate(Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary_for_a_month(
    month: str,
    department: Optional[str] = None
):
    try:
        start_date_obj, end_date_obj, _ = parse_month_input(month)
    except ValueError as e:
        raise HTTPException(422, str(e))

    start_date = start_date_obj.isoformat()
    end_date = end_date_obj.isoformat()

    match_filter = {"joined_on": {"$lte": end_date}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
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
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        }
    ]

    employees = list(db.employees.aggregate(pipeline))
    dept_data = {}
    for emp in employees:
        dept = emp["department"]

        if dept not in dept_data:
            dept_data[dept] = {
                "department": dept,
                "headcount": 0,
                "present_days": 0.0,
                "work_hours_list": [],
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0
            }

        dept_data[dept]["headcount"] += 1

        for log in emp.get("logs", []):
            log_date = date.fromisoformat(log["date"])
            is_weekday = log_date.weekday() < 5
            status = log.get("status")

            if status in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
                if log.get("half_day"):
                    dept_data[dept]["present_days"] += 0.5
                else:
                    dept_data[dept]["present_days"] += 1.0

            if status in ("PRESENT", "WFH", "ON_DUTY") and log.get("work_hours") is not None:
                dept_data[dept]["work_hours_list"].append(log["work_hours"])

            late_mins = log.get("late_minutes", 0)
            if late_mins > 0:
                dept_data[dept]["late_count"] += 1
                dept_data[dept]["total_late_minutes"] += late_mins

            if status == "LEAVE":
                dept_data[dept]["leave_count"] += 1

            if status == "ON_DUTY":
                dept_data[dept]["on_duty_count"] += 1

    items = []
    for dept in sorted(dept_data.keys()):
        info = dept_data[dept]
        wh_list = info["work_hours_list"]

        avg_work_hours = None
        if len(wh_list) > 0:
            avg_val = Decimal(str(sum(wh_list))) / Decimal(str(len(wh_list)))
            avg_work_hours = float(round_number(avg_val))

        items.append({
            "department": dept,
            "headcount": info["headcount"],
            "present_days": info["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": info["late_count"],
            "total_late_minutes": info["total_late_minutes"],
            "leave_count": info["leave_count"],
            "on_duty_count": info["on_duty_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_comers_leaderboard_of_a_month(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    try:
        start_date_obj, end_date_obj, _ = parse_month_input(month)
    except ValueError as e:
        raise HTTPException(422, str(e))

    start_date = start_date_obj.isoformat()
    end_date = end_date_obj.isoformat()

    pipeline = [
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
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.append({"$sort": {"total_late_minutes": -1, "_id": 1}})

    docs = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for idx, doc in enumerate(docs):
        if idx > 0 and doc["total_late_minutes"] == docs[idx - 1]["total_late_minutes"]:
            rank = items[-1]["rank"]
        else:
            rank = idx + 1

        if rank > limit:
            break

        items.append({
            "rank": rank,
            "emp_code": doc["_id"],
            "name": doc["emp"]["name"],
            "department": doc["emp"]["department"],
            "total_late_minutes": doc["total_late_minutes"],
            "late_count": doc["late_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def daily_attendance_trend_for_a_department(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(422, "Invalid date range: 'to' must be >= 'from' and within 92 days")

    dept_employees = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1}
    ))
    if not dept_employees:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in dept_employees]
    dt_from = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    dt_to = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }
        },
        {"$addFields": {"day_date": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "day_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to + timedelta(days=1)]
                }
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day_date"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {"$sort": {"date": 1}}
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for i, r in enumerate(rows):
        cur_date_str = r["date"]
        cur_date = date.fromisoformat(cur_date_str)
        is_working_day = cur_date.weekday() < 5

        headcount = sum(1 for e in dept_employees if e["joined_on"] <= cur_date_str)
        present_count = float(r["present_count"])
        late_count = int(r["late_count"])

        attendance_rate = None
        if is_working_day and headcount > 0:
            rate_val = Decimal(str(present_count)) / Decimal(str(headcount))
            attendance_rate = float(round_rate(rate_val))

        items.append({
            "date": cur_date_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None
        })

    for i in range(len(items)):
        window = items[max(0, i - 6): i + 1]
        valid_rates = [
            Decimal(str(w["attendance_rate"]))
            for w in window
            if w["attendance_rate"] is not None
        ]
        if valid_rates:
            avg_rate = sum(valid_rates) / Decimal(str(len(valid_rates)))
            items[i]["moving_avg_7d"] = float(round_rate(avg_rate))
        else:
            items[i]["moving_avg_7d"] = None

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def mongodb_explain_endpoint_for_an_endpoint_main_query(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    limit: int = 10,
    page: int = 1,
    page_size: int = 20
):
    from bson import json_util

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
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

        raw_explain = db.command(
            "explain",
            {
                "find": collection,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            verbosity="executionStats"
        )

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        collection = "attendance_logs"
        start_date_obj, end_date_obj, _ = parse_month_input(month)
        pipeline = [{
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_obj.isoformat(), "$lte": end_date_obj.isoformat()}
            }
        }]
        raw_explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        collection = "employees"
        _, end_date_obj, _ = parse_month_input(month)
        match_filter = {"joined_on": {"$lte": end_date_obj.isoformat()}}
        if department:
            match_filter["department"] = department

        pipeline = [{"$match": match_filter}]
        raw_explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        collection = "attendance_logs"
        start_date_obj, end_date_obj, _ = parse_month_input(month)
        pipeline = [{
            "$match": {
                "date": {"$gte": start_date_obj.isoformat(), "$lte": end_date_obj.isoformat()},
                "late_minutes": {"$gt": 0}
            }
        }]
        raw_explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        collection = "attendance_logs"
        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [{
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        }]
        raw_explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )

    else:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")

    sanitized_explain = json.loads(json_util.dumps(raw_explain))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": sanitized_explain
    }


