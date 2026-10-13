# DATA_MODEL.md: what is stored in MongoDB

This file describes the two collections the grader **seeds directly into MongoDB**. Your code must read them exactly as
described here, and what your code writes must look the same. The HTTP contract and the business rules (R1 - R10) live in
`openapi.yaml`; this file refers to rules by number and does not repeat them.

| | |
|---|---|
| Database | the name in `MONGO_DB` |
| Collections | `employees`, `attendance_logs` |
| Indexes | **None** except the default `_id` index. Creating the ones you need, at startup, is your job. |

## 1. Types at a glance

| Kind of value | In MongoDB | In the HTTP API |
|---|---|---|
| An instant (a punch, a creation time, a history time) | BSON date (datetime, UTC) | integer epoch **milliseconds** |
| A calendar date (`date`, `joined_on`) | string `"YYYY-MM-DD"` | the same string |
| A time of day (`shift_start`, `shift_end`) | string `"HH:MM"`, 24-hour, IST | the same string |
| A duration | `work_hours` double, minutes as ints | the same numbers |

Calendar dates and times of day are plain strings **on purpose**: a date is not an instant, and turning it into one forces
a time zone on it. Instants are real datetimes in MongoDB, and your code is responsible for converting them to and from
epoch milliseconds at the API boundary. By default PyMongo returns **naive** datetimes (no time zone); decide how you
will handle that.

## 2. `employees`

One document per employee.

| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | **Internal to MongoDB.** Never accepted or returned by the API; do not use it to identify employees. |
| `emp_code` | string | **The employee's identifier everywhere in this assignment.** A customer-generated business code assigned by the customer's HR team, for example `EMP0001`. It must be unique and never changes. It is **not** the `_id`. |
| `name` | string | |
| `email` | string | |
| `department` | string | Case-sensitive. |
| `shift_start` | string `"HH:MM"` | IST. Start of the employee's shift. |
| `shift_end` | string `"HH:MM"` | IST. If `shift_end` <= `shift_start`, the shift is overnight (see R1). |
| `joined_on` | string `"YYYY-MM-DD"` | First day of employment (see R7, R9). |
| `created_at` | BSON date | When the record was created. Set by the API when it creates an employee; never supplied by a client. |

Sample:
```json
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05",
  "created_at": {
    "$date": "2026-01-05T04:30:00Z"
  }
}
```

## 3. `attendance_logs`

At most **one document per (`emp_code`, `date`)**. `emp_code` points at `employees.emp_code`. There is no ObjectId reference.

| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Internal. Not part of the API; a record is addressed by (`emp_code`, `date`). |
| `emp_code` | string | The employee. |
| `date` | string `"YYYY-MM-DD"` | The **attendance day**, an IST calendar date, defined by R1. For an overnight shift it is the day the shift started. |
| `status` | string | Exactly one of the five values in the status table below. |
| `punch_in` | BSON date or null | Set at punch-in. Null (or missing) for `ABSENT` / `LEAVE`. |
| `punch_out` | BSON date or null | Null until the employee punches out. |
| `work_hours` | double or null | **Computed by the API**, never supplied by a client (R4). Null until `punch_out` exists. |
| `late_minutes` | int | **Computed by the API** at punch-in (R2). Missing means 0. |
| `overtime_minutes` | int | **Computed by the API** at punch-out (R3). Missing means 0. |
| `half_day` | bool | **Computed by the API** at punch-out (R5). Missing means false. |
| `history` | array | Audit trail of manual corrections. See section 4. Missing means empty. |

### Status values

| `status` | Meaning | Punch times | Counts as present (R6) | Who creates it |
|---|---|---|---|---|
| `PRESENT` | Worked on site | `punch_in` required | yes | Punch-in |
| `WFH` | Worked from home | `punch_in` required | yes | Punch-in |
| `ON_DUTY` | Official duty away from the office | `punch_in` required | yes | Punch-in |
| `ABSENT` | Did not work, not on approved leave | both null | no | Seeded back-office data, or a correction |
| `LEAVE` | On approved leave | both null | no | Seeded back-office data, or a correction |

No endpoint creates `ABSENT` or `LEAVE` records. A weekday with **no document at all** is different from an `ABSENT`
document; the day has simply not been recorded.

### Stored derived values are authoritative

`work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are written by the API, and only by the API, when it
punches in, punches out or corrects a record. Analytics must **read the stored values** and not recompute them from the
punch times. In the grader's seeded data the stored values are what the analytics are defined over.

For a record your own code writes:

| Situation | `work_hours` | `late_minutes` | `overtime_minutes` | `half_day` |
|---|---|---|---|---|
| presence status, no `punch_out` yet | null | from R2 | 0 | false |
| presence status, with `punch_out` | R4 | from R2 | R3 | R5 |
| `ABSENT` or `LEAVE` | null | 0 | 0 | false |

## 4. `history`: the audit trail

`history` records **manual corrections only** (regularizations). Punch-in and punch-out are the original events and never
write to it. Each correction appends exactly one entry; entries are never edited or removed.

| Field | Type | Notes |
|---|---|---|
| `at` | BSON date | When the correction was made. |
| `by` | string | Who made it (free text, no authentication in this assignment). |
| `reason` | string | Why. |
| `changes` | object | One key per field that **actually changed**, each `{ "from": ..., "to": ... }`. Keys can be `status`, `punch_in`, `punch_out`, `work_hours`, `late_minutes`, `overtime_minutes`, `half_day`. |

In MongoDB the `from` / `to` values of `punch_in` and `punch_out` are BSON dates, like the fields themselves. In the API they
are epoch milliseconds. This is easy to forget because the dates sit inside a nested object.
