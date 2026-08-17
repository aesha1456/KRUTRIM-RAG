# Workers — Distributed Extraction and Ingestion

The `workers/` package contains the distributed worker processes used by Part A. Workers poll the extraction server for leased jobs, perform one bounded unit of work, submit a result, and keep a local cache until the server acknowledges the submission.

Three worker types are supported:

- **Text worker:** extracts text and structured metadata from PDF chunks.
- **Qdrant worker:** embeds and stores proposition or section batches.
- **Neo4j worker:** writes graph hierarchy, entities, specifications, and relationships.

Workers are independent processes. They may run on separate office machines or on separate processes on one machine, depending on the deployment layout.

## Directory layout

```text
workers/
├── base_worker.py          Shared HTTP session, lease heartbeat, retry, and result cache
├── text_workers.py         PDF extraction worker
├── qdrant_workers.py       Qdrant batch ingestion worker
├── neo4j_workers.py        Neo4j batch ingestion worker
├── config.py               Shared worker configuration
├── logger.py               Worker logging and timing decorators
├── test_base_worker.py     Base-worker tests
└── test_logger.py          Worker logger tests
```

## Worker lifecycle

Every worker follows the same high-level protocol:

```text
Start process
   │
   ├── Load configuration and create a persistent HTTP session
   ├── Create worker-specific logger and result cache
   ├── Replay locally cached results from a previous process
   │
   ▼
Poll extraction server
   │
   ├── WAIT      sleep and poll again
   └── PROCESS   receive job_id, lease_id, and batch metadata
          │
          ├── Start lease heartbeat
          ├── Download the required PDF/checkpoint data
          ├── Execute one extraction or ingestion unit
          ├── Cache the successful result locally
          ├── Submit the result with retry
          ├── Remove the cache entry after acknowledgement
          └── Stop the heartbeat and clean temporary files
```

The `lease_id` is important. It identifies the current assignment and prevents a stale worker from submitting a result after the server has reassigned the job.

## Text worker

Entry point: `workers/text_workers.py`

The text worker:

1. Obtains a leased PDF chunk from the extraction server.
2. Downloads the chunk bytes to a temporary local file.
3. Uses the fast OpenDataLoader path for normal extraction.
4. Uses direct Docling plus EasyOCR when OCR is enabled.
5. Normalizes extractor output into a common element schema.
6. Preserves page numbers, headings, tables, native references, and bounding boxes.
7. Returns Markdown plus structured `elements` metadata.
8. Removes the temporary PDF after processing.

The worker also manages local copies of Docling and Java resources. This avoids simultaneous reads and file-lock conflicts when several workers use a shared NAS or SMB model directory.

### Text result shape

A successful extraction result has the following conceptual structure:

```json
{
  "markdown": "...",
  "elements": [
    {
      "element_id": "...",
      "native_ref": "...",
      "type": "paragraph|heading|table",
      "content": "...",
      "page_number": 1,
      "heading_level": 2,
      "bounding_box": {},
      "table": null
    }
  ],
  "engine": "opendataloader"
}
```

Part A uses this metadata during chunking and page metadata generation. Do not remove geometry or page fields when changing normalization code.

## Qdrant worker

Entry point: `workers/qdrant_workers.py`

The Qdrant worker:

1. Polls `/get_qdrant_job`.
2. Receives a batch kind, range, and lease.
3. Downloads only the required checkpoint:
   - proposition batches use the proposition checkpoint;
   - section batches use the ready checkpoint.
4. Calls `parta.processing.ingest_qdrant.run_qdrant_batch()`.
5. Submits counts and batch metadata to `/submit_qdrant_result`.
6. Deletes temporary checkpoint files after the batch finishes.

The processing implementation is responsible for embedding and Qdrant upserts. The worker is intentionally a thin transport and lifecycle boundary.

## Neo4j worker

Entry point: `workers/neo4j_workers.py`

The Neo4j worker:

1. Creates one process-local Neo4j driver during `start_worker()`.
2. Polls `/get_neo4j_job`.
3. Downloads the ready checkpoint for the assigned chunk range.
4. Calls `parta.processing.ingest_neo4j.run_neo4j_batch()`.
5. Submits graph counters and result metadata to `/submit_neo4j_result`.
6. Closes the driver during shutdown.

The driver is deliberately not created at import time. This keeps the worker compatible with process spawning and avoids sharing a database driver across process boundaries.

## Shared reliability mechanisms

### Leases and heartbeats

The extraction server assigns a job with a lease. `LeaseHeartbeat` periodically renews that lease while the worker is processing. If the worker stops heartbeating, the server can recover the expired assignment and make it eligible for retry.

Always stop the heartbeat in a `finally` block. A heartbeat from a completed job can otherwise keep stale work alive on the server.

### Result cache

`ResultCache` writes a successful payload locally before submission. If the network fails after processing, the worker replays the cached payload during the next startup. The cache is cleared only after the server acknowledges the result.

Keep cache payloads compatible with the corresponding submit endpoint:

- `/submit_result` for text extraction.
- `/submit_qdrant_result` for Qdrant batches.
- `/submit_neo4j_result` for Neo4j batches.

### Submission retries

`submit_with_retry()` retries transient submission failures while preserving the original `job_id`, `worker_id`, and `lease_id`. A stale lease is expected to be rejected by the server; do not silently replace it with a new lease in the worker.

## Configuration

Edit `workers/config.py` or provide environment overrides where supported:

- `SERVER_URL` — Part A extraction server address.
- `HEARTBEAT_INTERVAL_SECONDS` — lease renewal interval.
- `WAIT_SLEEP_SECONDS` and `ERROR_SLEEP_SECONDS` — polling behavior.
- `QDRANT_POINT_BATCH_SIZE` and `QDRANT_UPLOAD_PARALLEL` — vector upload tuning.
- `NEO4J_CONNECTION_POOL_SIZE` and `NEO4J_LOCAL_WORKERS` — graph worker tuning.
- `OPENDATALOADER_HYBRID_URL` — optional external hybrid extraction backend.
- `RAG_DEVICE` — `auto`, `cuda`, or `cpu` selection.
- `RESULT_CACHE_DIR` — local crash-recovery cache root.

The text worker additionally uses the portable model and Java directories under `parta/portable/`, with per-worker local cache directories selected at runtime.

## Running workers

From the repository root, install the dependencies required by the worker type and run one process per worker:

```bash
python -m workers.text_workers
python -m workers.qdrant_workers
python -m workers.neo4j_workers
```

The text worker imports OCR and extraction dependencies during startup and may require the portable Docling, Java, and Hugging Face assets. Qdrant and Neo4j workers require access to their respective databases and to the Part A processing modules.

A worker should be started only after the Part A extraction server is reachable. The worker will retry connection failures, but it cannot process jobs until the server and required database/model services are available.

## Testing

Run the worker unit tests from the repository root:

```bash
PYTHONPATH=. pytest -q workers/test_base_worker.py workers/test_logger.py
```

These tests cover session setup, keepalive configuration, result-cache behavior, submission retries, logging, and timing decorators. Do not require a live extraction server for unit tests.

## Troubleshooting

### Worker repeatedly receives `WAIT`

Check that:

- Part A extraction server is running.
- The worker `SERVER_URL` resolves to the correct host and port.
- The corresponding job type has been started by Part A.
- Another worker has not already claimed all available jobs.

### Result is rejected as stale

The lease may have expired or another worker may have completed the assignment. Check worker heartbeat logs and server lease settings. Do not manually resubmit with a different lease identifier.

### Results remain in the local cache

This usually indicates a network interruption or an expired lease after processing. The cache is intentionally retained for replay. Confirm that the server is reachable before deleting cache files.

### Text worker model or Java startup errors

Check the local synchronized cache, the NAS source paths, Java availability, and offline Hugging Face settings. A worker may spend several minutes warming the hybrid backend before processing its first OCR-free chunk.

### Neo4j or Qdrant batch failures

Check database connectivity and verify that the downloaded checkpoint matches the assigned batch kind and range. The worker reports failures to the extraction server; the Part A Phase 2 controller can then resume the failed stage.

## Development notes

- Keep worker runtime loops inside `start_worker()` and behind `if __name__ == "__main__"` guards.
- Do not create process-sensitive database drivers or model backends at module import time.
- Preserve lease identity in every result payload.
- Cache results before network submission and clear them only after acknowledgement.
- Always clean temporary files and stop heartbeats in `finally` blocks.
- Avoid committing local CA keys, model files, worker caches, logs, uploaded PDFs, or database credentials.
