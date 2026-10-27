"""
Window & Virtual Display Guardian
=================================
1. User Window Rescue:
   Ensures user apps (Chrome, Secom, Notion, Explorer, etc.) NEVER get hidden
   or lost inside the virtual monitor. Automatically moves them back to the Primary screen.
   Only allows Codex/headless background processes on the Virtual Display.

2. 5-Minute Inactivity Auto-Manager:
   Turns off / disconnects the Virtual Display when Codex has been idle for 5 minutes,
   freeing GPU resources and preventing real monitor quality degradation.
   Automatically turns it back on when Codex starts working.

3. Real Monitor Quality Protection:
   Ensures the Primary monitor never gets forced into low-res / degraded display modes.

Author: GeunHu Kim (delight0517)
License: MIT
"""

import sys
import os
import time
import subprocess
import ctypes
from ctypes import wintypes
import psutil
import win32gui
import win32api
import win32process
import win32con

# Codex and automation processes strictly permitted to use the Virtual Display
CODEX_EXCLUSIVE_KEYWORDS = [
    "codex", "mcp-server", "conhost", "playwright", 
    "puppeteer", "headless", "selenium"
]

# System overlays to never move
SYSTEM_TITLES = [
    "Program Manager", "소프트웨어 선생님 — 화면 안내", 
    "Windows Input Experience", "Taskbar", "Shell_TrayWnd"
]

IDLE_TIMEOUT_SECONDS = 300  # 5 minutes

class VirtualDisplayGuardian:
    def __init__(self):
        self.last_codex_active_time = time.time()
        self.virtual_display_active = True
        self.codex_pids = set()
        self.prev_codex_cpu = {}
        self.log_file = os.path.join(
            os.environ.get('APPDATA', 'C:\\'), 
            r'35CloserTimer\logs\virtual_display_guardian.log'
        )
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def log(self, msg: str):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] {msg}\n"
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass
        try:
            if sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
                sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
                sys.stdout.flush()
        except Exception:
            pass

    def get_monitor_layout(self):
        """Finds primary screen bounds and virtual display bounds."""
        primary_rect = (0, 0, 2048, 1152) # Default fallback
        vdd_rect = None
        
        try:
            monitors = win32api.EnumDisplayMonitors()
            for hmon, hdc, rect in monitors:
                info = win32api.GetMonitorInfo(hmon)
                is_primary = bool(info.get('Flags', 0) & 1)
                r = info['Monitor']
                if is_primary:
                    primary_rect = r
                else:
                    # Secondary monitor with X >= 2000 or small resolution is usually VDD
                    w = r[2] - r[0]
                    h = r[3] - r[1]
                    if r[0] >= 2000 or (w <= 1366 and h <= 768 and r[0] != 0):
                        vdd_rect = r
        except Exception:
            pass
            
        return primary_rect, vdd_rect

    def check_codex_activity(self) -> bool:
        """Returns True if Codex or AI worker processes are active or consuming CPU."""
        active = False
        current_pids = set()
        
        for p in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent']):
            try:
                name = (p.info['name'] or '').lower()
                cmd = ' '.join(p.info['cmdline'] or []).lower()
                
                is_codex = 'codex' in name or 'codex' in cmd
                if is_codex:
                    pid = p.info['pid']
                    current_pids.add(pid)
                    # Check CPU
                    cpu = p.info['cpu_percent'] or 0.0
                    if cpu > 0.5:
                        active = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
                
        # If any Codex process was active or new Codex process appeared
        if active or (current_pids - self.codex_pids):
            self.last_codex_active_time = time.time()
            self.codex_pids = current_pids
            return True
            
        self.codex_pids = current_pids
        idle_time = time.time() - self.last_codex_active_time
        return idle_time < IDLE_TIMEOUT_SECONDS

    def rescue_user_windows(self, primary_rect, vdd_rect):
        """Scans windows and immediately rescues any user windows from the virtual display."""
        if not vdd_rect:
            vdd_min_x = 2200
        else:
            vdd_min_x = vdd_rect[0]

        moved = []
        def enum_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            if w <= 30 or h <= 30:
                return True
                
            title = win32gui.GetWindowText(hwnd)
            if not title or any(st in title for st in SYSTEM_TITLES):
                return True
                
            # If the window is situated in the virtual monitor region
            if rect[0] >= vdd_min_x:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    pname = psutil.Process(pid).name().lower()
                except Exception:
                    pname = ""
                    
                is_codex_bg = any(k in title.lower() or k in pname for k in CODEX_EXCLUSIVE_KEYWORDS)
                if not is_codex_bg:
                    # Move to Primary screen safely
                    target_x = primary_rect[0] + 100
                    target_y = primary_rect[1] + 100
                    target_w = min(max(w, 900), primary_rect[2] - primary_rect[0] - 150)
                    target_h = min(max(h, 650), primary_rect[3] - primary_rect[1] - 150)
                    try:
                        win32gui.SetWindowPos(
                            hwnd, win32con.HWND_NOTOPMOST,
                            target_x, target_y, target_w, target_h,
                            win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE
                        )
                        moved.append((pname, title, (target_x, target_y, target_w, target_h)))
                    except Exception:
                        pass
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass
            
        for pname, title, new_pos in moved:
            self.log(f"[Rescue] Brought '{title}' ({pname}) from Virtual Display to Main Screen {new_pos[:2]}")

    def set_display_mode(self, mode: str):
        """Switches display mode: 'extend' (VDD on) or 'internal' (PC screen only, VDD off)."""
        try:
            cmd = "DisplaySwitch.exe /extend" if mode == "extend" else "DisplaySwitch.exe /internal"
            subprocess.run(cmd, shell=True, check=False)
            self.log(f"[DisplaySwitch] Switched to mode: {mode}")
        except Exception as e:
            self.log(f"[DisplaySwitch] Error: {e}")

    def run_loop(self):
        self.log("Virtual Display & Window Auto-Guardian Daemon started.")
        while True:
            try:
                primary_rect, vdd_rect = self.get_monitor_layout()
                
                # 1. Window Rescue Pass (Protect user apps)
                self.rescue_user_windows(primary_rect, vdd_rect)
                
                # 2. Check Codex Activity & 5-minute timeout
                is_active = self.check_codex_activity()
                idle_sec = time.time() - self.last_codex_active_time
                
                if is_active:
                    if not self.virtual_display_active:
                        self.log("[AutoManager] Codex activity detected! Activating Virtual Display (/extend)...")
                        self.set_display_mode("extend")
                        self.virtual_display_active = True
                else:
                    if self.virtual_display_active and idle_sec >= IDLE_TIMEOUT_SECONDS:
                        self.log(f"[AutoManager] Codex has been idle for {int(idle_sec)}s (>= 5 min). Disabling Virtual Display (/internal) to restore real monitor quality...")
                        self.rescue_user_windows(primary_rect, (0, 0, 9999, 9999)) # Evacuate any remaining windows
                        self.set_display_mode("internal")
                        self.virtual_display_active = False
            except Exception as e:
                self.log(f"Loop error: {e}")
                
            time.sleep(2.5)

if __name__ == "__main__":
    guardian = VirtualDisplayGuardian()
    if len(sys.argv) > 1 and sys.argv[1] == "--rescue-now":
        p_rect, v_rect = guardian.get_monitor_layout()
        guardian.rescue_user_windows(p_rect, v_rect)
        print("Manual rescue complete.")
    else:
        guardian.run_loop()
