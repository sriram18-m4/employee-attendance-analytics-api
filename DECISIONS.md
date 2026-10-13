# DECISIONS.md

### 1. Indexes
* **`employees`**: Unique index on `emp_code` enforces business identifier uniqueness. Compound index on `(department, joined_on)` and index on `department` support department summary and trend queries without table scans.
* **`attendance_logs`**: Unique compound index on `(emp_code, date)` guarantees at most one record per employee-day and blocks punch-in races. Compound index on `(date: -1, emp_code: 1)` serves the sorted attendance list. Compound index on `(date: 1, late_minutes: 1)` accelerates the leaderboard.
* **Rejected index**: A separate index on `history` or `work_hours`. These derived and array fields have high update churn, so indexing them would increase write overhead with little query benefit.

### 2. Punch-in race
Both concurrent requests calculate the same attendance date and run `insert_one`. The database engine commits the first insert (returning 201). The second insert violates the unique index on `(emp_code, date)`, throws `DuplicateKeyError`, which our `try/except` catches and returns as HTTP 409 Conflict.

### 3. Ties
We compute ranks using `$setWindowFields` with `$rank` and filter `rank <= limit` afterward. If the top records have late minutes [60, 45, 45, 30] and `limit=2`, ranks are [1, 2, 2, 4]. Both tied employees at rank 2 are included, returning 3 rows total.

### 4. Headcount
The department summary aggregation starts on `employees` (matching `joined_on <= end_of_month`), grouping by department with `$sum: 1`. Logs are joined via `$lookup`. Employees with zero logs still increment the department's headcount.

### 5. At 100x scale
Maintain pre-aggregated daily and monthly counters in a summary collection updated atomically during punch-out and regularization, instead of scanning raw attendance logs on every analytics call.

