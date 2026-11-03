import os,tempfile,threading,time,unittest,uuid
from pathlib import Path
from atlas import io_budget as io

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.a=self.root/'a';self.b=self.root/'b';self.a.mkdir();self.b.mkdir();self.physical=uuid.uuid4().hex
        io.configure([{'root':str(self.a),'volume':'a','physical':self.physical,'bus_type':'USB'},{'root':str(self.b),'volume':'b','physical':self.physical,'bus_type':'USB'}])
    def tearDown(self):io.configure([]);self.temp.cleanup()
    def test_partitions_share_one_budget_and_wait(self):
        entered=threading.Event();release=threading.Event();second=threading.Event()
        def first():
            with io.lease(self.a):entered.set();release.wait(3)
        def next_task():
            with io.lease(self.b):second.set()
        t=threading.Thread(target=first);u=threading.Thread(target=next_task);t.start();self.assertTrue(entered.wait(2));u.start()
        try:
            self.assertFalse(second.wait(.15));state=next(d for d in io.status()['devices'] if d['identity']=='physical:'+self.physical);self.assertEqual(state['active'],1);self.assertEqual(state['queued'],1);self.assertEqual(state['limit'],1)
        finally:release.set();t.join(3);u.join(3)
        self.assertTrue(second.is_set())
    def test_exception_and_nested_calls_release_budget(self):
        with self.assertRaises(RuntimeError):
            with io.lease(self.a):
                with io.lease(self.a):raise RuntimeError('synthetic')
        with io.lease(self.a):pass
        state=next(d for d in io.status()['devices'] if d['identity']=='physical:'+self.physical);self.assertEqual(state['active'],0);self.assertEqual(state['queued'],0)
    def test_cancelled_wait_does_not_leak(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(OSError):
            with io.lease(self.a,cancel=cancel):self.fail('cancelled lease entered')
        with io.lease(self.a):pass
