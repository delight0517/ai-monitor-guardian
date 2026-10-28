"""Bounded native UI execution on a fresh, never-switched Win32 desktop.

This isolates GUI focus, not files, credentials, network or untrusted code.
The caller owns target authorization and captures only its launched windows.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import math
import sys
import threading
import time
import uuid
from functools import wraps


@dataclass(frozen=True)
class OwnedWindow:
    """A top-level window observed on this session's private desktop."""

    hwnd: int
    pid: int


@dataclass(frozen=True)
class WindowCapture:
    """In-memory PrintWindow pixels; format is 32-bit BGRX, bottom-up rows."""

    hwnd: int
    pid: int
    width: int
    height: int
    pixel_format: str
    pixels: bytes


_CAPTURE_HELPER = r'''import ctypes,struct,sys
from ctypes import wintypes
import win32gui,win32ui
hwnd,width,height=map(int,sys.argv[1:4])
window_dc=win32gui.GetWindowDC(hwnd)
if not window_dc: raise SystemExit(2)
source_dc=memory_dc=bitmap=None
try:
 source_dc=win32ui.CreateDCFromHandle(window_dc)
 memory_dc=source_dc.CreateCompatibleDC()
 bitmap=win32ui.CreateBitmap()
 bitmap.CreateCompatibleBitmap(source_dc,width,height)
 old_bitmap=memory_dc.SelectObject(bitmap)
 user32=ctypes.windll.user32
 user32.PrintWindow.argtypes=[wintypes.HWND,wintypes.HDC,wintypes.UINT]
 user32.PrintWindow.restype=wintypes.BOOL
 if not user32.PrintWindow(hwnd,memory_dc.GetSafeHdc(),2): raise SystemExit(3)
 info=bitmap.GetInfo()
 if info['bmBitsPixel']!=32: raise SystemExit(4)
 pixels=bitmap.GetBitmapBits(True)
 if len(pixels)!=width*height*4: raise SystemExit(5)
 sys.stdout.buffer.write(struct.pack('<II',width,height))
 sys.stdout.buffer.write(pixels)
 sys.stdout.buffer.flush()
 memory_dc.SelectObject(old_bitmap)
finally:
 if bitmap is not None: win32gui.DeleteObject(bitmap.GetHandle())
 if memory_dc is not None: memory_dc.DeleteDC()
 if source_dc is not None: source_dc.DeleteDC()
 win32gui.ReleaseDC(hwnd,window_dc)
'''
_CAPTURE_WORKER_TIMEOUT_SECONDS = 3.0


def _serialized_lifecycle(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)
    return call


class PrivateDesktopSession:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("private_desktop_requires_windows")
        import win32api, win32con, win32event, win32job, win32process
        self.api, self.con, self.event = win32api, win32con, win32event
        self.jobs, self.process = win32job, win32process
        self.name = "AiDebug_" + uuid.uuid4().hex
        self._lifecycle_lock = threading.RLock()
        self.pid = None
        self._process = self._thread = self._job = self._desktop = None
        self.closed = False
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        self.u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          wintypes.DWORD, ctypes.c_void_p]
        self.u.CreateDesktopW.restype = wintypes.HANDLE
        self.u.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.u.CloseDesktop.restype = wintypes.BOOL
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.u.EnumDesktopWindows.restype = wintypes.BOOL
        self.k.PeekNamedPipe.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        self.k.PeekNamedPipe.restype = wintypes.BOOL
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

    @_serialized_lifecycle
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

    def _job_pids(self) -> set[int]:
        if self.closed or self._job is None:
            raise RuntimeError("private_desktop_session_closed")
        return set(self.jobs.QueryInformationJobObject(
            self._job, self.jobs.JobObjectBasicProcessIdList))

    @_serialized_lifecycle
    def list_owned_windows(self) -> tuple[OwnedWindow, ...]:
        """Enumerate only top-level HWNDs on this desktop that belong to its Job."""
        if self._desktop is None or self.pid is None:
            raise RuntimeError("private_desktop_not_launched")
        owned_pids = self._job_pids()
        windows: list[OwnedWindow] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _parameter):
            pid = wintypes.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in owned_pids:
                windows.append(OwnedWindow(int(hwnd), int(pid.value)))
            return True

        callback = callback_type(visit)
        if not self.u.EnumDesktopWindows(self._desktop, callback, 0):
            error = ctypes.get_last_error()
            if error:
                raise ctypes.WinError(error)
        return tuple(windows)

    def wait_for_owned_window(self, timeout_seconds: float = 8.0,
                              poll_interval: float = 0.05) -> tuple[OwnedWindow, ...]:
        """Require an owned top-level window on the private desktop, or fail closed.

        A single-instance app that forwards to a pre-existing user process will not
        produce an HWND on this desktop and therefore cannot be marked QA-ready.
        """
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 60):
            raise ValueError("window_timeout_invalid")
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
                or not math.isfinite(poll_interval) or not 0.01 <= poll_interval <= 1):
            raise ValueError("window_poll_interval_invalid")
        if self.pid is None or self.closed:
            raise RuntimeError("private_desktop_not_running")
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                windows = self.list_owned_windows()
                if windows:
                    return windows
                if self.poll() is not None:
                    raise RuntimeError("launched_process_exited_without_private_desktop_window")
                if time.monotonic() >= deadline:
                    raise RuntimeError("private_desktop_window_timeout")
                time.sleep(poll_interval)
        except Exception:
            # Do not leave a launch that failed ownership verification running.
            self.close()
            raise

    @_serialized_lifecycle
    def capture_owned_window(self, hwnd: int) -> WindowCapture:
        """Capture a verified HWND in a killable helper without switching desktops."""
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            raise ValueError("window_handle_invalid")
        match = next((window for window in self.list_owned_windows() if window.hwnd == hwnd), None)
        if match is None:
            raise RuntimeError("window_not_owned_on_private_desktop")

        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0 or width > 8192 or height > 8192 or width * height > 16_777_216:
            raise RuntimeError("window_capture_dimensions_invalid")

        pixels = self._capture_in_bounded_helper(hwnd, width, height)
        return WindowCapture(hwnd, match.pid, width, height, "BGRX_BOTTOM_UP", pixels)

    def _capture_in_bounded_helper(self, hwnd: int, width: int, height: int) -> bytes:
        import win32file
        import win32pipe
        import win32security

        read_pipe = write_pipe = helper_job = helper_process = helper_thread = None
        output = bytearray()
        expected_size = 8 + width * height * 4
        deadline = time.monotonic() + _CAPTURE_WORKER_TIMEOUT_SECONDS
        try:
            security = win32security.SECURITY_ATTRIBUTES()
            read_pipe, write_pipe = win32pipe.CreatePipe(security, 0)
            self.api.SetHandleInformation(read_pipe, 1, 0)

            helper_job = self.jobs.CreateJobObject(None, "")
            limits = self.jobs.QueryInformationJobObject(
                helper_job, self.jobs.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= self.jobs.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.jobs.SetInformationJobObject(
                helper_job, self.jobs.JobObjectExtendedLimitInformation, limits)

            startup = self.process.STARTUPINFO()
            startup.dwFlags |= self.process.STARTF_USESTDHANDLES
            startup.lpDesktop = "winsta0\\" + self.name
            startup.hStdInput = write_pipe
            startup.hStdOutput = write_pipe
            startup.hStdError = write_pipe
            command = subprocess.list2cmdline([
                sys.executable, "-c", _CAPTURE_HELPER,
                str(hwnd), str(width), str(height),
            ])
            helper_process, helper_thread, _pid, _tid = self.process.CreateProcess(
                sys.executable, command, None, None, True,
                self.con.CREATE_NO_WINDOW | self.con.CREATE_SUSPENDED,
                None, os.getcwd(), startup,
            )
            try:
                self.jobs.AssignProcessToJobObject(helper_job, helper_process)
                self.process.ResumeThread(helper_thread)
            except Exception:
                self.process.TerminateProcess(helper_process, 1)
                raise
            self.api.CloseHandle(write_pipe)
            write_pipe = None

            available = wintypes.DWORD()
            while True:
                if not self.k.PeekNamedPipe(
                        ctypes.c_void_p(int(read_pipe)), None, 0, None, ctypes.byref(available), None):
                    error = ctypes.get_last_error()
                    if error:
                        raise ctypes.WinError(error)
                    available.value = 0
                if available.value:
                    read_error, chunk = win32file.ReadFile(read_pipe, min(available.value, 64 * 1024))
                    if read_error:
                        raise OSError(read_error, "capture_pipe_read_failed")
                    output.extend(chunk)
                    if len(output) > expected_size:
                        raise RuntimeError("window_capture_buffer_size_invalid")
                state = self.event.WaitForSingleObject(helper_process, 0)
                if state != self.event.WAIT_TIMEOUT and not available.value:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("window_capture_timeout")
                time.sleep(0.01)

            if self.process.GetExitCodeProcess(helper_process) != 0:
                raise RuntimeError("window_capture_unsupported_or_failed")
            if len(output) != expected_size:
                raise RuntimeError("window_capture_buffer_size_invalid")
            out_width, out_height = __import__("struct").unpack("<II", output[:8])
            if (out_width, out_height) != (width, height):
                raise RuntimeError("window_capture_dimensions_changed")
            return bytes(output[8:])
        finally:
            if helper_job is not None:
                try:
                    if helper_process is not None and self.event.WaitForSingleObject(helper_process, 0) == self.event.WAIT_TIMEOUT:
                        self.jobs.TerminateJobObject(helper_job, 1)
                        self.event.WaitForSingleObject(helper_process, 1000)
                finally:
                    self.api.CloseHandle(helper_job)
            for handle in (helper_thread, helper_process, read_pipe, write_pipe):
                if handle is not None:
                    self.api.CloseHandle(handle)

    @_serialized_lifecycle
    def poll(self) -> int | None:
        if self._process is None:
            return None
        if self.event.WaitForSingleObject(self._process, 0) == self.event.WAIT_TIMEOUT:
            return None
        return self.process.GetExitCodeProcess(self._process)

    @_serialized_lifecycle
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
