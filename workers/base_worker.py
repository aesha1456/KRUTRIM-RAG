"""
base_worker.py — Workers
========================
Shared infrastructure for all worker types (text, qdrant, neo4j).

Provides:
  • configure_tcp_keepalive() — prevents idle connections from being dropped
  • ResultCache — crash-safe result cache with replay-on-startup
  • submit_with_retry() — exponential backoff for server submissions
  • create_session() — persistent requests.Session with keepalive headers
  • Safe file operations — retry-on-lock for Windows / network filesystems
  • detect_device() — CUDA/CPU detection for worker startup
"""

import json
import logging
import os
import socket
import ssl
import sys
import tempfile
import time
import threading
from pathlib import Path

import requests
from urllib3.connection import HTTPConnection

_log = logging.getLogger(__name__)

# ─── TCP keepalive ────────────────────────────────────────────────────────────
#
# Windows uses WSAIoctl (SIO_KEEPALIVE_VALS) to configure keepalive timing
# because setsockopt only exposes SO_KEEPALIVE (on/off).  The default idle
# time is 2 hours — useless for workers behind NAT/proxies that drop idle
# connections in 60-300 s.  We monkey-patch HTTPConnection._new_conn so
# every socket gets Windows keepalive applied at creation time.

_keepalive_applied = False

# (onoff, idle_ms, interval_ms) — Windows socket.ioctl expects a 3-int seq.
_keepalive_vals = (1, 60_000, 10_000)  # on, 60 s idle, 10 s interval

_real_new_conn = HTTPConnection._new_conn


def _new_conn(self: HTTPConnection):
    """Create socket, set SO_KEEPALIVE, then apply Windows keepalive timing.

    socket.ioctl(SIO_KEEPALIVE_VALS) only exists on Windows - on POSIX the
    socket object has no ioctl() at all (AttributeError), so guard on os.name.
    """
    conn = _real_new_conn(self)
    if os.name == "nt":
        try:
            conn.ioctl(socket.SIO_KEEPALIVE_VALS, _keepalive_vals)
        except OSError:
            _log.warning("WSAIoctl failed; keepalive timing may use system defaults")
    return conn


def configure_tcp_keepalive():
    """Apply TCP keepalive to all urllib3 HTTPConnections.

    Idempotent — safe to call multiple times or from multiple worker modules.
    Prevents idle connections from being silently dropped by load balancers,
    proxies, and NAT gateways.

    - Enables SO_KEEPALIVE on every socket.
    - On Windows: additionally configures timing via WSAIoctl
      (first probe after 60 s idle, then every 10 s).
    """
    global _keepalive_applied
    if _keepalive_applied:
        return

    HTTPConnection.default_socket_options = HTTPConnection.default_socket_options + [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]
    HTTPConnection._new_conn = _new_conn
    _keepalive_applied = True


# ─── Safe file operations (retry on transient locks) ─────────────────────────

def safe_unlink(path: Path, attempts: int = 8, base_delay: float = 0.15) -> None:
    """Delete a file, retrying on PermissionError with exponential backoff."""
    for i in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as e:
            if i == attempts - 1:
                _log.warning(
                    "Giving up deleting %s after %d attempts: %s",
                    path, attempts, e,
                )
                return
            time.sleep(base_delay * (2 ** i))


def safe_write_bytes(
    path: Path, data: bytes, attempts: int = 5, base_delay: float = 0.15
) -> None:
    """Write bytes to a file, retrying on PermissionError."""
    for i in range(attempts):
        try:
            with open(path, "wb") as f:
                f.write(data)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def safe_write_text(
    path: Path, text: str, attempts: int = 5, base_delay: float = 0.15
) -> None:
    """Write text to a file, retrying on PermissionError."""
    for i in range(attempts):
        try:
            path.write_text(text, encoding="utf-8")
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def safe_read_text(
    path: Path, attempts: int = 5, base_delay: float = 0.15
) -> str:
    """Read text from a file, retrying on PermissionError."""
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))


# ─── SSL / CA bundle hardening ───────────────────────────────────────────────
#
# Workers whose conda env lives on an SMB/NAS share can hit
# `[ASN1: NOT_ENOUGH_DATA] not enough data (_ssl.c:4040)` on HTTPS calls:
#   * CPython loads EVERY cert from the Windows certificate store at once and
#     a single malformed/truncated entry nukes the whole context
#     (python/cpython#104135 - "not enough data: cadata does not contain a
#     certificate");
#   * a truncated read of certifi's cacert.pem over SMB (the same file-lock
#     contention that causes WinError 32) hands OpenSSL a partial PEM/DER
#     blob, which is exactly "not enough data".
# Fix: build a LOCAL bundle (certifi's cacert.pem + the bundled self-signed
# dev CA in workers/local_ca.pem), verify it parses, and point requests/
# OpenSSL at it (REQUESTS_CA_BUNDLE / SSL_CERT_FILE). When a cafile is
# provided, ssl.create_default_context() skips load_default_certs(), so the
# Windows cert store is never parsed at all. Pre-existing env values (e.g. an
# explicitly configured corporate bundle) are respected.

_ssl_bundle_hardened = False

# Bundled dev CA - the last-resort trust anchor when the NAS-hosted
# certifi cacert.pem is missing or corrupt (a truncated NAS copy is what
# triggers the Windows '_ssl.c:4040 not enough data' crash). Always keeps
# the bundle parseable so requests/OpenSSL never read the broken file.
_LOCAL_CA_PATH = Path(__file__).resolve().parent / "local_ca.pem"


def _atomic_write(dest: Path, data: bytes):
    """Write bytes atomically (tmp + os.replace) so concurrent worker
    processes on one PC can never interleave writes and corrupt a shared
    cache file. Leftover tmp files from crashed workers are swept only when
    they are older than a day, so a live worker's in-progress tmp is never
    deleted out from under it."""
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    try:
        safe_write_bytes(tmp, data)
        os.replace(tmp, dest)
    finally:
        safe_unlink(tmp)
    now = time.time()
    for stale in dest.parent.glob(f"{dest.name}.*.tmp"):
        try:
            if now - stale.stat().st_mtime > 86400:
                safe_unlink(stale)
        except OSError:
            pass


def _verify_bundle(path: Path) -> bool:
    """True when OpenSSL can actually parse the PEM bundle as CA certs."""
    try:
        ssl.create_default_context(cafile=str(path))
        return True
    except (ssl.SSLError, OSError):
        return False


def _harden_ssl_cert_bundle():
    """Ensure a clean, LOCAL CA bundle and point requests/OpenSSL at it.

    Fallback chain:
      1. existing REQUESTS_CA_BUNDLE / SSL_CERT_FILE / CURL_CA_BUNDLE env
         value that points at a real file - the user explicitly configured it;
      2. certifi's cacert.pem + the bundled workers/local_ca.pem combined
         (preferred: real CAs AND the local dev CA);
      3. workers/local_ca.pem alone - reached when certifi is missing OR its
         bundle is corrupt (e.g. a truncated NAS copy). Sources that fail
         OpenSSL parsing are dropped one by one before giving up.

    Idempotent - safe to call from every worker's create_session().
    """
    global _ssl_bundle_hardened
    if _ssl_bundle_hardened:
        return
    _ssl_bundle_hardened = True

    existing = (
        os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
        or os.environ.get("CURL_CA_BUNDLE")
    )
    # Conda sets SSL_CERT_FILE to a file inside the active environment. It is
    # not an explicit corporate override; copy it to a private local path so
    # every worker reads one stable PEM file instead of the environment file.
    conda_root = Path(os.environ.get("CONDA_PREFIX", sys.prefix)).resolve()
    existing_path = Path(existing).resolve() if existing else None
    is_conda_default = bool(
        existing_path and existing_path.is_relative_to(conda_root)
    )
    if existing_path and existing_path.is_file() and not is_conda_default:
        _log.info("Using configured CA bundle: %s", existing)
        return

    sources: list[Path] = []
    try:
        import certifi
        certifi_path = Path(certifi.where())
        if certifi_path.is_file():
            sources.append(certifi_path)
    except ImportError:
        pass
    if _LOCAL_CA_PATH.is_file():
        sources.append(_LOCAL_CA_PATH)

    if not sources:
        _log.warning(
            "No CA sources available (certifi missing and %s absent); "
            "keeping system defaults",
            _LOCAL_CA_PATH,
        )
        return

    try:
        local_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "worker_ssl_cache"
        local_dir.mkdir(parents=True, exist_ok=True)
        local_bundle = local_dir / "cacert.pem"
        sig_file = local_dir / "cacert.sig"

        # Read every source; a single unreadable source (transient SMB lock)
        # must not kill the whole hardening - skip it instead.
        contents: list[bytes] = []
        sig_parts: list[str] = []
        for src in sources:
            try:
                data = src.read_bytes()
            except OSError as e:
                _log.warning("Could not read CA source %s (%s); skipping it", src, e)
                continue
            contents.append(data)
            sig_parts.append(f"{src}:{len(data)}")
        if not contents:
            _log.warning("No CA sources could be read; keeping system defaults")
            return

        cached_sig = sig_file.read_text(encoding="utf-8") if sig_file.is_file() else ""
        all_sig = "|".join(sig_parts)
        up_to_date = local_bundle.is_file() and cached_sig == all_sig
        if up_to_date and not _verify_bundle(local_bundle):
            # Cached bundle went corrupt on disk - force a rebuild.
            up_to_date = False
            safe_unlink(sig_file)

        if not up_to_date:
            # Assemble the best bundle that parses: try ALL sources first,
            # then drop sources from the front until verification passes. A
            # corrupt/truncated NAS certifi copy must not block the fallback
            # to the bundled local CA.
            while contents:
                _atomic_write(local_bundle, b"".join(data + b"\n" for data in contents))
                if _verify_bundle(local_bundle):
                    break
                _log.warning(
                    "CA bundle with %d source(s) failed verification; dropping %s",
                    len(contents), sig_parts[0],
                )
                contents.pop(0)
                sig_parts.pop(0)
            if not contents:
                _log.warning("No CA source passed verification; keeping system defaults")
                return
            safe_write_text(sig_file, "|".join(sig_parts))

        os.environ["REQUESTS_CA_BUNDLE"] = str(local_bundle)
        os.environ["SSL_CERT_FILE"] = str(local_bundle)
        _log.info(
            "Using local CA bundle (%d source(s)): %s", len(sig_parts), local_bundle
        )
    except Exception as e:
        _log.warning("SSL bundle hardening failed (%s); keeping system defaults", e)


# ─── OpenSSL 3.0.21+ default-context workaround (python/cpython#151504) ──────
#
# OpenSSL >= 3.0.21 (the CVE-2026-34180 ASN.1 hardening) reports the normal
# end of a DER certificate buffer as ASN1_R_NOT_ENOUGH_DATA instead of the
# old ASN1_R_HEADER_TOO_LONG. CPython's _ssl.c only whitelists the old code,
# so SSLContext.load_verify_locations(cadata=...) now raises
# "[ASN1: NOT_ENOUGH_DATA] not enough data (_ssl.c:...)" even for perfectly
# valid input. On Windows that breaks ssl.create_default_context() outright,
# because the OS cert store is loaded through cadata= - every default-context
# HTTPS call (aiohttp, httpx, huggingface_hub, transformers, urllib) dies.
#
# Workaround: wrap create_default_context so every default context gets our
# LOCAL cafile (REQUESTS_CA_BUNDLE / SSL_CERT_FILE from the hardening above).
# With a cafile passed, CPython skips load_default_certs() entirely and the
# Windows cert store is never parsed. requests-style libraries were already
# covered (they read the env vars); this covers the default-context ones too,
# including contexts built at import time.

_orig_create_default_context = ssl.create_default_context


def _create_default_context(
    purpose=ssl.Purpose.SERVER_AUTH,
    *,
    cafile=None,
    capath=None,
    cadata=None,
):
    if cafile is None and capath is None and cadata is None:
        # Same env vars the hardening honors (incl. CURL_CA_BUNDLE) - see
        # _harden_ssl_cert_bundle().
        bundle = (
            os.environ.get("REQUESTS_CA_BUNDLE")
            or os.environ.get("SSL_CERT_FILE")
            or os.environ.get("CURL_CA_BUNDLE")
        )
        if bundle and Path(bundle).is_file():
            cafile = bundle
    return _orig_create_default_context(purpose, cafile=cafile, capath=capath, cadata=cadata)


# Install at import time (all workers import base_worker first) so libraries
# that build SSL contexts on import are covered before any default context can
# hit the Windows cert-store cadata path.
ssl.create_default_context = _create_default_context
if hasattr(ssl, "_create_default_https_context"):
    ssl._create_default_https_context = _create_default_context
_harden_ssl_cert_bundle()


# ─── Session ──────────────────────────────────────────────────────────────────

def create_session() -> requests.Session:
    """Create a persistent requests.Session with ngrok header and TCP keepalive."""
    configure_tcp_keepalive()
    _harden_ssl_cert_bundle()
    session = requests.Session()
    session.headers.update({"ngrok-skip-browser-warning": "true"})
    return session


class LeaseHeartbeat:
    """Keep a server-side job lease alive while a worker is processing it."""

    def __init__(self, server_url: str, worker_id: str, interval: float = 45.0):
        self.server_url = server_url.rstrip("/")
        self.worker_id = worker_id
        self.interval = interval
        self._job_id = None
        self._lease_id = None
        self._stop = threading.Event()
        self._thread = None

    def start(self, job_id: str, lease_id: str) -> None:
        self.stop()
        self._job_id = job_id
        self._lease_id = lease_id
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._job_id = None
        self._lease_id = None

    def _run(self) -> None:
        session = create_session()
        while not self._stop.wait(self.interval):
            job_id = self._job_id
            lease_id = self._lease_id
            if not job_id or not lease_id:
                continue
            try:
                response = session.post(
                    f"{self.server_url}/heartbeat",
                    json={
                        "job_id": job_id,
                        "worker_id": self.worker_id,
                        "lease_id": lease_id,
                    },
                    timeout=15,
                )
                if response.status_code != 200:
                    _log.warning(
                        "Lease heartbeat failed for %s: HTTP %d",
                        job_id,
                        response.status_code,
                    )
            except Exception as exc:
                _log.warning("Lease heartbeat error for %s: %s", job_id, exc)


# ─── ResultCache ──────────────────────────────────────────────────────────────

class ResultCache:
    """Crash-safe local result cache with startup replay capability.

    Stores job results on local disk so they survive worker restarts, network
    blips, and server downtime.  On next startup, cached results are replayed
    to the server before polling for new jobs.

    Usage
    -----
        cache = ResultCache("qdrant", base_dir=RESULT_CACHE_DIR)
        cache.store(job_id, payload)
        cache.submit_with_retry(session, f"{SERVER_URL}/submit_qdrant_result", payload)
        cache.clear(job_id)

        # On startup:
        cache.replay(session, f"{SERVER_URL}/submit_qdrant_result")
    """

    def __init__(self, worker_type: str, base_dir: Path | None = None):
        if base_dir is None:
            base_dir = Path(tempfile.gettempdir()) / "worker_result_cache"
        self.cache_dir = base_dir / worker_type
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.cache_dir / f"{job_id}.json"

    def store(self, job_id: str, payload: dict) -> None:
        """Persist a result payload to the local cache."""
        safe_write_text(self._path(job_id), json.dumps(payload, ensure_ascii=False))

    def clear(self, job_id: str) -> None:
        """Remove a successfully-submitted result from the cache."""
        safe_unlink(self._path(job_id))

    def replay(self, session: requests.Session, submit_url: str) -> None:
        """Replay all cached results to the server, clearing on success."""
        cached = sorted(self.cache_dir.glob("*.json"))
        if not cached:
            return
        _log.info("Replaying %d cached result(s)...", len(cached))
        for cache_file in cached:
            try:
                payload = json.loads(safe_read_text(cache_file))
                r = session.post(submit_url, json=payload, timeout=30)
                try:
                    response = r.json() if r.content else {}
                except (ValueError, TypeError):
                    response = {}
                accepted = response.get("accepted")
                terminal = response.get("terminal") is True
                if r.status_code == 200 and (accepted is True or terminal):
                    safe_unlink(cache_file)
                    _log.info("Replayed and cleared: %s", cache_file.name)
                else:
                    _log.warning(
                        "Replay not accepted HTTP %d response=%s: %s",
                        r.status_code,
                        response,
                        cache_file.name,
                    )
            except Exception as e:
                _log.warning("Replay error for %s: %s", cache_file.name, e)


# ─── submit_with_retry ────────────────────────────────────────────────────────

def submit_with_retry(
    session: requests.Session,
    endpoint: str,
    payload: dict,
    *,
    initial_delay: float = 5.0,
    max_delay: float = 60.0,
    timeout: float = 30.0,
) -> bool:
    """POST a payload to the server with exponential backoff on failure.

    Blocks until the server accepts the payload. Returns ``False`` when the
    server deliberately ignores a stale/duplicate result. Handles both
    ConnectionError and non-200 responses.
    """
    delay = initial_delay
    while True:
        try:
            r = session.post(endpoint, json=payload, timeout=timeout)
            if r.status_code == 200:
                try:
                    response = r.json() if r.content else {}
                except (ValueError, TypeError):
                    response = {}
                if response.get("terminal") is True:
                    # The server already recorded a terminal outcome. There is
                    # nothing left for this payload to retry, so callers may
                    # safely clear their local cache even when accepted=False.
                    return True
                if response.get("accepted") is True:
                    return True
                _log.warning("Server returned no positive acknowledgement: %s", response)
                return False
            _log.warning(
                    "Submit returned HTTP %d, retrying in %.1fs...",
                    r.status_code, delay,
                )
        except requests.exceptions.ConnectionError:
            _log.warning(
                    "Connection error on submit, retrying in %.1fs...", delay,
                )
        time.sleep(delay)
        delay = min(delay * 2, max_delay)


# ─── GPU / Device detection ────────────────────────────────────────────────

def detect_device(logger: logging.Logger | logging.LoggerAdapter | None = None) -> str:
    """Detect CUDA availability and return 'cuda' or 'cpu'.

    Also sets ``RAG_DEVICE`` in the environment so that downstream
    imports (e.g. ``partb.config.DEVICE``) pick up the right value
    without requiring the caller to set it manually.
    """
    has_torch = False
    has_cuda = False
    gpu_name = "N/A"

    try:
        import torch
        has_torch = True
        has_cuda = torch.cuda.is_available()
        if has_cuda:
            gpu_name = torch.cuda.get_device_name(0)
    except ImportError:
        pass

    from workers.config import RAG_DEVICE

    device = ("cuda" if has_cuda else "cpu") if RAG_DEVICE == "auto" else RAG_DEVICE
    os.environ["RAG_DEVICE"] = device

    if logger:
        logger.info("PyTorch available  : %s", has_torch)
        logger.info("CUDA available     : %s", has_cuda)
        logger.info("GPU name           : %s", gpu_name)
        logger.info("Selected device    : %s", device)

    return device
