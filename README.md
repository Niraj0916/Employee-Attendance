# Employee Attendance & Analytics API

FastAPI backend service implementing attendance tracking, manual regularization with audit trails, and MongoDB aggregation analytics. Conforms to OpenAPI contract v2.0.

## How to Run

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**:
   Set `MONGO_URI` and `MONGO_DB` (or create a local `.env`):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
   *Note: Real environment variables take precedence over `.env`.*

3. **Seed Sample Data (Optional)**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

The application creates all required database indexes automatically and idempotently on startup.

## Testing

Run the automated test suites:
```bash
python test_runner.py
python test_edge_cases.py
```

## Implemented Endpoints

- **System**: `GET /health` (readiness ping)
- **Employees**: `POST /employees`, `GET /employees` (sorted, filtered, paginated)
- **Attendance**:
  - `POST /attendance/punch-in` (race-safe, R1 overnight, R2 10m grace)
  - `POST /attendance/punch-out` (atomic concurrency, R3 overtime >= 30m, R4 work hours, R5 half-day)
  - `GET /attendance` (index-backed sorting and pagination)
  - `PATCH /attendance/{emp_code}/{date}` (regularization with audit history)
- **Analytics (MongoDB Aggregation Pipelines)**:
  - `GET /analytics/employees/{emp_code}/monthly` (working days from joined_on, presence count)
  - `GET /analytics/departments/summary` (preserves zero-log headcount, average work hours)
  - `GET /analytics/leaderboard/late` (standard competition ranking with ties 1, 2, 2, 4)
  - `GET /analytics/departments/{department}/trend` (gap-filled daily series with 7-day moving average)
- **Admin**:
  - `GET /admin/explain/{endpoint}` (returns executionStats proving IXSCAN usage)

## Deliverables

- `app/main.py`: Single-file FastAPI application.
- `requirements.txt`: Project dependencies.
- `REVIEW.md`: Starter code audit and fixes (Part A).
- `DECISIONS.md`: Architectural design decisions (Part C).
- `README.md`: Execution instructions and notes.
