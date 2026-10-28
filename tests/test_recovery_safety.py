from dataclasses import replace
import json,math,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from ai_debug_display.placement_journal import PlacementJournal
from ai_debug_display.reconnect_recovery import RecoveryOutput,RecoveryWindow,WindowIdentity,ReconnectRecovery,apply_one_recovery
from test_reconnect_recovery import PHYSICAL,VIRTUAL,IDENTITY,ORIGINAL,STRANDED

def ready():
    p=ReconnectRecovery(stable_seconds=2);p.remember(ORIGINAL,[PHYSICAL,VIRTUAL],user_confirmed=True)
    p.poll([PHYSICAL,VIRTUAL],[],now=0)
    return p,p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=3)['plans'][0]

class PlacementJournalTests(unittest.TestCase):
    def test_bound_journal_persists_remember_and_failed_write_keeps_active_records(self):
        with tempfile.TemporaryDirectory() as td:
            journal=PlacementJournal(Path(td)/'placements.json')
            p=ReconnectRecovery(journal=journal)
            p.remember(ORIGINAL,[PHYSICAL,VIRTUAL],user_confirmed=True)
            resumed=ReconnectRecovery(journal=journal)
            self.assertEqual(resumed.export_records(),p.export_records())
            before=p.export_records();before_bytes=journal.path.read_bytes()
            with patch('ai_debug_display.placement_journal.os.replace',side_effect=OSError('fixture')):
                with self.assertRaises(OSError):p.remember(replace(ORIGINAL,rect=(300,100,1200,800)),[PHYSICAL,VIRTUAL],user_confirmed=True)
            self.assertEqual(p.export_records(),before);self.assertEqual(journal.path.read_bytes(),before_bytes)

    def test_restart_roundtrip_and_unchanged_reload_do_not_rewrite_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            p,_=ready();journal=PlacementJournal(Path(td)/'placements.json')
            journal.save(p.export_records());before=journal.path.read_bytes()
            resumed=ReconnectRecovery();self.assertEqual(resumed.reload(journal),1)
            self.assertEqual(resumed.export_records(),p.export_records())
            self.assertEqual(resumed.reload(journal),1)
            self.assertEqual(journal.path.read_bytes(),before)

    def test_truncated_nonfinite_duplicate_and_partial_records_keep_active_set(self):
        with tempfile.TemporaryDirectory() as td:
            p,_=ready();journal=PlacementJournal(Path(td)/'placements.json');journal.save(p.export_records())
            good=json.loads(journal.path.read_text());original=p.export_records()
            malformed=[good['records'][0],{'identity':{}}]
            payloads=['{',json.dumps({'schema_version':True,'records':[]}),
                      '{"schema_version":1,"schema_version":1,"records":[]}',
                      json.dumps({'schema_version':1,'records':malformed}),
                      json.dumps({'schema_version':1,'records':[good['records'][0],good['records'][0]]}),
                      json.dumps(good).replace('200, 100, 1100, 800','NaN, 100, 1100, 800')]
            for payload in payloads:
                with self.subTest(payload=payload[:40]):
                    journal.path.write_text(payload)
                    with self.assertRaises(ValueError):p.reload(journal)
                    self.assertEqual(p.export_records(),original)

    def test_invalid_save_or_atomic_replace_failure_preserves_last_file(self):
        with tempfile.TemporaryDirectory() as td:
            p,_=ready();journal=PlacementJournal(Path(td)/'placements.json');journal.save(p.export_records())
            before=journal.path.read_bytes()
            with self.assertRaises(ValueError):journal.save([object()])
            with patch('ai_debug_display.placement_journal.os.replace',side_effect=OSError('fixture')):
                with self.assertRaises(OSError):journal.save(p.export_records())
            self.assertEqual(journal.path.read_bytes(),before)
            self.assertEqual(list(Path(td).glob('*.tmp')),[])

    def test_wrong_journal_field_types_never_load(self):
        with tempfile.TemporaryDirectory() as td:
            p,_=ready();journal=PlacementJournal(Path(td)/'placements.json');journal.save(p.export_records())
            good=json.loads(journal.path.read_text())
            for field,value in [('hwnd',True),('pid','4321'),('process_started','not-a-time'),('executable_path','relative.exe')]:
                candidate=json.loads(json.dumps(good));candidate['records'][0]['identity'][field]=value
                journal.path.write_text(json.dumps(candidate))
                with self.assertRaises(ValueError):journal.load()
            for rect in ([True,0,10,20],[0,0,math.inf,20],[0,0,0,20],[0,0,10.0,20]):
                candidate=json.loads(json.dumps(good));candidate['records'][0]['rect']=rect
                journal.path.write_text(json.dumps(candidate))
                with self.assertRaises(ValueError):journal.load()

class RecoverySafetyTests(unittest.TestCase):
    def test_invalid_snapshot_types_fail_before_planning_or_sorting(self):
        p,plan=ready()
        for outputs in ([{'stable_id':'fake'}],[PHYSICAL,PHYSICAL]):
            with self.assertRaises(ValueError):p.poll(outputs,[STRANDED],now=4)
        self.assertFalse(p.revalidate(plan,[PHYSICAL,VIRTUAL],STRANDED))
        for rect in ([0,0,True,5],[0,0,float('nan'),5],[0,0,10.0,5],[5,5,1,1]):
            with self.assertRaises(ValueError):RecoveryOutput('x','physical',rect)
        with self.assertRaises(ValueError):replace(PHYSICAL,kind='unknown')
        with self.assertRaises(ValueError):replace(PHYSICAL,attached=1)
        with self.assertRaises(ValueError):replace(IDENTITY,pid=True)

    def test_tied_or_unusable_original_is_not_saved(self):
        p=ReconnectRecovery();left=replace(PHYSICAL,stable_id='left',work_rect=(-2560,0,0,1400))
        with self.assertRaises(ValueError):p.remember(replace(ORIGINAL,rect=(-400,100,400,800)),[PHYSICAL,left],user_confirmed=True)
        with self.assertRaises(ValueError):p.remember(replace(ORIGINAL,rect=(-899,100,1,800)),[PHYSICAL,VIRTUAL],user_confirmed=True)
        self.assertEqual(len(p.saved),0)

    def test_one_pixel_overlap_and_inaccessible_titlebar_are_recovered(self):
        for rect in ((-899,100,1,800),(200,-695,1100,5)):
            p,_=ready();plans=p.poll([PHYSICAL,VIRTUAL],[replace(STRANDED,rect=rect)],now=4)['plans']
            self.assertEqual(len(plans),1)
        p,_=ready()
        normal=replace(ORIGINAL,rect=(-100,100,800,800))
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[normal],now=4)['plans'],[])

    def test_backward_time_restarts_stability_and_invalidates_old_generation(self):
        p,old=ready()
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=1)['state'],'waiting_for_stable_physical')
        self.assertFalse(p.revalidate(old,[PHYSICAL,VIRTUAL],STRANDED))
        self.assertEqual(p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=2)['plans'],[])
        self.assertEqual(len(p.poll([PHYSICAL,VIRTUAL],[STRANDED],now=3)['plans']),1)
        for now in (True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):p.poll([PHYSICAL,VIRTUAL],[],now=now)

    def test_reconnect_drift_invalidates_delayed_plan(self):
        p,old=ready();new=replace(PHYSICAL,work_rect=(0,0,1920,1000))
        p.poll([new,VIRTUAL],[STRANDED],now=4)
        self.assertFalse(p.revalidate(old,[PHYSICAL,VIRTUAL],STRANDED))

    def invoke(self,p,plan,*,read_window=None,read_outputs=None,move=None,context=None):
        return apply_one_recovery(plan,policy=p,read_window=read_window or (lambda _:STRANDED),
                                  read_outputs=read_outputs or (lambda:[PHYSICAL,VIRTUAL]),
                                  read_context=context or (lambda:('foreground','desktop')),
                                  move_nonactivating=move or (lambda *_:None),user_confirmed=True)

    def test_success_consumes_generation_and_duplicate_callback_cannot_move_again(self):
        p,plan=ready();current=[STRANDED];moves=[]
        def move(identity,rect):moves.append(rect);current[0]=replace(STRANDED,rect=rect)
        result=self.invoke(p,plan,read_window=lambda _:current[0],move=move)
        self.assertTrue(result['verified'])
        current[0]=STRANDED
        duplicate=self.invoke(p,plan,read_window=lambda _:current[0],move=move)
        self.assertFalse(duplicate['attempted']);self.assertEqual(len(moves),1)

    def test_pre_move_reader_failure_never_invokes_mover(self):
        p,plan=ready();moves=[]
        def fail():raise OSError('fixture')
        result=self.invoke(p,plan,read_outputs=fail,move=lambda *args:moves.append(args))
        self.assertFalse(result['attempted']);self.assertEqual(result['reason'],'preflight_failed');self.assertEqual(moves,[])

    def test_post_move_exception_preserves_truthful_attempt_and_return_path(self):
        for stage in ('window','outputs','context'):
            with self.subTest(stage=stage):
                p,plan=ready();moved=[False]
                def move(*args):moved[0]=True
                def maybe(value,label):
                    if moved[0] and stage==label:raise OSError('fixture')
                    return value
                result=self.invoke(p,plan,move=move,read_window=lambda _:maybe(STRANDED,'window'),
                                   read_outputs=lambda:maybe([PHYSICAL,VIRTUAL],'outputs'),context=lambda:maybe(('foreground','desktop'),'context'))
                self.assertTrue(result['attempted']);self.assertFalse(result['verified'])
                self.assertEqual(result['reason'],'verification_incomplete');self.assertEqual(result['original_rect'],STRANDED.rect)

    def test_disappearing_window_and_partially_failed_move_are_not_reported_unattempted(self):
        p,plan=ready();moved=[False]
        def move(*args):moved[0]=True
        result=self.invoke(p,plan,move=move,read_window=lambda _:None if moved[0] else STRANDED)
        self.assertTrue(result['attempted']);self.assertFalse(result['verified']);self.assertTrue(result['mutation_uncertain'])
        p,plan=ready();count=[]
        def partial(*args):count.append(args);raise OSError('after_native_move')
        result=self.invoke(p,plan,move=partial)
        self.assertTrue(result['attempted']);self.assertTrue(result['mutation_uncertain']);self.assertEqual(len(count),1)
        self.assertEqual(result['original_rect'],STRANDED.rect)

if __name__=='__main__':unittest.main()
