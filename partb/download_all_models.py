#!/usr/bin/env python3
"""Provision local models required by Part B and the text worker.

Run this script once on a machine with Hugging Face/Docling network access,
then copy ``parta/portable`` to the offline worker machine:

    python partb/download_all_models.py

The resulting layout is:

    parta/portable/
    ├── docling/              Docling + English EasyOCR artifacts
    ├── huggingface/          Hugging Face Hub cache (refs/snapshots/blobs)
    ├── gliner/               Flat GLiNER model used by Part B
    ├── jina-reranker-v3/     Flat Transformers model used by Part B
    └── nomic/                Flat SentenceTransformer model used by Part B

Portable Java is provisioned separately with ``build_portable_java.py``. The
text worker deliberately enables offline mode and never downloads missing
files. This script is the online provisioning step that must run first.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = PROJECT_ROOT / "parta" / "portable"


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=os.environ.get("RAG_PORTABLE_DIR", str(DEFAULT_TARGET)),
        help="Portable model directory (default: parta/portable)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not download anything; verify an already-provisioned directory",
    )
    parser.add_argument(
        "--skip-docling",
        action="store_true",
        help="Provision only the Part B retrieval models",
    )
    parser.add_argument(
        "--skip-retrieval",
        action="store_true",
        help="Provision only Docling and EasyOCR artifacts",
    )
    return parser.parse_args()


def _configure_huggingface_cache(target: Path) -> Path:
    """Point Hub downloads at a portable cache before importing HF libraries."""
    cache_root = target / "huggingface"
    cache_root.mkdir(parents=True, exist_ok=True)

    # huggingface_hub reads these variables at import time. Do not use
    # setdefault: an old machine-wide path would otherwise silently receive
    # the downloads and the copied portable directory would be incomplete.
    os.environ["HF_HOME"] = str(cache_root)
    os.environ["HF_HUB_CACHE"] = str(cache_root / "hub")
    os.environ.setdefault("HF_DATASETS_CACHE", str(cache_root / "datasets"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    return cache_root


def _ensure_online():
    offline_vars = [
        name
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
        if _is_true(os.environ.get(name))
    ]
    if offline_vars:
        names = ", ".join(offline_vars)
        raise RuntimeError(
            f"{names} is enabled. This is the online provisioning script; "
            "unset those variables or run with --verify-only on the offline machine."
        )


def _run(command: list[str], *, env: dict[str, str] | None = None):
    print("+", " ".join(command), flush=True)
    try:
        subprocess.run(command, check=True, env=env)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required command was not found: {command[0]!r}. "
            "Install the project dependencies before provisioning."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Command failed with exit code {exc.returncode}: {' '.join(command)}") from exc


def _has_weight_file(directory: Path) -> bool:
    return any(
        path.is_file()
        for pattern in ("*.safetensors", "*.bin", "*.pth", "*.onnx", "*.pt")
        for path in directory.rglob(pattern)
    )


def _verify_flat_model(name: str, directory: Path, required: tuple[str, ...]):
    missing = [relative for relative in required if not (directory / relative).is_file()]
    if missing or not _has_weight_file(directory):
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if not _has_weight_file(directory):
            details.append("no model weight file (*.safetensors, *.bin, *.pt, or *.onnx)")
        raise RuntimeError(f"{name} is incomplete at {directory}: {'; '.join(details)}")
    print(f"OK {name}: {directory}")


def _verify_huggingface_cache(cache_root: Path):
    hub_root = cache_root / "hub"
    repos = sorted(
        path
        for path in hub_root.glob("models--*")
        if path.is_dir()
    )
    if not repos:
        raise RuntimeError(f"No Hugging Face model repositories found under {hub_root}")

    invalid = []
    for repo in repos:
        snapshots = repo / "snapshots"
        refs = repo / "refs"
        if not snapshots.is_dir() or not any(path.is_dir() for path in snapshots.iterdir()):
            invalid.append(f"{repo.name}: missing snapshots")
            continue

        ref_files = [path for path in refs.iterdir() if path.is_file()] if refs.is_dir() else []
        if not ref_files:
            invalid.append(f"{repo.name}: missing refs")
            continue
        for ref_file in ref_files:
            revision = ref_file.read_text(encoding="utf-8").strip()
            if not revision or not (snapshots / revision).is_dir():
                invalid.append(f"{repo.name}: {ref_file.name} points to missing snapshot {revision!r}")

    if invalid:
        raise RuntimeError("Invalid Hugging Face cache:\n  " + "\n  ".join(invalid))
    print(f"OK Hugging Face cache: {hub_root} ({len(repos)} model repositories)")


def _verify_docling(directory: Path):
    if not directory.is_dir():
        raise RuntimeError(f"Docling artifacts directory does not exist: {directory}")
    files = [path for path in directory.rglob("*") if path.is_file()]
    if not files:
        raise RuntimeError(f"Docling artifacts directory is empty: {directory}")

    # docling-tools stores EasyOCR files below an easyocr/model directory in
    # current releases. Accept equivalent layouts so this remains compatible
    # with older releases, but fail loudly if no EasyOCR weight was prefetched.
    easyocr_root = directory / "easyocr"
    easyocr_weights = [
        path for path in easyocr_root.rglob("*.pth")
        if path.is_file()
    ] if easyocr_root.exists() else []
    if not easyocr_weights:
        raise RuntimeError(
            f"Docling artifacts exist at {directory}, but no EasyOCR .pth weights "
            "were found below docling/easyocr. Re-run the EasyOCR prefetch step."
        )
    print(f"OK Docling artifacts: {directory} ({len(files)} files)")
    print(f"OK EasyOCR English weights: {len(easyocr_weights)} .pth files")


def _download_retrieval_models(target: Path):
    """Download complete snapshots and materialize the app's flat directories.

    Loading a model and calling ``save_pretrained`` can omit repository files
    used by ``trust_remote_code`` (notably Jina's custom Python modules). A
    complete Hub snapshot preserves those files and also records refs/snapshots
    in the portable Hub cache. The application still receives the flat paths
    it already expects, so no consumer changes are required.
    """
    cache_root = target / "huggingface"
    hub_cache = cache_root / "hub"
    model_dirs = {
        "urchade/gliner_multi-v2.1": target / "gliner",
        "jinaai/jina-reranker-v3": target / "jina-reranker-v3",
        "nomic-ai/nomic-embed-text-v1.5": target / "nomic",
    }

    from huggingface_hub import snapshot_download

    for repo_id, destination in model_dirs.items():
        destination.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {repo_id}...")
        snapshot_dir = Path(
            snapshot_download(
                repo_id=repo_id,
                cache_dir=str(hub_cache),
                token=os.environ.get("HF_TOKEN") or None,
            )
        )
        # Snapshot directories may contain symlinks into blobs/. Copy the
        # resolved files into the flat portable directory so it remains usable
        # when moved between machines/filesystems, including Windows.
        shutil.copytree(snapshot_dir, destination, dirs_exist_ok=True)
        print(f"  saved complete snapshot to {destination}")

    # Verify the cache while the exact download process is still in scope.
    _verify_huggingface_cache(cache_root)


def _docling_tools_help() -> str:
    try:
        result = subprocess.run(
            ["docling-tools", "models", "download", "--help"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Could not inspect `docling-tools models download --help`; "
            "install a compatible Docling CLI first."
        ) from exc
    return f"{result.stdout}\n{result.stderr}"


def _docling_output_args(help_text: str, output_dir: Path) -> list[str]:
    # Help commonly renders aliases as `-o, --output PATH`; match option
    # tokens rather than relying on whitespace-separated columns.
    option_tokens = set(re.findall(r"(?<![\\w-])(--output|-o)(?=[, =\\t]|$)", help_text))
    if "--output" in option_tokens:
        return ["--output", str(output_dir)]
    if "-o" in option_tokens:
        return ["-o", str(output_dir)]
    raise RuntimeError(
        "This docling-tools version does not expose an output-directory option; "
        "use the project's pinned Docling 2.91.0 environment."
    )


def _docling_easyocr_args(help_text: str) -> list[str]:
    if "--easyocr-lang" not in help_text:
        raise RuntimeError(
            "This docling-tools version does not support --easyocr-lang; "
            "install the project's pinned Docling 2.91.0 environment."
        )
    return ["--easyocr-lang", "en"]


def _download_docling_models(target: Path):
    """Prefetch Docling's artifacts and EasyOCR English weights.

    Docling officially exposes this through docling-tools. Keeping this as a
    subprocess avoids importing Docling before its destination is configured
    and works with the CLI shipped alongside Docling 2.91.0.
    """
    docling_dir = target / "docling"
    docling_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DOCLING_ARTIFACTS_PATH"] = str(docling_dir)

    if shutil.which("docling-tools") is None:
        raise RuntimeError(
            "docling-tools was not found on PATH. Install docling==2.91.0 "
            "and its CLI, then rerun this script."
        )

    help_text = _docling_tools_help()
    output_args = _docling_output_args(help_text, docling_dir)

    # The base command downloads layout/table/code/formula and OCR artifacts.
    _run(["docling-tools", "models", "download", *output_args], env=env)

    # The base prefetch does not necessarily include EasyOCR recognition
    # weights. Request the exact language used by workers/text_workers.py.
    _run(
        [
            "docling-tools",
            "models",
            "download",
            "easyocr",
            *_docling_easyocr_args(help_text),
            *output_args,
        ],
        env=env,
    )


def verify_all(target: Path, *, include_docling: bool = True, include_retrieval: bool = True):
    if include_retrieval:
        _verify_flat_model("GLiNER", target / "gliner", ("gliner_config.json",))
        _verify_flat_model("Jina reranker", target / "jina-reranker-v3", ("config.json",))
        _verify_flat_model("Nomic", target / "nomic", ("modules.json",))
        _verify_huggingface_cache(target / "huggingface")
    if include_docling:
        _verify_docling(target / "docling")


def main() -> int:
    args = _parse_args()
    target = Path(args.target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    include_retrieval = not args.skip_retrieval
    include_docling = not args.skip_docling

    if args.verify_only:
        verify_all(target, include_docling=include_docling, include_retrieval=include_retrieval)
        print(f"Offline model verification passed: {target}")
        return 0

    _ensure_online()
    _configure_huggingface_cache(target)

    if include_retrieval:
        _download_retrieval_models(target)
    if include_docling:
        _download_docling_models(target)

    verify_all(target, include_docling=include_docling, include_retrieval=include_retrieval)
    print(f"All requested models are provisioned for offline use: {target}")
    print("Copy the complete directory, including the Hugging Face cache, to the offline worker machine.")
    print("Provision portable Java separately with: python build_portable_java.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
