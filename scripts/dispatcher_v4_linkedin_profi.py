"""Bounded desktop stage execution and independent Windows shutdown timer."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def seconds_left(finish_at):
    return float("inf") if finish_at is None else (finish_at - datetime.now(finish_at.tzinfo)).total_seconds()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def arm_windows_shutdown(finish_at, logger=print):
    """OS-owned timer continues even when Python exits or crashes."""
    if finish_at is None or sys.platform != "win32":
        return False
    seconds = max(0, math.ceil(seconds_left(finish_at)))
    result = subprocess.run(["shutdown", "/s", "/f", "/t", str(seconds),
                             "/c", "Automation FINISH: LinkedIn and Profi.ru"],
                            capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    logger(f"Windows FINISH timer: {finish_at.isoformat()}, exit={result.returncode}")
    return result.returncode == 0


def cancel_owned_shutdown(logger=print):
    if sys.platform == "win32":
        result = subprocess.run(["shutdown", "/a"], capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        logger(f"Cancel own FINISH timer: exit={result.returncode}")


class WindowsJob:
    """Own only this stage and its descendants; closing the handle kills leftovers."""
    def __init__(self, pid, logger=print):
        self.handle = None
        if sys.platform != "win32":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        handle = kernel.CreateJobObjectW(None, None)
        process = None
        try:
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "Job setup failed")
            process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not kernel.AssignProcessToJobObject(handle, process):
                raise OSError(ctypes.get_last_error(), "Job assignment failed")
            self.handle, self.kernel = handle, kernel
        except OSError as error:
            if handle:
                kernel.CloseHandle(handle)
            logger(f"Job guard unavailable; PID tree fallback: {error}")
        finally:
            if process:
                kernel.CloseHandle(process)

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 124)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def stop_tree(proc, job, logger=print):
    if job is not None:
        try:
            job.terminate()
        except Exception as error:
            logger(f"Job termination warning: {error}")
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger(f"PID {proc.pid} stop warning: {error}")
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        logger(f"PID {proc.pid} did not stop; next GUI stage is blocked")
        return False
    return True


class StageRunner:
    def __init__(self, root, finish_at=None, logger=print, env=None):
        self.root = Path(root)
        self.finish_at = finish_at
        self.logger = logger
        self.env = dict(os.environ if env is None else env)
        if finish_at:
            self.env["DISPATCHER_FINISH_AT"] = finish_at.isoformat()
        self.env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

    def run(self, command, name, timeout_seconds, log_path=None):
        started = datetime.now().isoformat(timespec="seconds")
        result = {"stage": name, "started_at": started, "state": "starting", "exit_code": None,
                  "safe_to_continue": True, "finish_at": self.finish_at.isoformat() if self.finish_at else None}
        if seconds_left(self.finish_at) <= 0:
            return {**result, "state": "skipped_finish", "exit_code": 124}
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        logfile = Path(log_path) if log_path else logs / (name + "_latest.log")
        result["log_path"] = str(logfile)
        proc = None
        job = None
        try:
            with logfile.open("w", encoding="utf-8", errors="replace") as stream:
                proc = subprocess.Popen(command, cwd=str(self.root), stdout=stream,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self.env,
                    start_new_session=sys.platform != "win32")
                job = WindowsJob(proc.pid, self.logger)
                result["pid"] = proc.pid
                self.logger(f"{name}: started PID {proc.pid}")
                deadline = time.monotonic() + max(1, float(timeout_seconds))
                while proc.poll() is None:
                    if seconds_left(self.finish_at) <= 0 or time.monotonic() >= deadline:
                        reason = "FINISH" if seconds_left(self.finish_at) <= 0 else "stage timeout"
                        self.logger(f"{name}: {reason}; stopping PID tree {proc.pid}")
                        stopped = stop_tree(proc, job, self.logger)
                        result.update(state="timeout", exit_code=124, timeout_reason=reason,
                                      safe_to_continue=stopped)
                        break
                    time.sleep(min(1, max(0.01, seconds_left(self.finish_at))))
                else:
                    result.update(state="ok" if proc.returncode == 0 else "error", exit_code=proc.returncode)
        except KeyboardInterrupt:
            result.update(state="cancelled", exit_code=130)
            if proc is not None:
                result["safe_to_continue"] = stop_tree(proc, job, self.logger)
            raise
        except Exception as error:
            result.update(state="exception", exit_code=1, error=str(error))
            if proc is not None:
                result["safe_to_continue"] = stop_tree(proc, job, self.logger)
        finally:
            if job is not None:
                try:
                    job.close()
                except Exception as error:
                    result.update(state="exception", exit_code=1, safe_to_continue=False,
                                  error=f"Process guard close failed: {error}")
            result["finished_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                atomic_json(logs / (name + "_status.json"), result)
            except OSError as error:
                self.logger(f"{name}: local diagnostic write failed: {error}")
        self.logger(f"{name}: {result['state']}, exit={result['exit_code']}")
        return result


def pause_before_stage(seconds, finish_at=None, min_after=0, logger=print):
    seconds = max(0, float(seconds))
    if seconds_left(finish_at) < seconds + max(0, float(min_after)):
        logger("Next stage skipped: insufficient time before FINISH")
        return False
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if seconds_left(finish_at) <= 0:
            return False
        time.sleep(min(1, max(0.01, end - time.monotonic())))
    return True


# ---- Dispatcher configuration, diagnostics and lifecycle ----

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
LOG_DIR = ROOT / "logs"

LOCAL_CONTROL_FALLBACK = ROOT / "control.json"
TOKEN_CANDIDATES = [
    ROOT / "github_token.txt",
    ROOT / "reports" / "github_token.txt",
]

DEFAULT_CONTROL_URL = (
    "https://raw.githubusercontent.com/"
    "Devorvant/linkedin-monitor-data/main/control.json"
)

CONFIG_POLL_SECONDS = 30
DOWNLOAD_TIMEOUT = 20


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "dispatcher_current.log").open("a", encoding="utf-8", errors="replace") as file:
            file.write(line + "\n")
    except OSError as error:
        print(f"Diagnostic file unavailable: {error}", flush=True)


def read_token() -> str:
    env_token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    ).strip()

    if env_token:
        return env_token

    for path in TOKEN_CANDIDATES:
        if path.exists():
            token = path.read_text(encoding="utf-8-sig").strip()
            if token:
                return token

    return ""


def http_get_text(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "linkedin-dispatcher/1.0",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8-sig")


def load_control() -> dict:
    try:
        raw = http_get_text(DEFAULT_CONTROL_URL)
        cfg = json.loads(raw)
        if not isinstance(cfg, dict):
            raise ValueError("control.json должен быть объектом.")
        if cfg.get("enabled", True):
            resolve_times(datetime.now(), cfg)
        try:
            atomic_json(LOCAL_CONTROL_FALLBACK, cfg)
        except OSError as error:
            log(f"Не удалось сохранить fallback control.json: {error}")
        log("control.json загружен с GitHub.")
        return cfg
    except Exception as exc:
        log(f"Не удалось скачать control.json: {exc}")

    if LOCAL_CONTROL_FALLBACK.exists():
        cfg = json.loads(
            LOCAL_CONTROL_FALLBACK.read_text(encoding="utf-8-sig")
        )
        log("Использую локальный fallback control.json.")
        return cfg

    raise RuntimeError(
        "control.json недоступен и локального fallback-файла нет."
    )


def parse_hhmm(value: str, name: str) -> tuple[int, int]:
    nums = re.findall(r"\d+", str(value))
    if len(nums) < 2:
        raise ValueError(f"Неверное время {name}: {value!r}")

    hour = int(nums[0])
    minute = int(nums[1])

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Неверное время {name}: {hour:02d}:{minute:02d}")

    return hour, minute


def resolve_times(now: datetime, cfg: dict) -> tuple[datetime, datetime]:
    schedule = cfg.get("schedule", cfg)
    sh, sm = parse_hhmm(schedule["start_time"], "start_time")
    fh, fm = parse_hhmm(schedule["finish_time"], "finish_time")
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    finish = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    if (fh, fm) <= (sh, sm):
        if now < finish:
            start -= timedelta(days=1)
        else:
            finish += timedelta(days=1)
    if now < start:
        return start, finish
    if now < finish:
        if cfg.get("run_if_started_late", True):
            return now, finish
        return start + timedelta(days=1), finish + timedelta(days=1)
    # A missed morning cycle must not become a 24-hour wait after an error.
    # Normal boot waits for today's START; after today's FINISH we stop/off.
    return start, finish


def wait_until(target: datetime, label: str) -> None:
    last_minute = None

    while True:
        now = datetime.now()
        left = (target - now).total_seconds()

        if left <= 0:
            log(f"{label}: время наступило.")
            return

        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key != last_minute:
            last_minute = minute_key
            log(f"Ожидание {label}: {target:%Y-%m-%d %H:%M}")

        time.sleep(min(CONFIG_POLL_SECONDS, max(1.0, left)))


def download_worker(cfg: dict) -> Path:
    script_url = cfg["script_url"]
    script_name = cfg.get("script_name", "linkedin_worker.py")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / script_name
    temp = target.with_suffix(target.suffix + ".download")

    log(f"Скачиваю worker: {script_url}")
    raw = http_get_text(script_url)

    temp.write_text(raw, encoding="utf-8")

    # Простая проверка, что это хотя бы валидный Python.
    compile(raw, str(temp), "exec")

    temp.replace(target)
    log(f"Worker сохранён: {target}")
    return target


def run_worker(worker_path: Path, finish_at=None, cfg=None, shutdown_armed=False) -> int:
    current_python = Path(sys.executable)
    candidate = current_python.with_name("python.exe") if current_python.name.lower() == "pythonw.exe" else current_python
    python_exe = candidate if candidate.exists() else current_python
    env = dict(os.environ)
    env["DISPATCHER_MANAGED"] = "1"
    env["DISPATCHER_SHUTDOWN_ARMED"] = "1" if shutdown_armed else "0"
    runner = StageRunner(ROOT, finish_at, log, env)
    result = runner.run([str(python_exe), str(worker_path), "-a"], "chain",
                        (cfg or {}).get("worker_timeout_seconds", 1200),
                        LOG_DIR / "worker_latest.log")
    return int(result["exit_code"])


def github_api_request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
):
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "linkedin-dispatcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def upload_text_to_github(
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
) -> None:
    token = read_token()
    if not token:
        log("GitHub token не найден. Лог на GitHub не отправлен.")
        return

    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"

    sha = None
    try:
        info = github_api_request(
            "GET",
            api_url + f"?ref={branch}",
            token,
        )
        sha = info.get("sha")
    except HTTPError as exc:
        if exc.code != 404:
            raise

    payload = {
        "message": message,
        "content": base64.b64encode(
            content.encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }

    if sha:
        payload["sha"] = sha

    github_api_request("PUT", api_url, token, payload)
    log(f"Отправлен GitHub log/status: {path}")


def upload_logs(cfg: dict, worker_code: int, started: datetime, ended: datetime) -> None:
    repo = cfg.get("github_repo", "Devorvant/linkedin-monitor-data")
    branch = cfg.get("github_branch", "main")

    dispatcher_log = LOG_DIR / "dispatcher_current.log"
    worker_log = LOG_DIR / "worker_latest.log"

    combined = []
    combined.append("=== DISPATCHER ===")
    if dispatcher_log.exists():
        combined.append(
            dispatcher_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )

    combined.append("\n=== WORKER ===")
    if worker_log.exists():
        combined.append(
            worker_log.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )

    for name in ("linkedin_latest.log", "actions_latest.log", "profi_latest.log"):
        path = LOG_DIR / name
        if path.exists():
            combined.append("\n=== " + name + " ===")
            combined.append(path.read_text(encoding="utf-8", errors="replace"))
    chain = {}
    path = LOG_DIR / "chain_status.json"
    if path.exists():
        try:
            chain = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            log(f"Chain status unavailable: {error}")
    combined_text = "\n".join(combined)

    status = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": ended.isoformat(timespec="seconds"),
        "worker_exit_code": worker_code,
        "success": worker_code == 0,
        "computer": "pc2",
        "chain": chain,
    }

    try:
        upload_text_to_github(
            repo,
            branch,
            cfg.get("remote_log_path", "logs/dispatcher_latest.log"),
            combined_text,
            f"Update dispatcher log {ended:%Y-%m-%d %H:%M:%S}",
        )
    except Exception as exc:
        log(f"Ошибка отправки текстового лога: {exc}")

    try:
        upload_text_to_github(
            repo,
            branch,
            cfg.get("remote_status_path", "dispatcher_status.json"),
            json.dumps(status, ensure_ascii=False, indent=2),
            f"Update dispatcher status {ended:%Y-%m-%d %H:%M:%S}",
        )
    except Exception as exc:
        log(f"Ошибка отправки status JSON: {exc}")


def shutdown_windows() -> None:
    log("FINISH: выключаем Windows.")
    if sys.platform != "win32":
        log("Выключение Windows недоступно на этой платформе.")
        return
    try:
        result = subprocess.run(["shutdown", "/s", "/f", "/t", "0"],
                                check=False, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            # FINISH has arrived: replace a remaining timer with immediate shutdown.
            log(f"Immediate shutdown exit={result.returncode}; retry at FINISH.")
            subprocess.run(["shutdown", "/a"], check=False, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            retry = subprocess.run(["shutdown", "/s", "/f", "/t", "0"], check=False, timeout=5,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log(f"Shutdown retry exit={retry.returncode}")
    except Exception as error:
        log(f"Windows shutdown command failed: {error}")


def publish_diagnostics(cfg, code, started, ended, finish_at):
    """Network reporting is a child stage, bounded by the same FINISH."""
    atomic_json(LOG_DIR / "dispatcher_status.json", {
        "started_at": started.isoformat(), "finished_at": ended.isoformat(),
        "worker_exit_code": code, "success": code == 0,
        "finish_at": finish_at.isoformat() if finish_at else None,
    })
    remaining = seconds_left(finish_at)
    if remaining <= 5:
        log("FINISH close: local diagnostics retained; remote upload skipped.")
        return
    request_path = LOG_DIR / "diagnostic_upload.json"
    atomic_json(request_path, {"cfg": cfg, "code": code,
                              "started": started.isoformat(), "ended": ended.isoformat()})
    StageRunner(ROOT, finish_at, log).run(
        [sys.executable, str(Path(__file__).resolve()), "--upload-diagnostics", str(request_path)],
        "diagnostic_upload", min(60, remaining - 2))


def main() -> None:
    cfg = None
    finish_at = None
    shutdown_armed = False
    user_cancelled = False
    started = datetime.now()
    code = 1
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("chain_status.json", "worker_latest.log", "linkedin_latest.log",
                     "actions_latest.log", "profi_latest.log"):
            try:
                (LOG_DIR / name).unlink(missing_ok=True)
            except OSError as error:
                log(f"Old diagnostic cleanup warning: {error}")
        (LOG_DIR / "dispatcher_current.log").write_text("", encoding="utf-8")
        log("Dispatcher v4: LinkedIn -> pause -> Profi.ru.")
        cfg = load_control()
        if not cfg.get("enabled", True):
            log("Automation disabled.")
            if cfg.get("shutdown_when_disabled", True):
                shutdown_windows()
            return
        start_at, finish_at = resolve_times(datetime.now(), cfg)
        should_shutdown = cfg.get("shutdown_after_finish", True)
        # Arm before any START wait, download, worker or diagnostic upload.
        if should_shutdown:
            try:
                shutdown_armed = arm_windows_shutdown(finish_at, log)
            except Exception as error:
                log(f"Windows FINISH timer setup failed: {error}")
            if not shutdown_armed:
                log("WARNING: OS shutdown timer unavailable; dispatcher watchdog remains active.")
        if datetime.now() < start_at:
            wait_until(start_at, "START")
        if datetime.now() >= finish_at:
            log("Today's FINISH has passed. Worker skipped; no rollover to tomorrow.")
            return
        refreshed = load_control()
        cfg = refreshed
        if not cfg.get("enabled", True):
            log("Automation disabled before START.")
            if cfg.get("shutdown_when_disabled", True):
                shutdown_windows()
            elif shutdown_armed:
                cancel_owned_shutdown(log)
                shutdown_armed = False
            return
        if not cfg.get("shutdown_after_finish", True) and shutdown_armed:
            cancel_owned_shutdown(log)
            shutdown_armed = False
        # Finish for this cycle stays the originally calculated deadline.
        worker_path = download_worker(cfg)
        started = datetime.now()
        code = run_worker(worker_path, finish_at if cfg.get("shutdown_after_finish", True) else None,
                          cfg, shutdown_armed)
        ended = datetime.now()
        publish_diagnostics(cfg, code, started, ended, finish_at)
    except KeyboardInterrupt:
        user_cancelled = True
        log("Остановлено пользователем. Отменяем только свой таймер выключения.")
        if shutdown_armed:
            cancel_owned_shutdown(log)
        return
    except Exception as error:
        log(f"DISPATCHER ERROR: {error}")
        try:
            if cfg and cfg.get("enabled", True):
                publish_diagnostics(cfg, code, started, datetime.now(), finish_at)
        except Exception as diagnostic_error:
            log(f"Dispatcher diagnostics failed: {diagnostic_error}")
        # Do not recalculate FINISH after a failure or accidentally pick tomorrow.
    finally:
        # KeyboardInterrupt returns through finally: distinguish explicit cancel.
        cancelled = user_cancelled
        if cfg and cfg.get("enabled", True) and cfg.get("shutdown_after_finish", True) and not cancelled:
            if finish_at is not None:
                try:
                    if datetime.now() < finish_at:
                        wait_until(finish_at, "FINISH")
                except KeyboardInterrupt:
                    log("FINISH wait cancelled by user.")
                    if shutdown_armed:
                        cancel_owned_shutdown(log)
                    return
                except Exception as error:
                    log(f"FINISH wait failed: {error}; OS timer armed={shutdown_armed}")
                    if shutdown_armed:
                        return
                shutdown_windows()
            else:
                log("FINISH could not be resolved; fail-safe shutdown.")
                shutdown_windows()
        elif cfg is None and not cancelled:
            log("Control unavailable; fail-safe shutdown.")
            shutdown_windows()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--upload-diagnostics":
        request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        upload_logs(request["cfg"], request["code"],
                    datetime.fromisoformat(request["started"]),
                    datetime.fromisoformat(request["ended"]))
    else:
        main()
