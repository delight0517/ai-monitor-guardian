import json,os,sys,tempfile,time,unittest
from pathlib import Path
from ai_debug_display.private_desktop import PrivateDesktopSession

@unittest.skipUnless(os.name=='nt','native Windows fixture')
class ReconnectNativeTests(unittest.TestCase):
    def test_owned_private_window_roundtrip_without_user_focus_or_topology_change(self):
        import win32api,win32gui,win32process
        def topology():
            rows=[]
            for i in range(64):
                try:d=win32api.EnumDisplayDevices(None,i)
                except Exception:break
                if d.StateFlags&1:
                    m=win32api.EnumDisplaySettings(d.DeviceName,-1)
                    rows.append((d.DeviceID,d.StateFlags,m.Position_x,m.Position_y,m.PelsWidth,m.PelsHeight))
            return rows
        before=topology();qa_foreground=False
        with tempfile.TemporaryDirectory(prefix='private-reconnect-test-') as td:
            report_path=Path(td)/'result.json'
            with PrivateDesktopSession() as session:
                pid=session.launch(sys.executable,[str(Path(__file__).with_name('private_recovery_window_fixture.py')),str(report_path)],cwd=str(Path(__file__).resolve().parents[1]))
                deadline=time.monotonic()+12
                while session.poll() is None and time.monotonic()<deadline:
                    foreground=win32gui.GetForegroundWindow()
                    if foreground and win32process.GetWindowThreadProcessId(foreground)[1]==pid:qa_foreground=True
                    time.sleep(.02)
                self.assertEqual(session.poll(),0)
                result=json.loads(report_path.read_text(encoding='utf-8'))
                self.assertEqual(result['pid'],pid)
                self.assertTrue(result['verified']);self.assertTrue(result['restored_to_saved'])
                self.assertEqual(result['off_state'],'physical_unavailable')
                self.assertEqual(result['reconnect_state'],'waiting_for_stable_physical')
                self.assertFalse(qa_foreground);self.assertEqual(topology(),before)
            evidence=Path(__file__).resolve().parents[1]/'evidence'
            evidence.mkdir(exist_ok=True)
            result.update({'qa_owned_foreground_seen':qa_foreground,'real_monitor_topology_unchanged':topology()==before,'desktop_closed':session.closed,'hardware_power_cycle_tested':False})
            (evidence/'reconnect-native-latest.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

if __name__=='__main__':unittest.main()
