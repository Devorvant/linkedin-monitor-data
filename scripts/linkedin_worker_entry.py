from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
SAVED_PAGES = ROOT / "saved_pages"
RUNTIME_WORKER = SCRIPT_DIR / "linkedin_worker_unified_runtime.py"
WORKER_URL = "https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/linkedin_worker_unified.py"
PAGE_EXTENSIONS = {".mhtml", ".mht", ".html", ".htm"}


def cleanup_saved_pages() -> tuple[int, int]:
    """Delete only stale captured LinkedIn page files before a new cycle."""
    SAVED_PAGES.mkdir(parents=True, exist_ok=True)
    removed = 0
    failed = 0

    for path in SAVED_PAGES.iterdir():
        if not path.is_file() or path.suffix.lower() not in PAGE_EXTENSIONS:
            continue
        try:
            path.unlink()
            removed += 1
            print(f"Cleanup: removed {path.name}")
        except OSError as exc:
            failed += 1
            print(f"Cleanup warning: could not remove {path.name}: {exc}")

    print(f"Cleanup saved_pages: removed={removed}, failed={failed}")
    return removed, failed


def download_worker() -> None:
    req = Request(WORKER_URL, headers={"User-Agent": "linkedin-monitor-worker-entry"})
    with urlopen(req, timeout=30) as response:
        payload = response.read()

    # Fail before execution if the downloaded worker is not valid Python.
    source = payload.decode("utf-8")
    compile(source, str(RUNTIME_WORKER), "exec")
    RUNTIME_WORKER.write_bytes(payload)
    print(f"Downloaded worker: {RUNTIME_WORKER}")


def main() -> int:
    cleanup_saved_pages()
    download_worker()

    cmd = [sys.executable, str(RUNTIME_WORKER), *sys.argv[1:]]
    print("Starting LinkedIn worker...")
    completed = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
