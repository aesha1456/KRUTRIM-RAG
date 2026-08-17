"""
processing/propositions.py
---------------------------
STEP 3 of the new processing pipeline.

Reads  → data/checkpoints/{book_id}_ready.json   (enriched by triple_rep.py)
Writes → data/checkpoints/{book_id}_propositions.json

Responsibilities:
  This file implements "Proposition Indexing" — the single biggest
  quality improvement over naive 600-word chunking.

  Instead of embedding large paragraphs (where the vector is an
  average of many unrelated facts), we embed one atomic sentence
  at a time. Each sentence vector is maximally precise.

  At retrieval time (Part B):
    1. Search "propositions" collection → get top-K precise matches
    2. Use parent_chunk_id on each match → fetch full parent section
       from "sections" collection
    3. Send full sections as context to LLM
  This is called "small-to-big retrieval."

  ── FOR TEXT CHUNKS ──────────────────────────────────────────────────
  Uses NLTK sent_tokenize to split chunk["content"] into sentences.
  Each sentence becomes one proposition IF it passes quality filters:
    - Minimum 8 words (rejects headers, stray labels, page numbers)
    - Minimum 30 characters
    - Not purely numeric
    - Not a table-of-contents line (ends with page number pattern)
  Each proposition dict:
    {
      "proposition_id" : stable uuid5,
      "text"           : the sentence,
      "parent_chunk_id": chunk["chunk_id"]  ← small-to-big link,
      "section_path"   : chunk["section_path"],
      "page"           : chunk["page_range"]["start"],
      "source_type"    : "text",
      "book_id"        : chunk["book_id"],
    }

  ── FOR TABLE CHUNKS ─────────────────────────────────────────────────
  Uses chunk["structured_json"]["rows"] (produced by triple_rep.py).
  Each row → one auto-generated natural language sentence.
  100% deterministic. Zero ML. Zero hallucination risk.

  Generation logic:
    headers = ["Parameter", "Value", "Unit"]
    row     = {"Parameter": "Thrust", "Value": "799", "Unit": "kN"}

    If headers look like [entity, property, value] pattern:
      → "{section_name} {Parameter} is {Value} {Unit}."
      → "Vikas Engine Thrust is 799 kN."

    Otherwise (generic):
      → "Parameter: Thrust | Value: 799 | Unit: kN."

  Each proposition dict:
    {
      "proposition_id" : stable uuid5,
      "text"           : generated sentence,
      "parent_chunk_id": chunk["chunk_id"],
      "section_path"   : chunk["section_path"],
      "page"           : chunk["page_range"]["start"],
      "source_type"    : "table",
      "book_id"        : chunk["book_id"],
      "row_data"       : the original row dict (for Neo4j TableRow nodes),
    }

  The "linearized_text" field from triple_rep.py is ALSO kept as one
  additional proposition per table — it represents the whole table as
  a single searchable unit, complementing the per-row propositions.

Called by pipeline_controller.py:
    from processing.propositions import run_propositions
    prop_path = run_propositions(book_id, json_path, callback)
"""

import re
from parta.logger import time_it, async_time_it
import json
import uuid
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# NLTK SETUP — offline, uses portable/nltk_data if present
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _load_nltk(base_dir: Path):
    """
    Loads NLTK with offline data path if available.
    Falls back to simple regex splitter if NLTK is not available.
    Returns the sentence tokenizer function to use.
    """
    import nltk

    # Use portable nltk_data if it exists (air-gapped environment)
    nltk_data_path = base_dir / "portable" / "nltk_data"
    if nltk_data_path.exists():
        if str(nltk_data_path) not in nltk.data.path:
            nltk.data.path.insert(0, str(nltk_data_path))

    try:
        # Test that punkt tokenizer is available
        nltk.data.find("tokenizers/punkt")
        from nltk.tokenize import sent_tokenize
        print("[PROPOSITIONS] ✅ NLTK punkt tokenizer loaded.")
        return sent_tokenize
    except LookupError:
        try:
            # Try punkt_tab (newer NLTK versions)
            nltk.data.find("tokenizers/punkt_tab")
            from nltk.tokenize import sent_tokenize
            print("[PROPOSITIONS] ✅ NLTK punkt_tab tokenizer loaded.")
            return sent_tokenize
        except LookupError:
            print("[PROPOSITIONS] ⚠  NLTK punkt not found. "
                  "Using regex sentence splitter as fallback.")
            return _regex_sent_tokenize


@time_it
def _regex_sent_tokenize(text: str) -> list:
    """
    Simple regex-based sentence splitter.
    Used as fallback when NLTK punkt is not available.
    Splits on: . ! ? followed by whitespace and an uppercase letter.
    """
    # Split on sentence-ending punctuation
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in sentences if s.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# QUALITY FILTERS
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that indicate a sentence is noise, not a useful proposition
_NOISE_PATTERNS = [
    re.compile(r"^\d+[\.\)]\s*$"),                    # bare numbers "1." "42)"
    re.compile(r"^[ivxlcdm]+[\.\)]\s*$", re.I),       # roman numerals
    re.compile(r"^\W+$"),                              # only punctuation/symbols
    re.compile(r"\.{3,}"),                             # dot leaders "........"
    re.compile(r"\d+\s*$"),                            # ends in bare page number
    re.compile(r"^(fig|figure|table|ref|see)\s", re.I), # bare figure/table refs
    re.compile(r"^(page|pg)\s*\d+", re.I),            # "page 42"
    re.compile(r"^\s*(back|next|index|home|print)\s*$", re.I),  # UI noise
]

@time_it
def _is_quality_sentence(sentence: str) -> bool:
    """
    Returns True if the sentence is worth embedding as a proposition.

    Rejects:
      - Fewer than 8 words
      - Fewer than 30 characters
      - Purely numeric strings
      - Noise patterns (see above)
      - Table of contents lines (text followed by many spaces then a number)
    """
    text = sentence.strip()

    if len(text) < 30:
        return False

    words = text.split()
    if len(words) < 8:
        return False

    # Purely numeric
    if re.match(r"^[\d\s\.\,\-\+]+$", text):
        return False

    # Table of contents line: "Section Title .......... 42"
    if re.search(r"\.{3,}\s*\d+\s*$", text):
        return False

    # Check against noise patterns
    for pattern in _NOISE_PATTERNS:
        if pattern.search(text):
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# PROPOSITION ID — stable, deterministic
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _make_proposition_id(book_id: str, parent_chunk_id: str, index: int) -> str:
    """
    Deterministic UUID for a proposition.
    Same document, same chunk, same index → same ID.
    Ensures Qdrant upserts are idempotent.
    """
    key = f"{book_id}::prop::{parent_chunk_id}::{index}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))


# ─────────────────────────────────────────────────────────────────────────────
# TEXT PROPOSITION EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _extract_text_propositions(
    chunk:         dict,
    sent_tokenize, # the tokenizer function
    prop_index:    int,
) -> tuple:
    """
    Splits a text chunk into atomic sentence propositions.

    Returns:
        (propositions: list, next_prop_index: int)
    """
    content       = chunk.get("content", "").strip()
    chunk_id      = chunk["chunk_id"]
    book_id       = chunk["book_id"]
    section_path  = chunk["section_path"]
    page          = chunk.get("page_range", {}).get("start", 0)

    if not content:
        return [], prop_index

    # Tokenize into sentences
    try:
        sentences = sent_tokenize(content)
    except Exception:
        sentences = _regex_sent_tokenize(content)

    propositions = []
    for sentence in sentences:
        sentence = sentence.strip()

        if not _is_quality_sentence(sentence):
            continue

        pid = _make_proposition_id(book_id, chunk_id, prop_index)
        prop_index += 1

        propositions.append({
            "proposition_id":  pid,
            "text":            sentence,
            "parent_chunk_id": chunk_id,
            "section_path":    section_path,
            "page":            page,
            "source_type":     "text",
            "book_id":         book_id,
            "element_ids":     chunk.get("element_ids", []),
            "bounding_boxes":  chunk.get("bounding_boxes", []),
        })

    return propositions, prop_index


# ─────────────────────────────────────────────────────────────────────────────
# TABLE PROPOSITION GENERATION
# ─────────────────────────────────────────────────────────────────────────────

# Common header patterns that suggest a [subject, property, value] table
_SUBJECT_HEADERS = {
    "parameter", "item", "name", "component", "system",
    "subsystem", "element", "description", "property",
    "specification", "spec", "characteristic",
}
_PROPERTY_HEADERS = {
    "parameter", "property", "characteristic", "metric",
    "attribute", "field", "type", "category",
}
_VALUE_HEADERS = {
    "value", "values", "data", "result", "reading",
    "measurement", "quantity", "amount",
}
_UNIT_HEADERS = {
    "unit", "units", "uom", "measure",
}


@time_it
def _detect_table_pattern(headers: list) -> str:
    """
    Looks at table headers and returns the generation pattern to use.

    Returns:
      "spec"    → [parameter/name, value, unit?] — most common in aerospace
      "generic" → anything else
    """
    lowered = [h.lower().strip() for h in headers]

    # Check if any header looks like a parameter/name column
    has_param = any(h in _SUBJECT_HEADERS or h in _PROPERTY_HEADERS
                    for h in lowered)
    has_value = any(h in _VALUE_HEADERS for h in lowered)

    if has_param and has_value:
        return "spec"

    return "generic"


@time_it
def _row_to_sentence_spec(
    row:          dict,
    headers:      list,
    section_name: str,
) -> str:
    """
    Generates a natural sentence from a spec-style table row.

    Template: "{section_name} {Parameter} is {Value} {Unit}."

    Example:
        section_name = "Vikas Engine"
        row = {"Parameter": "Thrust", "Value": "799", "Unit": "kN"}
        → "Vikas Engine Thrust is 799 kN."
    """
    lowered_headers = {h.lower().strip(): h for h in headers}

    # Find which header is parameter, value, unit
    param_header = next(
        (lowered_headers[h] for h in lowered_headers
         if h in _SUBJECT_HEADERS or h in _PROPERTY_HEADERS),
        headers[0] if headers else None
    )
    value_header = next(
        (lowered_headers[h] for h in lowered_headers
         if h in _VALUE_HEADERS),
        headers[1] if len(headers) > 1 else None
    )
    unit_header = next(
        (lowered_headers[h] for h in lowered_headers
         if h in _UNIT_HEADERS),
        None
    )

    param = row.get(param_header, "").strip() if param_header else ""
    value = row.get(value_header, "").strip() if value_header else ""
    unit  = row.get(unit_header,  "").strip() if unit_header  else ""

    if not param or not value:
        return _row_to_sentence_generic(row, headers)

    # Build sentence
    if section_name and section_name.lower() not in param.lower():
        subject = f"{section_name} {param}"
    else:
        subject = param

    if unit:
        return f"{subject} is {value} {unit}."
    else:
        return f"{subject} is {value}."


@time_it
def _row_to_sentence_generic(row: dict, headers: list) -> str:
    """
    Generates a generic key-value sentence from a table row.

    Template: "Column1: Value1 | Column2: Value2 | Column3: Value3."

    Example:
        row = {"Stage": "PS1", "Propellant": "HTPB", "Mass": "138 t"}
        → "Stage: PS1 | Propellant: HTPB | Mass: 138 t."
    """
    parts = []
    for header in headers:
        val = row.get(header, "").strip()
        if val:
            parts.append(f"{header}: {val}")
    if parts:
        return " | ".join(parts) + "."
    return ""


@time_it
def _extract_table_propositions(
    chunk:      dict,
    prop_index: int,
) -> tuple:
    """
    Generates propositions from a table chunk.

    Two types of propositions per table:
      1. One proposition per row (precise, searchable facts)
      2. One proposition for the whole table linearized_text
         (captures the overall table as a retrievable unit)

    Returns:
        (propositions: list, next_prop_index: int)
    """
    chunk_id      = chunk["chunk_id"]
    book_id       = chunk["book_id"]
    section_path  = chunk.get("section_path", [])
    page          = chunk.get("page_range", {}).get("start", 0)

    structured    = chunk.get("structured_json", {})
    headers       = structured.get("headers", [])
    rows          = structured.get("rows", [])
    linearized    = chunk.get("linearized_text", "")

    propositions  = []

    # Section name for context in spec sentences
    # Use the deepest level of section_path
    section_name = section_path[-1] if section_path else ""

    # ── Per-row propositions ──────────────────────────────────────────────────
    if headers and rows:
        pattern = _detect_table_pattern(headers)

        for row in rows:
            if pattern == "spec":
                sentence = _row_to_sentence_spec(row, headers, section_name)
            else:
                sentence = _row_to_sentence_generic(row, headers)

            if not sentence or len(sentence.strip()) < 10:
                continue

            pid = _make_proposition_id(book_id, chunk_id, prop_index)
            prop_index += 1

            propositions.append({
                "proposition_id":  pid,
                "text":            sentence.strip(),
                "parent_chunk_id": chunk_id,
                "section_path":    section_path,
                "page":            page,
                "source_type":     "table_row",
                "book_id":         book_id,
                "row_data":        row,   # preserved for Neo4j TableRow nodes
                "element_ids":     chunk.get("element_ids", []),
                "bounding_boxes":  chunk.get("bounding_boxes", []),
            })

    # ── Whole-table linearized proposition ───────────────────────────────────
    # Only add if meaningfully different from row propositions
    if linearized and len(linearized.strip()) > 30:
        pid = _make_proposition_id(book_id, chunk_id, prop_index)
        prop_index += 1

        propositions.append({
            "proposition_id":  pid,
            "text":            linearized.strip(),
            "parent_chunk_id": chunk_id,
            "section_path":    section_path,
            "page":            page,
            "source_type":     "table_full",
            "book_id":         book_id,
            "element_ids":     chunk.get("element_ids", []),
            "bounding_boxes":  chunk.get("bounding_boxes", []),
        })

    return propositions, prop_index


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — called by pipeline_controller.py
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def run_propositions(
    book_id:           str,
    json_path:         str,
    base_dir:          str,
    progress_callback = None,
) -> str:
    """
    Main entry point called by pipeline_controller.py

    Args:
        book_id   : e.g. "PSLV-C50"
        json_path : path to {book_id}_ready.json (from run_triple_rep)
        base_dir  : project root (for NLTK path)
        progress_callback : optional fn(percent, stage, message, extra=None)

    Returns:
        str — absolute path to written {book_id}_propositions.json

    Raises:
        FileNotFoundError if json_path does not exist
        RuntimeError if no propositions are produced
    """
    input_path  = Path(json_path)
    base        = Path(base_dir)
    output_dir  = base / "data" / "checkpoints"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{book_id}_propositions.json"

    if not input_path.exists():
        raise FileNotFoundError(
            f"[PROPOSITIONS] Checkpoint not found: {json_path}\n"
            f"               Has triple_rep.py completed successfully?"
        )

    if progress_callback:
        progress_callback(
            percent=60,
            stage="Proposition Extraction",
            message=f"Loading chunks for {book_id}...",
        )

    print(f"\n[PROPOSITIONS] Reading: {input_path.name}")

    with open(input_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    if not chunks:
        raise RuntimeError(
            f"[PROPOSITIONS] No chunks in {json_path}. "
            "Check that chunk.py and triple_rep.py ran successfully."
        )

    # ── Load NLTK (offline-safe) ──────────────────────────────────────────────
    sent_tokenize = _load_nltk(base)

    text_chunks  = [c for c in chunks if c.get("type") == "text"]
    table_chunks = [c for c in chunks if c.get("type") == "table"]

    print(f"[PROPOSITIONS] Processing {len(text_chunks)} text chunks "
          f"and {len(table_chunks)} table chunks...")

    if progress_callback:
        progress_callback(
            percent=61,
            stage="Proposition Extraction",
            message=(
                f"Splitting {len(text_chunks)} text sections into sentences "
                f"and {len(table_chunks)} tables into row propositions..."
            ),
        )

    # ── Extract all propositions ──────────────────────────────────────────────
    all_propositions = []
    prop_index       = 0

    text_prop_count  = 0
    table_prop_count = 0

    for chunk in chunks:
        chunk_type = chunk.get("type")

        if chunk_type == "text":
            props, prop_index = _extract_text_propositions(
                chunk, sent_tokenize, prop_index
            )
            text_prop_count += len(props)
            all_propositions.extend(props)

        elif chunk_type == "table":
            props, prop_index = _extract_table_propositions(
                chunk, prop_index
            )
            table_prop_count += len(props)
            all_propositions.extend(props)

    # ── Validate output ───────────────────────────────────────────────────────
    if not all_propositions:
        raise RuntimeError(
            f"[PROPOSITIONS] Zero propositions extracted from {book_id}. "
            "Check that the markdown contains real sentences and tables."
        )

    print(f"[PROPOSITIONS] ✅ {len(all_propositions)} total propositions")
    print(f"               {text_prop_count}  from text sentences")
    print(f"               {table_prop_count} from table rows + full tables")

    if progress_callback:
        progress_callback(
            percent=63,
            stage="Proposition Extraction",
            message=(
                f"{len(all_propositions)} propositions extracted — "
                f"{text_prop_count} text, {table_prop_count} table. "
                f"Saving checkpoint..."
            ),
            extra={
                "total_propositions": len(all_propositions),
                "text_propositions":  text_prop_count,
                "table_propositions": table_prop_count,
            },
        )

    # ── Write checkpoint ──────────────────────────────────────────────────────
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_propositions, f, indent=2, ensure_ascii=False)

    print(f"[PROPOSITIONS] 💾 Checkpoint saved → {output_file.name}")

    if progress_callback:
        progress_callback(
            percent=64,
            stage="Proposition Extraction",
            message=(
                f"Proposition extraction complete. "
                f"{len(all_propositions)} atomic facts ready for embedding."
            ),
        )

    return str(output_file)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE — run directly for debugging
# python processing/propositions.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    BASE_DIR = Path(__file__).resolve().parent.parent

    def _print_callback(percent, stage, message, extra=None):
        print(f"  [{percent}%] {stage}: {message}")

    checkpoint_dir = BASE_DIR / "data" / "checkpoints"
    ready_files    = sorted(checkpoint_dir.glob("*_ready.json"))

    if not ready_files:
        print(f"[PROPOSITIONS] No *_ready.json files in {checkpoint_dir}")
        print("               Run chunk.py and triple_rep.py first.")
        sys.exit(1)

    for rf in ready_files:
        book_id = rf.stem.replace("_ready", "")
        print(f"\n{'='*60}")
        print(f"  Processing: {book_id}")
        print(f"{'='*60}")

        try:
            out = run_propositions(
                book_id, str(rf), str(BASE_DIR), _print_callback
            )

            # Quick inspection
            with open(out, "r", encoding="utf-8") as fh:
                props = json.load(fh)

            print(f"\n  Total propositions : {len(props)}")

            text_props  = [p for p in props if p["source_type"] == "text"]
            table_row_p = [p for p in props if p["source_type"] == "table_row"]
            table_full  = [p for p in props if p["source_type"] == "table_full"]

            print(f"  Text propositions  : {len(text_props)}")
            print(f"  Table row props    : {len(table_row_p)}")
            print(f"  Table full props   : {len(table_full)}")

            print(f"\n  Sample text proposition:")
            if text_props:
                p = text_props[0]
                print(f"    Path : {' > '.join(p['section_path'])}")
                print(f"    Text : {p['text'][:120]}...")

            print(f"\n  Sample table proposition:")
            if table_row_p:
                p = table_row_p[0]
                print(f"    Path : {' > '.join(p['section_path'])}")
                print(f"    Text : {p['text']}")

        except Exception as e:
            print(f"  ❌ Error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
