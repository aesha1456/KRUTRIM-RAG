"""
processing/build_metadata.py
-----------------------------
STEP 3c of Phase 1 — runs AFTER triple_rep.py in pipeline_controller.py.

Reads  → data/processed/{book_id}.md              (source of truth for page content)
         data/checkpoints/{book_id}_ready.json     (structured_json for table formatting)
Writes → data/metadata/{book_id}_metadata.json

WHY WE READ FROM THE .md FILE (not from _ready.json):
  chunk.py splits the document by section headers, not by page numbers.
  A single section can span multiple pages, and multiple sections can share
  one page. When build_metadata tried to reconstruct pages from chunks, the
  content was scrambled — chunks from different sections were mixed together
  in the wrong order, and page numbers were off by one.

  The .md file is the ground truth. Docling inserts ## --- PAGE N --- markers
  at every page boundary. Everything between two markers is exactly what appears
  on that page in the PDF, in the exact order it appears. Splitting by these
  markers gives us perfect page content with no mixing and no approximation.

WHY WE STILL USE _ready.json:
  The .md file has tables as raw pipe markdown (| col | val |). This is what
  causes the LLM to say "see the table" instead of including values in its
  answer. triple_rep.py already parsed these tables into structured_json
  {headers, rows} which we can format as clean bullet lists.

  We use _ready.json ONLY as a lookup for structured_json — we match each
  pipe table found in the .md page content to its corresponding table chunk
  by comparing the first header row of the pipe table to the headers[] array
  in structured_json. When a match is found, the pipe table is replaced with
  a formatted bullet list. When no match is found, the pipe table is parsed
  directly from the .md text (always works, slightly less robust for malformed
  tables). Raw pipe markdown is only kept as a last resort.

OUTPUT FORMAT:
  {
    "3": {
      "page_number": 3,
      "sections":    ["4.3 RF Subsystem", "4.3.2 S x C Up-Converter"],
      "full_content": "exact page text with tables formatted as bullet lists"
    },
    "__meta__": {
      "book_id":      "PSLV-C50",
      "total_pages":  312,
      "created_at":   "2025-04-27T..."
    }
  }

Called by pipeline_controller.py after run_triple_rep():
    from processing.build_metadata import run_build_metadata
    metadata_path = run_build_metadata(book_id, ready_path, str(BASE_DIR), callback)
"""

from __future__ import annotations
from parta.logger import time_it, async_time_it

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# PAGE MARKER SPLITTING
# ─────────────────────────────────────────────────────────────────────────────

# Matches Docling page markers: ## --- PAGE 42 ---
_PAGE_MARKER_RE = re.compile(r"##\s*---\s*PAGE\s+(\d+)\s*---")


@time_it
def _split_md_by_pages(markdown: str) -> dict[int, str]:
    """
    Splits the .md file by Docling page markers.

    Everything between ## --- PAGE N --- and ## --- PAGE N+1 ---
    is the raw content of page N, in exactly the order it appears in the PDF.

    Content before the first PAGE marker (if any) is assigned to page 0
    (cover / preamble text). This is usually empty or just a title.

    Returns: {page_number: raw_page_text}
    """
    pages: dict[int, str] = {}
    current_page = 0
    current_lines: list[str] = []

    for line in markdown.split("\n"):
        m = _PAGE_MARKER_RE.match(line.strip())
        if m:
            # Save what we accumulated for the previous page
            text = "\n".join(current_lines).strip()
            if text:
                pages[current_page] = text
            # Start the new page
            current_page = int(m.group(1))
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last page
    text = "\n".join(current_lines).strip()
    if text:
        pages[current_page] = text

    return pages


# ─────────────────────────────────────────────────────────────────────────────
# TABLE DETECTION IN RAW PAGE TEXT
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _is_table_line(line: str) -> bool:
    """Returns True if a line is part of a pipe table."""
    stripped = line.strip()
    return stripped.startswith("|") or stripped.count("|") >= 2


@time_it
def _is_separator_row(line: str) -> bool:
    """Returns True if a line is a table separator (|---|---|)."""
    stripped = line.strip()
    if not (stripped.startswith("|") or stripped.startswith("-")):
        return False
    return bool(re.match(r"^[\|\-\:\s]+$", stripped))


@time_it
def _extract_pipe_tables(page_text: str) -> list[dict]:
    """
    Finds all pipe table blocks in a page's raw text.

    Returns list of:
    {
      "lines":      [str, ...],   the raw lines of the table
      "start_pos":  int,          character offset in page_text where table starts
      "end_pos":    int,          character offset where table ends
      "raw":        str,          the table as a single string
      "first_headers": [str, ...] parsed header row (for matching to _ready.json)
    }
    """
    lines       = page_text.split("\n")
    tables      = []
    in_table    = False
    table_lines = []
    table_start_line = 0

    for i, line in enumerate(lines):
        if _is_table_line(line):
            if not in_table:
                in_table         = True
                table_lines      = [line]
                table_start_line = i
            else:
                table_lines.append(line)
        else:
            if in_table:
                # End of a table block
                if len(table_lines) >= 2:  # need at least header + separator
                    raw = "\n".join(table_lines)
                    headers = _parse_pipe_header(table_lines[0])
                    tables.append({
                        "lines":         table_lines,
                        "start_line":    table_start_line,
                        "end_line":      i - 1,
                        "raw":           raw,
                        "first_headers": headers,
                    })
                in_table    = False
                table_lines = []

    # Flush last table
    if in_table and len(table_lines) >= 2:
        raw     = "\n".join(table_lines)
        headers = _parse_pipe_header(table_lines[0])
        tables.append({
            "lines":         table_lines,
            "start_line":    table_start_line,
            "end_line":      len(lines) - 1,
            "raw":           raw,
            "first_headers": headers,
        })

    return tables


@time_it
def _parse_pipe_header(header_line: str) -> list[str]:
    """
    Parses the header row of a pipe table into a list of column names.
    "| Parameter | Value | Unit |" → ["Parameter", "Value", "Unit"]
    """
    line = header_line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|") if cell.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# TABLE FORMATTING
# ─────────────────────────────────────────────────────────────────────────────

_PARAM_HEADERS = {
    "parameter", "param", "item", "name", "description",
    "property", "characteristic", "spec", "specification", "attribute",
}
_VALUE_HEADERS = {
    "value", "values", "data", "result", "reading",
    "measurement", "quantity", "specification", "spec",
}
_UNIT_HEADERS = {"unit", "units", "uom", "measure"}


@time_it
def _format_from_structured_json(structured: dict, section_label: str) -> str:
    """
    Formats a table's structured_json into a clean bullet list.
    Called when we can match a pipe table to a _ready.json table chunk.

    Output:
      [TABLE — Section Name]:
        - Parameter: Value Unit
        - Parameter: Value Unit
    """
    headers = structured.get("headers") or []
    rows    = structured.get("rows") or []

    if not rows:
        return ""

    lowered_h = [h.lower().strip() for h in headers]

    param_col = next(
        (headers[i] for i, h in enumerate(lowered_h) if h in _PARAM_HEADERS), None
    )
    value_col = next(
        (headers[i] for i, h in enumerate(lowered_h) if h in _VALUE_HEADERS), None
    )
    unit_col = next(
        (headers[i] for i, h in enumerate(lowered_h) if h in _UNIT_HEADERS), None
    )

    use_spec_mode = param_col is not None and value_col is not None
    lines = [f"[TABLE — {section_label}]:"] if section_label else ["[TABLE]:"]

    for row in rows:
        low = {(k or "").lower().strip(): str(v or "").strip() for k, v in row.items()}

        if use_spec_mode:
            param = str(row.get(param_col, "")).strip()
            value = str(row.get(value_col, "")).strip()
            unit  = str(row.get(unit_col,  "")).strip() if unit_col else ""
            if param and value:
                unit_str = f" {unit}" if unit else ""
                lines.append(f"  - {param}: {value}{unit_str}")
            else:
                parts = [f"{h}: {str(row.get(h,''))}" for h in headers
                         if str(row.get(h, "")).strip()]
                if parts:
                    lines.append("  - " + " | ".join(parts))
        else:
            parts = [f"{h}: {str(row.get(h,''))}" for h in headers
                     if str(row.get(h, "")).strip()]
            if parts:
                lines.append("  - " + " | ".join(parts))

    return "\n".join(lines) if len(lines) > 1 else ""


@time_it
def _format_from_pipe_direct(table_lines: list[str], section_label: str) -> str:
    """
    Formats a pipe table directly from its raw lines, without structured_json.
    Used as fallback when no matching _ready.json chunk is found.

    Produces the same bullet list format as _format_from_structured_json.
    """
    data_rows = []
    headers   = []

    for i, line in enumerate(table_lines):
        if _is_separator_row(line):
            continue
        cells = _parse_pipe_header(line)
        if not cells:
            continue
        if not headers:
            headers = cells
        else:
            data_rows.append(cells)

    if not headers or not data_rows:
        return ""

    lowered_h = [h.lower().strip() for h in headers]
    param_idx = next(
        (i for i, h in enumerate(lowered_h) if h in _PARAM_HEADERS), None
    )
    value_idx = next(
        (i for i, h in enumerate(lowered_h) if h in _VALUE_HEADERS), None
    )
    unit_idx = next(
        (i for i, h in enumerate(lowered_h) if h in _UNIT_HEADERS), None
    )

    use_spec = param_idx is not None and value_idx is not None
    lines = [f"[TABLE — {section_label}]:"] if section_label else ["[TABLE]:"]

    for row_cells in data_rows:
        # Pad row if shorter than headers
        padded = row_cells + [""] * (len(headers) - len(row_cells))

        if use_spec:
            param = padded[param_idx].strip() if param_idx < len(padded) else ""
            value = padded[value_idx].strip() if value_idx < len(padded) else ""
            unit  = padded[unit_idx].strip()  if unit_idx is not None and unit_idx < len(padded) else ""
            if param and value:
                unit_str = f" {unit}" if unit else ""
                lines.append(f"  - {param}: {value}{unit_str}")
            else:
                parts = [f"{h}: {v}" for h, v in zip(headers, padded) if v.strip()]
                if parts:
                    lines.append("  - " + " | ".join(parts))
        else:
            parts = [f"{h}: {v}" for h, v in zip(headers, padded) if v.strip()]
            if parts:
                lines.append("  - " + " | ".join(parts))

    return "\n".join(lines) if len(lines) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# TABLE LOOKUP FROM _ready.json
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _build_table_lookup(chunks: list[dict]) -> dict[int, list[dict]]:
    """
    Builds a page-keyed lookup of table chunks that have structured_json.

    Returns: {page_number: [table_chunk, table_chunk, ...]}

    We index on both start and end page so we catch multi-page tables.
    """
    lookup: dict[int, list[dict]] = defaultdict(list)

    for chunk in chunks:
        chunk_type = chunk.get("type") or chunk.get("chunk_type", "")
        if chunk_type != "table":
            continue

        structured = chunk.get("structured_json")
        if not structured or not structured.get("headers"):
            continue  # no usable structured data

        page_range = chunk.get("page_range", {})
        if isinstance(page_range, dict):
            start = int(page_range.get("start", 0))
            end   = int(page_range.get("end", start))
        elif isinstance(page_range, list) and page_range:
            start = int(page_range[0])
            end   = int(page_range[1]) if len(page_range) > 1 else start
        else:
            continue

        for pg in range(start, end + 1):
            lookup[pg].append(chunk)

    return lookup


@time_it
def _headers_match(pipe_headers: list[str], structured_headers: list[str]) -> bool:
    """
    Returns True if the pipe table's header row matches the structured_json headers.
    Comparison is case-insensitive and whitespace-normalised.
    Matches if at least 60% of headers overlap (handles partial/malformed tables).
    """
    if not pipe_headers or not structured_headers:
        return False

    norm_pipe = [h.lower().strip() for h in pipe_headers]
    norm_struct = [h.lower().strip() for h in structured_headers]

    matches = sum(1 for h in norm_pipe if h in norm_struct)
    threshold = max(1, int(min(len(norm_pipe), len(norm_struct)) * 0.6))
    return matches >= threshold


@time_it
def _find_best_table_chunk(
    pipe_headers: list[str],
    candidates:   list[dict],
) -> Optional[dict]:
    """
    Given a list of table chunks on this page, find the one whose
    structured_json.headers best match the pipe table's header row.
    Returns None if no match found.
    """
    for chunk in candidates:
        structured_headers = chunk.get("structured_json", {}).get("headers", [])
        if _headers_match(pipe_headers, structured_headers):
            return chunk
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION HEADING EXTRACTION FROM PAGE TEXT
# ─────────────────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")


@time_it
def _extract_sections_from_page(page_text: str) -> list[str]:
    """
    Finds all markdown section headings in a page's raw text.
    Returns list of heading strings (without the # prefix).
    Skips Docling page markers.
    """
    sections = []
    for line in page_text.split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            title = m.group(1).strip()
            # Skip page markers
            if not re.match(r"---\s*PAGE\s+\d+", title):
                sections.append(title)
    return sections


# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONTENT BUILDER — MAIN LOGIC
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _build_page_entry(
    page_num:      int,
    raw_page_text: str,
    table_lookup:  dict[int, list[dict]],
) -> dict:
    """
    Builds a single page entry for the metadata JSON.

    For each pipe table found in raw_page_text:
      1. Try to match it to a structured_json chunk from _ready.json
         → format as clean bullet list using structured_json rows
      2. If no match → parse pipe table directly from .md lines
         → format as clean bullet list (slightly less robust for malformed tables)
      3. If both fail → keep the raw pipe markdown (never crash)

    Then replaces every pipe table block in the raw text with its
    formatted version. Everything else stays exactly as Docling wrote it.
    """
    sections     = _extract_sections_from_page(raw_page_text)
    pipe_tables  = _extract_pipe_tables(raw_page_text)
    candidates   = table_lookup.get(page_num, [])

    # Build the full_content by replacing pipe tables with formatted versions
    # We work line by line to preserve exact document order
    lines        = raw_page_text.split("\n")
    skip_until   = -1      # line index to skip to (used to skip replaced table lines)
    output_lines: list[str] = []

    # Build a map: start_line → pipe_table entry (for fast lookup)
    table_by_start = {t["start_line"]: t for t in pipe_tables}

    i = 0
    while i < len(lines):
        if i in table_by_start:
            tbl = table_by_start[i]

            # Determine the section context for this table
            # Walk back through output_lines to find the most recent heading
            section_label = ""
            for prev_line in reversed(output_lines):
                m = _HEADING_RE.match(prev_line.strip())
                if m:
                    title = m.group(1).strip()
                    if not re.match(r"---\s*PAGE\s+\d+", title):
                        section_label = title
                        break

            # Try to format using structured_json first
            formatted = ""
            if candidates:
                best_chunk = _find_best_table_chunk(tbl["first_headers"], candidates)
                if best_chunk:
                    formatted = _format_from_structured_json(
                        best_chunk["structured_json"], section_label
                    )

            # Fallback: parse pipe table directly
            if not formatted:
                formatted = _format_from_pipe_direct(tbl["lines"], section_label)

            # Fallback: keep raw pipe markdown
            if not formatted:
                formatted = tbl["raw"]

            output_lines.append(formatted)
            # Skip all lines that were part of this table
            i = tbl["end_line"] + 1
        else:
            output_lines.append(lines[i])
            i += 1

    full_content = "\n".join(output_lines).strip()

    # Clean up the page markers from the final content
    full_content = _PAGE_MARKER_RE.sub("", full_content)
    full_content = re.sub(r"\n{3,}", "\n\n", full_content).strip()

    return {
        "page_number":  page_num,
        "sections":     sections,
        "full_content": full_content,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — called by pipeline_controller.py
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def run_build_metadata(
    book_id:           str,
    ready_path:        str,
    base_dir:          str,
    progress_callback = None,
) -> str:
    """
    Main entry point called by pipeline_controller.py after run_triple_rep().

    MUST run after triple_rep.py — needs structured_json on table chunks.

    Args:
        book_id    : e.g. "PSLV-C50"
        ready_path : path to {book_id}_ready.json (enriched by triple_rep.py)
        base_dir   : project root (BASE_DIR in pipeline_controller)
        progress_callback : optional fn(percent, stage, message, extra=None)

    Returns:
        str — absolute path to written {book_id}_metadata.json
    """
    base      = Path(base_dir)
    md_file   = base / "data" / "processed" / f"{book_id}.md"
    out_dir   = base / "data" / "metadata"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file  = out_dir / f"{book_id}_metadata.json"

    if not md_file.exists():
        raise FileNotFoundError(
            f"[METADATA] .md file not found: {md_file}\n"
            f"           Has text extraction completed successfully?"
        )

    ready_file = Path(ready_path)
    if not ready_file.exists():
        raise FileNotFoundError(
            f"[METADATA] _ready.json not found: {ready_path}\n"
            f"           Run chunk.py and triple_rep.py first."
        )

    if progress_callback:
        progress_callback(58, "Page Metadata",
                          f"Building page-level content index for {book_id}...")

    print(f"\n[METADATA] Reading .md: {md_file.name}")
    print(f"[METADATA] Reading chunks: {ready_file.name}")

    # Load inputs
    markdown = md_file.read_text(encoding="utf-8")
    chunks   = json.loads(ready_file.read_text(encoding="utf-8"))

    if not markdown.strip():
        raise RuntimeError(f"[METADATA] Markdown file is empty: {md_file}")

    # Step 1: Split .md by page markers → exact page content
    page_texts = _split_md_by_pages(markdown)
    print(f"[METADATA] {len(page_texts)} pages found in .md file")

    # Step 2: Build table lookup from _ready.json
    table_lookup  = _build_table_lookup(chunks)
    table_page_ct = len(table_lookup)
    print(f"[METADATA] Table lookup covers {table_page_ct} pages")

    # Step 3: Build one entry per page
    metadata: dict[str, dict] = {}

    for page_num in sorted(page_texts.keys()):
        raw_text = page_texts[page_num]
        if not raw_text.strip():
            continue

        entry = _build_page_entry(page_num, raw_text, table_lookup)
        if entry["full_content"]:
            metadata[str(page_num)] = entry

    total_pages = len(metadata)

    # Step 4: Write metadata block
    metadata["__meta__"] = {
        "book_id":      book_id,
        "total_pages":  total_pages,
        "total_chunks": len(chunks),
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }

    out_file.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[METADATA] DONE: {total_pages} pages indexed -> {out_file.name}")

    if progress_callback:
        progress_callback(59, "Page Metadata",
                          f"Page index built — {total_pages} pages.")

    return str(out_file)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE
# python processing/build_metadata.py <book_id> <base_dir>
# python processing/build_metadata.py PSLV-C50 .
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python build_metadata.py <book_id> <base_dir>")
        print("Example: python build_metadata.py PSLV-C50 .")
        sys.exit(1)

    book_id_arg  = sys.argv[1]
    base_dir_arg = Path(sys.argv[2]).resolve()
    ready_arg    = base_dir_arg / "data" / "checkpoints" / f"{book_id_arg}_ready.json"

    if not ready_arg.exists():
        print(f"❌ _ready.json not found at {ready_arg}")
        sys.exit(1)

    def _cb(percent, stage, message, extra=None):
        print(f"  [{percent}%] {stage}: {message}")

    out = run_build_metadata(book_id_arg, str(ready_arg), str(base_dir_arg), _cb)
    print(f"\n✅ Written → {out}")

    # Inspection
    data  = json.loads(Path(out).read_text(encoding="utf-8"))
    meta  = data.get("__meta__", {})
    pages = {k: v for k, v in data.items() if k != "__meta__"}

    print(f"\nMeta: {meta}")
    print(f"Total pages with content: {len(pages)}")

    # Find a page with a table
    table_pages = [k for k, v in pages.items() if "[TABLE" in v.get("full_content", "")]
    print(f"Pages with formatted tables: {len(table_pages)}")

    if table_pages:
        k = sorted(table_pages, key=int)[0]
        entry = pages[k]
        print(f"\n── Sample TABLE page (page {k}) ──")
        print(f"Sections: {entry['sections']}")
        preview = entry["full_content"][:800]
        print(preview)
        if len(entry["full_content"]) > 800:
            print(f"... ({len(entry['full_content'])} total chars)")

    # Show a text-only page
    text_pages = [k for k in sorted(pages.keys(), key=int) if k not in table_pages]
    if text_pages:
        k = text_pages[len(text_pages) // 2]
        entry = pages[k]
        print(f"\n── Sample TEXT page (page {k}) ──")
        print(f"Sections: {entry['sections']}")
        print(entry["full_content"][:400])
