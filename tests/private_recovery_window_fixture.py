"""Owned private-desktop window; injected monitor geometry, no hardware changes."""
import json,os,sys,tkinter as tk
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import win32api,win32con,win32gui,win32process
from ai_debug_display.reconnect_recovery import RecoveryOutput,WindowIdentity,RecoveryWindow,ReconnectRecovery,apply_one_recovery

root=tk.Tk();root.title('Owned reconnect recovery fixture');root.geometry('500x300+100+100')
tk.Label(root,text='Private reconnect test window').pack()
def exercise():
    root.update_idletasks();hwnd=win32gui.GetAncestor(root.winfo_id(),2)
    started=win32process.GetProcessTimes(win32api.GetCurrentProcess())['CreationTime'].isoformat()
    identity=WindowIdentity(hwnd,os.getpid(),started,sys.executable)
    physical=RecoveryOutput('fixture-physical','physical',(0,0,1000,700),primary=True)
    virtual=RecoveryOutput('fixture-virtual','virtual',(-2000,0,0,700))
    outputs=[physical,virtual]
    def read_window(expected):
        if expected!=identity or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1]!=os.getpid():return None
        return RecoveryWindow(identity,win32gui.GetWindowRect(hwnd))
    def move(expected,rect):
        if read_window(expected) is None:raise RuntimeError('owned_window_identity_changed')
        win32gui.SetWindowPos(hwnd,0,rect[0],rect[1],rect[2]-rect[0],rect[3]-rect[1],win32con.SWP_NOACTIVATE|win32con.SWP_NOZORDER)
    policy=ReconnectRecovery(stable_seconds=2)
    original=read_window(identity)
    policy.remember(original,outputs,user_confirmed=True)
    move(identity,(-1500,100,-1000,400))
    stranded=read_window(identity)
    off=policy.poll([virtual],[stranded],now=0)
    waiting=policy.poll(outputs,[stranded],now=1)
    plan=policy.poll(outputs,[stranded],now=3)['plans'][0]
    result=apply_one_recovery(plan,policy=policy,read_outputs=lambda:outputs,read_window=read_window,
                             read_context=lambda:('private-fixture',win32gui.GetForegroundWindow()),
                             move_nonactivating=move,user_confirmed=True)
    result.update({'monitor_geometry':'injected_private_desktop_fixture_only','pid':os.getpid(),'hwnd':hwnd,
                   'off_state':off['state'],'reconnect_state':waiting['state'],'restored_to_saved':read_window(identity).rect==original.rect})
    Path(sys.argv[1]).write_text(json.dumps(result),encoding='utf-8');root.destroy()
root.after(150,exercise);root.after(10000,root.destroy);root.mainloop()
