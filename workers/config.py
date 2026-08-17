"""Configuration shared by the three office worker processes.

Each PC runs one text, one Qdrant, and one Neo4j worker.  Change ``SERVER_URL``
on each office machine if the extraction server is not local.
"""

import os
import tempfile
from pathlib import Path


from partb.dbnet import extraction_url

# Address of the Part A extraction server on the office LAN. CSL-first,
# SACNet-fallback; launchers may still override per worker through the
# environment.
SERVER_URL = os.environ.get("SERVER_URL", extraction_url())

# Worker lease and polling behavior.
HEARTBEAT_INTERVAL_SECONDS = 45
WAIT_SLEEP_SECONDS = 2
ERROR_SLEEP_SECONDS = 5

# Qdrant upload tuning.
QDRANT_POINT_BATCH_SIZE = 128
QDRANT_UPLOAD_PARALLEL = 2

# Neo4j tuning. Keep local extraction at one stream for shared-GPU PCs.
NEO4J_CONNECTION_POOL_SIZE = 4
NEO4J_LOCAL_WORKERS = 1

# Optional local worker behavior.
OPENDATALOADER_HYBRID_URL = ""
RAG_DEVICE = "auto"  # "auto", "cuda", or "cpu"
RESULT_CACHE_DIR = Path(tempfile.gettempdir()) / "worker_result_cache"

