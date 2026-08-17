"""
processing/triple_rep.py
-------------------------
STEP 2 of the new processing pipeline.

Reads  → data/checkpoints/{book_id}_ready.json   (written by chunk.py)
Writes → data/checkpoints/{book_id}_ready.json   (same file, enriched in-place)

Responsibilities:
  For every chunk where type == "table":
    Parses the raw markdown pipe table in chunk["content"] and adds
    three new fields to that chunk dict:

    chunk["structured_json"] = {
        "headers": ["Parameter", "Value", "Unit"],
        "rows": [
            {"Parameter": "Thrust", "Value": "799", "Unit": "kN"},
            {"Parameter": "ISP",    "Value": "293", "Unit": "s"},
        ]
    }
    → Used by ingest_neo4j.py to build TableRow nodes in Neo4j

    chunk["linearized_text"] = (
        "Parameter: Thrust | Value: 799 | Unit: kN. "
        "Parameter: ISP | Value: 293 | Unit: s."
    )
    → Used by propositions.py to generate embeddable sentences
    → Also stored as a fallback text representation in Qdrant

    chunk["original_markdown"] = "| Parameter | Value |..."
    → The raw pipe table exactly as chunk.py produced it
    → Audit trail — never modified after this step

  For every chunk where type == "text":
    Passes through completely unchanged.
    No fields added, no content modified.

  Robustness:
    - Handles tables with missing cells (ragged rows)
    - Handles separator rows (|---|---|) — skipped from data rows
    - Handles leading/trailing whitespace in cells
    - Handles tables with no header (treats first row as header)
    - If a table cannot be parsed at all, adds empty structured_json
      and uses original_markdown as linearized_text (never crashes)

Called by pipeline_controller.py:
    from processing.triple_rep import run_triple_rep
    run_triple_rep(book_id, json_path, callback)
"""

import re
from parta.logger import time_it, async_time_it
import json
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# TABLE PARSING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _split_table_row(line: str) -> list:
    """
    Splits a markdown pipe table row into individual cell strings.

    Input:  "| Thrust | 799 kN | Sea Level |"
    Output: ["Thrust", "799 kN", "Sea Level"]

    Handles:
      - Leading/trailing pipes
      - Extra whitespace inside cells
      - Empty cells (returns empty string for them)
    """
    # Strip leading/trailing whitespace and pipes
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]

    cells = [cell.strip() for cell in line.split("|")]
    return cells


@time_it
def _is_separator_row(line: str) -> bool:
    """
    Returns True if the line is a markdown table separator.
    Separator rows look like: |---|---|  or  |:--|:--:|--:|
    They contain only dashes, colons, pipes, and spaces.
    """
    stripped = line.strip()
    # Must start with | or -
    if not (stripped.startswith("|") or stripped.startswith("-")):
        return False
    # After removing pipes, dashes, colons, spaces — should be empty
    cleaned = re.sub(r"[\|\-\:\s]", "", stripped)
    return cleaned == ""


@time_it
def _parse_markdown_table(markdown: str) -> Optional[dict]:
    """
    Parses a raw markdown pipe table string into a structured dict.

    Returns:
        {
            "headers": ["Col1", "Col2", "Col3"],
            "rows":    [
                {"Col1": "val", "Col2": "val", "Col3": "val"},
                ...
            ]
        }

    Returns None if the table cannot be parsed (e.g. malformed input).

    Strategy:
      - First non-separator, non-empty line → headers
      - Second line (if separator) → skip
      - Remaining non-separator, non-empty lines → data rows
      - Ragged rows: missing cells → empty string, extra cells → ignored
    """
    lines = [
        line for line in markdown.strip().split("\n")
        if line.strip()  # drop blank lines inside the table block
    ]

    if not lines:
        return None

    # ── Find header row ───────────────────────────────────────────────────────
    header_line_idx = None
    for i, line in enumerate(lines):
        if not _is_separator_row(line) and _is_table_line(line):
            header_line_idx = i
            break

    if header_line_idx is None:
        return None

    headers_raw = _split_table_row(lines[header_line_idx])

    # Remove completely empty headers (trailing pipe artefacts)
    headers = [h for h in headers_raw if h]

    if not headers:
        return None

    # ── Collect data rows (skip separator rows and the header row) ────────────
    rows = []
    for i, line in enumerate(lines):
        if i == header_line_idx:
            continue
        if _is_separator_row(line):
            continue
        if not line.strip():
            continue

        cells = _split_table_row(line)

        # Build dict — zip with headers, handle ragged rows
        row_dict = {}
        for col_idx, header in enumerate(headers):
            if col_idx < len(cells):
                row_dict[header] = cells[col_idx]
            else:
                row_dict[header] = ""   # missing cell → empty string

        # Only add rows that have at least one non-empty value
        if any(v.strip() for v in row_dict.values()):
            rows.append(row_dict)

    return {
        "headers": headers,
        "rows":    rows,
    }


@time_it
def _is_table_line(line: str) -> bool:
    """Returns True if line looks like a markdown table row."""
    stripped = line.strip()
    return stripped.startswith("|") or stripped.count("|") >= 2


@time_it
def _build_linearized_text(
    structured: dict,
    section_path: list,
) -> str:
    """
    Converts structured_json into a readable text string for embedding.

    Format per row:
        "Column1: Value1 | Column2: Value2 | Column3: Value3."

    All rows are joined with a space to form one string.

    The section_path is prepended as context so that the embedding
    captures WHAT system/component this table belongs to.

    Example output:
        "Section: Vikas Engine Performance Parameters. "
        "Parameter: Thrust | Value: 799 | Unit: kN. "
        "Parameter: Specific Impulse | Value: 293 | Unit: s. "
        "Parameter: Chamber Pressure | Value: 58.5 | Unit: bar."
    """
    if not structured or not structured.get("rows"):
        return ""

    parts = []

    # Prepend section context — critical for disambiguation
    # "Thrust: 799 kN" alone is useless; with context it is precise
    if section_path:
        context = " > ".join(section_path)
        parts.append(f"Section: {context}.")

    headers = structured.get("headers", [])

    for row in structured["rows"]:
        cells = []
        for header in headers:
            value = row.get(header, "").strip()
            if value:
                cells.append(f"{header}: {value}")
        if cells:
            parts.append(" | ".join(cells) + ".")

    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# PROCESSING FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _enrich_table_chunk(chunk: dict) -> dict:
    """
    Takes a single chunk dict of type=="table" and adds the three
    representations. Returns the enriched chunk dict.

    Never raises — if parsing fails, fills fields with safe defaults
    so the pipeline can continue.
    """
    raw_markdown = chunk.get("content", "")
    section_path = chunk.get("section_path", [])

    # rep_3 — always set first (original, never changes)
    chunk["original_markdown"] = raw_markdown

    try:
        structured = chunk.get("structured_json") or _parse_markdown_table(raw_markdown)

        if structured and structured.get("headers"):
            # rep_1 — structured JSON for Neo4j
            chunk["structured_json"] = structured

            # rep_2 — linearized text for Qdrant embedding
            chunk["linearized_text"] = _build_linearized_text(
                structured, section_path
            )

        else:
            # Table could not be parsed — use safe defaults
            # This happens with malformed/complex tables from Docling
            chunk["structured_json"] = {"headers": [], "rows": []}
            chunk["linearized_text"] = raw_markdown  # embed raw as fallback

    except Exception as e:
        # Absolute worst case — never crash the pipeline
        print(f"[TRIPLE_REP] ⚠ Table parse failed for chunk "
              f"{chunk.get('chunk_id', '?')}: {e}")
        chunk["structured_json"] = {"headers": [], "rows": []}
        chunk["linearized_text"] = raw_markdown

    return chunk


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — called by pipeline_controller.py
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def run_triple_rep(
    book_id:           str,
    json_path:         str,
    progress_callback = None,
) -> str:
    """
    Main entry point called by pipeline_controller.py

    Args:
        book_id   : e.g. "PSLV-C50"
        json_path : absolute path to {book_id}_ready.json (from run_chunking)
        progress_callback : optional fn(percent, stage, message, extra=None)

    Returns:
        str — same json_path (file is enriched in-place)

    Raises:
        FileNotFoundError if json_path does not exist
        RuntimeError if JSON is empty or malformed
    """
    input_path = Path(json_path)

    if not input_path.exists():
        raise FileNotFoundError(
            f"[TRIPLE_REP] Checkpoint file not found: {json_path}\n"
            f"             Has chunk.py completed successfully?"
        )

    if progress_callback:
        progress_callback(
            percent=56,
            stage="Table Processing",
            message=f"Loading chunks for {book_id}...",
        )

    print(f"\n[TRIPLE_REP] Reading: {input_path.name}")

    with open(input_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    if not chunks:
        raise RuntimeError(
            f"[TRIPLE_REP] No chunks found in {json_path}. "
            "Check that chunk.py produced valid output."
        )

    # ── Separate table and text chunks ────────────────────────────────────────
    table_chunks = [c for c in chunks if c.get("type") == "table"]
    text_chunks  = [c for c in chunks if c.get("type") == "text"]

    print(f"[TRIPLE_REP] Found {len(table_chunks)} table chunks to process")
    print(f"[TRIPLE_REP] Found {len(text_chunks)} text chunks (pass-through)")

    if progress_callback:
        progress_callback(
            percent=57,
            stage="Table Processing",
            message=(
                f"Building triple representations for "
                f"{len(table_chunks)} tables..."
            ),
        )

    # ── Enrich table chunks ───────────────────────────────────────────────────
    success_count = 0
    fallback_count = 0

    enriched_chunks = []
    for chunk in chunks:
        if chunk.get("type") == "table":
            enriched = _enrich_table_chunk(chunk)

            # Track quality
            if enriched.get("structured_json", {}).get("headers"):
                success_count += 1
            else:
                fallback_count += 1

            enriched_chunks.append(enriched)
        else:
            # Text chunks pass through unchanged
            enriched_chunks.append(chunk)

    # ── Write enriched data back to same file ─────────────────────────────────
    print(f"[TRIPLE_REP] ✅ {success_count} tables parsed successfully")
    if fallback_count:
        print(f"[TRIPLE_REP] ⚠  {fallback_count} tables used raw fallback "
              f"(malformed markdown — still embeddable)")

    if progress_callback:
        progress_callback(
            percent=58,
            stage="Table Processing",
            message=(
                f"{success_count} tables structured, "
                f"{fallback_count} used raw fallback. "
                f"Saving checkpoint..."
            ),
            extra={
                "tables_structured": success_count,
                "tables_fallback":   fallback_count,
            },
        )

    with open(input_path, "w", encoding="utf-8") as f:
        json.dump(enriched_chunks, f, indent=2, ensure_ascii=False)

    print(f"[TRIPLE_REP] 💾 Checkpoint updated → {input_path.name}")

    if progress_callback:
        progress_callback(
            percent=59,
            stage="Table Processing",
            message="Triple representation complete. Ready for propositions.",
        )

    return str(input_path)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE — run directly for debugging
# python processing/triple_rep.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    BASE_DIR = Path(__file__).resolve().parent.parent

    def _print_callback(percent, stage, message, extra=None):
        print(f"  [{percent}%] {stage}: {message}")

    checkpoint_dir = BASE_DIR / "data" / "checkpoints"
    json_files = sorted(checkpoint_dir.glob("*_ready.json"))

    if not json_files:
        print(f"[TRIPLE_REP] No *_ready.json files in {checkpoint_dir}")
        print("             Run chunk.py first.")
        sys.exit(1)

    for jf in json_files:
        book_id = jf.stem.replace("_ready", "")
        print(f"\n{'='*60}")
        print(f"  Processing: {book_id}")
        print(f"{'='*60}")

        try:
            out = run_triple_rep(book_id, str(jf), _print_callback)

            # Quick inspection
            with open(out, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            table_chunks = [c for c in data if c.get("type") == "table"]
            print(f"\n  Inspecting first 2 table chunks:")

            shown = 0
            for c in data:
                if c.get("type") != "table":
                    continue
                print(f"\n  ── Table chunk: {' > '.join(c['section_path'])}")

                sj = c.get("structured_json", {})
                print(f"     Headers : {sj.get('headers', [])}")
                print(f"     Rows    : {len(sj.get('rows', []))}")

                lt = c.get("linearized_text", "")
                preview = lt[:200] + "..." if len(lt) > 200 else lt
                print(f"     Linear  : {preview}")

                shown += 1
                if shown >= 2:
                    break

        except Exception as e:
            print(f"  ❌ Error: {e}")
            sys.exit(1)
