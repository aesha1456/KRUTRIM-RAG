# Part A — Ingestion and Extraction API

Part A is the ingestion side of the RAG system. It accepts PDF uploads, coordinates distributed extraction, converts extracted content into searchable checkpoints, and starts the vector and graph ingestion stages used by Part B.

## Responsibilities

Part A owns the following responsibilities:

- PDF upload and validation.
- User authentication for the upload interface.
- MongoDB job records and progress reporting.
- Phase 1 document extraction and local checkpoint generation.
- Phase 2 Qdrant and Neo4j ingestion orchestration.
- Resumable Phase 2 execution after a stage failure.
- Progress updates for the frontend through Server-Sent Events (SSE).
- The extraction server API used by text, Qdrant, and Neo4j workers.

Part A does not answer user questions. Question answering, retrieval, chat persistence, and PDF viewing are implemented in Part B.

## Directory layout

```text
parta/
├── main_api.py                         FastAPI upload and job API
├── pipeline_controller.py              Two-phase pipeline orchestration
├── config.py                            Part A runtime configuration
├── extraction/
│   └── extraction_server.py             Leased extraction and batch-job server
├── processing/
│   ├── chunk.py                         Markdown/structured document chunking
│   ├── triple_rep.py                    Table representation enrichment
│   ├── build_metadata.py                Page and viewer metadata generation
│   ├── propositions.py                  Proposition checkpoint generation
│   ├── ingest_qdrant.py                 Vector ingestion implementation
│   └── ingest_neo4j.py                  Graph ingestion implementation
├── frontend/                            Upload and progress web interface
└── data/
    ├── raw/                             Uploaded PDFs
    ├── processed/                       Extracted Markdown and structured JSON
    ├── checkpoints/                     Phase 2 input files
    └── metadata/                        Page-level viewer metadata
```

The `data/` directories are created at runtime and may be located outside the repository when deployment configuration overrides the data root.

## End-to-end data flow

```text
PDF upload
   │
   ▼
main_api.py: /upload_book
   │  Creates a MongoDB job and queues background work
   ▼
pipeline_controller.run_phase1
   │
   ├── extraction_server: /start_extraction
   │      Splits the PDF into leased chunk jobs
   │
   ├── text workers process chunks
   │      Return Markdown and structured element metadata
   │
   ├── extraction_server: /get_result/{book_id}
   │      Reassembles completed chunks in PDF order
   │
   ├── processing.chunk.run_chunking
   ├── processing.triple_rep.run_triple_rep
   ├── processing.build_metadata.run_build_metadata
   └── processing.propositions.run_propositions
          │
          ├── {book_id}_ready.json
          └── {book_id}_propositions.json

pipeline_controller.run_phase2
   │
   ├── Qdrant batch jobs → vector collections
   └── Neo4j batch jobs  → document/entity graph
          │
          ▼
      library record marked ready
```

## Pipeline phases

### Phase 1 — extraction and preprocessing

`run_phase1()` performs the expensive, checkpointed work:

1. Validates the uploaded PDF.
2. Requests chunk creation from the extraction server.
3. Polls extraction progress until all chunks complete.
4. Fetches the assembled Markdown and structured extraction result.
5. Writes processed Markdown and structured JSON.
6. Builds section chunks while preserving page and OCR metadata.
7. Enriches tables with structured and linearized representations.
8. Builds page metadata for source expansion and PDF highlighting.
9. Generates proposition records.
10. Stores `ready_path` and `prop_path` in MongoDB.

A successful Phase 1 job has status `extraction_done`. If it fails, the status becomes `extraction_failed`.

### Phase 2 — database ingestion

`run_phase2()` reads the checkpoint paths from MongoDB and runs the two independent ingestion stages concurrently:

- **Qdrant:** stores proposition and section vectors.
- **Neo4j:** stores the document hierarchy, specifications, entities, relationships, and table nodes.

Each stage has independent progress. A completed stage is skipped when `/resume/{job_id}` is used, so a failure in one database does not require the other database to be rebuilt.

A successful job has status `completed`; an ingestion failure has status `ingestion_failed` while leaving checkpoints available for resumption.

## Important data contracts

### MongoDB job document

The API and pipeline controller communicate through the `jobs` collection. Important fields include:

- `job_id` and `book_id` — request and document identifiers.
- `uploaded_by` — user that submitted the document.
- `status`, `percent`, `stage`, and `message` — frontend progress state.
- `ready_path` — path to the chunk checkpoint used by both ingestion stages.
- `prop_path` — path to the proposition checkpoint used by Qdrant.
- `qdrant_progress` and `neo4j_progress` — independent stage state.
- `confidence_report` — final extraction and ingestion summary.

Keep the checkpoint field names consistent. The resume path depends on `ready_path` and `prop_path`.

### Checkpoint files

The main checkpoint files are:

- `{book_id}_ready.json` — normalized section/chunk records with page and source metadata.
- `{book_id}_propositions.json` — atomic text and table propositions linked to parent chunks.
- `{book_id}.md` — assembled Markdown for inspection and fallback processing.
- `{book_id}.json` — structured extraction elements, including OCR geometry when available.

## Configuration

Primary settings are in `parta/config.py`:

- `EXTRACTION_SERVER_URL` — extraction server address.
- `MONGO_URI` and `MONGO_DB_NAME` — job and library storage.
- `EXTRACTION_CHUNK_SIZE` — number of PDF pages assigned to one extraction job.
- `NEO4J_BATCH_SIZE` and `QDRANT_BATCH_SIZE` — Phase 2 batch sizes.
- `LEASE_SECONDS`, `HEARTBEAT_INTERVAL_SECONDS`, and retry settings — distributed job behavior.
- `JWT_SECRET`, `JWT_ALGORITHM`, and token lifetimes — API authentication.

Review the related network resolution helpers in `partb/dbnet.py` when Part A and the workers run on different machines.

## Running locally

From the repository root, install the Part A dependencies and run the API from the `parta` directory so its existing imports resolve correctly:

```bash
pip install -r parta/requirements.txt
cd parta
PYTHONPATH=.. uvicorn main_api:app --host 0.0.0.0 --port 8000
```

The extraction server exposes the FastAPI application in `parta/extraction/extraction_server.py`. Start it using the deployment command for your environment, or use the equivalent module path:

```bash
PYTHONPATH=. uvicorn parta.extraction.extraction_server:app --host 0.0.0.0 --port 8004
```

The extraction server must be reachable by Part A and by the text, Qdrant, and Neo4j workers.

## Testing

Run the focused extraction tests from the repository root:

```bash
PYTHONPATH=. pytest -q parta/extraction/test_extraction_server.py
```

Processing modules may require the configured MongoDB, Qdrant, Neo4j, embedding models, and OCR dependencies. Prefer unit tests and fixture-based tests for local changes that do not require those services.

## Development notes

- Preserve page numbers, element identifiers, bounding boxes, and native references when changing extraction or chunking code; Part B uses this metadata for source highlighting.
- Treat checkpoint paths as a stable interface between Phase 1, Phase 2, and resume logic.
- Keep Qdrant and Neo4j progress independent so one failed stage can be retried without repeating successful work.
- Avoid importing worker runtime loops from library code. Worker entry points are guarded to remain compatible with multiprocessing and process spawning.
- Do not commit real database credentials, JWT secrets, model artifacts, uploaded PDFs, or generated checkpoints.
