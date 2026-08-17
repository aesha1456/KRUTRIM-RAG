"""Central configuration for Part B.

Edit this file for retrieval, model, and database settings.  Runtime values
are intentionally explicit here instead of being loaded from ``partb/.env``.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo layout: .../RAG/partb/this_file.py → repo root is parent of partb/
PARTB_DIR = Path(__file__).resolve().parent
REPO_ROOT = PARTB_DIR.parent
PARTA_DIR = REPO_ROOT / "parta"
PORTABLE_DIR = PARTA_DIR / "portable"
RERANKER_DIR = PORTABLE_DIR / "jina-reranker-v3"

# ── Offline / air-gapped Hub setup ──────────────────────────────────────────
# Pin every HuggingFace lookup (weights, remote code, tokenizers) to the
# portable cache and forbid network egress unless the user overrides.
HF_CACHE_ROOT = PORTABLE_DIR / "huggingface"
os.environ.setdefault("HF_HOME", str(HF_CACHE_ROOT))
os.environ.setdefault("HF_HUB_CACHE", str(HF_CACHE_ROOT / "hub"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Part A data paths (page viewer) — overridable via RAG_PARTA_DATA_DIR
PARTA_DATA_DIR = PARTA_DIR / "data"
CHECKPOINTS_DIR = PARTA_DATA_DIR / "checkpoints"
METADATA_DIR = PARTA_DATA_DIR / "metadata"
QDRANT_DIR = PARTA_DATA_DIR / "qdrant"


# Mongo (same as Part A defaults)
MONGO_URI = "mongodb://localhost:27017"
MONGO_DB =  "rag_system"


# JWT — MUST match Part A in production
JWT_SECRET = "ISRO_RAG_SECRET_CHANGE_IN_PROD"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS =8

# LiteLLM OpenAI-compatible proxy (Fallback)
LITELLM_BASE_URL = "http://localhost:4000/v1"
LITELLM_API_KEY = ""
LITELLM_MODEL = "mistral:7b-instruct"

# Ollama Load Balancer (Primary) — Flask router, src/main.py MAIN_PORT
OLLAMA_LB_URL = "http://127.0.0.1:6969"
OLLAMA_LB_PORT = 6969
OLLAMA_STREAM_PORT = 11434


# Local database settings used by both retrieval and Part A ingestion.
# Hosts resolve CSL-first, SACNet-fallback (see partb/dbnet.py).
from partb.dbnet import mongo_uri, neo4j_uri, qdrant_url

MONGO_URI = mongo_uri()
QDRANT_URL = qdrant_url()
QDRANT_API_KEY = ""  # local instance has no API key
COLLECTION_PROPS     = "RAG_PROPOSITIons"
COLLECTION_SECTIONS  = "RAG_sections"


NEO4J_URI = neo4j_uri()
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = ""  # set your local Neo4j password
NEO4J_DATABASE = "neo4j"

import json
from partb.logger import time_it, async_time_it

import collections

ENTITY_LABELS = ["Component", "Material", "Specification", "Standard", "Entity"]

# Dynamically load from unie_synthetic.json if present
_json_path = PARTB_DIR / "unie_synthetic.json"
if _json_path.exists():
    try:
        with open(_json_path, 'r', encoding='utf-8') as f:
            _data = json.load(f)
        # Extract generic entity labels (ignore relation pairs with '<>' and generic 'match')
        _all_labels = [
            item[2] for record in _data 
            for item in record.get('ner', []) 
            if '<>' not in item[2] and item[2].lower() != "match"
        ]
        _counter = collections.Counter(_all_labels)
        # Take the top 30 most frequent entity types
        ENTITY_LABELS = [k for k, v in _counter.most_common(30)]
    except Exception as e:
        print(f"Warning: Failed to load labels from JSON: {e}")
NEO4J_ENTITY_LIMIT = 50
PROP_RETRIEVE_LIMIT = 40
SECT_RETRIEVE_LIMIT = 40
PAGE_EXPAND_MAX_CHARS = 9000


# Retrieval constants (aligned with parta/test.py)
BOOST_BOTH = 0.12
LONG_CHUNK_WORDS = 450

# ── Reranker tuning ──────────────────────────────────────────────────────────
# Jina Reranker v3 has 131K context and processes ALL candidate documents
# listwise in a single forward pass via model.rerank(query, documents).
# Segment-max pooling is only needed for text exceeding 131K tokens.
RERANK_SEGMENT_TOKENS = 60000
RERANK_FULL_CHUNK_WORDS = 80000

# Drop candidates whose rerank score is below this floor. Jina Reranker v3
# produces unbounded logit scores (not sigmoid-squashed), so the absolute
# values differ from the previous BGE model. Tune empirically after reading
# the score-distribution logs (rerank_candidates logs min/median/max per
# query). 0.0 = no filtering.
RERANK_MIN_SCORE = 0.0

# Score-distribution logging sample rate — log min/median/max of rerank scores
# be calibrated against real data rather than guessed.
RERANK_LOG_DISTRIBUTION_EVERY = 1

# Enable ColBERT multi-vector late interaction (replaces Jina)
USE_COLBERT = False

# Compute device for ML models (cuda / cpu)
TORCH_AVAILABLE = False
HAS_GPU = False
try:
    import torch
    TORCH_AVAILABLE = True
    HAS_GPU = torch.cuda.is_available()
except ImportError:
    pass

DEVICE = "cuda" if HAS_GPU else "cpu"


# ── Proposition-to-Section mapping (2.5 & 2.6) ────────────────────────
# Weight for proposition-child score boost in reranking (2.6).
# When a section was found via a proposition's parent_chunk_id, the max
# proposition score is stored and used as an additional boost in rerank.
PROP_SCORE_BOOST_WEIGHT = 0.10
# Minimum parent sections found before we supplement with more direct search (2.5.2).
MIN_PARENT_SECTIONS = 3


# ── Adaptive Retrieval Depth ──────────────────────────────────────────────
# Two-pass: start conservative, expand if reranker scores are low.
ENABLE_ADAPTIVE_DEPTH = False
# First pass uses this fraction of normal proposition/section limits.
ADAPTIVE_DEPTH_INITIAL_FRACTION = 0.5
# If top reranker score is below threshold on first pass, trigger second pass.
# Jina v3 produces raw logit scores (NOT sigmoid 0-1), so typical good scores
# range from 0.2 to 0.5. Use a low threshold to avoid unnecessary second passes.
ADAPTIVE_DEPTH_SCORE_THRESHOLD = 0.15
# Multiply first-pass limits by this for the second pass.
ADAPTIVE_DEPTH_EXPAND_MULTIPLIER = 2


# ── Hybrid Search (Vector + BM25) ────────────────────────────────────────────────
ENABLE_HYBRID = False
HYBRID_RRF_K = 60
# How many extra candidates per retrieval path to allow RRF room to re-rank.
HYBRID_POOL_MULTIPLIER = 2


# MMR (Maximum Marginal Relevance) diversity
ENABLE_MMR = False
MMR_LAMBDA = 0.7
MMR_POOL_MULTIPLIER = 2


# Query classification & routing
ENABLE_QUERY_CLASSIFICATION = True

# Per-(mode, query_type) overrides applied on top of MODE_CONFIG.
# Each key: mode -> query_type -> {overrides}
#   boost_both_mult:        Multiplier for BOOST_BOTH when a candidate comes from both Qdrant + Neo4j
#   page_expand_range:      Pages before/after the main page to fetch (0 = no expansion)
#   final_top_n_adjust:     Added to the mode's final_top_n (can be negative)
#   context_max_chars_adjust: Added to the mode's context_max_chars (can be negative)
#   cross_book:             If True, ignore user's book filter and search all books
QUERY_TYPE_OVERRIDES: dict[str, dict[str, dict]] = {
    "fast": {
        "spec_lookup": {"boost_both_mult": 2.0, "page_expand_range": 1, "final_top_n_adjust": 0, "context_max_chars_adjust": 0, "cross_book": False},
        "process":     {"boost_both_mult": 1.0, "page_expand_range": 2, "final_top_n_adjust": 0, "context_max_chars_adjust": 0, "cross_book": False},
        "comparison":  {"boost_both_mult": 1.5, "page_expand_range": 1, "final_top_n_adjust": 2, "context_max_chars_adjust": 0, "cross_book": True},
        "overview":    {"boost_both_mult": 1.0, "page_expand_range": 0, "final_top_n_adjust": -3, "context_max_chars_adjust": -2000, "cross_book": False},
    },
    "balanced": {
        "spec_lookup": {"boost_both_mult": 2.0, "page_expand_range": 1, "final_top_n_adjust": 0, "context_max_chars_adjust": 0, "cross_book": False},
        "process":     {"boost_both_mult": 1.0, "page_expand_range": 2, "final_top_n_adjust": 2, "context_max_chars_adjust": 2000, "cross_book": False},
        "comparison":  {"boost_both_mult": 1.5, "page_expand_range": 1, "final_top_n_adjust": 4, "context_max_chars_adjust": 2000, "cross_book": True},
        "overview":    {"boost_both_mult": 1.0, "page_expand_range": 0, "final_top_n_adjust": -2, "context_max_chars_adjust": -4000, "cross_book": False},
    },
    "deep": {
        "spec_lookup": {"boost_both_mult": 2.0, "page_expand_range": 1, "final_top_n_adjust": 0, "context_max_chars_adjust": 0, "cross_book": False},
        "process":     {"boost_both_mult": 1.0, "page_expand_range": 2, "final_top_n_adjust": 2, "context_max_chars_adjust": 2000, "cross_book": False},
        "comparison":  {"boost_both_mult": 1.5, "page_expand_range": 1, "final_top_n_adjust": 4, "context_max_chars_adjust": 2000, "cross_book": True},
        "overview":    {"boost_both_mult": 1.0, "page_expand_range": 0, "final_top_n_adjust": -3, "context_max_chars_adjust": -2000, "cross_book": False},
    },
}

# Defaults when no type-specific override is defined (e.g. "general" type)
QUERY_TYPE_GENERAL = {"boost_both_mult": 1.0, "page_expand_range": 1, "final_top_n_adjust": 0, "context_max_chars_adjust": 0, "cross_book": False}


# Greedy context allocation — select items by value density (score/chars)
# instead of fixed priority order (specs → pages → chunks).
CONTEXT_GREEDY = True


# Apply Qdrant/Neo env for processing.* imports (ingest_vectors / ingest_graph)
@time_it
def apply_parta_service_env() -> None:
    """Ensure parta modules see DB URLs even if only Part B is started."""
    os.environ.setdefault("RAG_QDRANT_URL", QDRANT_URL)
    os.environ.setdefault("RAG_QDRANT_API_KEY", QDRANT_API_KEY)
    os.environ.setdefault("RAG_NEO4J_URI", NEO4J_URI)
    os.environ.setdefault("RAG_NEO4J_USER", NEO4J_USER)
    os.environ.setdefault("RAG_NEO4J_PASSWORD", NEO4J_PASSWORD)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


MODE_ORDER = ("fast", "balanced", "deep")

# Per-mode retrieval limits for propositions and sections.
# These control how many candidates are initially fetched per retrieval path.
# Adaptive depth scales these down by ADAPTIVE_DEPTH_INITIAL_FRACTION for
# the first pass, then back up by ADAPTIVE_DEPTH_EXPAND_MULTIPLIER if needed.
# Each mode can be tuned independently.
# ollama_model must match a model name known to the Ollama LB GPU pool (olb.py SERVER_MODELS).
MODE_CONFIG: dict[str, dict] = {
    "fast": {
        "ollama_model": "mistral:7b-instruct",
        "litellm_model": "open-mistral-nemo",
        "qdrant_over_retrieve": 40,
        "prop_retrieve_limit": 20,
        "sect_retrieve_limit": 20,
        "final_top_n": 6,
        "context_max_chars": 10000,
        "history_pairs": 3,
        "llm_timeout_s": 600.0,
        "prop_score_boost_weight": 0.10,
    },
    "balanced": {
        "ollama_model": "mistral:7b-instruct",
        "litellm_model": "mistral-small-latest",
        "qdrant_over_retrieve": 40,
        "prop_retrieve_limit": 30,
        "sect_retrieve_limit": 30,
        "final_top_n": 8,
        "context_max_chars": 14000,
        "history_pairs": 5,
        "llm_timeout_s": 600.0,
        "prop_score_boost_weight": 0.20,
    },
    "deep": {
        "ollama_model": "qwen3:14b",
        "litellm_model": "mistral-large-latest",
        "qdrant_over_retrieve": 48,
        "prop_retrieve_limit": 40,
        "sect_retrieve_limit": 40,
        "final_top_n": 10,
        "context_max_chars": 16000,
        "history_pairs": 6,
        "llm_timeout_s": 600.0,
        "prop_score_boost_weight": 0.20,
    },
}
