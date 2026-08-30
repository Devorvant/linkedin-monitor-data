from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
RUNTIME = SCRIPT_DIR / "action_executor_runtime.py"
EXECUTOR_URL = "https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/action_executor.py"


def download_executor() -> None:
    req = Request(EXECUTOR_URL, headers={"User-Agent": "linkedin-monitor-action-executor-entry"})
    with urlopen(req, timeout=30) as response:
        payload = response.read()
    source = payload.decode("utf-8")
    compile(source, str(RUNTIME), "exec")
    RUNTIME.write_bytes(payload)
    print(f"Downloaded executor: {RUNTIME}")


def main() -> int:
    download_executor()
    cmd = [sys.executable, str(RUNTIME), *sys.argv[1:]]
    completed = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
