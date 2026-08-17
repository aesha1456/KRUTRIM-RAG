"""Part A runtime configuration.

Edit this file for the machine running the Part A API and extraction server.
Worker fleet settings live in ``workers/config.py``; retrieval settings live
in ``partb/config.py``.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PARTA_DIR = REPO_ROOT / "parta"
DATA_DIR = PARTA_DIR / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
CHECKPOINTS_DIR = DATA_DIR / "checkpoints"

# Master/API and extraction-server addresses.
API_HOST = "0.0.0.0"
API_PORT = 8000
from partb.dbnet import extraction_url, mongo_uri

EXTRACTION_SERVER_URL = extraction_url()

# Pull-queue scheduling.
EXTRACTION_CHUNK_SIZE = 10
TEMP_EXTRACT_DIR_PREFIX = "temp_extract"
NEO4J_BATCH_SIZE = 30
QDRANT_BATCH_SIZE = 200
LEASE_SECONDS = 600
HEARTBEAT_INTERVAL_SECONDS = 45
MAX_ATTEMPTS = 3
# Retry failed jobs later and at the end of the queue instead of immediately
# hammering the same failing chunk.
RETRY_BASE_DELAY_SECONDS = 10
RETRY_MAX_DELAY_SECONDS = 300
# The reaper is independent of worker polling, so a crashed worker's lease is
# recovered even when every healthy worker is busy.
LEASE_REAPER_INTERVAL_SECONDS = 15
CLEANUP_DELAY_SEC = 300

# Office workers are started independently on the five PCs.

# Part A application services.
from partb.dbnet import mongo_uri

MONGO_URI = mongo_uri()
MONGO_DB_NAME = "rag_system"
JWT_SECRET = "ISRO_RAG_SECRET_CHANGE_IN_PROD"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8
# Remember-me ("stay signed in") token lifetime.
JWT_REMEMBER_DAYS = 7

