# DECISIONS.md

1. **Indexes.**
We created: `employees(emp_code)` (unique) for identity and lookup; `employees(department, emp_code)` for covered department filtering and sorted listing; `attendance_logs(emp_code, date)` (unique) for natural-key uniqueness and race-safety; `attendance_logs(date DESC, emp_code ASC)` for index-backed sorted pagination; and `attendance_logs(emp_code, punch_in DESC)` for closing open punches. We considered and rejected a standalone index on `attendance_logs.status` due to low selectivity and redundancy with compound date indexes.

2. **Punch-in race.**
Both concurrent requests validate the employee and execute `insert_one`. The database-level unique compound index on `(emp_code, date)` acts as the arbiter: the first operation inserts successfully returning 201, while the concurrent operation throws a `DuplicateKeyError` (E11000). Our code catches this exception and returns 409 Conflict, preventing duplicate records without distributed locks.

3. **Ties.**
Our pipeline applies `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4), tie-broken by `emp_code` ascending. Because `$match: {rank: {$lte: limit}}` is applied after ranking, both tied employees are returned, satisfying the contract guarantee that ties at the cutoff are never arbitrarily truncated.

4. **Headcount.**
The department summary aggregation starts directly from the `employees` collection (filtering `joined_on <= month_end`), joining `attendance_logs` via `$lookup`. Headcount counts eligible employees (`$sum: 1`) before flattening logs, ensuring zero-log employees are fully counted.

5. **One thing you would change for 100x data.**
I would maintain pre-computed daily rollups and monthly counters updated asynchronously via MongoDB Change Streams, serving analytics lookups in $O(1)$ time rather than aggregating millions of raw punch records on demand.
