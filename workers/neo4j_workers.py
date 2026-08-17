"""
neo4j_workers.py — Batch Neo4j ingestion worker.
Polls the server for jobs, downloads data files, runs batch ingestion,
and submits results with retry + local crash-safe caching.
"""

import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workers.logger import log_process, setup_worker_logger
from workers.config import (
    HEARTBEAT_INTERVAL_SECONDS,
    NEO4J_CONNECTION_POOL_SIZE,
    RESULT_CACHE_DIR,
    SERVER_URL,
)
from workers.base_worker import (
    create_session,
    LeaseHeartbeat,
    ResultCache,
    submit_with_retry,
    detect_device,
)

import requests

from neo4j import GraphDatabase
from parta.processing.ingest_neo4j import run_neo4j_batch, NEO4J_URI, NEO4J_AUTH

# ─── Constants ────────────────────────────────────────────────────────────────
WORKER_ID = f"neo4j-{uuid.uuid4().hex[:6]}"
BASE_DIR = Path(__file__).resolve().parent.parent / "parta"

# ─── Logger setup ─────────────────────────────────────────────────────────────
logger = setup_worker_logger("neo4j", WORKER_ID)

# ─── Persistent session — reuses TCP connection across all poll cycles ────────
_session = create_session()

# ─── Crash-safe result cache ──────────────────────────────────────────────────
_cache = ResultCache("neo4j", base_dir=RESULT_CACHE_DIR)
_heartbeat = LeaseHeartbeat(SERVER_URL, WORKER_ID, HEARTBEAT_INTERVAL_SECONDS)

# The driver is created inside start_worker(), never while this module is being
# imported. That is important on Windows: a spawned child imports this module
# again, and active Neo4j drivers must not be created or shared across process
# boundaries during interpreter bootstrap.
_neo4j_driver = None


@log_process
def execute_neo4j_batch(book_id, ready_path, batch_start, batch_count):
    if _neo4j_driver is None:
        raise RuntimeError("Neo4j worker driver has not been initialized")
    return run_neo4j_batch(
        book_id=book_id,
        ready_path=ready_path,
        base_dir=str(BASE_DIR),
        batch_start=batch_start,
        batch_count=batch_count,
        driver=_neo4j_driver,
    )


# ─── Main worker entry point ──────────────────────────────────────────────────
def start_worker():
    """Run the polling loop with all runtime state initialized after import.

    The Neo4j ingestion path uses threads for optional local extraction, but
    keeping this entry point guarded also makes the worker safe if a dependency
    or future optimization starts a process under Windows' ``spawn`` method.
    """
    global _neo4j_driver
    is_connected = False

    logger.info("=" * 80)
    logger.info("NEO4J WORKER STARTED (batch mode)")
    logger.info("SERVER      : %s", SERVER_URL)
    logger.info("BASE_DIR    : %s", BASE_DIR)
    logger.info("CACHE_DIR   : %s", _cache.cache_dir)
    detect_device(logger)
    logger.info("=" * 80)

    # Replay cached results before polling for new jobs.
    _cache.replay(_session, f"{SERVER_URL}/submit_neo4j_result")

    # Create one driver for this worker process and reuse it across batches.
    # Neo4j drivers are thread-safe and expensive to create, but should not be
    # initialized in a parent process and then inherited by spawned children.
    logger.info("[NEO4J-BATCH] Connecting to %s ...", NEO4J_URI)
    _neo4j_driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=NEO4J_AUTH,
        max_connection_lifetime=3600,
        connection_acquisition_timeout=120,
        max_connection_pool_size=NEO4J_CONNECTION_POOL_SIZE,
    )
    _neo4j_driver.verify_connectivity()
    logger.info("[NEO4J-BATCH] Neo4j connected (persistent driver).")

    try:
        while True:
            job_id = None
            book_id = None
            local_ready_path = None

            try:
                r = _session.get(
                    f"{SERVER_URL}/get_neo4j_job",
                    params={"worker_id": WORKER_ID},
                )

                if not is_connected:
                    logger.info("Connected to server")
                    is_connected = True

                if r.status_code != 200:
                    logger.error("get_neo4j_job failed: HTTP %d", r.status_code)
                    time.sleep(5)
                    continue

                job = r.json()
                if job.get("action") != "PROCESS":
                    time.sleep(2)
                    continue

                job_id = job.get("job_id")
                lease_id = job.get("lease_id")
                book_id = job.get("book_id")
                batch_start = job.get("start_offset", 0)
                batch_count = job.get("page_count", 0)
                batch_idx = job.get("chunk_idx", 0)
                _heartbeat.start(job_id, lease_id)

                logger.info("\n" + "=" * 80)
                logger.info("NEW NEO4J BATCH JOB")
                logger.info("JOB ID     : %s", job_id)
                logger.info("BOOK ID    : %s", book_id)
                logger.info(
                    "BATCH      : #%s (chunks %d–%d)",
                    batch_idx, batch_start, batch_start + batch_count - 1,
                )
                logger.info("=" * 80)

                # ── Download ready.json from server ────────────────────────────
                logger.info("Downloading ready file for '%s'...", book_id)
                r2 = _session.get(f"{SERVER_URL}/download_ready/{book_id}")
                if r2.status_code != 200:
                    raise RuntimeError(
                        f"download_ready failed: HTTP {r2.status_code} — {r2.text[:200]}"
                    )

                with tempfile.NamedTemporaryFile(
                    mode="wb", suffix="_ready.json", delete=False
                ) as f:
                    f.write(r2.content)
                    local_ready_path = f.name

                logger.info("Ready file saved to %s", local_ready_path)

                # ── Run batch ingestion ────────────────────────────────────────
                result = execute_neo4j_batch(
                    book_id, local_ready_path, batch_start, batch_count
                )

                logger.info(
                    "Neo4j batch completed — entities=%d, specs=%d",
                    result.get("entities_written", 0),
                    result.get("specs_written", 0),
                )

                # ── Build payload, cache locally before attempting submit ─────
                payload = {
                    "job_id": job_id,
                    "worker_id": WORKER_ID,
                    "lease_id": lease_id,
                    "success": True,
                    "content": result,
                }
                _cache.store(job_id, payload)
                accepted = submit_with_retry(
                    _session, f"{SERVER_URL}/submit_neo4j_result", payload
                )
                if accepted:
                    _cache.clear(job_id)
                    logger.info(
                        "Completion acknowledged for batch #%s of '%s'",
                        batch_idx,
                        book_id,
                    )
                else:
                    logger.warning(
                        "Neo4j result for batch #%s was not accepted; keeping local cache for replay",
                        batch_idx,
                    )

                logger.info("=" * 80)

            except requests.exceptions.ConnectionError:
                if is_connected:
                    logger.error("Disconnected from server. Waiting to reconnect...")
                    is_connected = False
                else:
                    logger.error(
                        "Failed to connect to server at %s. Retrying...", SERVER_URL
                    )
                time.sleep(5)

            except Exception as e:
                logger.error("Neo4j worker error: %s", e)
                logger.error(traceback.format_exc())

                if job_id:
                    try:
                        _session.post(
                            f"{SERVER_URL}/submit_neo4j_result",
                            json={
                                "job_id": job_id,
                                "worker_id": WORKER_ID,
                                "lease_id": lease_id,
                                "success": False,
                                "content": "",
                                "error": str(e),
                            },
                            timeout=30,
                        )
                    except Exception as e2:
                        logger.error("Error submitting failure: %s", e2)
                        logger.error(traceback.format_exc())

                time.sleep(5)

            finally:
                _heartbeat.stop()
                # ── Always clean up the temp file ─────────────────────────────
                if local_ready_path:
                    try:
                        Path(local_ready_path).unlink(missing_ok=True)
                    except Exception:
                        pass

    except KeyboardInterrupt:
        logger.info("Shutting down Neo4j worker...")
    finally:
        _heartbeat.stop()
        if _neo4j_driver is not None:
            _neo4j_driver.close()
            _neo4j_driver = None


if __name__ == "__main__":
    start_worker()
