# Domain Review Checklists

Reference for per-PR agent deep review (Pass 3). Apply relevant sections based on `ctx["domains_touched"]`.

---

## Python Quality

### Code Style & Idioms
- [ ] Type annotations on all public functions and methods (PEP 484)
- [ ] Dataclasses or Pydantic models used instead of raw dicts for structured data
- [ ] f-strings preferred over .format() or % formatting
- [ ] List/dict/set comprehensions preferred over map/filter with lambda
- [ ] `pathlib.Path` used instead of `os.path` string concatenation
- [ ] No mutable default arguments (`def foo(x=[])` is a bug)
- [ ] Context managers (`with`) used for all resource acquisition (files, DB connections, locks)
- [ ] `__all__` defined in modules that are meant to be imported as libraries

### Error Handling
- [ ] No bare `except:` or `except Exception:` without re-raising or specific logging
- [ ] Custom exceptions inherit from appropriate base class, not just `Exception`
- [ ] Errors are caught at the right layer — not swallowed deep in utilities
- [ ] `finally` or context managers used to guarantee cleanup (no resource leaks)
- [ ] Error messages include enough context to debug (request ID, input summary)

### Performance
- [ ] No I/O inside tight loops (DB calls, HTTP requests, file reads)
- [ ] Generator expressions used instead of list comprehensions when the result is iterated once
- [ ] `functools.lru_cache` or `functools.cache` for expensive pure computations
- [ ] Bulk operations preferred (batch_get, executemany) over per-item loops
- [ ] Async functions not calling blocking I/O without `asyncio.to_thread` or executor

### Logging
- [ ] `structlog` or `logging` with structured fields, not print()
- [ ] Log levels used correctly: DEBUG for verbose tracing, INFO for events, WARNING for recoverable issues, ERROR for failures
- [ ] No sensitive data (PII, credentials, tokens) in log messages
- [ ] Correlation IDs / request IDs propagated through call chains

### Security
- [ ] `subprocess` calls use list form, not shell=True with user input
- [ ] `eval()` / `exec()` not used on user-controlled strings
- [ ] `pickle.loads` not used on untrusted input
- [ ] `yaml.safe_load` used instead of `yaml.load`
- [ ] Secrets loaded from env vars or AWS Secrets Manager, not hardcoded

---

## Testing

### Coverage & Structure
- [ ] New business logic has corresponding unit tests
- [ ] Happy path AND at least one failure/edge case tested per function
- [ ] Tests are in the right category: unit (mocked I/O) vs integration (real I/O)
- [ ] Test file mirrors source structure (`src/foo/bar.py` -> `tests/foo/test_bar.py`)
- [ ] No tests that only assert the mock was called — they test nothing about behavior

### pytest Best Practices
- [ ] Fixtures in `conftest.py` at appropriate scope (function / module / session)
- [ ] `@pytest.mark.parametrize` for multiple input variants instead of loops
- [ ] No `time.sleep()` in tests, use `freezegun` or mock time
- [ ] `pytest.raises` used as context manager, not just calling the function and ignoring the error
- [ ] No `assert` in fixtures (use `pytest.fail()` for setup failures)

### Mocking
- [ ] `unittest.mock.patch` targets the import location, not the definition location
- [ ] `MagicMock` side_effect used to simulate exceptions
- [ ] AWS services mocked with `moto`, not raw MagicMock, for realistic behavior
- [ ] Database calls mocked at the driver layer, not the ORM layer, for unit tests

### CI Hygiene
- [ ] Tests are deterministic (no random seeds, no reliance on system clock without mocking)
- [ ] Tests clean up after themselves (temp files, DB records, env vars)
- [ ] No `pytest.skip` without a tracked issue reference

---

## AWS Lambda

### Handler Design
- [ ] Handler is thin — business logic lives in importable modules, not inline in the handler
- [ ] Expensive initialization (DB connections, SDK clients, config loading) done at module level, outside the handler function (reused across warm invocations)
- [ ] Handler explicitly validates the event schema at the boundary (never assume shape)
- [ ] Return value matches what the event source expects (API GW expects `statusCode`+`body`, SQS expects nothing or partial batch failures)

### Cold Start Optimization
- [ ] Lambda package size minimized (no dev dependencies, no compiled binaries for wrong arch)
- [ ] ARM64 (Graviton2) considered for Python workloads (cheaper + often faster)
- [ ] Lambda Layers used for large shared dependencies (reduces deployment package)
- [ ] `POWERTOOLS_LOG_LEVEL` and other config via env vars, not hardcoded

### Concurrency & Timeouts
- [ ] Timeout set explicitly and is less than the upstream caller's timeout (API GW: 29s max)
- [ ] Reserved concurrency set on latency-sensitive functions to prevent throttling by noisy neighbors
- [ ] Provisioned concurrency considered for P99 latency-sensitive paths
- [ ] Function does not assume sequential execution of batches (SQS/Kinesis can fan out)

### Error Handling
- [ ] SQS event source: returns `batchItemFailures` for partial batch failures (not all-or-nothing)
- [ ] Kinesis event source: `bisectBatchOnFunctionError` or `reportBatchItemFailures` configured
- [ ] DLQ configured on event source mappings and async invocations
- [ ] Lambda Destinations used for async success/failure routing where appropriate

### IAM
- [ ] Execution role uses least-privilege — specific actions on specific resources
- [ ] No `*` in resource ARNs unless truly necessary (use `arn:aws:dynamodb:*:*:table/my-table`)
- [ ] VPC config only when Lambda needs to reach resources in a VPC (adds cold start penalty)
- [ ] Secrets accessed via AWS Secrets Manager or SSM Parameter Store, not env vars for sensitive values

### Observability
- [ ] AWS Lambda Powertools used for structured logging, tracing (X-Ray), and metrics
- [ ] Custom metrics emitted via `aws_lambda_powertools.metrics` (not CloudWatch `put_metric_data` in hot path)
- [ ] Cold start logged at INFO level
- [ ] Correlation ID propagated from event headers into all log records

---

## DynamoDB

### Data Modeling
- [ ] Access patterns identified and documented before table design
- [ ] Single-table design used when multiple entity types share access patterns
- [ ] Composite sort key enables range queries (`entity#subentity#id`)
- [ ] GSIs have projected attributes limited to what queries actually need (not ALL unless justified)
- [ ] Sparse indexes used for conditional GSIs (only items that have the attribute are indexed)

### Read/Write Patterns
- [ ] No `Scan` on tables with >10k items unless it's a one-off admin operation
- [ ] `Query` used with both PK and a SK condition where possible (reduces RCU)
- [ ] `batch_write_item` / `batch_get_item` used for bulk operations (25 write / 100 read limits)
- [ ] Transact writes (`transact_write_items`) used for multi-item atomic operations
- [ ] Conditional writes used to prevent lost updates (optimistic locking with `version` attribute)

### Capacity & Cost
- [ ] PAY_PER_REQUEST used for unpredictable workloads, provisioned for steady-state
- [ ] TTL attribute set on ephemeral/session data (avoids manual deletes and reduces storage cost)
- [ ] Item sizes kept under 1KB where possible (each RCU/WCU covers 1KB; large items multiply cost)
- [ ] Avoid hot partition keys (e.g., current date, sequential IDs, single user ID for all writes)

### Streams & Events
- [ ] DynamoDB Streams `StreamViewType` is the minimum needed (`NEW_IMAGE` vs `NEW_AND_OLD_IMAGES`)
- [ ] Stream consumer Lambda has appropriate batch size and bisect-on-error enabled
- [ ] Stream processor is idempotent (DynamoDB Streams guarantees at-least-once)

---

## PostgreSQL

### Query Safety
- [ ] All user input passed via parameterized queries, never string interpolation
- [ ] `LIKE` patterns with user input escape `%` and `_` metacharacters
- [ ] No `SELECT *` in production queries (enumerate columns explicitly)
- [ ] `LIMIT` clause on all queries that could return unbounded rows

### Indexing
- [ ] New columns used in `WHERE`, `JOIN ON`, or `ORDER BY` have appropriate indexes
- [ ] Composite index column order matches query patterns (leading column selectivity)
- [ ] Partial indexes used for queries on a filtered subset (e.g., `WHERE deleted_at IS NULL`)
- [ ] `EXPLAIN ANALYZE` run on new queries touching large tables

### Transactions & Locks
- [ ] Transaction isolation level explicitly set when default (READ COMMITTED) is insufficient
- [ ] Long-running transactions avoided (hold locks for as short as possible)
- [ ] `SELECT ... FOR UPDATE` used correctly (not on reads that don't need locking)
- [ ] Deadlock risk assessed when acquiring multiple locks (consistent ordering)

### Migrations
- [ ] Migrations are reversible (have a `down` step)
- [ ] Additive changes (new columns, new tables) deployed before code that uses them
- [ ] Destructive changes (column drops, type changes) deployed after code that no longer uses them
- [ ] Large table migrations use `pg_repack` or `CONCURRENTLY` indexes to avoid full table locks
- [ ] Migration tested on a production-size dataset for runtime estimate

### Connection Management
- [ ] Connection pool configured with appropriate `min`, `max`, `timeout`
- [ ] Lambda uses `RDS Proxy` or persistent connection pooler (PgBouncer) to avoid connection exhaustion
- [ ] `pool_pre_ping=True` (SQLAlchemy) or equivalent to detect stale connections

---

## AWS S3

### Security
- [ ] Bucket has `BlockPublicAcls`, `BlockPublicPolicy`, `IgnorePublicAcls`, `RestrictPublicBuckets` all set to `true`
- [ ] Server-side encryption enabled (SSE-S3 minimum, SSE-KMS for sensitive data)
- [ ] Bucket policy denies non-HTTPS requests (`aws:SecureTransport` condition)
- [ ] Presigned URLs have short expiry (15 minutes for downloads, 5 minutes for uploads)
- [ ] No `s3:*` in IAM policies — use specific actions (`s3:GetObject`, `s3:PutObject`)

### Performance & Cost
- [ ] Large file uploads use multipart upload (>100MB)
- [ ] CloudFront used in front of S3 for frequently accessed public objects
- [ ] S3 Transfer Acceleration used only when clients are geographically distant
- [ ] `S3.Client.generate_presigned_url` called outside of Lambda hot path (cache the URL)
- [ ] S3 Intelligent-Tiering or lifecycle rules configured for objects not accessed frequently

### Reliability
- [ ] Versioning enabled on buckets storing critical data
- [ ] Object Lock / MFA delete considered for compliance/audit buckets
- [ ] S3 event notifications (SNS/SQS/Lambda) have retry + DLQ configured

---

## Terraform

### Code Quality
- [ ] Resources use `for_each` over `count` when managing collections of similar resources (avoids re-creation on reorder)
- [ ] Variables have `type`, `description`, and `default` (if optional)
- [ ] Sensitive variables marked with `sensitive = true`
- [ ] Output values expose only what consumers need (not entire resource objects)
- [ ] Locals used to avoid repeating expressions; locals are not overused for simple values

### Modules
- [ ] Modules have a clear single responsibility
- [ ] Module inputs validated with `validation` blocks where appropriate
- [ ] Module version pinned when sourcing from registry (`~> 3.0`, not `>= 3.0`)
- [ ] Internal module state not leaked via outputs that expose sensitive internals

### State Management
- [ ] Remote backend configured (S3 + DynamoDB locking)
- [ ] State file encrypted at rest (S3 SSE)
- [ ] No sensitive values stored in plaintext in state (use `sensitive = true` + data source lookup)
- [ ] Workspaces or separate state files used per environment (not a single state for prod + dev)
- [ ] `terraform state mv` or `moved` blocks used when renaming resources (avoids destroy + recreate)

### Dangerous Changes
- [ ] `terraform plan` output reviewed for any `destroy` actions on stateful resources (RDS, DynamoDB, S3)
- [ ] `lifecycle { prevent_destroy = true }` set on critical resources
- [ ] `create_before_destroy` used when replacing resources that other resources depend on
- [ ] `ignore_changes` used only when truly needed and commented with justification

### Security
- [ ] IAM policies follow least-privilege (no `*` actions without specific justification)
- [ ] Security groups do not have `0.0.0.0/0` ingress on port 22 or 3389
- [ ] KMS keys have deletion window set and key rotation enabled
- [ ] CloudTrail / Config rules enabled for audit in production

---

## Architecture

### Coupling & Cohesion
- [ ] Services communicate through well-defined interfaces (API contracts, event schemas), not direct DB access
- [ ] Domain logic not leaking into transport/handler layers (Lambda handlers, FastAPI routers)
- [ ] Shared libraries versioned and not monkey-patched in consumers
- [ ] Circular dependencies between modules absent

### Event-driven Patterns
- [ ] Events are named in past tense (`OrderPlaced`, not `PlaceOrder`)
- [ ] Event schemas are versioned and backward compatible
- [ ] Consumers are idempotent (same event processed twice produces same result)
- [ ] Event ordering assumptions documented (SQS does not guarantee FIFO without FIFO queues)

### Resilience
- [ ] Retry logic uses exponential backoff with jitter (not fixed intervals)
- [ ] Circuit breaker or timeout wrapping calls to external services
- [ ] Graceful degradation path when a dependency is unavailable
- [ ] Health check endpoints do not call all downstream dependencies (liveness vs readiness separation)

### Observability
- [ ] Distributed trace spans cross service boundaries (X-Ray / OpenTelemetry)
- [ ] Dashboards exist (or are planned) for new business metrics introduced by this PR
- [ ] Alert thresholds set for new Lambda functions / queues / DB tables
- [ ] SLOs defined or referenced for user-facing paths

### Scalability
- [ ] Stateless design — no assumption of sticky routing or in-process state between requests
- [ ] Hot partitions / hot keys avoided in DynamoDB and SQS
- [ ] Fan-out patterns use SNS + SQS (not direct Lambda-to-Lambda synchronous chains)
- [ ] Pagination implemented on all list endpoints (no unbounded result sets)
