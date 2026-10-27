"""
AI Monitor Guardian CLI
=======================
Control and inspect the AI Virtual Display & Window Rescue System.
Author: GeunHu Kim (delight0517)
License: MIT
"""

import sys
import os

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
import time
import subprocess
import winreg
import psutil
from window_guardian import VirtualDisplayGuardian

def show_status():
    print("=" * 60)
    print("🖥️  AI Monitor Guardian Status")
    print("=" * 60)
    
    guardian = VirtualDisplayGuardian()
    p_rect, v_rect = guardian.get_monitor_layout()
    print(f"• Primary Monitor Bounds: {p_rect}")
    print(f"• Virtual Display Bounds: {v_rect or 'Not detected / Inactive'}")
    
    # Check daemon running
    running_pids = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmd = ' '.join(p.info['cmdline'] or []).lower()
            if 'window_guardian.py' in cmd and p.info['pid'] != os.getpid():
                running_pids.append(p.info['pid'])
        except Exception:
            pass
            
    if running_pids:
        print(f"• Guardian Daemon: RUNNING (PID: {', '.join(map(str, running_pids))})")
    else:
        print("• Guardian Daemon: STOPPED")
        
    # Check Codex activity
    is_active = guardian.check_codex_activity()
    idle_sec = int(time.time() - guardian.last_codex_active_time)
    print(f"• Codex Activity: {'ACTIVE' if is_active else 'IDLE'} (Idle for {idle_sec}s, timeout: 300s)")
    
    # Autostart registry status
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run")
        val, _ = winreg.QueryValueEx(key, "WindowGuardian")
        print(f"• Autostart (HKCU): ENABLED ({val})")
    except Exception:
        print("• Autostart (HKCU): DISABLED")
        
    print("=" * 60)

def rescue_now():
    print("🔍 Scanning for lost user windows on Virtual Display...")
    guardian = VirtualDisplayGuardian()
    p_rect, v_rect = guardian.get_monitor_layout()
    guardian.rescue_user_windows(p_rect, v_rect)
    print("✅ Rescue operation complete! All user windows restored to Primary Screen.")

def enable_vdd():
    print("📺 Activating Virtual Display (/extend)...")
    subprocess.run("DisplaySwitch.exe /extend", shell=True)
    print("✅ Virtual Display active.")

def disable_vdd():
    print("🔌 Disconnecting Virtual Display (/internal)...")
    guardian = VirtualDisplayGuardian()
    p_rect, _ = guardian.get_monitor_layout()
    guardian.rescue_user_windows(p_rect, (0, 0, 9999, 9999))
    subprocess.run("DisplaySwitch.exe /internal", shell=True)
    print("✅ Virtual Display disconnected. Real physical screen restored to 100% focus.")

def install_autostart():
    py_dir = os.path.dirname(os.path.abspath(__file__))
    guardian_py = os.path.join(py_dir, "window_guardian.py")
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    cmd = f'"{pythonw}" "{guardian_py}"'
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, "WindowGuardian", 0, winreg.REG_SZ, cmd)
    print(f"✅ Registered autostart in HKCU Run:\n   {cmd}")
    
    # Start process now
    subprocess.Popen([pythonw, guardian_py])
    print("🚀 Background daemon started.")

def uninstall_autostart():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, "WindowGuardian")
        print("✅ Removed WindowGuardian from HKCU Run.")
    except Exception as e:
        print(f"⚠️ Could not remove key: {e}")
        
    for p in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmd = ' '.join(p.info['cmdline'] or [])
            if 'window_guardian.py' in cmd:
                p.terminate()
                print(f"🛑 Terminated daemon process (PID: {p.info['pid']})")
        except Exception:
            pass

def print_help():
    print("""
AI Monitor Guardian - Commands:
  status     Show current monitor layout, Codex activity, and daemon state
  rescue     Immediately rescue any lost user windows back to Primary Screen
  on         Manually activate Virtual Display (/extend)
  off        Manually disconnect Virtual Display (/internal)
  install    Install autostart background daemon (runs silently on boot)
  uninstall  Remove autostart and stop the daemon
""")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)
        
    cmd = sys.argv[1].lower()
    if cmd == "status":
        show_status()
    elif cmd in ("rescue", "rescue-now"):
        rescue_now()
    elif cmd in ("on", "enable"):
        enable_vdd()
    elif cmd in ("off", "disable"):
        disable_vdd()
    elif cmd == "install":
        install_autostart()
    elif cmd == "uninstall":
        uninstall_autostart()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
