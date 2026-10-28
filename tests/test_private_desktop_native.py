"""Native Windows job cleanup without UI, user profiles or provider calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_debug_display import private_desktop
from ai_debug_display.private_desktop import PrivateDesktopSession


@unittest.skipUnless(os.name == "nt", "Windows native lifecycle")
class NativePrivateDesktopTests(unittest.TestCase):
    def test_invalid_launch_releases_desktop_on_context_exit(self):
        session = PrivateDesktopSession()
        with session:
            with self.assertRaises(ValueError):
                session.launch("relative.exe", [], cwd="relative")
        self.assertTrue(session.closed)
        session.close()  # idempotent teardown

    def test_close_terminates_owned_parent_and_grandchild_and_fences_relaunch(self):
        import win32api, win32con, win32process
        with tempfile.TemporaryDirectory(prefix="private-desktop-cleanup-") as td:
            marker = Path(td) / "owned.json"
            code = (
                "import subprocess,sys,time,json,os;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
                f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));"
                "time.sleep(120)"
            )
            with PrivateDesktopSession() as session:
                pid = session.launch(sys.executable, ["-c", code], cwd=td)
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "owned child did not create its marker")
                owned = json.loads(marker.read_text())
                self.assertEqual(owned[0], pid)
                with self.assertRaises(RuntimeError):
                    session.launch(sys.executable, ["-c", "pass"], cwd=td)
            self.assertTrue(session.closed)
            for owned_pid in owned:
                try:
                    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, owned_pid)
                except Exception:
                    continue
                try:
                    self.assertNotEqual(win32process.GetExitCodeProcess(handle), 259)
                finally:
                    win32api.CloseHandle(handle)

    def test_only_reports_and_captures_a_window_owned_by_the_private_desktop_job(self):
        with tempfile.TemporaryDirectory(prefix="private-desktop-window-qa-") as td:
            marker = Path(td) / "window.json"
            code = (
                "import json,os,tkinter as tk;from pathlib import Path;import win32con,win32gui;"
                "root=tk.Tk();root.title('Private desktop capture fixture');root.geometry('320x220+40+40');"
                "root.configure(bg='#ff0000');tk.Label(root,text='synthetic QA only',bg='#ff0000',fg='#ffffff').pack();"
                "root.update_idletasks();hwnd=win32gui.GetAncestor(root.winfo_id(),win32con.GA_ROOT);"
                f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(),hwnd=hwnd)));"
                "root.after(20000,root.destroy);root.mainloop()"
            )
            with PrivateDesktopSession() as session:
                session.launch(sys.executable, ["-c", code], cwd=td)
                deadline = time.monotonic() + 8
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "isolated fixture failed to create its private window")
                fixture = json.loads(marker.read_text(encoding="utf-8"))
                windows = session.wait_for_owned_window(timeout_seconds=3)
                self.assertIn((fixture["hwnd"], fixture["pid"]), {(w.hwnd, w.pid) for w in windows})
                capture = session.capture_owned_window(fixture["hwnd"])
                self.assertEqual(capture.pixel_format, "BGRX_BOTTOM_UP")
                self.assertEqual(len(capture.pixels), capture.width * capture.height * 4)
                red_pixels = sum(
                    1 for offset in range(0, len(capture.pixels), 4)
                    if capture.pixels[offset] < 60
                    and capture.pixels[offset + 1] < 60
                    and capture.pixels[offset + 2] > 180
                )
                sample = [tuple(capture.pixels[i:i + 4]) for i in range(0, min(len(capture.pixels), 400), 4)]
                self.assertGreater(red_pixels, 1000, f"bounded WM_PRINT missed red fixture; sample={sample[:8]}")
                helper_marker = Path(td) / "capture-helper-pid.txt"
                blocking_helper = (
                    "import os,time;from pathlib import Path;"
                    f"Path({str(helper_marker)!r}).write_text(str(os.getpid()));time.sleep(30)"
                )
                with patch.object(private_desktop, "_CAPTURE_HELPER", blocking_helper), \
                     patch.object(private_desktop, "_CAPTURE_WORKER_TIMEOUT_SECONDS", 1.0):
                    with self.assertRaisesRegex(RuntimeError, "window_capture_timeout"):
                        session.capture_owned_window(fixture["hwnd"])
                self.assertTrue(helper_marker.exists(), "capture timeout fixture did not start")
                helper_pid = int(helper_marker.read_text(encoding="utf-8"))
                import win32api, win32con, win32process
                try:
                    helper_handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, helper_pid)
                except Exception:
                    helper_handle = None  # No live process remains for that PID.
                if helper_handle is not None:
                    try:
                        self.assertNotEqual(win32process.GetExitCodeProcess(helper_handle), 259)
                    finally:
                        win32api.CloseHandle(helper_handle)
                self.assertIsNone(session.poll(), "capture timeout must leave the QA target running")

    def test_windowless_forwarder_candidate_fails_closed_and_closes_owned_job(self):
        with PrivateDesktopSession() as session:
            session.launch(sys.executable, ["-c", "pass"], cwd=os.getcwd())
            with self.assertRaisesRegex(RuntimeError, "exited_without_private_desktop_window"):
                session.wait_for_owned_window(timeout_seconds=3)
            self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
