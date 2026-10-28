"""Explicit agent activity, never CPU/process-name heuristics."""
import threading
import time
class ActivityLease:
    def __init__(self, timeout=300, clock=time.monotonic):
        if not 0 < timeout <= 300: raise ValueError("invalid_idle_timeout")
        self.clock, self.timeout = clock, timeout
        self.lock=threading.Lock()
        self.last=clock()
        self.stopped=False
    def activity(self):
        with self.lock:
            now=self.clock()
            if self.stopped or now-self.last>=self.timeout:
                self.stopped=True
                return False
            self.last=now
            return True
    def stop(self):
        with self.lock: self.stopped=True
    def expired(self):
        with self.lock:
            if self.clock()-self.last>=self.timeout:self.stopped=True
            return self.stopped
