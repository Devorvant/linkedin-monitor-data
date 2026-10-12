"""Stable Windows launcher: download the collector/parser and run it on the PC.

Place this file beside github_token.txt and saved_pages. MHTML is parsed on the
PC; JSON goes to GitHub for history and metrics. Code comes from a pinned commit.
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
    source = download("https://raw.githubusercontent.com/" + REPOSITORY + "/" + commit + "/scripts/profi_worker_unified.py")
    worker = cache / "profi_worker_unified_runtime.py"
    compile(source.decode("utf-8"), str(worker), "exec")
    temporary = worker.with_suffix(".tmp")
    temporary.write_bytes(source)
    temporary.replace(worker)
    print("Profi.ru: текущая версия " + commit[:8])
    command = [sys.executable, str(worker),
               "--root", str(ROOT), *sys.argv[1:]]
    return subprocess.run(command, cwd=str(ROOT), check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Не удалось запустить Profi.ru: " + str(error), file=sys.stderr)
        raise SystemExit(1)
