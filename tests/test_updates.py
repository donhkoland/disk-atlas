import os,tempfile,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
from atlas import storage,updates

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.patch=patch.object(storage,'DB',Path(self.temp.name)/'test.sqlite3');self.patch.start();storage.init()
        with storage.connect() as c:c.execute("INSERT INTO drives(id,name,root,last_scan) VALUES('d','Test','test',1000)")
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def test_disabled_until_configured(self):
        with patch.object(updates,'launch') as launch:updates.tick([],now=100000);launch.assert_not_called()
        self.assertEqual(updates.policies()[0]['enabled'],0)
    def test_due_connected_and_persistent_origin(self):
        updates.configure('d',True,24)
        with patch.object(updates,'locate',return_value='test'),patch.object(updates,'launch',return_value='scan') as launch:
            updates.tick([],now=100000);updates.tick([],now=100001);launch.assert_called_once_with('d')
        with storage.connect() as c:self.assertEqual(c.execute('SELECT origin FROM scan_origins').fetchone()[0],'scheduled')
        self.assertEqual(updates.policies()[0]['enabled'],1)
    def test_offline_and_paused_never_launch(self):
        updates.configure('d',True,1)
        with patch.object(updates,'locate',return_value=None),patch.object(updates,'launch') as launch:updates.tick([],now=100000);launch.assert_not_called()
        with storage.connect() as c:c.execute("INSERT INTO scans(id,drive_id,status,started) VALUES('s','d','paused',1000)")
        with patch.object(updates,'locate',return_value='test'),patch.object(updates,'launch') as launch:updates.tick([],now=100000);launch.assert_not_called()
    def test_history_survives_hidden_cards(self):
        with storage.connect() as c:
            c.execute("INSERT INTO scans(id,drive_id,status,started,finished) VALUES('s','d','completed',1000,1100)")
            c.execute("INSERT INTO scan_visibility VALUES('s',1200)")
            c.execute("INSERT INTO changes VALUES('s','a.txt','added')")
        r=updates.history('d');self.assertEqual(r['total'],1);self.assertEqual(r['items'][0]['finished'],1100);self.assertEqual(r['items'][0]['changes']['added'],1);self.assertTrue(r['items'][0]['hidden'])
    def test_invalid_policy(self):
        for enabled,hours in [(1,24),(True,0),(True,721),(True,1.5)]:
            with self.assertRaises(ValueError):updates.configure('d',enabled,hours)
