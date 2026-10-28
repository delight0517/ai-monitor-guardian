import unittest
from ai_debug_display.activity_lease import ActivityLease
class LeaseTests(unittest.TestCase):
 def test_idle_and_refresh(self):
  now=[0];l=ActivityLease(clock=lambda:now[0]);now[0]=299;self.assertFalse(l.expired());self.assertTrue(l.activity());now[0]=598;self.assertFalse(l.expired());now[0]=599;self.assertTrue(l.expired());self.assertFalse(l.activity())
 def test_stop_fences_activity(self):
  l=ActivityLease();l.stop();self.assertFalse(l.activity());self.assertTrue(l.expired())
 def test_late_activity_cannot_revive(self):
  now=[0];l=ActivityLease(clock=lambda:now[0]);now[0]=300;self.assertFalse(l.activity());self.assertTrue(l.expired())
