"""Stable Windows launcher: download the current capture code and run it.

Place this file beside github_token.txt and saved_pages. Order fields are parsed
in GitHub Actions. All downloaded modules come from one pinned repository commit.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

REPOSITORY = "Devorvant/linkedin-monitor-data"
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR


def download(url: str) -> bytes:
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "profi-monitor-entry", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=30) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("Не удалось скачать текущую версию.")


def main() -> int:
    commit = json.loads(download("https://api.github.com/repos/" + REPOSITORY + "/git/ref/heads/main"))["object"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Некорректная версия кода GitHub.")
    cache = ROOT / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    modules = []
    for remote, local in (("profi_capture_transport.py", "profi_capture_transport.py"),
                          ("profi_capture_worker.py", "profi_capture_worker_runtime.py")):
        source = download("https://raw.githubusercontent.com/" + REPOSITORY + "/" + commit + "/scripts/" + remote)
        compile(source.decode("utf-8"), local, "exec")
        modules.append((cache / local, source))
    # Validate both modules before replacing either cached source.
    for path, source in modules:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(source)
        temporary.replace(path)
    print("Profi.ru: текущая версия " + commit[:8])
    command = [sys.executable, str(cache / "profi_capture_worker_runtime.py"),
               "--root", str(ROOT), *sys.argv[1:]]
    return subprocess.run(command, cwd=str(ROOT), check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Не удалось запустить Profi.ru: " + str(error), file=sys.stderr)
        raise SystemExit(1)
