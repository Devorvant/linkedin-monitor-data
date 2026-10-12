from __future__ import annotations

import importlib.util
import json
import math
import os
from datetime import datetime, timedelta
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name.lower() == "cache" else SCRIPT_DIR
SAVED_PAGES = ROOT / "saved_pages"
LOG_DIR = ROOT / "logs"
RUNTIME_WORKER = SCRIPT_DIR / "linkedin_worker_unified_runtime.py"
RUNTIME_EXECUTOR = SCRIPT_DIR / "action_executor_runtime.py"
RUNTIME_PROFI = SCRIPT_DIR / "profi_worker_entry_runtime.py"
RUNTIME_HELPER = SCRIPT_DIR / "automation_runtime.py"
BASE = "https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/"
WORKER_URL = BASE + "scripts/linkedin_worker_unified.py"
EXECUTOR_URL = BASE + "scripts/action_executor.py"
PROFI_URL = BASE + "scripts/profi_worker_entry.py"
HELPER_URL = BASE + "scripts/automation_runtime.py"
CONTROL_URL = BASE + "control.json"
PAGE_EXTENSIONS = {".mhtml", ".mht", ".html", ".htm"}


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def write_chain_status(status):
    status["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = LOG_DIR / "chain_status.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as error:
        log(f"Diagnostic write error: {error}")


def load_chain_config():
    try:
        with urlopen(Request(CONTROL_URL, headers={"User-Agent": "linkedin-profi-entry", "Cache-Control": "no-cache"}), timeout=20) as response:
            cfg = json.loads(response.read().decode("utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError("control.json is not an object")
        return cfg
    except Exception as error:
        log(f"Control download failed: {error}")
        path = ROOT / "control.json"
        if path.exists():
            try:
                cfg = json.loads(path.read_text(encoding="utf-8-sig"))
                return cfg if isinstance(cfg, dict) else {}
            except (OSError, ValueError) as local_error:
                log(f"Local control failed: {local_error}")
        return {}


def run_deadline(cfg):
    value = os.environ.get("DISPATCHER_FINISH_AT")
    if value:
        return datetime.fromisoformat(value)
    # Older dispatchers do not pass the deadline. Only infer it on PC2 during
    # the scheduled active window; manual laptop/out-of-hours runs stay manual.
    if detect_executor_device() != "2" or not cfg.get("shutdown_after_finish", True):
        return None
    schedule = cfg.get("schedule", {})
    try:
        sh, sm = (int(x) for x in schedule["start_time"].split(":")[:2])
        fh, fm = (int(x) for x in schedule["finish_time"].split(":")[:2])
        now = datetime.now()
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        finish = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
        if finish <= start:
            if now < finish:
                start -= timedelta(days=1)
            else:
                finish += timedelta(days=1)
        return finish if start <= now < finish else None
    except (KeyError, ValueError, TypeError):
        return None


def arm_early_shutdown(finish_at):
    if finish_at is None or sys.platform != "win32":
        return False
    try:
        now = datetime.now(finish_at.tzinfo)
        seconds = max(0, math.ceil((finish_at - now).total_seconds()))
        result = subprocess.run(["shutdown", "/s", "/f", "/t", str(seconds),
                                 "/c", "Automation FINISH: LinkedIn and Profi.ru"],
                                capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"Independent Windows FINISH timer: exit={result.returncode}")
        return result.returncode == 0
    except Exception as error:
        log(f"Windows timer setup failed: {error}")
        return False


def cleanup_saved_pages() -> tuple[int, int]:
    """Delete only stale captured LinkedIn page files before a new cycle."""
    SAVED_PAGES.mkdir(parents=True, exist_ok=True)
    removed = 0
    failed = 0

    for path in SAVED_PAGES.iterdir():
        if (not path.is_file() or path.suffix.lower() not in PAGE_EXTENSIONS
                or not path.name.lower().startswith("linkedin_")):
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


def execute_chain(runtime, cfg, finish_at, status):
    options = cfg.get("profi_after_linkedin", {})
    if isinstance(options, bool):
        options = {"enabled": options}
    if not isinstance(options, dict):
        options = {}
    env = dict(os.environ)
    runner = runtime.StageRunner(ROOT, finish_at, log, env)

    def stage(name, url, path, args, timeout):
        status["phase"] = name
        write_chain_status(status)
        try:
            if runtime.seconds_left(finish_at) <= 0:
                result = {"stage": name, "state": "skipped_finish", "exit_code": 124, "safe_to_continue": True}
            else:
                download_python(url, path, "linkedin-profi-stage-entry")
                result = runner.run([sys.executable, str(path), *args], name, timeout)
        except Exception as error:
            result = {"stage": name, "state": "download_error", "exit_code": 1,
                      "error": str(error), "safe_to_continue": True}
            log(f"{name}: {error}")
        status["stages"].append(result)
        write_chain_status(status)
        return result

    linkedin = stage("linkedin", WORKER_URL, RUNTIME_WORKER, sys.argv[1:],
                     cfg.get("linkedin_timeout_seconds", 900))
    safe = linkedin.get("safe_to_continue", True)
    if linkedin["exit_code"] == 0 and executor_secret_available():
        if runtime.pause_before_stage(7, finish_at, logger=log):
            actions = stage("actions", EXECUTOR_URL, RUNTIME_EXECUTOR,
                            ["--device", detect_executor_device()], cfg.get("actions_timeout_seconds", 180))
            safe = safe and actions.get("safe_to_continue", True)
        else:
            status["stages"].append({"stage": "actions", "state": "skipped_finish", "exit_code": 124})
    else:
        reason = "LinkedIn failed" if linkedin["exit_code"] else "No configured approval secret"
        status["stages"].append({"stage": "actions", "state": "skipped", "exit_code": 0, "reason": reason})

    pc2 = os.environ.get("DISPATCHER_MANAGED") == "1" or detect_executor_device() == "2"
    if not options.get("enabled", True) or not pc2:
        status["stages"].append({"stage": "profi", "state": "disabled", "exit_code": 0})
    elif not safe:
        status["stages"].append({"stage": "profi", "state": "blocked_process_alive", "exit_code": 1})
    else:
        status["phase"] = "pause_before_profi"
        status["profi_delay_seconds"] = options.get("delay_seconds", 60)
        write_chain_status(status)
        log(f"LinkedIn stages finished. Pause before Profi.ru: {status['profi_delay_seconds']} seconds")
        if runtime.pause_before_stage(status["profi_delay_seconds"], finish_at,
                                      options.get("min_remaining_seconds", 120), log):
            stage("profi", PROFI_URL, RUNTIME_PROFI, ["-a"], options.get("timeout_seconds", 300))
        else:
            status["stages"].append({"stage": "profi", "state": "skipped_finish", "exit_code": 124,
                                     "reason": "Insufficient time for pause and Profi before FINISH"})
    failures = [x for x in status["stages"] if x.get("exit_code", 0) != 0]
    return failures[0]["exit_code"] if failures else 0


def main() -> int:
    status = {"started_at": datetime.now().isoformat(timespec="seconds"),
              "phase": "initializing", "stages": [], "success": False}
    timer_owned = False
    runtime = None
    try:
        cfg = load_chain_config()
        finish_at = run_deadline(cfg)
        status["finish_at"] = finish_at.isoformat() if finish_at else None
        # Old dispatchers have no independent timer; arm it before network work.
        if os.environ.get("DISPATCHER_SHUTDOWN_ARMED") != "1":
            timer_owned = arm_early_shutdown(finish_at)
        write_chain_status(status)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("linkedin", "actions", "profi"):
            for suffix in ("_latest.log", "_status.json"):
                path = LOG_DIR / (name + suffix)
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    log(f"Old diagnostic cleanup warning: {error}")
        cleanup_saved_pages()
        download_python(HELPER_URL, RUNTIME_HELPER, "linkedin-profi-runtime")
        spec = importlib.util.spec_from_file_location("automation_runtime", RUNTIME_HELPER)
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        code = execute_chain(runtime, cfg, finish_at, status)
        status.update(phase="finished", exit_code=code, success=code == 0)
        return code
    except KeyboardInterrupt:
        status.update(phase="cancelled", exit_code=130)
        if timer_owned:
            try:
                if runtime is not None:
                    runtime.cancel_owned_shutdown(log)
                elif sys.platform == "win32":
                    subprocess.run(["shutdown", "/a"], capture_output=True, timeout=5)
            except Exception as error:
                log(f"Timer cancellation failed: {error}")
        return 130
    except Exception as error:
        status.update(phase="exception", error=str(error), exit_code=1)
        log(f"Chain error: {error}")
        return 1
    finally:
        status["finished_at"] = datetime.now().isoformat(timespec="seconds")
        write_chain_status(status)


if __name__ == "__main__":
    raise SystemExit(main())
