"""OpenDataLoader hybrid PDF extraction worker."""

import sys
import os
import atexit
import json
import logging
import queue
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
import random
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workers.logger import log_process, setup_worker_logger
from workers.config import (
    ERROR_SLEEP_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    OPENDATALOADER_HYBRID_URL,
    RESULT_CACHE_DIR,
    SERVER_URL,
    WAIT_SLEEP_SECONDS,
)
from workers.base_worker import (
    create_session,
    LeaseHeartbeat,
    ResultCache,
    submit_with_retry,
    safe_unlink,
    detect_device,
)

# Import after base_worker has installed the local-cafile SSL workaround.
# opendataloader_pdf imports Docling/Hugging Face modules, some of which create
# an SSL context during import on Windows.
import requests
import opendataloader_pdf

BASE_DIR = Path(__file__).resolve().parent.parent
# 8 hex chars - the ID also names this worker's private cache dirs, so a
# longer ID makes accidental folder collisions between workers unlikely.
WORKER_ID = f"text-{uuid.uuid4().hex[:8]}"

logger = setup_worker_logger("text", WORKER_ID)

HYBRID_URL = OPENDATALOADER_HYBRID_URL

WAIT_SLEEP = WAIT_SLEEP_SECONDS
ERROR_SLEEP = ERROR_SLEEP_SECONDS


_delete_queue: "queue.Queue[Path]" = queue.Queue()


def _reaper_loop():
    while True:
        path = _delete_queue.get()
        safe_unlink(path)


threading.Thread(target=_reaper_loop, daemon=True, name="tmp-reaper").start()


def _queue_delete(path: Path):
    _delete_queue.put(path)


# --- NAS -> local model sync ------------------------------------------------
# SMB/NAS file-lock contention causes WinError 32 and transformers errors
# when multiple machines read the same model files over the network.
# Each worker gets its own local copy once, then never touches the NAS again.
NAS_DOCLING_DIR = BASE_DIR / "parta" / "portable" / "docling"
NAS_JAVA_DIR = BASE_DIR / "parta" / "portable" / "java"


def _java_on_path():
    """ensure the subprocess env has java.exe on PATH (OpenDataLoader shells out
    to the `java` binary). Prefer the locally-synced portable Java (NAS copy
    pulled once, like the docling models) so workers never read Java over the
    NAS/SMB share; fall back to RAG_JAVA_HOME / conda Library/bin."""
    local_bin = str(LOCAL_JAVA_DIR / "bin")
    path_entries = {p for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    if (Path(local_bin) / "java.exe").exists() and local_bin not in path_entries:
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
        logger.info("Using locally-synced portable Java: %s", local_bin)
        return
    if shutil.which("java"):
        return
    javabin = os.environ.get("RAG_JAVA_HOME") or str(Path(sys.prefix) / "Library" / "bin")
    if (Path(javabin) / "java.exe").exists() and "PATH" in os.environ:
        os.environ["PATH"] = javabin + os.pathsep + os.environ["PATH"]
        logger.info("Prepended Java bin to PATH: %s", javabin)


def _cache_base(name: str) -> Path:
    """Root of a per-worker private cache namespace on this machine."""
    return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / name


_win_kernel32 = None  # cached ctypes.WinDLL("kernel32", use_last_error=True)


def _pid_alive_windows(pid: int) -> bool:
    """Windows-only liveness probe (kept in its own function so static
    analyzers on ANY platform still analyze the implementation)."""
    global _win_kernel32
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # Win7+; existence info only
    ERROR_ACCESS_DENIED = 5
    if _win_kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE  # 64-bit handles
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _win_kernel32 = kernel32
    handle = _win_kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        _win_kernel32.CloseHandle(handle)
        return True
    # OpenProcess failed: a dead PID gives WinError 87 (invalid parameter) ->
    # not alive. Access denied (WinError 5) means it exists but is protected
    # (e.g. owned by another user) -> treat as alive.
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def _pid_alive_posix(pid: int) -> bool:
    """POSIX liveness probe (os.kill(pid, 0) is safe here - it only signals)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned elsewhere / unknown -> treat as alive
    return True


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check.

    IMPORTANT: os.kill(pid, 0) is NOT a liveness probe on Windows. CPython's
    os.kill maps to OpenProcess + TerminateProcess there, so a dead PID raises
    OSError [WinError 87] 'The parameter is incorrect' and a LIVE PID is
    actually TERMINATED (exit code 0). Two workers on one PC would therefore
    kill each other through the owner-PID sweep in _prepare_local_dir. Use
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) instead - it only asks
    whether the process exists, it never signals it.
    """
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _prepare_local_dir(nas_source: Path, current: Path):
    """Reclaim space from dead workers BEFORE syncing our own copy.

    A crashed/killed worker never runs its atexit cleanup, so its private
    folder would otherwise linger (and pile up with each restart). Every
    worker writes a `.worker_owner` file (PID + WORKER_ID) before copying, so
    we can tell who owns a folder:

      * owner PID still alive -> live worker, leave it alone;
      * owner PID dead + `.sync_signature` matches the NAS source -> the copy is
        complete: adopt it for ourselves with a same-volume rename (instant,
        no NAS re-copy) - this stops folders being re-created from scratch on
        every worker start;
      * owner PID dead + signature missing/mismatched -> partial/corrupt copy,
        delete it.

    Live workers' folders and our own are never touched.
    """
    base = current.parent
    if not base.exists():
        return
    nas_sig = _dir_signature(nas_source)
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child == current:
            continue
        try:
            owner = (child / ".worker_owner").read_text(encoding="utf-8").strip()
        except Exception:
            owner = None
        if not owner:
            continue
        try:
            pid, owner_id = (owner.split() + [""])[:2]
            pid = int(pid)
        except (ValueError, IndexError):
            continue
        if _pid_alive(pid):
            continue
        try:
            sig = (child / ".sync_signature").read_text(encoding="utf-8").strip()
        except Exception:
            sig = ""
        if sig and sig == nas_sig and nas_sig != "missing":
            try:
                os.replace(child, current)
                logger.info(
                    "Adopted complete local copy from dead worker %s: %s",
                    owner_id or pid, current,
                )
                continue
            except OSError as e:
                logger.warning("Could not adopt %s (%s); will sync from NAS", child, e)
                continue
        logger.info("Removing stale worker cache dir (owner PID %d gone): %s", pid, child)
        shutil.rmtree(child, ignore_errors=True)


def _write_owner_marker(cache_dir: Path):
    try:
        (cache_dir / ".worker_owner").write_text(
            f"{os.getpid()} {WORKER_ID}", encoding="utf-8"
        )
    except Exception:
        pass


def _rmtree_with_retry(cache_dir: Path, attempts: int = 8):
    """Delete a directory tree, retrying briefly - Windows can hold file
    handles (WinError 5 / WinError 32) for a moment after a process dies."""
    if not cache_dir.exists():
        return
    for attempt in range(attempts):
        try:
            shutil.rmtree(cache_dir)
            logger.info("Worker cache dir removed: %s", cache_dir)
            return
        except (PermissionError, OSError) as e:
            if attempt == attempts - 1:
                logger.warning(
                    "Could not remove worker cache dir %s (%s) - will be swept on next start",
                    cache_dir, e,
                )
                return
            logger.info("File locks still held on %s (%s); retrying...", cache_dir, e)
            time.sleep(min(0.3 * (2 ** attempt), 4.0))


def _taskkill_tree(pid: int) -> bool:
    """Windows-only: taskkill /PID <pid> /T /F the whole tree. Returns True on
    success; on failure (missing binary, access denied, already gone) logs the
    reason and returns False so the caller falls back to terminate/kill.
    Kept in its own function so static analyzers on any platform still analyze
    the implementation."""
    try:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("taskkill failed (%s) - falling back to terminate/kill", e)
        return False
    if r.returncode == 0:
        logger.info("taskkill terminated pid %d and its children", pid)
        return True
    logger.warning(
        "taskkill exit %d (%s) - falling back to terminate/kill",
        r.returncode, (r.stderr or r.stdout or "").strip()[:200],
    )
    return False


def _kill_process_tree(proc: "subprocess.Popen | None"):
    """Terminate a subprocess AND its whole tree (java.exe, robocopy, ...).

    Prefers `taskkill /T /F` - it kills children in one go - but some PCs
    fail on taskkill (missing binary, access denied, process already gone), so
    it falls back to plain terminate/kill and logs the reason.
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt" and _taskkill_tree(proc.pid):
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


# robocopy child processes that may still be running when the worker exits -
# tracked so Ctrl+C / atexit can kill them BEFORE the cache dirs are removed
# (a stray robocopy holds file locks -> WinError 5 on rmtree).
_active_copies: "set[subprocess.Popen]" = set()
_copies_lock = threading.Lock()


def _track_copy(proc: subprocess.Popen):
    with _copies_lock:
        _active_copies.add(proc)


def _untrack_copy(proc: subprocess.Popen):
    with _copies_lock:
        _active_copies.discard(proc)


def _kill_active_copies():
    with _copies_lock:
        procs = list(_active_copies)
    for proc in procs:
        _kill_process_tree(proc)


_cleaned_up = False


def _cleanup_worker_cache():
    """Kill everything that could hold file locks, then delete this worker's
    private cache dirs (docling + java).

    Safe to call from atexit AND from the Ctrl+C/SIGTERM handler (and even
    during module import, before the dirs/backend exist - they are looked up
    lazily). Delete retries briefly because Windows can hold handles for a
    moment after a process dies.
    """
    global _cleaned_up
    if _cleaned_up:
        return
    _cleaned_up = True

    stop_backend = globals().get("_stop_hybrid_backend")
    if stop_backend is not None:
        try:
            stop_backend()
        except Exception:
            logger.warning("Error stopping hybrid backend during cleanup", exc_info=True)
    _kill_active_copies()

    for name in ("LOCAL_DOCLING_DIR", "LOCAL_JAVA_DIR"):
        cache_dir = globals().get(name)
        if cache_dir is not None:
            _rmtree_with_retry(cache_dir)


def _dir_signature(path: Path) -> str:
    """File count + total size + newest mtime. Cheap, not cryptographic,
    good enough to detect 'the NAS copy changed since last sync'."""
    if not path.exists():
        return "missing"
    count = 0
    total_size = 0
    newest = 0.0
    for f in path.rglob("*"):
        if f.is_file():
            st = f.stat()
            count += 1
            total_size += st.st_size
            newest = max(newest, st.st_mtime)
    return f"{count}-{total_size}-{int(newest)}"


def _copytree_with_retry(nas_source: Path, local_dest: Path):
    """shutil.copytree with retries - SMB can throw WinError 32 when multiple
    PCs read the same NAS files at startup."""
    for attempt in range(8):
        try:
            shutil.copytree(nas_source, local_dest, dirs_exist_ok=True)
            return
        except PermissionError as e:
            if attempt == 7:
                logger.error("Failed to sync models after %d attempts: %s", attempt + 1, e)
                raise
            delay = (2 ** attempt) * 0.5 + random.uniform(0, 1)
            logger.warning("NAS locked (attempt %d), retrying in %.1fs...", attempt + 1, delay)
            time.sleep(delay)


def _robocopy_available() -> bool:
    """robocopy is dramatically faster than shutil.copytree for many small
    files (multithreaded, NTFS-aware). It only ships on Windows, so which()
    returning None elsewhere is a complete platform check - no os.name needed."""
    return shutil.which("robocopy") is not None


def _run_robocopy(src: Path, dst: Path, timeout: int = 3600):
    """One robocopy mirror run.

    Returns True on success (exit code < 8), False when robocopy reported a
    real failure, and None when robocopy could not be run at all (the caller
    then falls back to shutil.copytree).

    /MIR keeps dst identical to src (deletes stale files) while /XF spares
    the worker's own `.worker_owner` / `.sync_signature` markers; /R:/W:
    bound per-file retries so a locked NAS file can't hang for an hour;
    /MT:16 multithreads the copy.
    """
    # robocopy only exists on Windows, so a failed lookup already means
    # "not on this platform" - no separate os.name check needed.
    robocopy = shutil.which("robocopy")
    if not robocopy:
        return None
    cmd = [
        robocopy, str(src), str(dst),
        "/MIR",
        "/XF", ".worker_owner", ".sync_signature",
        "/R:3", "/W:2", "/MT:16",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    ]
    logger.info("robocopy mirror %s -> %s", src, dst)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",  # locale codepage - robocopy output is NOT utf-8
        )
    except OSError as e:
        logger.warning("robocopy could not start (%s) - will fall back to Python copy", e)
        return None
    _track_copy(proc)
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.error("robocopy timed out after %ds - killing it", timeout)
            _kill_process_tree(proc)
            try:
                proc.communicate(timeout=15)
            except Exception:
                pass
            if proc.poll() is None:
                # Kill demonstrably failed (the 'taskkill issue on some PCs'
                # case) - retrying would spawn MORE stuck robocopy processes
                # that all hold file locks. Stop here and fall back.
                logger.error("robocopy %s survived the kill - giving up on robocopy", proc.pid)
                return "stuck"
            return False
    finally:
        _untrack_copy(proc)
    rc = proc.returncode
    if rc is None or rc < 0:
        return False
    if rc >= 8:
        tail = "\n".join((out or "").strip().splitlines()[-5:])
        logger.error("robocopy failed (exit %d):\n%s", rc, tail)
        return False
    if rc:
        logger.info("robocopy done (exit %d)", rc)
    return True


def _robocopy_mirror_with_retry(nas_source: Path, local_dest: Path):
    """Mirror via robocopy; on repeated failure fall back to shutil.copytree
    (some PCs have broken/quirky robocopy builds)."""
    for attempt in range(8):
        result = _run_robocopy(nas_source, local_dest)
        if result is True:
            return
        if result is None:
            logger.warning("robocopy unavailable - falling back to shutil.copytree")
            _copytree_with_retry(nas_source, local_dest)
            return
        if result == "stuck":
            logger.warning("robocopy stuck and unkillable - falling back to shutil.copytree")
            _copytree_with_retry(nas_source, local_dest)
            return
        if attempt == 7:
            logger.error(
                "robocopy failed 8 times for %s - falling back to shutil.copytree",
                nas_source,
            )
            _copytree_with_retry(nas_source, local_dest)
            return
        delay = (2 ** attempt) * 0.5 + random.uniform(0, 1)
        logger.warning("robocopy failed (attempt %d), retrying in %.1fs...", attempt + 1, delay)
        time.sleep(delay)


def _sync_models_locally(nas_source: Path, local_dest: Path) -> Path:
    marker = local_dest / ".sync_signature"
    nas_sig = _dir_signature(nas_source)

    if marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == nas_sig and nas_sig != "missing":
                logger.info("Local model cache up to date: %s", local_dest)
                return local_dest
        except Exception:
            pass

    if nas_sig == "missing":
        logger.warning("NAS model source not found: %s, using local cache as-is", nas_source)
        return local_dest

    logger.info("Syncing models %s -> %s", nas_source, local_dest)

    if not local_dest.exists():
        local_dest.mkdir(parents=True, exist_ok=True)
    # Claim the folder BEFORE copying: if we crash mid-copy, the next worker's
    # sweep sees a dead owner PID and reclaims/removes it instead of letting a
    # partial multi-GB folder linger forever.
    _write_owner_marker(local_dest)

    if _robocopy_available():
        # robocopy /MIR also clears stale files in dst, so no rmtree first.
        _robocopy_mirror_with_retry(nas_source, local_dest)
    else:
        if local_dest.exists():
            shutil.rmtree(local_dest, ignore_errors=True)
        local_dest.mkdir(parents=True, exist_ok=True)
        _write_owner_marker(local_dest)
        _copytree_with_retry(nas_source, local_dest)

    marker.write_text(nas_sig, encoding="utf-8")
    logger.info("Model sync done")
    return local_dest


def _exit_signal_handler(signum, frame):
    """Ctrl+C / SIGTERM: delete this worker's docling + java cache dirs
    (killing any in-flight robocopy / hybrid backend first) and exit cleanly.
    Also covers Ctrl+C during the initial model sync - that is exactly when a
    stray robocopy would otherwise survive and hold file locks."""
    logger.info("Received exit signal %d - cleaning up worker cache dirs...", signum)
    try:
        _cleanup_worker_cache()
    except Exception:
        logger.warning("Error during signal cleanup", exc_info=True)
    # os._exit skips atexit and normal interpreter shutdown, so flush the log
    # handlers explicitly or the 'cache dir removed' lines are lost.
    logging.shutdown()
    os._exit(0)


def _register_exit_handlers():
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _exit_signal_handler)
        except (ValueError, OSError):
            pass


# Stagger NAS reads across multiple PCs - avoids N machines hitting the same
# SMB share at the exact same millisecond.
_register_exit_handlers()
time.sleep(random.uniform(1.0, 5.0))

# Each worker gets its OWN private cache dirs (keyed by WORKER_ID). Two workers
# on the same PC previously synced into the SAME folder, so the later one would
# rmtree the other's models mid-run and re-copy (WinError 32 / missing files).
# Reclaim/reuse leftovers from crashed workers first, then sync our own copies.
LOCAL_DOCLING_DIR = _cache_base("docling_worker_cache") / WORKER_ID
LOCAL_JAVA_DIR = _cache_base("java_worker_cache") / WORKER_ID

_prepare_local_dir(NAS_DOCLING_DIR, LOCAL_DOCLING_DIR)
_prepare_local_dir(NAS_JAVA_DIR, LOCAL_JAVA_DIR)

LOCAL_DOCLING_DIR = _sync_models_locally(NAS_DOCLING_DIR, LOCAL_DOCLING_DIR)

# Portable Java runtime - same NAS -> local sync as the docling models, so
# OpenDataLoader's `java` subprocess never touches the NAS/SMB share.
LOCAL_JAVA_DIR = _sync_models_locally(NAS_JAVA_DIR, LOCAL_JAVA_DIR)

_write_owner_marker(LOCAL_DOCLING_DIR)
_write_owner_marker(LOCAL_JAVA_DIR)

atexit.register(_cleanup_worker_cache)


def _field(value, *names, default=None):
    if not isinstance(value, dict):
        return default
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return default


def _element_text(element):
    value = _field(element, "content", "text", "markdown", default="")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in value
        ).strip()
    return str(value).strip() if value else ""


def _normalize_element(element, start_offset: int, index: int, chunk_idx: int):
    text = _element_text(element)
    local_page = _field(element, "page number", "page_number", "page", default=1)
    try:
        page = start_offset + int(local_page)
    except (TypeError, ValueError):
        page = start_offset + 1

    element_type = str(_field(element, "type", "element_type", default="paragraph")).lower()
    if element_type in {"p", "text", "body"}:
        element_type = "paragraph"
    if element_type in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        element_type = "heading"

    level = _field(element, "heading level", "heading_level", "level")
    try:
        level = int(level) if level is not None else None
    except (TypeError, ValueError):
        level = None

    bbox = _field(element, "bounding box", "bounding_box", "bbox")
    if isinstance(bbox, dict):
        # OCR/Docling uses a coordinate dictionary. Keep it intact so the
        # source highlight can interpret coord_origin later; converting it to
        # a four-item list loses whether y is TOPLEFT or BOTTOMLEFT.
        bbox = dict(bbox)
    elif isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        # OpenDataLoader's legacy schema is already a PDF coordinate array.
        bbox = list(bbox[:4])
    else:
        bbox = None

    raw_id = _field(element, "element_id", "id", default="")
    native_ref = _field(element, "native_ref", "self_ref")
    element_id = f"{chunk_idx}-{raw_id}" if raw_id else f"{chunk_idx}-{index}"

    return {
        "element_id": element_id,
        "native_ref": str(native_ref) if native_ref else None,
        "type": element_type,
        "content": text,
        "page_number": page,
        "heading_level": level,
        "bounding_box": bbox,
        "table": _field(element, "table", "structured", "structured_json"),
    }


def _normalize_document(document, start_offset: int, chunk_idx: int):
    if isinstance(document, list):
        document = {"elements": document}
    raw_elements = _field(document, "kids", "elements", "children", default=[])
    if not isinstance(raw_elements, list):
        raw_elements = []
    return [
        normalized
        for i, item in enumerate(raw_elements)
        if (normalized := _normalize_element(item, start_offset, i, chunk_idx))["content"]
    ]


def _read_output_file(directory: Path, suffix: str):
    files = sorted(directory.glob(f"*{suffix}"))
    if not files:
        raise FileNotFoundError(f"OpenDataLoader did not produce a {suffix} file in {directory}")
    return files[0]


_hybrid_process: subprocess.Popen | None = None


def _start_hybrid_backend():
    global HYBRID_URL, _hybrid_process
    if HYBRID_URL:
        logger.info("Using OpenDataLoader hybrid backend: %s", HYBRID_URL)
        return

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    command = [
        sys.executable,
        "-m",
        "opendataloader_pdf.hybrid_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--force-ocr",
        "--ocr-engine",
        "easyocr",
        "--ocr-lang",
        "en",
    ]
    sub_env = os.environ.copy()
    sub_env["DOCLING_ARTIFACTS_PATH"] = str(LOCAL_DOCLING_DIR)
    sub_env.setdefault("HF_HOME", str(NAS_DOCLING_DIR.parent / "huggingface"))
    sub_env.setdefault("HF_HUB_CACHE", str(NAS_DOCLING_DIR.parent / "huggingface" / "hub"))
    sub_env.setdefault("HF_HUB_OFFLINE", "1")
    sub_env.setdefault("TRANSFORMERS_OFFLINE", "1")

    logger.info("Starting OpenDataLoader hybrid backend on 127.0.0.1:%d...", port)
    _hybrid_process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=sub_env,
    )

    def _log_hybrid():
        proc = _hybrid_process
        if proc is None or proc.stdout is None:
            return
        for ln in proc.stdout:
            logger.debug("[hybrid-server] %s", ln.rstrip())

    threading.Thread(target=_log_hybrid, daemon=True, name="hybrid-log").start()

    # No startup timeout - the backend (Java + OCR models) can take minutes to
    # warm up on first boot, so wait as long as it needs.
    started_at = time.time()
    last_status_log = 0.0
    while True:
        if _hybrid_process.poll() is not None:
            raise RuntimeError("OpenDataLoader hybrid backend exited during startup")
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if resp.status_code == 200:
                HYBRID_URL = f"http://127.0.0.1:{port}"
                logger.info("Started OpenDataLoader hybrid backend: %s", HYBRID_URL)
                return
        except Exception:
            pass
        time.sleep(0.5)
        elapsed = time.time() - started_at
        if elapsed - last_status_log >= 30:
            logger.info(
                "OpenDataLoader hybrid backend still warming up (%.0fs elapsed)...",
                elapsed,
            )
            last_status_log = elapsed


def _stop_hybrid_backend():
    if _hybrid_process and _hybrid_process.poll() is None:
        # Kill the whole tree (taskkill /T) so the JVM/java.exe release their
        # file locks before the worker's cache dirs are removed at exit.
        _kill_process_tree(_hybrid_process)


_hybrid_started = False
_hybrid_error: Exception | None = None
_hybrid_ready = threading.Event()
_hybrid_kickoff_started = False
_hybrid_lock = threading.Lock()


def _start_hybrid_async():
    """Background thread: bring up the hybrid backend, then signal readiness."""
    global _hybrid_started, _hybrid_error, _hybrid_kickoff_started
    try:
        _start_hybrid_backend()
    except Exception as e:
        _hybrid_error = e
        logger.error("OpenDataLoader hybrid backend failed to start: %s", e)
        # Allow a later job to retry the startup - transient crashes happen on
        # first boot (JVM spawn, model warm-up, port races) and the worker no
        # longer dies to trigger a re-spawn.
        with _hybrid_lock:
            _hybrid_kickoff_started = False
    else:
        _hybrid_started = True
        atexit.register(_stop_hybrid_backend)
    finally:
        _hybrid_ready.set()


def _kickoff_hybrid():
    """Start the hybrid backend in the background; never blocks."""
    global _hybrid_kickoff_started
    with _hybrid_lock:
        if _hybrid_started or _hybrid_kickoff_started:
            return
        _hybrid_kickoff_started = True
        # Clear any stale readiness signal from a previous (failed) attempt so
        # waiters actually wait for this new attempt.
        _hybrid_ready.clear()
    threading.Thread(target=_start_hybrid_async, daemon=True, name="hybrid-start").start()


def _ensure_hybrid():
    """Block until the hybrid backend is ready, kicking it off if needed.

    Safe to call from any job thread: it only blocks when a job actually
    needs the backend while it is still warming up in the background.
    Note: while blocked, the assigned job's lease keeps ticking on the server;
    warm-ups longer than the server lease window may get the job re-assigned.
    """
    if _hybrid_started:
        return
    _kickoff_hybrid()
    if not _hybrid_ready.is_set():
        logger.info("Waiting for OpenDataLoader hybrid backend to warm up...")
    _hybrid_ready.wait()
    if not _hybrid_started:
        raise RuntimeError("OpenDataLoader hybrid backend failed to start") from _hybrid_error


_LABEL_TYPE = {
    "TITLE": "heading",
    "SECTION_HEADER": "heading",
    "TABLE": "table",
    "LIST_ITEM": "paragraph",
    "PARAGRAPH": "paragraph",
    "TEXT": "paragraph",
    "PAGE_HEADER": "paragraph",
    "PAGE_FOOTER": "paragraph",
    "FOOTNOTE": "paragraph",
    "FORMULA": "paragraph",
    "CODE": "paragraph",
}


def _docling_ocr_elements(doc, start_offset: int, chunk_idx: int):
    """Elements in the same shape OpenDataLoader emits, but read from a docling
    DoclingDocument (headings keep their level, tables carry their markdown,
    bodies get bounding boxes and page numbers)."""
    elements = []
    for i, (item, _parent) in enumerate(doc.iterate_items()):
        label = getattr(item, "label", None)
        label_name = getattr(label, "name", None) or str(label or "")
        elem_type = _LABEL_TYPE.get(label_name, "paragraph")

        text = str(getattr(item, "text", "") or "").strip()
        table_md = None
        if elem_type == "table":
            try:
                table_md = item.export_to_markdown().strip()
            except Exception:
                table_md = text

        content = table_md if table_md else text
        if not content:
            continue

        # Provenance is a list in current Docling releases. Older versions
        # exposed an object with an ``items`` attribute, so support both forms.
        page, bbox = start_offset + 1, None
        prov = getattr(item, "prov", None)
        if isinstance(prov, (list, tuple)):
            prov_items = prov
        else:
            prov_items = getattr(prov, "items", None) if prov is not None else None
        if prov_items:
            first_prov = prov_items[0]
            local_page = getattr(first_prov, "page_no", None)
            if local_page is not None:
                page = start_offset + int(local_page)
            b = getattr(first_prov, "bbox", None)
            if b is not None and all(hasattr(b, attr) for attr in ("l", "b", "r", "t")):
                origin = getattr(getattr(b, "coord_origin", None), "value", None)
                bbox = {
                    "left": b.l,
                    "top": b.t,
                    "right": b.r,
                    "bottom": b.b,
                    "coord_origin": origin or str(getattr(b, "coord_origin", "TOPLEFT")),
                }

        # Docling's self_ref is stable inside one document. Namespace it with
        # the extraction chunk so IDs remain unique after chunks are merged.
        native_ref = str(getattr(item, "self_ref", "") or "")
        element_id = f"{chunk_idx}:{native_ref}" if native_ref else f"{chunk_idx}-{i}"

        level = None
        if elem_type == "heading":
            # SectionHeaderItem carries an explicit level; Title -> level 1.
            if isinstance(getattr(item, "level", None), int):
                level = item.level
            elif label_name == "TITLE":
                level = 1

        elements.append({
            "element_id": element_id,
            "native_ref": native_ref or None,
            "type": elem_type,
            "content": content,
            "page_number": page,
            "heading_level": level,
            "bounding_box": bbox,
            "table": None,
        })
    return elements


@log_process
def _process_docling_ocr(pdf_path: Path, start_offset: int, chunk_idx: int):
    """Direct docling OCR (EasyOCR). Reliable for scanned/image PDFs where the
    OpenDataLoader docling-fast backend only emits full-page image refs."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, EasyOcrOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice

    # Worker's own model cache (holds the EasyOCR .pth weights) - never shared
    # with other workers on this machine. Must be passed explicitly, not via
    # env: docling's settings singleton is already built from the module-level
    # import above, so DOCLING_ARTIFACTS_PATH set here would be ignored.
    ocr_opts = EasyOcrOptions(lang=["en"])
    opts = PdfPipelineOptions(
        do_ocr=True,
        ocr_options=ocr_opts,
        artifacts_path=str(LOCAL_DOCLING_DIR),
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.AUTO),
    )
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    res = conv.convert(str(pdf_path))
    doc = res.document
    logger.info("[OCR] docling direct OCR extracted %d text items", len(list(doc.texts)))

    # Unlike OpenDataLoader's Markdown export, Docling's document-level
    # export does not include page separators. The downstream assembler and
    # page metadata builder use these markers to preserve page boundaries.
    # Export each page explicitly so a scanned 10-page PDF cannot collapse
    # into one logical page.
    page_numbers = sorted(getattr(doc, "pages", {}) or {})
    if page_numbers:
        markdown_parts = []
        for page_no in page_numbers:
            page_markdown = doc.export_to_markdown(page_no=page_no).strip()
            # Markers are chunk-local; extraction_server.py applies the
            # chunk offset once when it assembles the final document.
            markdown_parts.append(
                f"## --- PAGE {page_no} ---\n{page_markdown}"
            )
        markdown = "\n\n".join(markdown_parts)
    else:
        # Keep a useful result for unusual Docling documents that do not
        # expose a page map, while still satisfying the page-marker contract.
        markdown = (
            "## --- PAGE 1 ---\n"
            f"{doc.export_to_markdown().strip()}"
        )

    return {
        "markdown": markdown,
        "elements": _docling_ocr_elements(doc, start_offset, chunk_idx),
        "engine": "docling-ocr",
    }


@log_process
def _process_opendataloader(pdf_path: Path, start_offset: int, chunk_idx: int, ocr_enabled: bool = False):
    if ocr_enabled:
        # Scanned/image PDFs: OpenDataLoader's docling-fast backend emits
        # full-page image refs even with --force-ocr + hybrid_mode=full (verified).
        # Docling direct + EasyOCR reads the pixels and needs no Java on PATH.
        return _process_docling_ocr(pdf_path, start_offset, chunk_idx)
    _java_on_path()
    _ensure_hybrid()
    output_dir = pdf_path.parent / f"{pdf_path.stem}_odl"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        kwargs = dict(
            input_path=str(pdf_path),
            output_dir=str(output_dir),
            format="json,markdown",
            use_struct_tree=not ocr_enabled,
            markdown_page_separator="## --- PAGE %page-number% ---",
            quiet=True,
        )
        opendataloader_pdf.convert(**kwargs)
        markdown_path = _read_output_file(output_dir, ".md")
        json_path = _read_output_file(output_dir, ".json")
        document = json.loads(json_path.read_text(encoding="utf-8"))
        return {
            "markdown": markdown_path.read_text(encoding="utf-8"),
            "elements": _normalize_document(document, start_offset, chunk_idx),
            "engine": "opendataloader",
        }
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


@log_process
def process_chunk(pdf_bytes: bytes, start_offset: int, chunk_idx: int, ocr_enabled: bool = False) -> dict:
    if ocr_enabled:
        logger.info("Chunk %d: OCR ON - all pages through direct Docling OCR", chunk_idx)
    else:
        logger.info("Chunk %d: OCR OFF - Java fast path only", chunk_idx)

    tmp_path = Path(tempfile.gettempdir()) / f"opendataloader_{uuid.uuid4().hex}_{start_offset}.pdf"
    try:
        tmp_path.write_bytes(pdf_bytes)
        return _process_opendataloader(tmp_path, start_offset, chunk_idx, ocr_enabled)
    finally:
        _queue_delete(tmp_path)


_session = create_session()

_cache = ResultCache(
    "text",
    base_dir=RESULT_CACHE_DIR,
)
_heartbeat = LeaseHeartbeat(SERVER_URL, WORKER_ID, HEARTBEAT_INTERVAL_SECONDS)


is_connected = False


def start_worker():
    global is_connected

    logger.info("=" * 80)
    logger.info("TEXT WORKER STARTED (extraction mode)")
    logger.info("SERVER      : %s", SERVER_URL)
    logger.info("BASE_DIR    : %s", BASE_DIR)
    logger.info("MODEL_DIR   : %s (NAS-synced local cache)", LOCAL_DOCLING_DIR)
    logger.info("JAVA_DIR    : %s (NAS-synced local cache)", LOCAL_JAVA_DIR)
    logger.info("CACHE_DIR   : %s", _cache.cache_dir)
    detect_device(logger)
    logger.info("=" * 80)

    time.sleep(random.uniform(0.5, 4.0))

    _cache.replay(_session, f"{SERVER_URL}/submit_result")

    # Kick off the OpenDataLoader hybrid backend in the background and start
    # pulling jobs right away - jobs that need the backend wait for it, but
    # the worker is connected and registered while it warms up.
    _kickoff_hybrid()
    logger.info("OpenDataLoader hybrid backend warming up in the background")

    while True:
        job_id = None
        try:
            resp = _session.get(
                f"{SERVER_URL}/get_job",
                params={"worker_id": WORKER_ID},
            )

            if not is_connected:
                logger.info("Connected to server")
                is_connected = True

            if resp.status_code != 200:
                time.sleep(ERROR_SLEEP)
                continue

            data = resp.json()

            if data.get("action") == "WAIT":
                time.sleep(WAIT_SLEEP)
                continue

            if data.get("action") != "PROCESS":
                continue

            job_id = data["job_id"]
            lease_id = data["lease_id"]
            _heartbeat.start(job_id, lease_id)
            book_id = data["book_id"]
            chunk_idx = data["chunk_idx"]
            start_offset = data.get("start_offset", 0)
            ocr_enabled = data.get("ocr_enabled", False)
            fallback_mode = data.get("fallback_mode", False)
            if fallback_mode:
                logger.info(
                    "Chunk %d: normal OpenDataLoader fallback after %d attempts",
                    chunk_idx,
                    data.get("attempt_count", 0),
                )

            logger.info("Processing chunk %d (book=%s)", chunk_idx, book_id)

            chunk_resp = _session.get(
                f"{SERVER_URL}/chunk/{job_id}",
            )

            if chunk_resp.status_code != 200:
                error = f"Failed to download chunk {job_id} (HTTP {chunk_resp.status_code})"
                logger.error(error)
                submit_with_retry(
                    _session,
                    f"{SERVER_URL}/submit_result",
                    {
                        "job_id": job_id,
                        "worker_id": WORKER_ID,
                        "lease_id": lease_id,
                        "success": False,
                        "error": error,
                    },
                )
                continue

            try:
                content = process_chunk(
                    chunk_resp.content,
                    start_offset,
                    chunk_idx,
                    ocr_enabled=(ocr_enabled and not fallback_mode),
                )
            except Exception as e:
                logger.error("Extraction failed for chunk %d: %s", chunk_idx, e)
                try:
                    _session.post(
                        f"{SERVER_URL}/submit_result",
                        json={
                            "job_id": job_id,
                            "worker_id": WORKER_ID,
                            "lease_id": lease_id,
                            "success": False,
                            "content": "",
                            "error": str(e),
                        },
                        timeout=30,
                    )
                except Exception as e2:
                    logger.error("Error submitting failure: %s", e2)
                continue

            payload = {
                "job_id": job_id,
                "worker_id": WORKER_ID,
                "lease_id": lease_id,
                "success": True,
                "content": content,
            }

            _cache.store(job_id, payload)
            accepted = submit_with_retry(_session, f"{SERVER_URL}/submit_result", payload)
            if accepted:
                _cache.clear(job_id)
                logger.info("Completed chunk %d", chunk_idx)
            else:
                logger.warning(
                    "Result for chunk %d was not accepted; keeping local cache for replay",
                    chunk_idx,
                )

        except requests.exceptions.ConnectionError:
            if is_connected:
                logger.error("Disconnected from server. Reconnecting...")
                is_connected = False
            time.sleep(ERROR_SLEEP)

        except Exception as e:
            logger.error("Error: %s", e)
            time.sleep(ERROR_SLEEP)
        finally:
            _heartbeat.stop()


if __name__ == "__main__":
    start_worker()
