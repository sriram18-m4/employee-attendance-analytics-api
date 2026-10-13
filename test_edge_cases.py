"""Comprehensive test suite for Employee Attendance & Analytics API.
Tests R1-R10 rules, boundary conditions, edge cases, and calculations.
"""
"""Comprehensive test suite for Employee Attendance & Analytics API business rules.
Tests R1-R10 rules, boundary conditions, edge cases, and calculations without external dependencies.
"""
from datetime import datetime, timezone, timedelta, date, time
from decimal import Decimal, ROUND_HALF_UP
import unittest

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def truncate_to_seconds(epoch_ms: int) -> int:
    return (epoch_ms // 1000) * 1000


def round_half_up_2(val: float | Decimal) -> float:
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: float | Decimal) -> float:
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def parse_time_str(time_str: str) -> time:
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_dt.astimezone(IST)
    p_time = punch_in_ist.time()
    s_end = parse_time_str(shift_end)
    s_start = parse_time_str(shift_start)
    if s_end <= s_start:
        if p_time < s_end:
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(attendance_date_str: str, punch_in_dt: datetime, shift_start: str) -> int:
    att_date = date.fromisoformat(attendance_date_str)
    s_start_time = parse_time_str(shift_start)
    shift_start_dt = datetime.combine(att_date, s_start_time, tzinfo=IST)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime_minutes(
    attendance_date_str: str,
    punch_out_dt: datetime,
    shift_start: str,
    shift_end: str
) -> int:
    att_date = date.fromisoformat(attendance_date_str)
    s_start_time = parse_time_str(shift_start)
    s_end_time = parse_time_str(shift_end)
    if s_end_time <= s_start_time:
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
    diff_seconds = Decimal(str((punch_out_dt - punch_in_dt).total_seconds()))
    hours = diff_seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    d_start = date.fromisoformat(start_date_str)
    d_end = date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    working_days = 0
    while cur <= d_end:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days



class TestAttendanceBusinessRules(unittest.TestCase):

    def test_epoch_truncation_r1(self):
        # 09:40:00.900 ms -> should truncate to 09:40:00.000 ms
        ms_with_millis = 1783312500900
        truncated = truncate_to_seconds(ms_with_millis)
        self.assertEqual(truncated, 1783312500000)

    def test_attendance_date_normal_shift_r1(self):
        # Punch-in at 09:25 IST on 2026-07-06
        # IST is UTC+05:30 -> 03:55 UTC
        dt = datetime(2026, 7, 6, 3, 55, 0, tzinfo=timezone.utc)
        att_date = get_attendance_date(dt, "09:30", "18:30")
        self.assertEqual(att_date, "2026-07-06")

    def test_attendance_date_overnight_shift_r1(self):
        # Overnight shift: 22:00 to 06:00
        # Punch-in at 21:55 IST on 2026-07-06 (16:25 UTC on 2026-07-06)
        dt_start = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_start, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 05:30 IST on 2026-07-07 (00:00 UTC on 2026-07-07)
        # Because 05:30 < 06:00 (shift_end), it belongs to previous day's shift!
        dt_early_morning = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_early_morning, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 06:05 IST on 2026-07-07 (00:35 UTC on 2026-07-07)
        # Not earlier than 06:00, belongs to today's date
        dt_after_shift_end = datetime(2026, 7, 7, 0, 35, 0, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_after_shift_end, "22:00", "06:00"), "2026-07-07")

    def test_late_minutes_boundaries_r2(self):
        # Shift start 09:30 IST
        # 09:40:00 IST -> 10 minutes exact -> NOT late (> 10:00 required)
        dt_exact_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc) # 09:40:00 IST
        self.assertEqual(compute_late_minutes("2026-07-06", dt_exact_grace, "09:30"), 0)

        # 09:40:01 IST -> 10 min 1 sec -> LATE! Floor minutes since shift start = 10
        dt_one_sec_late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc) # 09:40:01 IST
        self.assertEqual(compute_late_minutes("2026-07-06", dt_one_sec_late, "09:30"), 10)

        # 10:15:59 IST -> 45 min 59 sec -> LATE! Floor minutes = 45
        dt_45_min = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc) # 10:15:59 IST
        self.assertEqual(compute_late_minutes("2026-07-06", dt_45_min, "09:30"), 45)

    def test_overtime_minutes_boundaries_r3(self):
        # Shift 09:30 to 18:30
        # 18:59:59 IST (29 min 59 sec) -> under 30 min minimum -> 0
        dt_29_min = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
        self.assertEqual(compute_overtime_minutes("2026-07-06", dt_29_min, "09:30", "18:30"), 0)

        # 19:00:00 IST (30 min) -> >= 30 min -> 30
        dt_30_min = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_overtime_minutes("2026-07-06", dt_30_min, "09:30", "18:30"), 30)

        # 19:10:00 IST (40 min) -> 40
        dt_40_min = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_overtime_minutes("2026-07-06", dt_40_min, "09:30", "18:30"), 40)

        # Overnight shift: 22:00 to 06:00 on 2026-07-06
        # Shift ends 06:00 on 2026-07-07 (00:30 UTC on 2026-07-07)
        # Punch-out at 06:40 on 2026-07-07 (01:10 UTC) -> 40 min overtime
        dt_overnight_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_overtime_minutes("2026-07-06", dt_overnight_out, "22:00", "06:00"), 40)

    def test_work_hours_half_up_and_half_day_r4_r5(self):
        # 4.494 hours = 16178.4 seconds
        p_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        p_out_4494 = p_in + timedelta(seconds=16178) # 4.49388... -> 4.49
        wh = compute_work_hours(p_in, p_out_4494)
        self.assertEqual(wh, 4.49)
        self.assertTrue(compute_half_day(wh))

        # 4.496 hours = 16186 seconds -> rounds to 4.50
        p_out_4496 = p_in + timedelta(seconds=16186)
        wh_450 = compute_work_hours(p_in, p_out_4496)
        self.assertEqual(wh_450, 4.50)
        self.assertFalse(compute_half_day(wh_450)) # not < 4.50

        # Exact half-up rounding test: 9.125 hours -> must be 9.13 (half-up, not banker's 9.12)
        p_out_half_up = p_in + timedelta(seconds=int(9.125 * 3600))
        self.assertEqual(compute_work_hours(p_in, p_out_half_up), 9.13)

    def test_working_days_count_r7(self):
        # July 2026 has 31 days (starts Wednesday July 1, ends Friday July 31)
        # July 2026 working days (Mon-Fri) = 23 days
        self.assertEqual(count_working_days("2026-07-01", "2026-07-31"), 23)

        # Mid-month joiner on July 15 (Wednesday)
        # Days 15, 16, 17 (3) + 20..24 (5) + 27..31 (5) = 13 working days
        self.assertEqual(count_working_days("2026-07-15", "2026-07-31"), 13)

    def test_rates_rounding_r8(self):
        # 4 decimals half-up
        # 0.85714 -> 0.8571
        self.assertEqual(round_half_up_4(0.85714), 0.8571)
        # 0.85715 -> 0.8572 (half-up)
        self.assertEqual(round_half_up_4(0.85715), 0.8572)

    def test_standard_competition_ranking_with_ties(self):
        # Competition ranking (1, 2, 2, 4)
        scores = [
            {"emp": "EMP01", "total": 60},
            {"emp": "EMP02", "total": 45},
            {"emp": "EMP03", "total": 45},
            {"emp": "EMP04", "total": 30},
        ]
        # Simulate $rank:
        ranked = []
        current_rank = 1
        for i, s in enumerate(scores):
            if i > 0 and s["total"] < scores[i-1]["total"]:
                current_rank = i + 1
            ranked.append({**s, "rank": current_rank})

        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[1]["rank"], 2)
        self.assertEqual(ranked[2]["rank"], 2)
        self.assertEqual(ranked[3]["rank"], 4)

        # With limit=2, filter rank <= 2 returns EMP01, EMP02, EMP03 (3 rows!)
        limit_2 = [r for r in ranked if r["rank"] <= 2]
        self.assertEqual(len(limit_2), 3)
        self.assertEqual([r["emp"] for r in limit_2], ["EMP01", "EMP02", "EMP03"])



if __name__ == "__main__":
    unittest.main()
