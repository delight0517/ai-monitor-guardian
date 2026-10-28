"""Native Windows job cleanup without UI, user profiles or provider calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
