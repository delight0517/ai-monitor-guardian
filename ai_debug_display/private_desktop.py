"""Bounded native UI execution on a fresh, never-switched Win32 desktop.

This isolates GUI focus, not files, credentials, network or untrusted code.
The caller owns target authorization and captures only its launched windows.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
import uuid


class PrivateDesktopSession:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("private_desktop_requires_windows")
        import win32api, win32con, win32event, win32job, win32process
        self.api, self.con, self.event = win32api, win32con, win32event
        self.jobs, self.process = win32job, win32process
        self.name = "AiDebug_" + uuid.uuid4().hex
        self.pid = None
        self._process = self._thread = self._job = self._desktop = None
        self.closed = False
        self.u = ctypes.windll.user32
        self.u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          wintypes.DWORD, ctypes.c_void_p]
        self.u.CreateDesktopW.restype = wintypes.HANDLE
        self.u.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.u.CloseDesktop.restype = wintypes.BOOL
        # Deliberately omit DESKTOP_SWITCHDESKTOP. Never switch user input.
        self._desktop = self.u.CreateDesktopW(self.name, None, None, 0, 0x00FF, None)
        if not self._desktop:
            raise ctypes.WinError()
        try:
            self._job = self.jobs.CreateJobObject(None, "")
            limits = self.jobs.QueryInformationJobObject(
                self._job, self.jobs.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= self.jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.jobs.SetInformationJobObject(
                self._job, self.jobs.JobObjectExtendedLimitInformation, limits)
        except Exception:
            self.close()
            raise

    def launch(self, executable: str, args: list[str], *, cwd: str,
               env: dict[str, str] | None = None) -> int:
        if self.closed or self.pid is not None:
            raise RuntimeError("session_closed_or_already_launched")
        exe, folder = Path(executable), Path(cwd)
        if not exe.is_absolute() or not exe.is_file() or not folder.is_absolute() or not folder.is_dir():
            raise ValueError("absolute_existing_executable_and_cwd_required")
        startup = self.process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\" + self.name
        command = subprocess.list2cmdline([str(exe), *args])
        self._process, self._thread, self.pid, _ = self.process.CreateProcess(
            str(exe), command, None, None, False,
            self.con.CREATE_NO_WINDOW | self.con.CREATE_SUSPENDED,
            env, str(folder), startup)
        try:
            # Suspension prevents children escaping the owning cleanup job.
            self.jobs.AssignProcessToJobObject(self._job, self._process)
            self.process.ResumeThread(self._thread)
        except Exception:
            self.process.TerminateProcess(self._process, 1)
            self.close()
            raise
        return self.pid

    def poll(self) -> int | None:
        if self._process is None:
            return None
        if self.event.WaitForSingleObject(self._process, 0) == self.event.WAIT_TIMEOUT:
            return None
        return self.process.GetExitCodeProcess(self._process)

    def close(self, timeout_seconds: float = 3.0) -> None:
        if self.closed:
            return
        if self._job is not None:
            info = self.jobs.QueryInformationJobObject(self._job, self.jobs.JobObjectBasicAccountingInformation)
            if info["ActiveProcesses"]:
                deadline = time.monotonic() + max(0.0, timeout_seconds)
                wait_handles = []
                try:
                    for pid in self.jobs.QueryInformationJobObject(self._job, self.jobs.JobObjectBasicProcessIdList):
                        try:
                            wait_handles.append(self.api.OpenProcess(0x00100000, False, pid))
                        except Exception:
                            pass  # Process may have completed while enumerating.
                    self.jobs.TerminateJobObject(self._job, 1)
                    for handle in wait_handles:
                        remaining = max(0, int((deadline - time.monotonic()) * 1000))
                        if self.event.WaitForSingleObject(handle, remaining) == self.event.WAIT_TIMEOUT:
                            raise RuntimeError("owned_process_exit_not_signalled")
                    while time.monotonic() < deadline:
                        info = self.jobs.QueryInformationJobObject(self._job, self.jobs.JobObjectBasicAccountingInformation)
                        if not info["ActiveProcesses"]:
                            break
                        time.sleep(0.02)
                finally:
                    for handle in wait_handles:
                        self.api.CloseHandle(handle)
                if info["ActiveProcesses"]:
                    raise RuntimeError("owned_process_cleanup_not_finished")
            self.api.CloseHandle(self._job)
            self._job = None
        for field in ("_process", "_thread"):
            handle = getattr(self, field)
            if handle is not None:
                self.api.CloseHandle(handle)
                setattr(self, field, None)
        if self._desktop is not None:
            if not self.u.CloseDesktop(self._desktop):
                raise RuntimeError("private_desktop_close_failed")
            self._desktop = None
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
