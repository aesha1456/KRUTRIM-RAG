"""
processing/chunk.py
--------------------
STEP 1 of the new processing pipeline.

Replaces the old processing/clean.py entirely.

Responsibilities:
  - Reads the raw assembled markdown from data/processed/{book_id}.md
  - Splits the document by markdown headers (#, ##, ###)
  - Each header-delimited block becomes one "chunk"
  - Detects and isolates pipe-table blocks as type="table"
  - Remaining text in the same block becomes type="text"
  - Attaches full metadata to every chunk:
      chunk_id      : unique UUID (stable across runs via uuid5)
      section_path  : full breadcrumb  e.g. ["3. Propulsion", "3.2 Vikas Engine"]
      level         : header depth (1 = #, 2 = ##, 3 = ###)
      page_range    : {start, end} — FIXED: tracked as we walk lines, not
                      extracted from markers found inside chunk content
      type          : "text" or "table"
      content       : raw markdown text of this chunk
      parent_id     : chunk_id of the parent section (None for top-level)
      book_id       : passed in from pipeline_controller
      book_title    : human-readable title derived from book_id
  - Writes output to data/checkpoints/{book_id}_ready.json

PAGE NUMBER FIX:
  Old approach (broken):
    _extract_page_numbers(content_raw) scanned for PAGE markers INSIDE
    the chunk's content buffer. A section starting on page 3 might have
    a "## --- PAGE 4 ---" marker inside its content (because the section
    spans two pages). The old code returned [4] and set start=4, which
    was off by one — the section actually started on page 3.

  New approach (correct):
    We track current_page as a running counter while walking lines.
    When we see a "## --- PAGE N ---" marker → current_page = N.
    When we see a section header → we record current_page as that
    section's start_page BEFORE clearing the content buffer.
    The end_page is the last PAGE marker found inside the content.
    Result: page_range.start always matches where the heading appears
    in the PDF. This matches what build_metadata.py produces from the
    .md file directly.

Called by pipeline_controller.py:
    from processing.chunk import run_chunking
    json_path = run_chunking(book_id, str(BASE_DIR), callback)
"""

import re
from parta.logger import time_it, async_time_it
import json
import uuid
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Compiled regex for Docling page markers — used in multiple places
_PAGE_MARKER_RE = re.compile(r"##\s*---\s*PAGE\s+(\d+)\s*---")


@time_it
def _make_chunk_id(book_id: str, section_path: list, index: int) -> str:
    """
    Stable UUID derived from book + section path + index.
    Running the same document twice produces the same chunk IDs.
    This matters for Qdrant upsert idempotency.
    """
    key = f"{book_id}::{'>>'.join(section_path)}::{index}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))


@time_it
def _last_page_in_content(content_raw: str) -> Optional[int]:
    """
    Finds the LAST page marker in a chunk's raw content.
    Used to determine page_range.end for sections that span multiple pages.
    Returns None if no page markers found in this content block.
    """
    found = [int(m) for m in _PAGE_MARKER_RE.findall(content_raw)]
    return max(found) if found else None


@time_it
def _clean_content(text: str) -> str:
    """
    Light cleaning of chunk content.
    Removes the ## --- PAGE N --- markers from the content
    (they served their purpose for page tracking).
    Normalises excessive blank lines.
    Does NOT remove technical content.
    """
    text = _PAGE_MARKER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\.{3,}", " ", text)
    return text.strip()


@time_it
def _is_table_line(line: str) -> bool:
    """
    Returns True if a line is part of a markdown pipe table.
    """
    stripped = line.strip()
    return stripped.startswith("|") or stripped.count("|") >= 2


@time_it
def _split_text_and_tables(content: str) -> list:
    """
    Given the raw text content of one header-delimited section,
    splits it into alternating text and table sub-blocks.

    Returns a list of dicts:
        {"type": "text",  "content": "..."}
        {"type": "table", "content": "| col | col |\n|---|---|\n..."}

    Minimum table size: 2 lines (header + separator).
    Single-line pipe patterns are treated as inline text.
    """
    lines = content.split("\n")
    blocks = []
    current_type = None
    current_lines = []

    for line in lines:
        line_is_table = _is_table_line(line)

        if line_is_table:
            if current_type == "text" and current_lines:
                text_content = "\n".join(current_lines).strip()
                if text_content:
                    blocks.append({"type": "text", "content": text_content})
                current_lines = []
            current_type = "table"
            current_lines.append(line)
        else:
            if current_type == "table" and current_lines:
                table_content = "\n".join(current_lines).strip()
                if table_content and len(current_lines) >= 2:
                    blocks.append({"type": "table", "content": table_content})
                else:
                    blocks.append({"type": "text", "content": table_content})
                current_lines = []
            current_type = "text"
            current_lines.append(line)

    if current_lines:
        content_str = "\n".join(current_lines).strip()
        if content_str:
            if current_type == "table" and len(current_lines) >= 2:
                blocks.append({"type": "table", "content": content_str})
            else:
                blocks.append({"type": "text", "content": content_str})

    return blocks


@time_it
def _parse_header_line(line: str) -> Optional[tuple]:
    """
    Checks if a line is a markdown section header.
    Returns (level, title) or None.

    Explicitly IGNORES Docling page markers:
        ## --- PAGE N ---  → not a section header
    """
    match = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
    if not match:
        return None

    hashes = match.group(1)
    title  = match.group(2).strip()
    level  = len(hashes)

    # Skip Docling page markers
    if re.match(r"---\s*PAGE\s+\d+", title):
        return None

    return (level, title)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PARSER — page tracking fix is here
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def _parse_markdown_into_chunks(
    markdown:  str,
    book_id:   str,
    book_title: str,
) -> list:
    """
    Core parsing function. Walks the markdown line by line.

    PAGE TRACKING FIX:
      We maintain current_page as a running integer.
      Every time we see ## --- PAGE N --- we update current_page = N.
      Every time we see a section header, we record current_page as
      that section's start_page BEFORE clearing the content buffer.

      This means page_range.start = the page the section heading
      appears on in the PDF, not the first page marker found inside
      the section's content (which was the old broken behaviour).

    Section path (breadcrumb) is maintained as a stack:
        Level 1 → path = ["3. Propulsion Systems"]
        Level 2 → path = ["3. Propulsion Systems", "3.2 Vikas Engine"]
        Level 3 → path = ["3. Propulsion Systems", ..., "3.2.1 Fuel"]
    """
    lines = markdown.split("\n")

    # ── Phase 1: Walk lines, collect raw sections with correct start_page ─────
    raw_sections = []

    current_level      = 0
    current_title      = "__preamble__"
    content_buffer     = []
    current_page       = 1   # running page counter — updated on every PAGE marker
    section_start_page = 1   # the page where the current section heading appeared

    for line in lines:

        # ── Check for Docling page marker first ───────────────────────────────
        pm = _PAGE_MARKER_RE.match(line.strip())
        if pm:
            current_page = int(pm.group(1))
            # Add to content buffer so _last_page_in_content can find end_page
            content_buffer.append(line)
            continue

        # ── Check for section header ──────────────────────────────────────────
        parsed = _parse_header_line(line)
        if parsed:
            # Save the section we were collecting
            # Its end_page is the last PAGE marker inside its content
            raw_sections.append({
                "level":         current_level,
                "title":         current_title,
                "content_lines": content_buffer,
                "start_page":    section_start_page,
            })
            # Start the new section
            current_level      = parsed[0]
            current_title      = parsed[1]
            content_buffer     = []
            # KEY FIX: record current_page NOW — this is the page the
            # section heading appears on, before any content is added
            section_start_page = current_page
        else:
            content_buffer.append(line)

    # Flush the last section
    raw_sections.append({
        "level":         current_level,
        "title":         current_title,
        "content_lines": content_buffer,
        "start_page":    section_start_page,
    })

    # ── Phase 2: Build chunks from raw sections ───────────────────────────────
    chunks      = []
    chunk_index = 0
    path_stack      = []
    parent_id_stack = []

    for raw in raw_sections:
        level       = raw["level"]
        title       = raw["title"]
        start_page  = raw["start_page"]
        content_raw = "\n".join(raw["content_lines"])
        content     = _clean_content(content_raw)

        # Compute page_range
        # start: the page the section heading was on (tracked above — correct)
        # end:   the last PAGE marker found inside this section's content
        last_page = _last_page_in_content(content_raw)
        page_range = {
            "start": start_page,
            "end":   last_page if last_page is not None else start_page,
        }

        # Maintain breadcrumb stack
        if level == 0:
            section_path = ["[Preamble]"]
            parent_id    = None
        else:
            path_stack        = path_stack[:level - 1]
            parent_id_stack   = parent_id_stack[:level - 1]
            path_stack.append(title)
            section_path = list(path_stack)
            parent_id    = parent_id_stack[-1] if parent_id_stack else None

        # Skip entirely empty sections but still register in parent stack
        if not content and level > 0:
            header_chunk_id = _make_chunk_id(book_id, section_path, chunk_index)
            if level <= len(parent_id_stack) + 1:
                parent_id_stack.append(header_chunk_id)
            chunk_index += 1
            continue

        # Split content into text and table sub-blocks
        sub_blocks = _split_text_and_tables(content) if content else []

        # Register header chunk id for parent tracking
        if level > 0:
            header_chunk_id = _make_chunk_id(book_id, section_path, chunk_index)
            chunk_index += 1
            if level <= len(parent_id_stack) + 1:
                parent_id_stack = parent_id_stack[:level - 1]
                parent_id_stack.append(header_chunk_id)
        else:
            header_chunk_id = _make_chunk_id(book_id, section_path, chunk_index)
            chunk_index += 1

        # Emit sub-blocks as individual chunks
        for sub_idx, sub in enumerate(sub_blocks):
            sub_content = sub["content"].strip()
            if not sub_content or len(sub_content) < 20:
                continue

            cid = _make_chunk_id(
                book_id,
                section_path + [sub["type"], str(sub_idx)],
                chunk_index,
            )
            chunk_index += 1

            chunks.append({
                "chunk_id":     cid,
                "book_id":      book_id,
                "book_title":   book_title,
                "section_path": section_path,
                "level":        level,
                "page_range":   page_range,
                "type":         sub["type"],
                "content":      sub_content,
                "parent_id":    header_chunk_id if level > 0 else None,
            })

    return chunks


@time_it
def _parse_structured_into_chunks(document: dict, book_id: str, book_title: str, markdown: str = "") -> list:
    """Build chunks from OpenDataLoader's normalized semantic elements."""
    elements = document.get("elements", [])
    if not isinstance(elements, list) or not elements:
        return []
    has_real_markdown_heading = any(
        _parse_header_line(line) is not None
        and not re.match(r"^#\s+Text Extraction:\s*", line.strip(), re.IGNORECASE)
        for line in markdown.splitlines()
    )
    engines = document.get("engines") or document.get("engine") or []
    if isinstance(engines, str):
        engines = [engines]
    is_docling_ocr = any(str(engine).lower() == "docling-ocr" for engine in engines)
    has_native_ocr_metadata = any(
        e.get("native_ref")
        or isinstance(e.get("bounding_box"), dict)
        for e in elements
    )
    if (
        markdown
        and not any(e.get("type") == "heading" for e in elements)
        and has_real_markdown_heading
        and not (is_docling_ocr or has_native_ocr_metadata)
    ):
        # Untagged PDFs may expose every heading as a paragraph in JSON.
        # Keep the Markdown heading hierarchy until the extractor supplies
        # semantic heading elements.
        return []

    chunks = []
    path_stack = []
    parent_ids = []
    buffer = []
    chunk_index = 0

    def flush():
        nonlocal buffer, chunk_index
        if not buffer:
            return
        text = "\n\n".join(e["content"] for e in buffer if e.get("content")).strip()
        if not text or len(text) < 20:
            buffer = []
            return
        pages = [int(e.get("page_number", 1)) for e in buffer if e.get("page_number")]
        section_path = list(path_stack) or ["[Preamble]"]
        cid = _make_chunk_id(book_id, section_path + ["text", str(chunk_index)], chunk_index)
        chunks.append({
            "chunk_id": cid,
            "book_id": book_id,
            "book_title": book_title,
            "section_path": section_path,
            "level": len(path_stack),
            "page_range": {"start": min(pages or [1]), "end": max(pages or [1])},
            "type": "text",
            "content": text,
            "parent_id": parent_ids[-1] if parent_ids else None,
            # Keep the collection schema JSON-native: IDs are strings and
            # OCR boxes remain dictionaries with coord_origin metadata.
            "element_ids": [e.get("element_id") for e in buffer if e.get("element_id") is not None],
            "bounding_boxes": [e.get("bounding_box") for e in buffer if e.get("bounding_box") is not None],
        })
        chunk_index += 1
        buffer = []

    for element in elements:
        kind = str(element.get("type", "paragraph")).lower()
        content = str(element.get("content", "")).strip()
        if not content:
            continue

        if kind == "heading":
            flush()
            level = max(1, int(element.get("heading_level") or 1))
            path_stack = path_stack[: level - 1] + [content]
            parent_ids = parent_ids[: level - 1]
            parent_ids.append(_make_chunk_id(book_id, path_stack + ["section"], level))
            continue

        if kind == "table":
            flush()
            pages = [int(element.get("page_number", 1))]
            section_path = list(path_stack) or ["[Preamble]"]
            cid = _make_chunk_id(book_id, section_path + ["table", str(chunk_index)], chunk_index)
            table_chunk = {
                "chunk_id": cid,
                "book_id": book_id,
                "book_title": book_title,
                "section_path": section_path,
                "level": len(path_stack),
                "page_range": {"start": pages[0], "end": pages[0]},
                "type": "table",
                "content": content,
                "parent_id": parent_ids[-1] if parent_ids else None,
                "element_ids": [element.get("element_id")] if element.get("element_id") is not None else [],
                "bounding_boxes": [element.get("bounding_box")] if element.get("bounding_box") is not None else [],
            }
            if isinstance(element.get("table"), dict):
                table_chunk["structured_json"] = element["table"]
            chunks.append(table_chunk)
            chunk_index += 1
            continue

        buffer.append(element)

    flush()
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — called by pipeline_controller.py
# ─────────────────────────────────────────────────────────────────────────────

@time_it
def run_chunking(
    book_id:           str,
    base_dir:          str,
    progress_callback = None,
) -> str:
    """
    Main entry point called by pipeline_controller.py

    Args:
        book_id   : e.g. "PSLV-C50"
        base_dir  : absolute path to the project root
        progress_callback : optional fn(percent, stage, message, extra=None)

    Returns:
        str — absolute path to the written _ready.json file
    """
    base        = Path(base_dir)
    input_file  = base / "data" / "processed" / f"{book_id}.md"
    output_dir  = base / "data" / "checkpoints"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{book_id}_ready.json"

    if not input_file.exists():
        raise FileNotFoundError(
            f"[CHUNK] Markdown file not found: {input_file}\n"
            f"        Has text extraction completed successfully?"
        )

    if progress_callback:
        progress_callback(51, "Chunking", f"Reading extracted markdown for {book_id}...")

    print(f"\n[CHUNK] Reading: {input_file.name}")

    with open(input_file, "r", encoding="utf-8") as f:
        markdown = f.read()

    if not markdown.strip():
        raise RuntimeError(f"[CHUNK] Markdown file is empty: {input_file}")

    book_title = book_id.replace("_", " ").replace("-", " ").title()

    if progress_callback:
        progress_callback(52, "Chunking", "Splitting document by section headers...")

    structured_file = base / "data" / "processed" / f"{book_id}.json"
    chunks = []
    if structured_file.exists():
        try:
            with open(structured_file, "r", encoding="utf-8") as f:
                document = json.load(f)
            chunks = _parse_structured_into_chunks(document, book_id, book_title, markdown)
            print(f"[CHUNK] Used structured JSON: {len(document.get('elements', []))} elements")
        except Exception as e:
            print(f"[CHUNK] Structured JSON unavailable, falling back to Markdown: {e}")

    if not chunks:
        chunks = _parse_markdown_into_chunks(markdown, book_id, book_title)

    if not chunks:
        raise RuntimeError(
            f"[CHUNK] No chunks produced from {input_file.name}. "
            "Check that Docling produced valid markdown output."
        )

    text_chunks  = [c for c in chunks if c["type"] == "text"]
    table_chunks = [c for c in chunks if c["type"] == "table"]

    print(f"[CHUNK] ✅ {len(chunks)} total chunks produced")
    print(f"         {len(text_chunks)} text chunks")
    print(f"         {len(table_chunks)} table chunks")

    # Quick sanity check: show first 5 chunks with page numbers for verification
    print(f"\n[CHUNK] Page number sample (first 5 chunks):")
    for c in chunks[:5]:
        pr = c["page_range"]
        print(f"         [{c['type'].upper():5}] p{pr['start']}-{pr['end']} | "
              f"{' > '.join(c['section_path'])}")

    if progress_callback:
        progress_callback(
            54, "Chunking",
            f"Produced {len(chunks)} chunks — "
            f"{len(text_chunks)} text, {len(table_chunks)} tables. Saving...",
            extra={
                "total_chunks": len(chunks),
                "text_chunks":  len(text_chunks),
                "table_chunks": len(table_chunks),
            },
        )

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    print(f"[CHUNK] 💾 Checkpoint saved → {output_file.name}")

    if progress_callback:
        progress_callback(55, "Chunking", f"Chunking complete. {len(chunks)} sections ready.")

    return str(output_file)


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE
# python processing/chunk.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    BASE_DIR = Path(__file__).resolve().parent.parent

    def _print_callback(percent, stage, message, extra=None):
        print(f"  [{percent}%] {stage}: {message}")

    processed_dir = BASE_DIR / "data" / "processed"
    md_files = sorted(processed_dir.glob("*.md"))

    if not md_files:
        print(f"[CHUNK] No .md files found in {processed_dir}")
        sys.exit(1)

    for md_file in md_files:
        book_id = md_file.stem
        print(f"\n{'='*60}")
        print(f"  Processing: {book_id}")
        print(f"{'='*60}")
        try:
            out = run_chunking(book_id, str(BASE_DIR), _print_callback)
            print(f"\n  Output → {out}")

            with open(out, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            print(f"\n  First 5 chunks with page numbers:")
            for c in data[:5]:
                pr = c["page_range"]
                print(f"    [{c['type'].upper():5}] pages {pr['start']}-{pr['end']} | "
                      f"{' > '.join(c['section_path'])}")

        except Exception as e:
            print(f"  ❌ Error: {e}")
            sys.exit(1)
