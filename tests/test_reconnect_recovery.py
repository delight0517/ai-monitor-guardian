from dataclasses import replace
import unittest
from ai_debug_display.reconnect_recovery import RecoveryOutput,WindowIdentity,RecoveryWindow,ReconnectRecovery,apply_one_recovery

PHYSICAL=RecoveryOutput('physical-edid-A','physical',(0,0,2560,1400),primary=True,display_name='DISPLAY2')
VIRTUAL=RecoveryOutput('root-mttvdd-A','virtual',(-2560,0,0,1440),display_name='DISPLAY15')
IDENTITY=WindowIdentity(1234,4321,'2026-10-28T01:00:00Z',r'C:\Apps\ChatApp.exe')
ORIGINAL=RecoveryWindow(IDENTITY,(200,100,1100,800))
STRANDED=RecoveryWindow(IDENTITY,(-2000,100,-1100,800))

class ReconnectTests(unittest.TestCase):
    def setup_policy(self):
        policy=ReconnectRecovery(stable_seconds=2)
        policy.remember(ORIGINAL,[PHYSICAL,VIRTUAL],user_confirmed=True)
        return policy

    def test_monitors_off_ai_remains_private_and_no_window_moves_are_planned(self):
        p=self.setup_policy()
        result=p.poll([replace(VIRTUAL,primary=True)], [STRANDED],now=1)
        self.assertEqual(result['state'],'physical_unavailable')
        self.assertEqual(result['ai_backend'],'private_desktop')
        self.assertFalse(result['preview_allowed']);self.assertEqual(result['plans'],[])

    def test_restore_after_stable_reconnect_ignores_display_number_changes(self):
        p=self.setup_policy();p.poll([replace(VIRTUAL,primary=True)],[STRANDED],now=1)
        restored=replace(PHYSICAL,display_name='DISPLAY9')
        self.assertEqual(p.poll([restored,VIRTUAL],[STRANDED],now=2)['plans'],[])
        plans=p.poll([restored,VIRTUAL],[STRANDED],now=4)['plans']
        self.assertEqual(len(plans),1)
        self.assertEqual(plans[0].destination_rect,ORIGINAL.rect)
        self.assertEqual(plans[0].destination_output_id,PHYSICAL.stable_id)

    def test_hardware_state_unusable_even_if_windows_keeps_output_attached(self):
        p=self.setup_policy()
        p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=0)
        result=p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=10,physical_usable=False)
        self.assertEqual(result['plans'],[])
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=11)['state'],'waiting_for_stable_physical')

    def test_reused_pid_or_hwnd_does_not_adopt_a_new_window(self):
        p=self.setup_policy();p.poll([PHYSICAL,VIRTUAL],[],now=0)
        for identity in (replace(IDENTITY,process_started='2026-10-28T02:00:00Z'),replace(IDENTITY,pid=5555),replace(IDENTITY,executable_path=r'C:\Apps\other.exe')):
            self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[replace(STRANDED,identity=identity)],now=5)['plans'],[])

    def test_unrelated_and_already_physical_windows_are_not_moved(self):
        p=self.setup_policy();p.poll([PHYSICAL,VIRTUAL],[],now=0)
        unrelated=replace(STRANDED,identity=replace(IDENTITY,hwnd=9999))
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[ORIGINAL,unrelated],now=5)['plans'],[])

    def test_missing_original_or_duplicate_physical_identity_fails_closed(self):
        p=self.setup_policy();different=replace(PHYSICAL,stable_id='other-monitor')
        p.poll([different,VIRTUAL],[],now=0)
        self.assertEqual(p.poll([different,VIRTUAL],[STRANDED],now=5)['plans'],[])
        with self.assertRaises(ValueError):p.poll([PHYSICAL,PHYSICAL,VIRTUAL],[STRANDED],now=10)

    def test_changed_resolution_fits_saved_window_inside_real_work_area(self):
        p=self.setup_policy();small=replace(PHYSICAL,work_rect=(0,0,800,550))
        p.poll([small,VIRTUAL],[],now=0)
        plan=p.poll([small,VIRTUAL],[STRANDED],now=3)['plans'][0]
        self.assertEqual(plan.destination_rect,(0,0,800,550))

    def test_fresh_native_preflight_rejects_app_or_topology_drift(self):
        p=self.setup_policy();p.poll([PHYSICAL,VIRTUAL],[],now=0)
        plan=p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=3)['plans'][0]
        self.assertTrue(p.revalidate(plan,[PHYSICAL,VIRTUAL],STRANDED))
        self.assertFalse(p.revalidate(plan,[PHYSICAL,VIRTUAL],replace(STRANDED,rect=(-1900,0,-1000,700))))
        self.assertFalse(p.revalidate(plan,[replace(PHYSICAL,work_rect=(0,0,1920,1000)),VIRTUAL],STRANDED))

    def test_confirmation_and_minimized_state_are_preserved(self):
        p=ReconnectRecovery()
        with self.assertRaises(ValueError):p.remember(ORIGINAL,[PHYSICAL,VIRTUAL],user_confirmed=False)
        p=self.setup_policy();p.poll([PHYSICAL,VIRTUAL],[],now=0)
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[replace(STRANDED,minimized=True)],now=3)['plans'],[])

    def test_one_shot_rechecks_native_identity_before_any_move(self):
        p=self.setup_policy();p.poll([PHYSICAL,VIRTUAL],[],now=0)
        plan=p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=3)['plans'][0]
        moved=[]
        result=apply_one_recovery(plan,policy=p,read_outputs=lambda:[PHYSICAL,VIRTUAL],read_window=lambda _:replace(STRANDED,identity=replace(IDENTITY,pid=999)),read_context=lambda:('foreground','desktop'),move_nonactivating=lambda *args:moved.append(args),user_confirmed=True)
        self.assertFalse(result['attempted']);self.assertEqual(moved,[])

if __name__=='__main__':unittest.main()
