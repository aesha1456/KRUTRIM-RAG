# KRUTRIM — Hybrid GraphRAG System for ISRO Technical Documents

> **Offline · Air-Gapped · CPU-Only · Production-Ready**
>
> A dual-database Hybrid Graph RAG (Retrieval-Augmented Generation) system built at ISRO's Space Applications Centre (SAC) for querying large aerospace technical documents without any external API calls or internet connectivity.

---

## Table of Contents

1. [Overview](#overview)
2. [Research Motivation](#research-motivation)
3. [System Architecture](#system-architecture)
4. [Part A — Knowledge Base Generation](#part-a--knowledge-base-generation)
5. [Part B — Retrieval and Q&A](#part-b--retrieval-and-qa)
6. [Data Flow Diagrams](#data-flow-diagrams)
7. [Key Design Decisions](#key-design-decisions)
8. [Knowledge Base Quality](#knowledge-base-quality)
9. [Retrieval Pipeline](#retrieval-pipeline)
10. [Database Schemas](#database-schemas)
11. [Project Directory Structure](#project-directory-structure)
12. [Installation and Setup](#installation-and-setup)
13. [Running the System](#running-the-system)
14. [Configuration](#configuration)
15. [API Reference](#api-reference)
16. [Frontend](#frontend)
17. [Performance](#performance)
18. [Limitations and Future Work](#limitations-and-future-work)
19. [Tech Stack](#tech-stack)

---

## Overview

KRUTRIM is a fully offline Hybrid GraphRAG system designed to process large aerospace technical documents (500–700 pages) and answer precise technical questions about them. The system combines:

- **Vector search** (Qdrant) for semantic similarity retrieval
- **Graph traversal** (Neo4j) for structured entity and relationship lookup
- **Page-level context expansion** using a pre-built metadata index
- **Local LLM inference** via LiteLLM proxy or direct Ollama for answer generation

The name KRUTRIM (कृत्रिम) is the Hindi word for "artificial", reflecting the system's purpose of building an artificial intelligence layer over ISRO's institutional knowledge.

---

## Research Motivation

### The Problem With Naive RAG

Standard Retrieval-Augmented Generation systems use a simple pipeline:

```
PDF → Fixed-size chunks (400-600 words) → Embed → Vector DB → Retrieve → LLM
```

This approach has three critical failure modes for aerospace technical documents:

**1. Table Destruction**
Aerospace manuals contain dense specification tables that are the primary source of precise technical data. Fixed-size chunking breaks tables across chunk boundaries, making the data unretrievable. A query for "electrical specifications of the S×C Up-Converter" returns a chunk containing only the table heading but not the table rows.

**2. Context Dilution**
Embedding 600 words into a single vector produces a representation that is the average of all concepts in that passage. A paragraph discussing thrust values, fuel types, ignition timing, and gimbal angles produces one vector that weakly matches any specific query about any of those topics.

**3. No Structural Understanding**
A flat vector database has no understanding of document structure. It cannot answer "which sections discuss the turbopump assembly?" or "what are all components of Stage 2?" — questions that require structural traversal rather than semantic similarity.

### The KRUTRIM Solution

KRUTRIM addresses each failure mode with a specific architectural innovation:

| Problem | Solution | Component |
|---|---|---|
| Table destruction | Triple representation — tables stored as structured JSON, linearized text, and original markdown | `triple_rep.py` |
| Context dilution | Proposition indexing — atomic sentences embedded individually with page-level expansion at retrieval | `propositions.py` + `build_metadata.py` |
| No structural understanding | 5-layer Neo4j graph — document hierarchy, entity links, specification nodes, co-occurrence edges, table nodes | `ingest_neo4j.py` |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           KRUTRIM SYSTEM                                     │
│                                                                               │
│  ┌──────────────────────────────────┐  ┌─────────────────────────────────┐  │
│  │         PART A                   │  │         PART B                   │  │
│  │   Knowledge Base Generation      │  │     Retrieval and Q&A            │  │
│  │   Port 8000 (main_api.py)        │  │     Port 9000 (app.py)           │  │
│  │                                  │  │                                  │  │
│  │  PDF Upload                      │  │  User Query                      │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Docling Extraction              │  │  Proposition Search (Qdrant)     │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Smart Chunking                  │  │  Entity Extraction (GLiNER)      │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Triple Representation           │  │  Neo4j Graph Traversal           │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Page Metadata Index             │  │  Page Expansion (metadata.json)  │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Proposition Extraction          │  │  CrossEncoder Reranking          │  │
│  │       ↓                          │  │       ↓                          │  │
│  │  Qdrant Ingestion ──────────────►│  │  LLM Answer Generation           │  │
│  │  Neo4j Ingestion ───────────────►│  │       ↓                          │  │
│  │                                  │  │  Streamed Response               │  │
│  └──────────────────────────────────┘  └─────────────────────────────────┘  │
│                                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  Qdrant  │  │  Neo4j   │  │ MongoDB  │  │  Ollama  │  │  LiteLLM     │  │
│  │  :6333   │  │  :7687   │  │  :27017  │  │  :11434  │  │  Proxy :4000 │  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Part A — Knowledge Base Generation

### Pipeline Overview

Part A is a two-phase pipeline with full disk checkpointing between phases. If Phase 2 (ingestion) fails for any reason, it can be resumed without re-running the expensive Phase 1 (extraction).

```
Phase 1 — Extraction + Structural Processing
─────────────────────────────────────────────
PDF → Docling → .md → chunk.py → triple_rep.py → build_metadata.py → propositions.py
                                                                              ↓
                                                              Checkpoints saved to MongoDB

Phase 2 — Database Ingestion (resumable, parallel)
───────────────────────────────────────────────────
_ready.json + _propositions.json
        ├──── ingest_qdrant.py ──────► Qdrant (propositions + sections collections)
        └──── ingest_neo4j.py ──────► Neo4j  (5-layer graph)
```

### Phase 1 Detail

#### Step 1 — Docling Extraction (Distributed)

The PDF extraction uses a distributed Master-Worker architecture to handle 500–700 page aerospace documents without crashing on CPU-only machines.

```
Master Node                          Worker Machines
────────────                         ──────────────────
extraction_server.py (port 8004)     text_worker.py × N
        │                                    │
        │  Job queue (pull-based)            │
        │◄───────────────────────────────────┤
        │  Send 10-page chunk                │
        │──────────────────────────────────► │
        │                                    │  Docling converts chunk
        │                                    │  TableFormer extracts tables
        │  Submit markdown result            │
        │◄───────────────────────────────────┤
        │
        ▼
   Assembled {book_id}.md
   Saved to data/processed/
```

Key property: Docling inserts `## --- PAGE N ---` markers at every page boundary. These markers are the ground truth for all downstream page number tracking.

#### Step 2 — Smart Chunking (`chunk.py`)

**Page Number Tracking Fix:**
The naive approach extracts page numbers from markers found inside a chunk's content buffer — which produces off-by-one errors because a section starting on page 3 may contain a `## --- PAGE 4 ---` marker inside it.

KRUTRIM tracks `current_page` as a running counter while walking the markdown line by line. When a section header is encountered, `current_page` is recorded as `page_range.start` before the content buffer is cleared. This ensures the page number matches where the heading actually appears in the PDF.

```python
# Correct page tracking
for line in lines:
    pm = PAGE_MARKER_RE.match(line)
    if pm:
        current_page = int(pm.group(1))   # update running counter
        continue
    parsed = _parse_header_line(line)
    if parsed:
        section_start_page = current_page  # record BEFORE clearing buffer
        ...
```

**Output per chunk:**
```json
{
  "chunk_id":     "stable-uuid5-based-on-content",
  "section_path": ["3. Propulsion Systems", "3.2 Vikas Engine"],
  "level":        2,
  "page_range":   {"start": 47, "end": 49},
  "type":         "text",
  "content":      "The Vikas engine is a liquid-fuelled...",
  "parent_id":    "parent-section-chunk-id",
  "book_id":      "PSLV-C50"
}
```

#### Step 3 — Triple Representation (`triple_rep.py`)

Every table chunk gets three representations, each optimised for a different consumer:

```
Raw pipe markdown (from Docling):
  | Parameter | Value | Unit |
  |---|---|---|
  | Thrust    | 799   | kN   |
  | ISP       | 293   | s    |
         │
         ├──► structured_json   → Neo4j TableRow nodes
         │    {headers: [...], rows: [{Parameter: "Thrust", Value: "799", Unit: "kN"}]}
         │
         ├──► linearized_text   → Qdrant embedding
         │    "Section: 3.2 Vikas Engine. Parameter: Thrust | Value: 799 | Unit: kN."
         │
         └──► original_markdown → Audit trail (preserved verbatim)
```

#### Step 4 — Page Metadata Index (`build_metadata.py`)

Reads directly from `data/processed/{book_id}.md` (not from `_ready.json`) to produce an accurate page-by-page content index.

**Why read from `.md` directly:**
`_ready.json` contains chunks split by section headers. A single page may contain content from multiple sections, so reconstructing page content from chunks produces mixed, out-of-order results. The `.md` file with its `## --- PAGE N ---` markers is the only source that preserves exact page order.

**Table formatting:**
When building page content, pipe tables are replaced with formatted bullet lists:

```
Before (pipe markdown):         After (formatted for LLM):
| Parameter | Value | Unit |    [TABLE — 3.2 Vikas Engine]:
|---|---|---|              →      - Thrust: 799 kN
| Thrust | 799 | kN |              - Specific Impulse: 293 s
| ISP | 293 | s |                  - Chamber Pressure: 58.5 bar
```

This prevents the LLM from writing "see Table 3.2" because there is no table in the context — only readable bullet points the LLM naturally incorporates into its answer.

**Output:**
```json
{
  "47": {
    "page_number": 47,
    "sections":    ["3.2 Vikas Engine", "3.2.1 Fuel System"],
    "full_content": "[3.2 Vikas Engine]\nThe Vikas engine...\n\n[TABLE — 3.2 Vikas Engine]:\n  - Thrust: 799 kN\n  - ISP: 293 s"
  },
  "__meta__": {
    "book_id": "PSLV-C50",
    "total_pages": 312,
    "created_at": "2025-04-27T..."
  }
}
```

#### Step 5 — Proposition Extraction (`propositions.py`)

Implements **Proposition Indexing** — the single biggest quality improvement over naive RAG.

```
NAIVE: Embed 600-word paragraphs → diluted vector → weak retrieval

KRUTRIM: Split into atomic sentences → one vector per fact → precise retrieval

"The Vikas engine uses UH25 as fuel and N2O4 as oxidizer."  → proposition_id_1
"The turbopump assembly operates at 17,000 RPM."             → proposition_id_2
"Chamber pressure is 58.5 bar."                              → proposition_id_3

Each proposition carries parent_chunk_id → small-to-big retrieval at query time
```

Table propositions are generated deterministically from `structured_json`:
```
row: {"Parameter": "Thrust", "Value": "799", "Unit": "kN"}
→ "Vikas Engine Thrust is 799 kN."   (zero hallucination — pure rule-based)
```

### Phase 2 — Database Ingestion

#### Qdrant — Two-Collection Architecture (`ingest_qdrant.py`)

```
Collection "propositions"              Collection "sections"
──────────────────────────             ──────────────────────
Unit: one atomic sentence              Unit: one full section chunk
Purpose: precise fact retrieval        Purpose: full context retrieval

Payload per point:                     Payload per point:
  book_id                                book_id
  parent_chunk_id  ◄── small-to-big      chunk_id
  section_path         link              section_path
  page                                   page_range
  source_type                            chunk_type
  text                                   text
```

**Retrieval flow at query time:**
```
Search "propositions" → find precise match → use parent_chunk_id
→ fetch full section from "sections" → send complete context to LLM
```

#### Neo4j — Five-Layer Graph (`ingest_neo4j.py`)

```
Layer 1: Document Hierarchy (deterministic)
  (Book)──[HAS_CHAPTER]──►(Chapter)──[HAS_SECTION]──►(Section)──[NEXT_SECTION]──►(Section)

Layer 2: Specification Nodes (regex, zero hallucination)
  "thrust 799 kN" detected by regex
  (Entity: "vikas engine")──[HAS_SPECIFICATION]──►(Spec {value:799, unit:"kN"})

Layer 3: Entity-Section Links (GLiNER)
  (Entity: "turbopump assembly")──[MENTIONED_IN]──►(Section: "3.2 Vikas Engine")

Layer 4: Sentence Co-occurrence (GLiNER per sentence)
  Two entities in same sentence → strong signal
  (Entity: "turbopump")──[SENTENCE_CO_OCCURS {count:4}]──►(Entity: "vikas engine")

Layer 5: Table Nodes (from structured_json)
  (Section)──[HAS_TABLE]──►(Table {headers, row_count})──[HAS_ROW]──►(TableRow)
```

---

## Part B — Retrieval and Q&A

### Seven-Step Retrieval Pipeline

```
User Query
    │
    ▼ Step 1 ─────────────────────────────────────────────────────────────────
    Proposition Search (Qdrant "propositions" collection)
    Embed query with Nomic → top-40 atomic fact matches
    Each hit carries: text, parent_chunk_id, page_number, score
    │
    ▼ Step 2 ─────────────────────────────────────────────────────────────────
    Query Entity Extraction (GLiNER)
    GLiNER runs on query text → entity terms extracted
    e.g. "vikas engine", "thrust", "specific impulse"
    │
    ▼ Step 3 ─────────────────────────────────────────────────────────────────
    Neo4j Graph Traversal
    Entity terms → MATCH (e:Entity)-[:MENTIONED_IN]->(s:Section)
    Returns: section names where entities are mentioned
    Also: HAS_SPECIFICATION nodes → precise numeric values
    │
    ▼ Step 4 ─────────────────────────────────────────────────────────────────
    Small-to-Big Retrieval
    parent_chunk_ids from proposition hits → retrieve from "sections" collection
    Atomic hit → full parent section fetched
    │
    ▼ Step 5 ─────────────────────────────────────────────────────────────────
    Direct Section Search (Qdrant "sections" collection)
    Broad semantic search for additional context
    Sections matching Neo4j names marked from_neo4j=True
    │
    ▼ Step 6 ─────────────────────────────────────────────────────────────────
    Merge and Deduplicate
    Combine parent sections + direct sections
    BOOST_BOTH (+0.12) applied to sections found by both Qdrant and Neo4j
    │
    ▼ Step 7 ─────────────────────────────────────────────────────────────────
    CrossEncoder Reranking
    All candidates re-scored by cross-encoder model
    Top-8 sections selected
    │
    ▼ Page Expansion ─────────────────────────────────────────────────────────
    Rank-1 chunk on page N → fetch pages N-1, N, N+1 from metadata.json
    Ensures table continuation and heading context are never missed
    Rank-2 chunk → fetch its page (if different from above)
    │
    ▼ Context Build ──────────────────────────────────────────────────────────
    Block 1: Neo4j Spec nodes (verified numeric facts)
    Block 2: Page N-1 full content (intro/heading context)
    Block 3: Page N   full content (primary answer)
    Block 4: Page N+1 full content (table continuation)
    Block 5: Rank-2 page
    Block 6: Remaining ranked chunks 3-8
    │
    ▼ LLM Generation ────────────────────────────────────────────────────────
    Streamed via LiteLLM proxy or direct Ollama
    System prompt with hard ban on "see Table X" style responses
    SSE stream → frontend renders incrementally
```

---

## Data Flow Diagrams

### Part A Complete Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PART A DATA FLOW                             │
└─────────────────────────────────────────────────────────────────────┘

User uploads PDF via browser
         │
         ▼
main_api.py (port 8000)
         │  saves to data/raw/{book_id}.pdf
         │  creates MongoDB job document
         │  queues job
         ▼
pipeline_controller.py
         │
         │ ╔══════════════════════════════════╗
         │ ║        PHASE 1                   ║
         │ ║   (checkpointed to MongoDB)      ║
         │ ╚══════════════════════════════════╝
         │
         ├──► extraction_server.py (port 8004)
         │         │  ◄──── text_worker.py × N (Docling)
         │         │         TableFormer extracts table structure
         │         │         Layout model detects reading order
         │         ▼
         │    data/processed/{book_id}.md
         │         │
         ├──► chunk.py
         │         │  splits by # ## ### headers
         │         │  isolates pipe tables
         │         │  tracks page numbers correctly
         │         ▼
         │    data/checkpoints/{book_id}_ready.json
         │         │
         ├──► triple_rep.py
         │         │  enriches table chunks with:
         │         │    structured_json, linearized_text, original_markdown
         │         ▼
         │    data/checkpoints/{book_id}_ready.json (enriched)
         │         │
         ├──► build_metadata.py
         │         │  reads .md file directly (not _ready.json)
         │         │  splits by ## --- PAGE N --- markers
         │         │  formats tables as bullet lists
         │         ▼
         │    data/metadata/{book_id}_metadata.json
         │         │
         ├──► propositions.py
         │         │  atomic sentence splitting (NLTK)
         │         │  table row → natural language sentences
         │         ▼
         │    data/checkpoints/{book_id}_propositions.json
         │
         │  checkpoint paths saved to MongoDB job document
         │
         │ ╔══════════════════════════════════╗
         │ ║        PHASE 2                   ║
         │ ║   (resumable, parallel)          ║
         │ ╚══════════════════════════════════╝
         │
         ├──────────────────────────────┐
         ▼                              ▼
ingest_qdrant.py              ingest_neo4j.py
  Nomic embed-text-v1.5         GLiNER entity extraction
  "propositions" collection      Layer 1: Document hierarchy
  "sections" collection          Layer 2: Spec regex nodes
  localhost:6333                 Layer 3: Entity-section links
         │                       Layer 4: Sentence co-occurrence
         ▼                       Layer 5: Table nodes
data/qdrant/                     bolt://localhost:7687
{book_id}_chunks.json                   │
         │                              ▼
         └──────────────┬───────────────┘
                        ▼
              _generate_confidence_report()
                        │
                        ▼
              MongoDB library collection
              SSE → frontend success screen
```

### Part B Complete Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PART B DATA FLOW                             │
└─────────────────────────────────────────────────────────────────────┘

User types question in chat UI
         │
         ▼
POST /chats/{chat_id}/ask  (port 9000)
         │
         ▼
retrieve_bundle(query, book_ids, mode)
         │
         ├─1─► Qdrant search: "propositions" collection
         │          ↓ top-40 atomic fact hits
         │          ↓ each hit has: text, parent_chunk_id, page, score
         │
         ├─2─► GLiNER on query text
         │          ↓ entity terms: ["vikas engine", "thrust"]
         │
         ├─2b─► Neo4j: Entity-[MENTIONED_IN]->Section
         │          ↓ section names: ["3.2 Vikas Engine", ...]
         │
         ├─2c─► Neo4j: Entity-[HAS_SPECIFICATION]->Spec
         │          ↓ precise values: [{entity:"vikas engine", raw:"799 kN"}]
         │
         ├─3─► Qdrant retrieve: "sections" collection by parent_chunk_ids
         │          ↓ full section text (small-to-big)
         │
         ├─4─► Qdrant search: "sections" collection (broad)
         │          ↓ additional context, Neo4j-matched sections flagged
         │
         ├─5─► Merge + deduplicate candidates
         │          ↓ BOOST_BOTH applied to dual-source sections
         │
         ├─6─► CrossEncoder rerank → top-8 sections
         │
         ├─7─► Page expansion from metadata.json
         │          Rank-1 page N → fetch N-1, N, N+1
         │          Rank-2 page M → fetch M (if M ∉ {N-1, N, N+1})
         │
         ├─8─► Build context string
         │          Block 1: Spec nodes (Neo4j, verified facts)
         │          Block 2-4: Pages N-1, N, N+1 (full content)
         │          Block 5+: Remaining ranked chunks
         │
         ▼
LiteLLM proxy / Ollama direct
         │
         ▼  (SSE stream)
chat.html frontend
         │
         ├──► Renders answer incrementally (marked.js)
         └──► Renders source chips (book_id + page_range)
              User clicks chip → PDF viewer opens at that page
```

---

## Key Design Decisions

### 1. Proposition Indexing over Naive Chunking

**Problem:** A 600-word section about the Vikas engine contains 8 different technical facts. Embedding it as one vector produces a representation that weakly matches any specific query.

**Solution:** Split into atomic sentences. Each sentence gets its own vector. Query "what fuel does Vikas engine use" hits "The Vikas engine uses UH25 as fuel" with near-perfect cosine similarity.

**Result:** Retrieval precision increases dramatically for specific technical queries.

### 2. Page-Level Context Expansion (N-1, N, N+1)

**Problem:** A section heading and its specification table are often split across chunk boundaries. The top-ranked chunk contains the heading but not the table values. LLM says "context does not include specific values."

**Solution:** After ranking, take the page number of the rank-1 chunk and fetch pages N-1, N, and N+1 from `metadata.json`. The previous page provides the section heading and introduction. The current page has the primary content. The next page has any table continuation.

**Result:** LLM receives complete pages with heading + all table rows → complete answers.

### 3. Small-to-Big Retrieval

**Problem:** Proposition hits are atomic sentences — too short to provide sufficient context for the LLM.

**Solution:** Every proposition carries `parent_chunk_id`. After proposition search, fetch the full parent section from the "sections" collection. Search small for precision, retrieve large for context.

### 4. Dual-Source Confidence Boost

When a section is found by both Qdrant semantic search AND Neo4j graph traversal, it receives a `+0.12` boost to its reranking score. Dual-source confirmation is a strong signal of relevance.

### 5. GLiNER over Ollama for Entity Extraction

For building the Neo4j graph, KRUTRIM uses GLiNER (a small discriminative NER model) rather than an LLM-based extractor.

| Factor | GLiNER | Ollama/LLM |
|---|---|---|
| Speed | 0.1–0.5s per chunk | 25–60s per chunk |
| Hallucination | None (span detection) | Real risk |
| Reliability | 100% structured output | ~85-90% valid JSON |
| GPU required | No | Strongly yes |

On CPU-only hardware, running Ollama for entity extraction on a 500-page book would take 3-8 hours per book. GLiNER processes the same book in 5-15 minutes.

### 6. Table Triple Representation

Tables are stored in three forms simultaneously because each form serves a different consumer:

- `structured_json` → Neo4j TableRow nodes (queryable graph structure)
- `linearized_text` → Qdrant embedding (semantically searchable)
- `original_markdown` → Audit trail and fallback

### 7. Disk Checkpointing

The most expensive step (Docling extraction, 45-90 minutes) is isolated in Phase 1. If Phase 2 (database ingestion, 5-15 minutes) fails due to a database timeout or network error, the system resumes from the checkpoint files on disk without re-running extraction.

Both Qdrant and Neo4j ingestion stages are independently resumable — if Qdrant succeeds but Neo4j fails, only Neo4j is re-run on resume.

---

## Knowledge Base Quality

### Confidence Report

After every ingestion, KRUTRIM generates a confidence report:

```
total_pages:         312   total chunks extracted
good_pages:          298   chunks with ≥50 words
short_pages:         10    chunks with 10-49 words
blank_image_pages:   4     chunks with <10 words (likely image-only pages)
total_chunks:        847   total Qdrant vectors
avg_words_per_chunk: 143   average words per embedded chunk
coverage_percent:    95.5  percentage of pages with good content
entity_mentions:     2341  total GLiNER entity detections
distinct_entities:   412   unique entities in graph
```

### What Affects Knowledge Base Quality

| Factor | Impact | Mitigation |
|---|---|---|
| Merged-cell tables | Proposition sentences are semantically wrong | Page expansion provides raw table to LLM |
| Image-only pages | Blank chunks, no retrievable content | Coverage report flags these |
| Scanned PDFs | Docling OCR quality varies | Use high-resolution source PDFs |
| Very long sections | Single chunk spans many pages | Proposition indexing mitigates this |
| GLiNER threshold | Too low = noise, too high = missed entities | Default 0.45 for ingestion, 0.35 for query |

---

## Retrieval Pipeline

### Mode Configuration

Three modes trade off between speed and depth:

| Mode | Model | Context | Top-N | Use Case |
|---|---|---|---|---|
| Fast | gemma3:1b | 12,000 chars | 8 | Quick factual lookups |
| Balanced | mistral:7b-instruct | 14,000 chars | 8 | Standard Q&A |
| Deep | llama3.1:8b-instruct | 16,000 chars | 10 | Complex multi-part questions |

### System Prompt Design

The system prompt contains a hard ban on reference-style answers:

```
ABSOLUTE RULES — NEVER VIOLATE:
1. NEVER say "see Table X", "refer to Table Y", "as shown in Table Z",
   "refer to the above table", "see page N"
2. When context contains specification data, INCLUDE those values DIRECTLY
3. NEVER truncate your answer
4. NEVER guess, infer, or hallucinate facts
5. ALWAYS cite: [Book: <book_id> | Page: <start>-<end>]
```

This rule set, combined with the page-level context expansion that delivers complete tables, produces self-contained answers that include all relevant values inline.

---

## Database Schemas

### Qdrant

```
Collection: "propositions"
  Vector dimension: 768 (Nomic embed-text-v1.5)
  Distance: Cosine
  Payload indexes: book_id (keyword), source_type (keyword), page (integer)

Collection: "sections"
  Vector dimension: 768
  Distance: Cosine
  Payload indexes: book_id (keyword), chunk_type (keyword)
```

### Neo4j Node Labels

```
:Book        {id, title, ingested_at}
:Chapter     {name, book_id, level}
:Section     {name, book_id, level}
:Subsection  {name, book_id, level}
:Entity      {name, type, book_id, raw}
:Spec        {subject, value, unit, raw, section, book_id}
:Table       {id, title, headers, row_count, book_id}
:TableRow    {id, parameter, value, unit, row_data, book_id}
```

### Neo4j Relationship Types

```
HAS_CHAPTER          Book → Chapter
HAS_SECTION          Chapter → Section
HAS_SUBSECTION       Section → Subsection
NEXT_SECTION         Section → Section (sequential)
MENTIONED_IN         Entity → Section/Subsection
HAS_SPECIFICATION    Entity → Spec
SENTENCE_CO_OCCURS   Entity → Entity {count, sections}
HAS_TABLE            Section → Table
HAS_ROW              Table → TableRow
```

### MongoDB Collections

```
users      {user_id, name, email, password_hash, role}
jobs       {job_id, book_id, status, ready_path, prop_path, metadata_path,
            qdrant_progress, neo4j_progress, confidence_report}
library    {book_id, book_title, status, total_sections, qdrant_chunks_stored,
            neo4j_entities, confidence_report, completed_at}
chats      {chat_id, user_id, title, book_ids, default_mode}
messages   {message_id, chat_id, role, content, mode, sources}
```

---

## Project Directory Structure

```
project_root/
│
├── parta/                              Knowledge base generation
│   ├── main_api.py                     FastAPI app (port 8000)
│   ├── pipeline_controller.py          2-phase orchestrator
│   ├── text_worker.py                  Docling worker process
│   │
│   ├── extraction/                     Distributed extraction
│   │   ├── extraction_server.py        Pull-based job queue (port 8004)
│   │   └── master.py                   Client — polls and assembles .md
│   │
│   ├── processing/                     Core pipeline
│   │   ├── chunk.py                    Step 1 — Smart chunker
│   │   ├── triple_rep.py               Step 2 — Table triple representation
│   │   ├── build_metadata.py           Step 3 — Page metadata index
│   │   ├── propositions.py             Step 4 — Atomic proposition extraction
│   │   ├── ingest_qdrant.py            Step 5a — Vector ingestion
│   │   └── ingest_neo4j.py             Step 5b — Graph ingestion
│   │
│   ├── data/
│   │   ├── raw/                        Uploaded PDFs
│   │   ├── processed/                  Docling markdown output
│   │   ├── checkpoints/                _ready.json, _propositions.json
│   │   ├── metadata/                   _metadata.json (page index)
│   │   ├── qdrant/                     _chunks.json (confidence report)
│   │   └── neo4j/                      _neo4j_log.json
│   │
│   ├── frontend/
│   │   └── index.html                  Upload and monitoring UI
│   │
│   └── portable/                       Offline models (never committed)
│       ├── docling/                    TableFormer + layout models
│       ├── nomic/                      Nomic embed-text-v1.5
│       ├── gliner/                     GLiNER NER model
│       ├── reranker/                   CrossEncoder model
│       └── nltk_data/                  NLTK punkt tokenizer
│
└── partb/                              Retrieval and Q&A
    ├── app.py                          FastAPI app (port 9000)
    ├── config.py                       All constants, hardcoded
    ├── db.py                           MongoDB client
    ├── auth_jwt.py                     JWT helpers
    │
    ├── routers/
    │   ├── auth_router.py              /auth/login, /auth/signup
    │   ├── chats_router.py             /chats CRUD + /ask SSE endpoint
    │   ├── meta_router.py              /library, /health, /models
    │   └── pdf_router.py               /pdf/{book_id} — serves PDF binary
    │
    ├── retrieval/
    │   ├── pipeline.py                 7-step retrieval pipeline
    │   └── prompts.py                  System prompts with anti-reference rules
    │
    ├── services/
    │   ├── messages.py                 Message persistence
    │   └── pages.py                    Page text lookup (legacy)
    │
    ├── llm/
    │   └── stream_client.py            LiteLLM / Ollama streaming client
    │
    ├── frontend/
    │   └── chat.html                   Chat UI with PDF viewer
    │
    └── static/
        ├── marked.min.js               Markdown renderer (local, offline)
        ├── mermaid.min.js              Diagram renderer (local, offline)
        ├── isro-logo.svg
        └── pdfjs/
            ├── pdf.min.js              PDF.js v2.x legacy build
            └── pdf.worker.min.js
```

---

## Installation and Setup

### Prerequisites

```
Python 3.10.9
MongoDB 6.x
Neo4j 5.x
Qdrant (latest)
Ollama (optional — for local LLM inference)
LiteLLM (optional — as LLM proxy)
```

### Python Dependencies

**Part A:**
```bash
cd parta
pip install fastapi uvicorn pymongo bcrypt pyjwt \
            docling sentence-transformers qdrant-client \
            neo4j gliner nltk
```

**Part B:**
```bash
cd partb
pip install fastapi uvicorn pymongo bcrypt pyjwt \
            sentence-transformers qdrant-client neo4j \
            gliner nltk httpx pypdf litellm
```

### Offline Model Setup

Download all models once and place in `parta/portable/`:

```bash
# Nomic embedding model
python -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)
m.save('parta/portable/nomic')
"

# GLiNER NER model
python -c "
from gliner import GLiNER
m = GLiNER.from_pretrained('urchade/gliner_medium-v2.1')
m.save_pretrained('parta/portable/gliner')
"

# CrossEncoder reranker
python -c "
from sentence_transformers import CrossEncoder
m = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
m.save('parta/portable/reranker')
"

# NLTK punkt tokenizer
python -c "
import nltk
nltk.download('punkt', download_dir='parta/portable/nltk_data')
nltk.download('punkt_tab', download_dir='parta/portable/nltk_data')
"
```

Docling models download automatically on first run to `~/.cache/docling/`.

### PDF.js Setup (Part B frontend)

Download the **legacy** build (v2.16.105) from:
```
https://github.com/mozilla/pdf.js/releases/tag/v2.16.105
```

Download `pdfjs-2.16.105-legacy.zip`. Extract and copy to `partb/static/pdfjs/`:
```
pdf.min.js
pdf.worker.min.js
```

> **Important:** Use the legacy build, not the standard dist. The standard build uses ES module syntax (`import.meta`) which breaks when loaded as a regular script tag.

### Database Setup

```bash
# Start MongoDB (default port 27017)
mongod --dbpath /data/db

# Start Neo4j (default port 7687)
# Set password to match config: sac@1234
neo4j start

# Start Qdrant (default port 6333)
./qdrant
```

---

## Running the System

### Part A — Start Order

```bash
# Terminal 1: Job queue server (must start first)
uvicorn parta.extraction.extraction_server:app --host 0.0.0.0 --port 8004

# Terminal 2: Main API
cd parta
uvicorn main_api:app --host 0.0.0.0 --port 8000

# Terminal 3+: Worker machines (run on each machine with Docling installed)
cd parta
python text_worker.py
```

Open `http://localhost:8000` to upload PDFs and monitor ingestion progress.

### Part B — Start Order

```bash
# Start LiteLLM proxy (if using)
litellm --config litellm_config.yaml --port 4000

# Start Part B
cd partb
uvicorn app:app --host 0.0.0.0 --port 9000
```

Open `http://localhost:9000` for the chat interface.

---

## Configuration

All configuration is hardcoded (no environment variables) per the air-gapped deployment requirement.

### Part A `pipeline_controller.py`

```python
BASE_DIR = Path(__file__).resolve().parent
```

### Part B `config.py`

```python
MONGO_URI            = "mongodb://localhost:27017"
MONGO_DB             = "rag_system"
JWT_SECRET           = "ISRO_RAG_SECRET_CHANGE_IN_PROD"
LITELLM_BASE_URL     = "http://127.0.0.1:4000/v1"
USE_OLLAMA_DIRECT    = False
OLLAMA_URL           = "http://127.0.0.1:11434"
QDRANT_URL           = "http://localhost:6333"
COLLECTION_PROPS     = "propositions"
COLLECTION_SECTIONS  = "sections"
NEO4J_URI            = "bolt://localhost:7687"
NEO4J_USER           = "neo4j"
NEO4J_PASSWORD       = "sac@1234"
```

---

## API Reference

### Part A Endpoints (port 8000)

| Method | Path | Description |
|---|---|---|
| POST | /auth/signup | Create user account |
| POST | /auth/login | Get JWT token |
| POST | /upload_book | Upload PDF for ingestion |
| GET | /progress/{job_id} | SSE stream of pipeline progress |
| GET | /library | List all ingested books |
| GET | /library/check | Check if book_id exists |
| GET | /pending_jobs | List resumable failed jobs |
| POST | /resume/{job_id} | Resume Phase 2 of a failed job |
| GET | /jobs/{job_id} | Job status snapshot |
| GET | /health | Health check |

### Part B Endpoints (port 9000)

| Method | Path | Description |
|---|---|---|
| POST | /auth/login | Login (shared auth with Part A) |
| POST | /auth/signup | Create account |
| GET | /chats | List user's chats |
| POST | /chats | Create new chat |
| GET | /chats/{chat_id} | Get chat + messages |
| POST | /chats/{chat_id}/ask | Ask question (SSE stream) |
| DELETE | /chats/{chat_id} | Delete chat |
| GET | /library | List available books |
| GET | /health | Health check for all services |
| GET | /pdf/{book_id} | Serve PDF binary for viewer |
| GET | /pdf/{book_id}/info | Get PDF page count |

---

## Frontend

### Part A UI

- PDF upload with drag-and-drop
- Real-time progress bar via SSE
- Separate progress bars for Qdrant and Neo4j ingestion
- Confidence report on completion (coverage %, entity counts)
- Library view showing all ingested books

### Part B Chat UI

- Multi-chat sidebar
- Book selection per chat (can query multiple books simultaneously)
- Three response modes (Fast / Balanced / Deep) selectable per message
- Streaming answer display with markdown rendering
- Source chips showing page references under each answer
- Click source chip → PDF viewer opens at that exact page
- PDF viewer: Prev / page input / Next controls
- Regenerate button with mode selector

---

## Performance

### Part A Timing (500-page aerospace document, CPU-only)

| Stage | Time |
|---|---|
| Docling extraction (1 worker) | 45–90 min |
| Docling extraction (3 workers) | 15–30 min |
| chunk.py | < 30 sec |
| triple_rep.py | < 2 min |
| build_metadata.py | < 1 min |
| propositions.py | < 5 min |
| ingest_qdrant.py (Nomic embed) | 5–15 min |
| ingest_neo4j.py (GLiNER) | 5–15 min |
| **Total (3 workers)** | **~35–65 min** |

### Part B Query Timing

| Stage | Time |
|---|---|
| Proposition search (Qdrant) | 0.1–0.3s |
| GLiNER on query | 0.1–0.5s |
| Neo4j traversal | 0.1–0.3s |
| Section fetch + rerank | 0.5–2s |
| Page metadata lookup | < 0.1s |
| LLM generation (7b model) | 10–60s |
| **Total to first token** | **~2–4s** |

### PDF Viewer

PDF.js v2.x (legacy build) renders pages client-side in the browser. Page render time is 0.1–0.3 seconds per page. The PDF is loaded once from the server and cached in browser memory for the session.

---

## Limitations and Future Work

### Current Limitations

**1. Merged-Cell Tables**
Tables with merged cells (common in ISRO satellite specification tables) produce semantically incorrect propositions. For example, a table where the first column is a merged satellite name and subsequent columns are properties/values causes the proposition generator to treat column headers as semantic keys. The page expansion step mitigates this by providing the raw table, but proposition-level retrieval for these tables is weak.

**Planned fix:** Detect merged-cell pattern (>60% of rows have empty first column) and apply a different proposition template.

**2. Image-Only Pages**
Pages containing only diagrams, photographs, or scanned content produce blank or near-blank chunks. Docling's layout model attempts OCR but quality varies with scan resolution.

**Planned fix:** Store Docling's figure extraction output and attach figure descriptions to adjacent text chunks.

**3. CPU-Only Entity Extraction Quality**
GLiNER on CPU-only hardware produces entity and co-occurrence graphs that lack semantic relationship types. All edges between entities are `SENTENCE_CO_OCCURS` — two entities in the same sentence — rather than typed edges like `CONTAINS`, `MEASURES`, or `OPERATES_AT`.

**Planned fix:** Add Ollama-based relation extraction on GPU hardware as an optional Layer 6 on top of the GLiNER backbone.

**4. Single-Language Support**
The current system assumes English-language documents. ISRO manuals are primarily in English but some internal documents contain Hindi terminology.

### Future Work

- GPU-based Ollama enrichment for typed relation edges in Neo4j
- Multi-book knowledge fusion (entity deduplication across books)
- Automatic question suggestion based on document coverage
- Confidence scoring for Neo4j edges (entity pairs with count ≥ 3 across multiple sections)
- REST API for programmatic access without the chat UI
- Support for incremental ingestion (add new pages to existing book)

---

## Tech Stack

### Core Infrastructure

| Component | Technology | Version | Purpose |
|---|---|---|---|
| Web Framework | FastAPI | 0.100+ | REST API + SSE |
| ASGI Server | Uvicorn | Latest | HTTP server |
| Database | MongoDB | 6.x | Jobs, chats, library |
| Vector DB | Qdrant | Latest | Semantic search |
| Graph DB | Neo4j | 5.x | Structural graph |
| Auth | PyJWT + bcrypt | Latest | JWT authentication |

### ML and NLP

| Component | Technology | Purpose |
|---|---|---|
| PDF Extraction | Docling | Layout-aware PDF to markdown |
| Table Extraction | TableFormer (via Docling) | Table structure detection |
| Embedding Model | Nomic embed-text-v1.5 | 768-dim sentence embeddings |
| NER Model | GLiNER (medium v2.1) | Named entity recognition |
| Reranker | CrossEncoder ms-marco | Result reranking |
| Sentence Tokenizer | NLTK punkt | Proposition splitting |
| LLM | Ollama (Mistral/Llama) | Answer generation |
| LLM Proxy | LiteLLM | OpenAI-compatible interface |

### Frontend

| Component | Technology | Purpose |
|---|---|---|
| Markdown | marked.js v9 | Render LLM output |
| PDF Viewer | PDF.js v2.16 (legacy) | In-browser PDF rendering |
| Diagrams | mermaid.js | Optional diagram rendering |

---

## Authors and Acknowledgements

This system was designed and built during an internship at ISRO's Space Applications Centre (SAC), Ahmedabad.

The architecture draws on the following research concepts:
- **Proposition Indexing** — Chen et al., "Dense X Retrieval: What Retrieval Granularity Should We Use?" (2023)
- **Small-to-Big Retrieval** — LlamaIndex hierarchical retrieval pattern
- **HybridRAG** — combining vector and graph retrieval for improved precision
- **GraphRAG** — Microsoft Research, knowledge graph-augmented RAG

---

*KRUTRIM — Building artificial intelligence over institutional knowledge.*
