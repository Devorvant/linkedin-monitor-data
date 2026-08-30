from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
SAVED_PAGES = ROOT / "saved_pages"
RUNTIME_WORKER = SCRIPT_DIR / "linkedin_worker_unified_runtime.py"
RUNTIME_EXECUTOR = SCRIPT_DIR / "action_executor_runtime.py"
WORKER_URL = "https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/linkedin_worker_unified.py"
EXECUTOR_URL = "https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/action_executor.py"
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


def download_python(url: str, destination: Path, user_agent: str) -> None:
    req = Request(url, headers={"User-Agent": user_agent})
    with urlopen(req, timeout=30) as response:
        payload = response.read()

    source = payload.decode("utf-8")
    compile(source, str(destination), "exec")
    destination.write_bytes(payload)
    print(f"Downloaded: {destination}")


def detect_executor_device() -> str:
    # Existing installations: laptop is on C:, autonomous desktop is on H:.
    drive = ROOT.drive.upper()
    if drive == "H:":
        return "2"
    return "1"


def executor_secret_available() -> bool:
    if os.environ.get("APPROVAL_SECRET", "").strip():
        return True
    return (ROOT / "approval_secret.txt").exists()


def run_executor() -> int:
    if not executor_secret_available():
        print("Action executor skipped: APPROVAL_SECRET/approval_secret.txt not found.")
        return 0

    print("Main LinkedIn worker completed successfully.")
    print("Waiting 7 seconds before approved-action stage...")
    time.sleep(7)

    download_python(
        EXECUTOR_URL,
        RUNTIME_EXECUTOR,
        "linkedin-monitor-action-executor-entry",
    )

    device = detect_executor_device()
    print(f"Starting action executor (device={device})...")
    completed = subprocess.run(
        [sys.executable, str(RUNTIME_EXECUTOR), "--device", device],
        cwd=str(ROOT),
        check=False,
    )
    print(f"Action executor exit code: {completed.returncode}")
    return int(completed.returncode)


def main() -> int:
    cleanup_saved_pages()
    download_python(
        WORKER_URL,
        RUNTIME_WORKER,
        "linkedin-monitor-worker-entry",
    )

    cmd = [sys.executable, str(RUNTIME_WORKER), *sys.argv[1:]]
    print("Starting LinkedIn worker...")
    completed = subprocess.run(cmd, cwd=str(ROOT), check=False)
    worker_code = int(completed.returncode)

    if worker_code != 0:
        print(f"LinkedIn worker failed with exit code {worker_code}; action executor will not run.")
        return worker_code

    executor_code = run_executor()
    if executor_code != 0:
        print(f"Action executor failed with exit code {executor_code}.")
        return executor_code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
