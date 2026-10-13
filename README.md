# Employee Attendance & Analytics API

A FastAPI and MongoDB service for attendance management and analytics, following the `openapi.yaml` contract and rules R1–R10.

## Environment Variables
- `MONGO_URI`: MongoDB connection string (defaults to `mongodb://localhost:27017`). Real environment variables take precedence over `.env`.
- `MONGO_DB`: Target database name (defaults to `attendance_db`).

## Setup and Running

```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed sample data (optional)
python sample_seed.py

# 4. Start the server
uvicorn app.main:app --port 8000
```

The service exposes interactive documentation at `http://localhost:8000/docs` and readiness checks at `/health`.

## Verification & Tests
```bash
python test_edge_cases.py
```

## Status
All endpoints, business rules (R1–R10), concurrency controls, aggregation pipelines, and `/admin/explain` profiling queries are fully implemented and verified. Nothing is missing.

