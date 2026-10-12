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
