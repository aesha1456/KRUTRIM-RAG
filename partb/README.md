# Part B — Retrieval, Chat, and PDF Viewer

Part B is the query-facing application for the RAG system. It provides authentication, library and chat APIs, hybrid retrieval over Qdrant and Neo4j, streamed LLM responses, shared-chat links, and the browser PDF viewer.

Part B consumes the data prepared by Part A. It does not perform the primary PDF extraction workflow.

## Responsibilities

- Serve the chat frontend and static assets.
- Authenticate users with JWT tokens.
- Maintain users, chats, messages, and library metadata in MongoDB.
- Retrieve relevant propositions, sections, specifications, entities, and pages.
- Fuse vector and optional keyword retrieval results.
- Rerank candidates and assemble bounded context for the LLM.
- Stream retrieval status and generated answer tokens through SSE.
- Persist assistant answers and source metadata.
- Serve PDFs and page metadata to the viewer.
- Create and resolve read-only shared chat links.

## Directory layout

```text
partb/
├── app.py                              FastAPI application and lifespan setup
├── config.py                           Database, model, retrieval, and LLM settings
├── auth_jwt.py                         JWT creation and validation
├── db.py                               MongoClient singleton access
├── dbnet.py                            Network-aware service resolution
├── routers/
│   ├── auth_router.py                  Signup and login
│   ├── chats_router.py                 Chat CRUD and streamed questions
│   ├── meta_router.py                  Library, pages, models, and deletion
│   ├── pdf_router.py                   Authenticated PDF streaming
│   ├── router.py                       Legacy PDF router implementation
│   └── share_router.py                 Public shared-chat reads
├── services/
│   ├── messages.py                     Message persistence and history
│   └── pages.py                        Page metadata and legacy fallback access
├── retrieval/
│   ├── pipeline.py                     Retrieval, ranking, context, and RAG stream
│   ├── hybrid.py                       Optional BM25 and reciprocal-rank fusion
│   └── prompts.py                      System and query-type prompts
├── llm/
│   └── stream_client.py                 Ollama load-balancer and LiteLLM streaming
├── frontend/                           Chat UI, PDF viewer, and browser modules
└── static/                             Shared static assets
```

## Request and response flow

A normal chat question follows this path:

```text
Browser
  │
  ├── POST /chats/{chat_id}/ask
  │       Saves the user message and opens an SSE response
  ▼
routers/chats_router.py
  │
  └── retrieval.pipeline.run_rag_stream
          │
          ├── retrieve_bundle
          │     ├── Qdrant proposition and section search
          │     ├── GLiNER query entity extraction
          │     ├── Neo4j entity/specification lookup
          │     ├── Optional BM25 fusion
          │     ├── Reranking
          │     ├── Page expansion
          │     └── Context assembly
          │
          ├── Build system and user prompts
          └── llm.stream_client.stream_llm
                  ├── Ollama load balancer (primary)
                  └── LiteLLM OpenAI-compatible endpoint (fallback)
          │
          ▼
      SSE status, token, metrics, source, done, or error events
          │
          ▼
      Final assistant message and sources saved in MongoDB
```

## API groups

The routers are mounted by `app.py`:

| Router | Main paths | Purpose |
| --- | --- | --- |
| Authentication | `/auth/signup`, `/auth/login` | Create accounts and issue JWTs |
| Chats | `/chats`, `/chats/{chat_id}`, `/chats/{chat_id}/ask` | Chat lifecycle and questions |
| Metadata | `/library`, `/books/{book_id}/page/{page_number}`, `/models` | Library and page metadata |
| PDF | `/pdf/{book_id}`, `/pdf/{book_id}/info` | PDF bytes and page count |
| Sharing | `/shared/{share_token}` | Read-only shared chat data |
| Application | `/`, `/chat/{chat_id}`, `/shared/{share_token}` | Browser entry pages |

FastAPI documentation is available at `/api/docs` and `/api/redoc` when the application is running.

## Retrieval pipeline

`retrieval/pipeline.py` is the central query implementation. Its major stages are:

1. **Query classification** — selects retrieval behavior for specification, process, comparison, overview, or general questions.
2. **Vector retrieval** — searches proposition and section collections in Qdrant.
3. **Graph retrieval** — uses extracted entities and specifications in Neo4j.
4. **Candidate merging** — combines candidates by stable source/chunk identifiers.
5. **Hybrid retrieval** — optionally applies BM25 and reciprocal-rank fusion.
6. **Reranking** — applies the configured Jina reranker and source boosts.
7. **Page expansion** — loads page-level content and nearby pages when configured.
8. **Context construction** — applies the selected mode’s character budget and deduplication rules.
9. **Prompt construction** — adds source-aware instructions and bounded chat history.
10. **LLM streaming** — emits normalized events to the chat router.

The retrieval mode is one of `fast`, `balanced`, or `deep`. Mode settings are defined in `MODE_CONFIG` in `config.py`.

## SSE event contract

The chat endpoint streams JSON objects using the SSE format:

```text
data: {"type": "status", "message": "Searching knowledge base..."}

data: {"type": "token", "content": "..."}

data: {"type": "metrics", "metrics": {...}}

data: {"type": "done", "sources": [...], "full_text": "..."}
```

The frontend should handle at least these event types:

- `status` — retrieval or generation progress.
- `token` — one generated text fragment.
- `metrics` — optional provider timing and token information.
- `done` — final answer and source attribution.
- `error` — unrecoverable retrieval or generation failure.

The chat router persists the final assistant response when it receives the `done` event.

## Configuration

The main configuration is in `partb/config.py`:

- `MONGO_URI` and `MONGO_DB` — application persistence.
- `QDRANT_URL`, collection names, and optional API key — vector retrieval.
- `NEO4J_URI`, credentials, database, and entity limits — graph retrieval.
- `PARTA_DATA_DIR` — raw PDFs, checkpoints, Qdrant local data, and page metadata.
- `OLLAMA_LB_URL` — primary LLM load-balancer endpoint.
- `LITELLM_BASE_URL`, `LITELLM_API_KEY`, and model names — fallback LLM endpoint.
- `MODE_CONFIG` — retrieval depth, context limits, history size, and model selection.
- Hybrid search, MMR, adaptive depth, and query-classification flags.

`dbnet.py` resolves service hosts using the project’s network fallback rules. Keep Part A, Part B, and worker service settings aligned when they run on separate machines.

## Running locally

From the repository root:

```bash
pip install -r partb/requirements.txt
cd partb
PYTHONPATH=.. uvicorn app:app --host 0.0.0.0 --port 9000
```

Part B expects MongoDB and the configured Qdrant and Neo4j services. The retrieval pipeline also expects the configured local model files. The application attempts model and database warm-up during startup; missing optional services may prevent retrieval even when the HTTP process starts.

## Frontend notes

The browser application is under `partb/frontend/`:

- `index.html` — main chat shell.
- `js/state.js` — authentication, API helpers, and client state.
- `js/main.js` — application event coordination.
- `js/render.js` — chat and library rendering.
- `js/pdf.js` — PDF page navigation and source highlighting.
- `js/auth.js` — login and signup behavior.
- `js/settings.js` — client settings and model/retrieval controls.

The PDF iframe uses a query token because browsers do not attach an `Authorization` header to an iframe `src` request. Preserve this contract when changing the viewer or PDF routes.

## Testing and development

Run Part B tests from the repository root:

```bash
PYTHONPATH=. pytest -q partb/tests
```

For retrieval changes, test candidate merging, source metadata, page expansion, context limits, and SSE event ordering separately from live database and model integration tests.

## Development notes

- Keep `book_ids` filtering intact unless a query mode explicitly enables cross-book search.
- Preserve source fields such as `book_id`, `chunk_id`, `page_number`, and section labels; the UI uses them for citations and PDF highlighting.
- Maintain the normalized event shape between the Ollama load balancer and LiteLLM fallback.
- Keep chat ownership checks in router handlers and do not trust a client-provided chat ID without validation.
- Avoid loading large collections or models during every request; use the existing singleton and lazy-index patterns.
- Do not commit database passwords, API keys, JWT secrets, model artifacts, PDFs, or generated retrieval data.
