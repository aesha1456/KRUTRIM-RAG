import json
import time

import pytest

from parta.extraction import extraction_server as server
from parta.pipeline_controller import _write_structured_extraction
from parta.processing.chunk import _parse_structured_into_chunks


def _state(job_count=1):
    state = server._new_book_state(total=job_count, meta={"label": "TEST"})
    for index in range(job_count):
        jid = f"job-{index}"
        state["jobs"][jid] = server._job_row(
            job_id=jid,
            book_id="book",
            job_kind="extraction",
            chunk_idx=index,
        )
        state["queue"].append(jid)
    return state


def _assign(state, worker_id):
    return server._assign_pending_job(
        store={"book": state},
        worker_id=worker_id,
        expected_kind="extraction",
    )


def _fail_current_attempt(state, assignment, error="transient"):
    result = server._submit_result(
        store={"book": state},
        payload={
            "job_id": assignment["job_id"],
            "worker_id": assignment["worker_id"],
            "lease_id": assignment["lease_id"],
            "success": False,
            "error": error,
        },
    )
    state["jobs"][assignment["job_id"]]["retry_at"] = 0
    return result


def test_assignment_returns_lease_id_and_stale_result_is_ignored():
    state = _state()
    assignment = _assign(state, "worker-a")
    job = state["jobs"][assignment["job_id"]]

    stale = server._submit_result(
        store={"book": state},
        payload={
            "job_id": assignment["job_id"],
            "worker_id": "worker-a",
            "lease_id": "old-lease",
            "success": False,
            "error": "late result",
        },
    )

    assert stale["note"] == "stale lease result ignored"
    assert stale["accepted"] is False
    assert stale["terminal"] is False
    assert job["status"] == "PROCESSING"
    assert state["completed"] == 0
    assert state["failed"] == 0


def test_stale_success_is_ignored_while_new_lease_remains_authoritative():
    state = _state()
    assignment = _assign(state, "worker-a")
    job = state["jobs"][assignment["job_id"]]
    job["assigned_to"] = "worker-b"
    job["lease_id"] = "new-lease"

    result = server._submit_result(
        store={"book": state},
        payload={
            "job_id": assignment["job_id"],
            "worker_id": "worker-a",
            "lease_id": assignment["lease_id"],
            "success": True,
            "content": {"markdown": "cached result"},
        },
    )

    assert result["accepted"] is False
    assert result["terminal"] is False
    assert result["note"] == "stale lease result ignored"
    assert job["status"] == "PROCESSING"
    assert job["assigned_to"] == "worker-b"
    assert job["lease_id"] == "new-lease"
    assert state["completed"] == 0


def test_failed_job_is_rotated_but_result_order_remains_chunk_order():
    state = _state(job_count=3)
    first = _assign(state, "worker-a")
    first_job = state["jobs"][first["job_id"]]

    _fail_current_attempt(state, first, error="transient")

    assert state["queue"] == ["job-1", "job-2", "job-0"]
    assert first_job["retry_at"] == 0
    assert state["completed"] == 0
    assert state["failed"] == 0


def test_expired_lease_is_requeued_once_and_can_be_reassigned():
    state = _state()
    assignment = _assign(state, "worker-a")
    job = state["jobs"][assignment["job_id"]]
    job["lease_deadline"] = time.time() - 1

    rescued = server._requeue_expired_jobs({"book": state}, now=time.time())
    assert rescued == 1
    assert job["attempt_count"] == 1
    assert job["status"] == "PENDING"
    assert job["lease_id"] is None

    assert server._requeue_expired_jobs({"book": state}, now=time.time()) == 0
    assert job["attempt_count"] == 1


def test_completion_requires_actual_terminal_statuses():
    state = _state(job_count=2)
    state["completed"] = 2
    server._finish_check({"book": state}, "book")
    assert state["is_finished"] is False

    state["jobs"]["job-0"]["status"] = "COMPLETED"
    state["jobs"]["job-1"]["status"] = "COMPLETED"
    server._finish_check({"book": state}, "book")
    assert state["is_finished"] is True


def test_terminal_job_ignores_duplicate_failure():
    state = _state()
    assignment = _assign(state, "worker-a")
    payload = {
        "job_id": assignment["job_id"],
        "worker_id": assignment["worker_id"],
        "lease_id": assignment["lease_id"],
        "success": True,
        "content": {"markdown": "done"},
    }
    server._submit_result(store={"book": state}, payload=payload)
    server._submit_result(
        store={"book": state},
        payload={**payload, "success": False, "error": "duplicate"},
    )
    assert state["completed"] == 1
    assert state["failed"] == 0
    assert state["jobs"][assignment["job_id"]]["status"] == "COMPLETED"


def test_ocr_job_uses_normal_text_fallback_after_three_failures():
    state = _state()
    job = state["jobs"]["job-0"]
    job["ocr_enabled"] = True

    for attempt in range(3):
        assignment = _assign(state, f"worker-{attempt}")
        assert assignment["fallback_mode"] is False
        _fail_current_attempt(state, assignment, error=f"ocr failure {attempt + 1}")

    assert job["attempt_count"] == 3
    assert job["fallback_attempted"] is True
    assert job["status"] == "PENDING"

    fallback = _assign(state, "worker-fallback")
    assert fallback["attempt_count"] == 3
    assert fallback["fallback_mode"] is True

    server._submit_result(
        store={"book": state},
        payload={
            "job_id": fallback["job_id"],
            "worker_id": fallback["worker_id"],
            "lease_id": fallback["lease_id"],
            "success": True,
            "content": {"markdown": "normal text result"},
        },
    )

    assert job["status"] == "COMPLETED"
    assert state["completed"] == 1
    assert state["failed"] == 0


def test_ocr_job_uses_fallback_after_three_lease_expirations():
    state = _state()
    job = state["jobs"]["job-0"]
    job["ocr_enabled"] = True

    for attempt in range(3):
        assignment = _assign(state, f"lease-worker-{attempt}")
        assert assignment["fallback_mode"] is False
        job["lease_deadline"] = time.time() - 1
        assert server._requeue_expired_jobs({"book": state}, now=time.time()) == 1
        # The reaper schedules the retry; make it immediately eligible for the
        # deterministic next assignment in this unit test.
        job["retry_at"] = 0
        assert job["attempt_count"] == attempt + 1
        assert job["status"] == "PENDING"

    fallback = _assign(state, "lease-fallback-worker")
    assert fallback["attempt_count"] == 3
    assert fallback["fallback_mode"] is True


def test_extraction_temp_directories_are_unique_per_generation(tmp_path):
    first = server._new_extraction_chunk_dir(str(tmp_path), "book")
    second = server._new_extraction_chunk_dir(str(tmp_path), "book")
    assert first != second
    assert first.name.startswith("temp_extract_book_")
    assert second.name.startswith("temp_extract_book_")


def test_cleanup_skips_replacement_generation(tmp_path, monkeypatch):
    old_dir = tmp_path / "temp_extract_book_old"
    new_dir = tmp_path / "temp_extract_book_new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "chunk_0.pdf").write_bytes(b"old")
    (new_dir / "chunk_0.pdf").write_bytes(b"new")

    old_meta = {"book_id": "book", "chunk_dir": str(old_dir)}
    new_meta = {"book_id": "book", "chunk_dir": str(new_dir)}
    replacement = server._new_book_state(total=1, meta=new_meta)
    monkeypatch.setattr(server, "CLEANUP_DELAY_SEC", 0)
    monkeypatch.setitem(server.extractions, "book", replacement)

    server._cleanup_after_delay(old_meta)

    assert not old_dir.exists()
    assert new_dir.exists()
    assert (new_dir / "chunk_0.pdf").read_bytes() == b"new"
    server.extractions.pop("book", None)


def test_cleanup_removes_current_generation_under_lock(tmp_path, monkeypatch):
    chunk_dir = tmp_path / "temp_extract_book_current"
    chunk_dir.mkdir()
    (chunk_dir / "chunk_0.pdf").write_bytes(b"current")
    meta = {"book_id": "book", "chunk_dir": str(chunk_dir)}
    state = server._new_book_state(total=1, meta=meta)
    monkeypatch.setattr(server, "CLEANUP_DELAY_SEC", 0)
    monkeypatch.setitem(server.extractions, "book", state)

    server._cleanup_after_delay(meta)

    assert not chunk_dir.exists()
    server.extractions.pop("book", None)


def test_assembled_ocr_markers_preserve_page_offsets(monkeypatch):
    state = _state(job_count=2)
    state["is_finished"] = True
    state["jobs"]["job-0"]["status"] = "COMPLETED"
    state["jobs"]["job-0"]["result"] = {
        "markdown": "## --- PAGE 1 ---\npage one\n## --- PAGE 2 ---\npage two",
        "engine": "docling-ocr",
    }
    state["jobs"]["job-1"]["status"] = "COMPLETED"
    state["jobs"]["job-1"]["result"] = {
        "markdown": "## --- PAGE 1 ---\npage three",
        "engine": "docling-ocr",
    }
    state["completed"] = 2
    monkeypatch.setitem(server.extractions, "book", state)

    result = server.get_result("book")
    markdown = result["content"]["markdown"]

    assert markdown.count("## --- PAGE ") == 3
    assert "## --- PAGE 1 ---" in markdown
    assert "## --- PAGE 2 ---" in markdown
    assert "## --- PAGE 3 ---" in markdown
    assert "page one" in markdown
    assert "page three" in markdown

    server.extractions.pop("book", None)


def test_structured_ocr_elements_survive_assembled_markdown_title():
    document = {
        "engine": "docling-ocr",
        "elements": [
            {
                "element_id": "0:#/texts/0",
                "native_ref": "#/texts/0",
                "type": "paragraph",
                "content": "OCR paragraph without a semantic heading but enough text.",
                "page_number": 1,
                "bounding_box": {
                    "left": 1.0,
                    "top": 2.0,
                    "right": 30.0,
                    "bottom": 10.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    }

    chunks = _parse_structured_into_chunks(
        document,
        book_id="book",
        book_title="Book",
        markdown=(
            "# Text Extraction: book\n\n"
            "## --- PAGE 1 ---\n"
            "OCR paragraph"
        ),
    )

    assert len(chunks) == 1
    assert chunks[0]["element_ids"] == ["0:#/texts/0"]
    assert chunks[0]["bounding_boxes"][0]["coord_origin"] == "TOPLEFT"


def test_structured_ocr_elements_keep_ids_and_bounding_box_dicts():
    document = {
        "engine": "docling-ocr",
        "elements": [
            {
                "element_id": "0:#/texts/0",
                "native_ref": "#/texts/0",
                "type": "paragraph",
                "content": "OCR text with enough words to become a chunk.",
                "page_number": 2,
                "bounding_box": {
                    "left": 10.0,
                    "top": 20.0,
                    "right": 100.0,
                    "bottom": 40.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    }

    chunks = _parse_structured_into_chunks(document, "book", "Book")

    assert len(chunks) == 1
    assert chunks[0]["element_ids"] == ["0:#/texts/0"]
    assert chunks[0]["bounding_boxes"] == [document["elements"][0]["bounding_box"]]
    assert chunks[0]["page_range"] == {"start": 2, "end": 2}


def test_normal_structured_elements_keep_markdown_heading_hierarchy():
    document = {
        "engine": "opendataloader",
        "elements": [
            {
                "element_id": "0-0",
                "type": "paragraph",
                "content": "Normal extraction paragraph with enough words to chunk.",
                "page_number": 1,
                "bounding_box": [1, 2, 3, 4],
            }
        ],
    }

    chunks = _parse_structured_into_chunks(
        document,
        "book",
        "Book",
        markdown="# Text Extraction: book\n\n# Semantic title\nNormal text",
    )

    assert chunks == []


def test_structured_extraction_writer_preserves_worker_elements(tmp_path):
    path = _write_structured_extraction(
        tmp_path,
        "book",
        {
            "engine": "docling-ocr",
            "markdown": "## --- PAGE 1 ---\nOCR text",
            "elements": [
                {
                    "element_id": "0:#/texts/0",
                    "page_number": 1,
                    "bounding_box": {
                        "left": 1,
                        "top": 2,
                        "right": 3,
                        "bottom": 4,
                        "coord_origin": "TOPLEFT",
                    },
                }
            ],
        },
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["book_id"] == "book"
    assert saved["engine"] == "docling-ocr"
    assert saved["elements"][0]["element_id"] == "0:#/texts/0"
    assert saved["elements"][0]["bounding_box"]["coord_origin"] == "TOPLEFT"


def test_ocr_job_fails_after_fallback_also_fails():
    state = _state()
    job = state["jobs"]["job-0"]
    job["ocr_enabled"] = True

    for attempt in range(3):
        assignment = _assign(state, f"worker-{attempt}")
        _fail_current_attempt(state, assignment)

    fallback = _assign(state, "worker-fallback")
    assert fallback["fallback_mode"] is True
    _submit_result = server._submit_result(
        store={"book": state},
        payload={
            "job_id": fallback["job_id"],
            "worker_id": fallback["worker_id"],
            "lease_id": fallback["lease_id"],
            "success": False,
            "error": "normal OpenDataLoader fallback failure",
        },
    )

    assert _submit_result["status"] == "ok"
    assert job["status"] == "FAILED"
    assert job["attempt_count"] == 4
    assert state["failed"] == 1
