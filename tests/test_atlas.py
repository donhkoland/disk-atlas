import unittest,tempfile,time,os,sqlite3,threading
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from atlas import storage,scanner,server
from atlas.volumes import locate
class AtlasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.root=self.base/'source';self.root.mkdir();self.data=self.base/'data'
        for x in ('manifests','backups','logs'): (self.data/x).mkdir(parents=True)
        self.patches=[patch.object(storage,'DATA',self.data),patch.object(storage,'DB',self.data/'test.sqlite3'),patch.object(server,'DB',self.data/'test.sqlite3'),patch.object(server,'DATA',self.data),patch.object(scanner,'DATA',self.data)]
        self.vol=[dict(root=str(self.root)+os.sep,volume='test-volume',physical='physical-A',label='Test',filesystem='NTFS',capacity=1000000,free=900000)]
        self.patches += [patch.object(server,'available',lambda:self.vol),patch.object(scanner,'volumes',lambda:self.vol)]
        for p in self.patches:p.start()
        self.ctx=TestClient(server.app);self.client=self.ctx.__enter__();self.headers={'X-Atlas-Token':self.client.get('/api/session').json()['token']}
    def tearDown(self):
        self.ctx.__exit__(None,None,None)
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def post(self,path,body=None):
        r=self.client.post('/api/'+path,headers=self.headers,json=body)
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def register(self,root=None):return self.post('drives',dict(root=str(root or self.root),name='Archive',label='HD 001'))['id']
    def wait(self,sid):
        for _ in range(500):
            with storage.connect() as c:s=dict(c.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone())
            if sid not in scanner.workers:
                if s['error']: print('SCAN ERROR:',s['error'])
                return s
            time.sleep(.01)
        self.fail('Scanner did not finish')
    def scan(self,did):return self.wait(self.post('drives/'+did+'/scan')['id'])
    def test_offline_unicode_empty_folders_search_annotations_and_incremental(self):
        (self.root/'空 fotografía').mkdir();(self.root/'empty').mkdir();f=self.root/'空 fotografía'/'José RAW.txt';f.write_text('archive')
        did=self.register();self.assertEqual(self.scan(did)['status'],'completed')
        result=self.client.get('/api/entries',params={'q':'José'}).json();self.assertEqual(result['total'],1);eid=result['items'][0]['id']
        self.post('annotations',dict(ids=[eid],tags='ArchiveCopy',project='Milano',notes='Preserve'))
        self.vol=[];r=self.client.get('/api/entries',params={'q':'ArchiveCopy'}).json();self.assertEqual(r['total'],1);self.assertFalse(r['items'][0]['connected'])
        self.assertEqual(self.client.get('/api/entries',params={'q':'empty'}).json()['total'],1)
        self.vol=[dict(root=str(self.root)+os.sep,volume='test-volume',physical='physical-A')]
        f.write_text('changed contents');self.scan(did)
        self.assertEqual(self.client.get('/api/entries/'+str(eid)).json()['annotation']['notes'],'Preserve')
        f.unlink();self.scan(did)
        with storage.connect() as c:self.assertEqual(c.execute('SELECT present FROM entries WHERE id=?',(eid,)).fetchone()[0],0)
    def test_updates_restore_pagination_and_run_offline(self):
        f=self.root/'restored.txt';f.write_text('same');did=self.register();first=self.scan(did)
        f.unlink();self.scan(did);f.write_text('same');third=self.scan(did)
        r=self.client.get('/api/scans/'+third['id']+'/changes',params={'kind':'restored'}).json()
        self.assertEqual(r['total'],1);self.assertEqual(r['changes'][0]['kind'],'restored');self.assertTrue(r['scan']['finished'])
        with storage.connect() as c:c.executemany('INSERT INTO changes VALUES(?,?,?)',[(third['id'],'extra'+str(i),'added') for i in range(105)])
        r=self.client.get('/api/scans/'+third['id']+'/changes',params={'kind':'added','offset':100,'limit':100}).json();self.assertEqual(r['total'],105);self.assertEqual(len(r['changes']),5)
        self.post('drives/'+did+'/update-policy',{'enabled':True,'interval_hours':24})
        self.assertEqual(self.client.get('/api/updates').json()['policies'][0]['enabled'],1)
        self.vol=[];self.assertEqual(self.post('updates/run',{'drive_id':did})['started'],[])

    def test_json_anchor_automatic_offline_and_incremental(self):
        import json
        (self.root/'fotografía.txt').write_text('first');did=self.register();scan=self.scan(did)
        target=self.data/'manifests'/(did+'.json');r=json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(r['last_published_scan']['id'],scan['id']);self.assertEqual(r['entries'][0]['name'],'fotografía.txt');self.assertEqual(r['entry_count'],1)
        self.vol=[];status=self.client.get('/api/drives/'+did+'/json-snapshot').json();self.assertTrue(status['current']);self.assertTrue(status['available'])
        self.assertEqual(self.client.get('/api/download/manifests/'+did+'.json').status_code,200)
        self.assertEqual(self.client.get('/api/entries',params={'q':'fotografía'}).json()['total'],1)
        self.vol=[dict(root=str(self.root)+os.sep,volume='test-volume',physical='physical-A')]
        (self.root/'second.txt').write_text('second');scan=self.scan(did)
        r=json.loads(target.read_text(encoding='utf-8'));self.assertEqual(r['entry_count'],2);self.assertEqual(r['last_published_scan']['id'],scan['id'])
    def test_json_anchor_failure_preserves_previous_and_scan(self):
        import json
        from atlas import snapshots
        (self.root/'first.txt').write_text('first');did=self.register();first=self.scan(did);target=self.data/'manifests'/(did+'.json');before=target.read_bytes()
        (self.root/'second.txt').write_text('second')
        with patch.object(snapshots.os,'replace',side_effect=OSError('synthetic write failure')):second=self.scan(did)
        self.assertEqual(second['status'],'completed');self.assertEqual(target.read_bytes(),before)
        status=self.client.get('/api/drives/'+did+'/json-snapshot').json();self.assertFalse(status['current']);self.assertIn('synthetic',status['error'])
        self.assertFalse(list((self.data/'manifests').glob('*.tmp')))
        self.post('drives/'+did+'/json-snapshot');self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['entry_count'],2)

    def test_all_file_columns_sort_both_directions_and_paginate(self):
        import functools
        for name,size,stamp in [('zeta.txt',3,100000),('alpha.txt',20,300000),('middle.txt',8,200000)]:
            f=self.root/name;f.write_bytes(b'x'*size);os.utime(f,(stamp,stamp))
        did=self.register();self.scan(did);self.client.patch('/api/drives/'+did,headers=self.headers,json={'name':'Zeta drive'})
        second=self.root/'second';second.mkdir();(second/'beta.txt').write_text('bb');other=self.register(second);self.scan(other);self.client.patch('/api/drives/'+other,headers=self.headers,json={'name':'Alpha drive'})
        with patch.object(server,'locate',side_effect=lambda d,vol:str(self.root) if d['id']==did else None):
            for column,key in [('name',lambda e:e['name'].lower()),('drive',lambda e:e['drive_name'].lower()),('location',lambda e:(e['archived_root']+e['rel']).lower()),('size',lambda e:e['size']),('modified',lambda e:e['mtime']),('status',lambda e:0 if e['connected'] else 1)]:
                for direction in ('asc','desc'):
                    with self.subTest(column=column,direction=direction):
                        response=self.client.get('/api/entries',params={'sort':column,'direction':direction,'limit':2}).json()
                        next_page=self.client.get('/api/entries',params={'sort':column,'direction':direction,'offset':2,'limit':2}).json()
                        result=response['items']+next_page['items'];values=[key(e) for e in result]
                        self.assertEqual(values,sorted(values,reverse=direction=='desc'));self.assertEqual(len({e['id'] for e in result}),4)
            self.assertEqual(self.client.get('/api/entries',params={'sort':'name','direction':'invalid'}).status_code,400)

    def test_search_summary_covers_all_pages_and_offline(self):
        (self.root/'folder').mkdir();(self.root/'a.txt').write_bytes(b'abc');(self.root/'b.jpg').write_bytes(b'12345');did=self.register();self.scan(did)
        self.vol=[];r=self.client.get('/api/entries',params={'limit':1}).json();summary=r['summary']
        self.assertEqual(len(r['items']),1);self.assertEqual(r['total'],3);self.assertEqual(summary['files'],2);self.assertEqual(summary['folders'],1);self.assertEqual(summary['bytes'],8);self.assertEqual(summary['format_count'],2);self.assertEqual(summary['online'],0);self.assertEqual(summary['offline'],3)
        io=self.client.get('/api/io-budget').json();self.assertGreaterEqual(io['global_limit'],1);self.assertLessEqual(io['global_limit'],2)

    def test_no_ai_routes_and_technical_drive_data(self):
        did=self.register();self.scan(did)
        self.assertEqual(self.client.get('/api/intelligence/status').status_code,404)
        self.assertEqual(self.client.post('/api/intelligence/automatic',headers=self.headers,json={}).status_code,404)
        data=self.client.get('/api/drives').json()[0];self.assertEqual(data['technical']['capacity'],1000000);self.assertEqual(data['technical']['free'],900000);self.assertEqual(data['technical']['filesystem'],'NTFS')
        self.vol=[];data=self.client.get('/api/drives').json()[0];self.assertFalse(data['connected']);self.assertEqual(data['technical']['free'],900000)
    def test_integral_projects_personal_and_light_overview(self):
        first=self.root/'Projects'/'2022'/'First';first.mkdir(parents=True);(first/'one.txt').write_text('123')
        personal=self.root/'Memories'/'2020'/'01 January'/'First memory';personal.mkdir(parents=True);(personal/'p.jpg').write_text('1234')
        did=self.register();self.scan(did)
        second=self.root/'second-root';other=second/'Projects'/'2023'/'Second';other.mkdir(parents=True);(other/'two.mp4').write_text('12345')
        memory=second/'Memories'/'2021'/'02 Febrero'/'Second memory';memory.mkdir(parents=True);(memory/'q.jpg').write_text('12');another=self.register(second);self.scan(another)
        p=self.client.get('/api/projects',params={'root':'all'}).json();self.assertEqual({e['name'] for e in p['items']},{'First','Second'})
        p=self.client.get('/api/personal',params={'root':'all'}).json();self.assertEqual({e['event_name'] for e in p['items']},{'First memory','Second memory'})
        result=self.client.get('/api/entries',params={'q':'*.mp4'}).json();self.assertEqual(result['total'],1)
        summary=self.client.get('/api/overview').json();self.assertEqual(summary['totals']['files'],4)
        location=self.client.get('/api/entries/'+str(result['items'][0]['id'])+'/location').json();self.assertEqual(location['drive_id'],another);self.assertNotIn('related',location)
    def test_session_rejection_and_new_session_remains_protected(self):
        did=self.register()
        bad=self.client.patch('/api/drives/'+did,headers={'X-Atlas-Token':'obsolete'},json={'name':'rejected'})
        self.assertEqual(bad.status_code,403);self.assertEqual(bad.json()['detail'],'Session token required')
        token=self.client.get('/api/session').json()['token'];good=self.client.patch('/api/drives/'+did,headers={'X-Atlas-Token':token},json={'name':'accepted'});self.assertEqual(good.status_code,200)

    def test_grouped_project_locations_cover_and_offline_navigation(self):
        from PIL import Image
        project=self.root/'Projects'/'Sample Project';project.mkdir(parents=True);Image.new('RGB',(30,20),'blue').save(project/'photo.jpg');did=self.register();self.scan(did)
        second=self.root/'second-root';other=second/'Copies'/'Sample Project';other.mkdir(parents=True);(other/'notes.txt').write_text('second location');another=self.register(second);self.scan(another)
        self.vol=[]
        data=self.client.get('/api/library',params={'kind':'projects','q':'SAMPLE PROJECT'}).json();self.assertEqual(data['total'],1);group=data['items'][0];self.assertEqual(group['name'],'Sample Project');self.assertEqual(group['location_count'],2);self.assertEqual(group['drive_count'],2)
        self.assertTrue(all(e['path'].endswith('Sample Project') for e in group['locations']))
        cover=self.client.get('/api/entries/'+str(group['locations'][0]['id'])+'/cover').json()
        if cover['entry'] is None:cover=self.client.get('/api/entries/'+str(group['locations'][1]['id'])+'/cover').json()
        self.assertEqual(cover['entry']['ext'],'jpg')
        self.assertEqual(self.client.get('/api/entries',params={'drive':another,'parent':str(Path('Copies')/'Sample Project')}).json()['total'],1)
    def test_personal_same_named_events_in_different_years_stay_separate(self):
        for year in ('2020','2021'):
            folder=self.root/'Memories'/year/'01 January'/'Birthday';folder.mkdir(parents=True);(folder/'photo.jpg').write_text('photo')
        did=self.register();self.scan(did)
        data=self.client.get('/api/library',params={'kind':'personal','q':'Birthday'}).json();self.assertEqual(data['total'],2)
        self.assertEqual({g['year'] for g in data['items']},{'2020','2021'})

    def test_letter_reassignment(self):
        d={'volume':'stable','relative_root':'Photos'};new=self.base/'Z';(new/'Photos').mkdir(parents=True)
        self.assertEqual(locate(d,[dict(volume='stable',root=str(new))]),str(new/'Photos'))
        self.assertIsNone(locate(d,[dict(volume='different',root=str(new))]))
        self.assertIsNone(locate(d,[dict(volume='stable',root=str(new)),dict(volume='stable',root=str(new))]))
    def test_disconnect_preserves_previous_and_resume(self):
        (self.root/'one.txt').write_text('original');did=self.register();self.scan(did)
        (self.root/'two.txt').write_text('new')
        real=scanner.stage
        def disconnect(sid,batch):real(sid,batch);raise OSError('Disconnected')
        with patch.object(scanner,'stage',disconnect):s=self.scan(did)
        self.assertEqual(s['status'],'paused');self.assertEqual(self.client.get('/api/entries').json()['total'],1)
        self.post('scans/'+s['id']+'/resume');self.assertEqual(self.wait(s['id'])['status'],'completed');self.assertEqual(self.client.get('/api/entries').json()['total'],2)
    def test_duplicates_overlap_and_hashes(self):
        sub=self.root/'nested';sub.mkdir();(sub/'same.txt').write_text('same');(self.root/'same.txt').write_text('same')
        a=self.register();b=self.register(sub);self.scan(a);self.scan(b)
        groups=self.client.get('/api/duplicates').json();self.assertEqual(groups[0]['copies'],2);self.assertEqual(groups[0]['physical_copies'],1);self.assertEqual(groups[0]['level'],'candidate')
        for e in self.client.get('/api/entries').json()['items']:
            if not e['is_dir']:self.post('entries/'+str(e['id'])+'/hash',dict(partial=False))
        self.assertEqual(self.client.get('/api/duplicates').json()[0]['level'],'verified')
    def test_manifest_reimport_backup_and_security(self):
        (self.root/'one.txt').write_text('one');did=self.register();self.scan(did)
        eid=self.client.get('/api/entries').json()['items'][0]['id'];self.post('annotations',dict(ids=[eid],tags='Personal'))
        m=self.post('drives/'+did+'/manifest');blob=Path(m['path']).read_bytes()
        for _ in range(2):
            r=self.client.post('/api/import',content=blob,headers=self.headers);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/entries').json()['total'],1)
        self.assertEqual(self.client.get('/api/entries/'+str(eid)).json()['annotation']['tags'],'Personal')
        backup=self.post('backup');self.assertTrue(Path(backup['path']).is_file())
        self.assertEqual(self.client.post('/api/backup').status_code,403)
        self.assertEqual(self.client.get('/api/drives',headers={'host':'evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/export?format=json').status_code,200)
    def test_restart_checkpoint_and_rollback(self):
        (self.root/'old.txt').write_text('old');did=self.register();self.scan(did)
        try:
            with storage.connect() as c:
                c.execute('UPDATE entries SET present=0');raise RuntimeError('interrupted transaction')
        except RuntimeError:pass
        self.assertEqual(self.client.get('/api/entries').json()['total'],1)
        with storage.connect() as c:c.execute("INSERT INTO scans(id,drive_id,status,started) VALUES('restart',?,'running',?)",(did,time.time()))
        storage.init()
        with storage.connect() as c:self.assertEqual(c.execute("SELECT status FROM scans WHERE id='restart'").fetchone()[0],'paused')
    def test_large_directory_streaming_and_duplicate_names(self):
        for i in range(1200):(self.root/f'file-{i}.txt').write_text(str(i))
        (self.root/'sub').mkdir();(self.root/'sub'/'file-0.txt').write_text('0')
        did=self.register();s=self.scan(did);self.assertEqual(s['files'],1201)
        self.assertEqual(len(self.client.get('/api/entries').json()['items']),100)
        self.assertEqual(self.client.get('/api/entries',params={'q':'file-0.txt'}).json()['total'],2)
    def test_analytics_and_static_routes(self):
        for endpoint in ('/','/style.css','/app.js','/api/analytics','/api/duplicates','/api/scans'):
            r=self.client.get(endpoint);self.assertEqual(r.status_code,200,r.text)
        (self.root/'one.txt').write_text('test');did=self.register();self.scan(did)
        data=self.client.get('/api/analytics').json();self.assertEqual(data['totals']['files'],1)
        self.assertEqual(len(data['largest']),1)
    def test_protected_directory_is_warning_and_preserves_archived_files(self):
        protected=self.root/'protected';protected.mkdir()
        (protected/'old.txt').write_text('keep archived')
        (self.root/'readable.txt').write_text('readable')
        did=self.register();self.assertEqual(self.scan(did)['status'],'completed')
        (self.root/'new.txt').write_text('new content')
        real=scanner.os.scandir
        def denied(path):
            if Path(path).name=='protected': raise PermissionError(13,'Access denied',str(path))
            return real(path)
        with patch.object(scanner.os,'scandir',side_effect=denied): result=self.scan(did)
        self.assertEqual(result['status'],'completed_with_warnings')
        self.assertEqual(result['warnings'],1)
        self.assertEqual(self.client.get('/api/entries',params={'q':'new.txt'}).json()['total'],1)
        self.assertEqual(self.client.get('/api/entries',params={'q':'old.txt'}).json()['total'],1)
        with storage.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM changes WHERE scan_id=? AND kind='missing'",(result['id'],)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM queue WHERE scan_id=?',(result['id'],)).fetchone()[0],0)
    def test_root_access_denied_remains_paused(self):
        did=self.register()
        with patch.object(scanner.os,'scandir',side_effect=PermissionError(13,'Access denied')): result=self.scan(did)
        self.assertEqual(result['status'],'paused')
        self.assertIsNotNone(result['error'])
    def cleanup_group(self,contents=('identical','identical')):
        a=self.root/'old.txt';b=self.root/'latest.txt'
        a.write_text(contents[0]);b.write_text(contents[1])
        now=time.time();os.utime(a,(now-100,now-100));os.utime(b,(now,now))
        did=self.register();self.scan(did)
        items=self.client.get('/api/entries').json()['items']
        return a,b,[e['id'] for e in items]
    def test_cleanup_preview_confirm_and_replay(self):
        from atlas import cleanup
        a,b,ids=self.cleanup_group()
        plan=self.post('duplicates/prepare-cleanup',dict(ids=ids))
        keeper=next(e for e in plan['items'] if e['id']==plan['recommended_keep_id'])
        self.assertEqual(Path(keeper['path']).name,'latest.txt')
        self.assertTrue(a.exists());self.assertTrue(b.exists())
        body=dict(plan_id=plan['plan_id'],keep_id=keeper['id'])
        self.assertEqual(self.client.post('/api/duplicates/cleanup',json=body,headers=self.headers).status_code,400)
        body['confirm']=True
        trash=self.base/'test-trash';trash.mkdir()
        def fake_recycle(path):Path(path).rename(trash/Path(path).name)
        with patch.object(cleanup,'recycle',side_effect=fake_recycle):
            result=self.post('duplicates/cleanup',body)
        self.assertEqual(len(result['recycled']),1);self.assertFalse(a.exists());self.assertTrue(b.exists());self.assertTrue((trash/'old.txt').exists())
        self.assertTrue(Path(result['backup']['path']).is_file())
        self.assertEqual(self.client.get('/api/entries').json()['total'],1)
        self.assertEqual(self.client.post('/api/duplicates/cleanup',json=body,headers=self.headers).status_code,400)
    def test_cleanup_rejects_different_contents_and_offline(self):
        a,b,ids=self.cleanup_group(('AAAA','BBBB'))
        r=self.client.post('/api/duplicates/prepare-cleanup',json=dict(ids=ids),headers=self.headers)
        self.assertEqual(r.status_code,400);self.assertTrue(a.exists());self.assertTrue(b.exists())
        self.vol=[]
        r=self.client.post('/api/duplicates/prepare-cleanup',json=dict(ids=ids),headers=self.headers)
        self.assertEqual(r.status_code,400)
    def test_cleanup_changed_after_preview_never_recycles(self):
        from atlas import cleanup
        a,b,ids=self.cleanup_group()
        plan=self.post('duplicates/prepare-cleanup',dict(ids=ids));a.write_text('changed!!')
        with patch.object(cleanup,'recycle') as recycle:
            r=self.client.post('/api/duplicates/cleanup',json=dict(plan_id=plan['plan_id'],keep_id=plan['recommended_keep_id'],confirm=True),headers=self.headers)
            self.assertEqual(r.status_code,400);recycle.assert_not_called()
        self.assertTrue(a.exists());self.assertTrue(b.exists())
    def test_cleanup_recycle_failure_keeps_catalog_and_files(self):
        from atlas import cleanup
        a,b,ids=self.cleanup_group();plan=self.post('duplicates/prepare-cleanup',dict(ids=ids))
        with patch.object(cleanup,'recycle',side_effect=OSError('Recycle unavailable')):
            r=self.post('duplicates/cleanup',dict(plan_id=plan['plan_id'],keep_id=plan['recommended_keep_id'],confirm=True))
        self.assertEqual(len(r['errors']),1);self.assertEqual(r['recycled'],[])
        self.assertTrue(a.exists());self.assertTrue(b.exists());self.assertEqual(self.client.get('/api/entries').json()['total'],2)
    def test_cleanup_rejects_pending_scan_and_expired_plan(self):
        from atlas import cleanup
        a,b,ids=self.cleanup_group();plan=self.post('duplicates/prepare-cleanup',dict(ids=ids))
        cleanup.plans[plan['plan_id']]['expires']=time.time()-1
        r=self.client.post('/api/duplicates/cleanup',json=dict(plan_id=plan['plan_id'],keep_id=plan['recommended_keep_id'],confirm=True),headers=self.headers)
        self.assertEqual(r.status_code,400)
        with storage.connect() as c:
            did=c.execute('SELECT id FROM drives').fetchone()[0]
            c.execute("INSERT INTO scans(id,drive_id,status,started) VALUES('pending-cleanup',?,'paused',?)",(did,time.time()))
        r=self.client.post('/api/duplicates/prepare-cleanup',json=dict(ids=ids),headers=self.headers)
        self.assertEqual(r.status_code,400)
    def test_cleanup_overlap_alias_and_hardlink_rejection(self):
        from atlas import cleanup
        sub=self.root/'nested';sub.mkdir()
        old=sub/'old.txt';new=self.root/'new.txt';old.write_text('same');new.write_text('same')
        now=time.time();os.utime(old,(now-100,now-100));os.utime(new,(now,now))
        a=self.register();b=self.register(sub);self.scan(a);self.scan(b)
        ids=[e['id'] for e in self.client.get('/api/entries').json()['items'] if not e['is_dir']]
        plan=self.post('duplicates/prepare-cleanup',dict(ids=ids));self.assertEqual(len(plan['items']),2)
        trash=self.base/'test-trash';trash.mkdir()
        with patch.object(cleanup,'recycle',side_effect=lambda path:Path(path).rename(trash/Path(path).name)):
            self.post('duplicates/cleanup',dict(plan_id=plan['plan_id'],keep_id=plan['recommended_keep_id'],confirm=True))
        with storage.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM entries WHERE name='old.txt' AND present=1").fetchone()[0],0)
        os.link(new,self.root/'hard.txt');self.scan(a)
        ids=[e['id'] for e in self.client.get('/api/entries').json()['items'] if not e['is_dir']]
        r=self.client.post('/api/duplicates/prepare-cleanup',json=dict(ids=ids),headers=self.headers)
        self.assertEqual(r.status_code,400)
    def test_recycle_bin_is_not_reindexed(self):
        recycle=self.root/'$RECYCLE.BIN';recycle.mkdir();(recycle/'discarded.txt').write_text('old')
        (self.root/'active.txt').write_text('active');did=self.register();result=self.scan(did)
        self.assertEqual(result['status'],'completed_with_warnings')
        self.assertEqual(self.client.get('/api/entries').json()['total'],1)
    def test_global_search_drive_and_path_offline_after_history_clear(self):
        folder=self.root/'ArchiveCopy'/'Milano';folder.mkdir(parents=True)
        (folder/'delivery.txt').write_text('catalog')
        did=self.register();result=self.scan(did)
        with storage.connect() as c:c.execute("UPDATE drives SET name='HD Archive',label='Disk 001',tags='Editorial' WHERE id=?",(did,))
        self.post('scans-history/clear',dict(confirm=True))
        self.assertEqual(self.client.get('/api/scans').json(),[])
        self.vol=[]
        for q in ('ArchiveCopy','"ArchiveCopy" Milano','HD Archive','Editorial',str(Path('ArchiveCopy')/'Milano')):
            data=self.client.get('/api/entries',params={'q':q}).json()
            self.assertGreater(data['total'],0,q)
            self.assertEqual(data['by_drive'][0]['id'],did)
            self.assertFalse(data['items'][0]['connected'])
        self.post('scans-history/restore')
        self.assertEqual(len(self.client.get('/api/scans').json()),1)
    def test_history_clear_rejects_active_and_preserves_catalog(self):
        (self.root/'entry.txt').write_text('keep');did=self.register();result=self.scan(did)
        with storage.connect() as c:c.execute("INSERT INTO scans(id,drive_id,status,started) VALUES('active-history',?,'paused',?)",(did,time.time()))
        r=self.client.post('/api/scans-history/clear',json=dict(ids=['active-history'],confirm=True),headers=self.headers)
        self.assertEqual(r.status_code,400)
        self.post('scans-history/clear',dict(ids=[result['id']],confirm=True))
        self.assertEqual(len(self.client.get('/api/scans').json()),1)
        self.assertEqual(self.client.get('/api/entries').json()['total'],1)
        with storage.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM scans').fetchone()[0],2)
        r=self.client.post('/api/scans-history/clear',json=dict(confirm=False),headers=self.headers)
        self.assertEqual(r.status_code,400)
    def test_file_trash_confirm_alias_and_offline(self):
        from atlas import fileops
        sub=self.root/'nested';sub.mkdir();source=sub/'item.txt';source.write_text('delete test only')
        a=self.register();b=self.register(sub);self.scan(a);self.scan(b)
        entries=self.client.get('/api/entries',params={'q':'item.txt'}).json()['items'];eid=entries[0]['id']
        plan=self.post('entries/'+str(eid)+'/prepare-trash')
        self.assertEqual(self.client.post('/api/trash',json=dict(plan_id=plan['plan_id']),headers=self.headers).status_code,400)
        trash=self.base/'fake-trash';trash.mkdir()
        with patch.object(fileops,'recycle',side_effect=lambda path:Path(path).rename(trash/'item.txt')):
            self.post('trash',dict(plan_id=plan['plan_id'],confirm=True))
        self.assertFalse(source.exists());self.assertTrue((trash/'item.txt').exists())
        self.assertEqual(self.client.get('/api/entries',params={'q':'item.txt'}).json()['total'],0)
        r=self.client.post('/api/trash',json=dict(plan_id=plan['plan_id'],confirm=True),headers=self.headers);self.assertEqual(r.status_code,400)
        self.vol=[]
        r=self.client.post('/api/entries/'+str(eid)+'/prepare-trash',headers=self.headers);self.assertEqual(r.status_code,400)
    def test_trash_changed_file_nonempty_folder_and_empty_folder(self):
        from atlas import fileops
        f=self.root/'file.txt';f.write_text('original');folder=self.root/'nonempty';folder.mkdir();(folder/'child.txt').write_text('child');empty=self.root/'empty';empty.mkdir()
        did=self.register();self.scan(did)
        allitems=self.client.get('/api/entries').json()['items'];byname={e['name']:e['id'] for e in allitems}
        r=self.client.post('/api/entries/'+str(byname['nonempty'])+'/prepare-trash',headers=self.headers);self.assertEqual(r.status_code,400)
        plan=self.post('entries/'+str(byname['file.txt'])+'/prepare-trash');f.write_text('changed')
        with patch.object(fileops,'recycle') as recycle:
            r=self.client.post('/api/trash',json=dict(plan_id=plan['plan_id'],confirm=True),headers=self.headers);self.assertEqual(r.status_code,400);recycle.assert_not_called()
        plan=self.post('entries/'+str(byname['empty'])+'/prepare-trash')
        with patch.object(fileops,'recycle',side_effect=lambda path:Path(path).rename(self.base/'recycled-empty')):
            self.post('trash',dict(plan_id=plan['plan_id'],confirm=True))
        self.assertFalse(empty.exists())
    def test_image_thumbnail_optin_cache_offline_and_invalidation(self):
        from PIL import Image
        picture=self.root/'photo.png';Image.new('RGB',(800,600),'blue').save(picture)
        did=self.register();self.scan(did);eid=self.client.get('/api/entries').json()['items'][0]['id']
        self.assertEqual(self.client.get('/api/entries/'+str(eid)+'/thumbnail').status_code,404)
        original=picture.read_bytes();self.post('entries/'+str(eid)+'/thumbnail')
        self.assertEqual(picture.read_bytes(),original)
        r=self.client.get('/api/entries/'+str(eid)+'/thumbnail');self.assertEqual(r.status_code,200);self.assertEqual(r.headers['content-type'],'image/jpeg')
        import io
        with Image.open(io.BytesIO(r.content)) as thumb:self.assertLessEqual(thumb.width,480);self.assertLessEqual(thumb.height,320)
        self.vol=[];self.assertEqual(self.client.get('/api/entries/'+str(eid)+'/thumbnail').status_code,200)
        self.assertEqual(self.client.post('/api/entries/'+str(eid)+'/thumbnail',headers=self.headers).status_code,400)
        self.vol=[dict(root=str(self.root)+os.sep,volume='test-volume',physical='physical-A')]
        Image.new('RGB',(100,100),'red').save(picture);self.scan(did)
        self.assertEqual(self.client.get('/api/entries/'+str(eid)+'/thumbnail').status_code,404)
    def test_video_thumbnail_ffmpeg(self):
        import shutil,subprocess
        if not shutil.which('ffmpeg'):self.skipTest('FFmpeg unavailable')
        video=self.root/'clip.mp4'
        command=[shutil.which('ffmpeg'),'-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=160x90:d=0.2','-c:v','mpeg4','-y',str(video)]
        subprocess.run(command,check=True,capture_output=True,timeout=20,creationflags=0x08000000 if os.name=='nt' else 0)
        did=self.register();self.scan(did);eid=self.client.get('/api/entries').json()['items'][0]['id']
        self.post('entries/'+str(eid)+'/thumbnail');self.assertEqual(self.client.get('/api/entries/'+str(eid)+'/thumbnail').status_code,200)
    def test_open_connected_only_and_analytics_rows(self):
        (self.root/'file.txt').write_text('test');did=self.register();self.scan(did);eid=self.client.get('/api/entries').json()['items'][0]['id']
        with patch.object(server.os,'startfile',create=True) as openfile:
            self.post('entries/'+str(eid)+'/open',dict(mode='open'));openfile.assert_called_once()
        self.vol=[]
        with patch.object(server.os,'startfile',create=True) as openfile:
            self.assertEqual(self.client.post('/api/entries/'+str(eid)+'/open',json=dict(mode='open'),headers=self.headers).status_code,400);openfile.assert_not_called()
        analytics=self.client.get('/api/analytics').json()
        for collection in ('largest','oldest','recent'):
            self.assertEqual(analytics[collection][0]['drive_name'],'Archive');self.assertEqual(analytics[collection][0]['drive_id'],did)
    def test_folder_duplicates_inventory_hashes_and_overlap(self):
        for name in ('Project A','Project B'):
            sub=self.root/name/'empty';sub.mkdir(parents=True);(self.root/name/'same.txt').write_text('same')
        did=self.register();self.scan(did)
        groups=self.client.get('/api/duplicates/folders').json()
        match=next(g for g in groups if {e['name'] for e in g['items']}=={'Project A','Project B'})
        self.assertEqual(match['level'],'matching inventory');self.assertEqual(match['items'][0]['files'],1);self.assertEqual(match['items'][0]['folders'],1)
        for e in self.client.get('/api/entries').json()['items']:
            if not e['is_dir']:self.post('entries/'+str(e['id'])+'/hash',dict(partial=False))
        groups=self.client.get('/api/duplicates/folders').json();match=next(g for g in groups if {e['name'] for e in g['items']}=={'Project A','Project B'})
        self.assertEqual(match['level'],'verified hashes')
        self.vol=[];self.assertGreater(len(self.client.get('/api/duplicates/folders').json()),0)
    def test_project_year_category_profile_and_personal_collections(self):
        project=self.root/'Projects'/'2022'/'Sample Client';project.mkdir(parents=True);(project/'master.txt').write_text('project')
        personal=self.root/'Memories';occasion=personal/'2021'/'10 October'/'Family Birthday';occasion.mkdir(parents=True);(occasion/'photo.txt').write_text('memory');(personal/'Film'/'1401').mkdir(parents=True);(personal/'Phone').mkdir();(personal/'Family'/'2025'/'Holiday').mkdir(parents=True)
        decoy=self.root/'ArchiveCopy'/'Memories';decoy.mkdir(parents=True);(decoy/'one.txt').write_text('x')
        did=self.register();self.scan(did)
        data=self.client.get('/api/projects').json();self.assertEqual(data['total'],1);item=data['items'][0];self.assertEqual(item['year'],'2022');self.assertEqual(item['name'],'Sample Client');self.assertEqual(item['files'],1)
        self.post('projects/'+str(item['id'])+'/profile',dict(year='2021',category='Automotive / Video'))
        data=self.client.get('/api/projects').json();self.assertEqual(data['items'][0]['project_category'],'Automotive / Video');self.assertEqual(data['items'][0]['year'],'2021')
        p=self.client.get('/api/personal').json();self.assertEqual(p['total'],1);self.assertEqual(p['items'][0]['event_name'],'Family Birthday');self.assertEqual(p['items'][0]['month'],10);self.assertEqual({x['name'] for x in p['collections']},{'Film','Phone','Family'})
        self.vol=[];self.assertEqual(self.client.get('/api/projects').json()['total'],1);self.assertEqual(self.client.get('/api/personal').json()['total'],1)
    def test_library_all_containers_and_verified_duplicate_alerts(self):
        from atlas import library
        for container in ('Projects','Backup/Projects'):
            folder=self.root/container/'Sample Project';folder.mkdir(parents=True);(folder/'master.txt').write_text('same')
        for container in ('Memories','Backup/Personal'):
            folder=self.root/container/'Trips';folder.mkdir(parents=True);(folder/'photo.txt').write_text('memory')
        did=self.register();self.scan(did)
        def report(kind):
            for _ in range(400):
                data=self.client.get('/api/library',params={'kind':kind}).json()
                if data['analysis_status']!='analyzing':return data
                time.sleep(.02)
            self.fail('Library analysis did not finish')
        data=report('projects');group=next(g for g in data['items'] if g['name']=='Sample Project')
        self.assertEqual(group['location_count'],2);self.assertEqual(group['duplicate_analysis']['level'],'inventory')
        self.assertEqual(group['duplicate_analysis']['verified_savings'],0)
        personal=report('personal');self.assertEqual(next(g for g in personal['items'] if g['name']=='Trips')['location_count'],2)
        for e in self.client.get('/api/entries').json()['items']:
            if not e['is_dir']:self.post('entries/'+str(e['id'])+'/hash',dict(partial=False))
        data=report('projects');group=next(g for g in data['items'] if g['name']=='Sample Project')
        self.assertEqual(group['duplicate_analysis']['level'],'critical');self.assertEqual(group['duplicate_analysis']['verified_savings'],4)
        self.assertTrue((self.data/'manifests'/'library-analysis.json').exists())
        self.vol=[];self.assertEqual(report('projects')['items'][0]['duplicate_analysis']['level'],'critical')
        (self.root/'Backup'/'Projects'/'Sample Project'/'master.txt').write_text('changed')
        self.vol=[dict(root=str(self.root)+os.sep,volume='test-volume',physical='physical-A')];self.scan(did)
        group=next(g for g in report('projects')['items'] if g['name']=='Sample Project')
        self.assertEqual(group['duplicate_analysis']['level'],'name')

    def wait_batch(self,jid,states):
        for _ in range(500):
            result=self.client.get('/api/cleanup-batches/'+jid).json()
            if result['status'] in states:return result
            time.sleep(.02)
        self.fail('Batch did not reach expected state: '+str(result))
    def test_global_cleanup_background_confirm_and_different_names(self):
        from atlas import batches
        a,b,ids=self.cleanup_group();job=self.post('cleanup-batches',dict(scope_id=None));plan=self.wait_batch(job['id'],('ready','failed'))
        self.assertEqual(plan['status'],'ready');self.assertEqual(plan['groups'],1);self.assertEqual(plan['remove_count'],1)
        self.assertEqual(self.client.post('/api/cleanup-batches/'+job['id']+'/confirm',json={},headers=self.headers).status_code,400)
        trash=self.base/'batch-trash';trash.mkdir()
        with patch.object(batches,'recycle',side_effect=lambda path:Path(path).rename(trash/Path(path).name)):
            self.post('cleanup-batches/'+job['id']+'/confirm',dict(confirm=True));result=self.wait_batch(job['id'],('completed','stopped'))
        self.assertEqual(result['status'],'completed',result);self.assertEqual(result['recycled'],1);self.assertTrue(b.exists());self.assertFalse(a.exists())
        self.assertEqual(self.client.post('/api/cleanup-batches/'+job['id']+'/confirm',json=dict(confirm=True),headers=self.headers).status_code,400)
    def test_project_cleanup_is_scoped_and_cancelable(self):
        a=self.root/'Project';a.mkdir();outside=self.root/'outside.txt';outside.write_text('same');(a/'one.txt').write_text('same');(a/'two.txt').write_text('same')
        did=self.register();self.scan(did);eid=next(e['id'] for e in self.client.get('/api/entries').json()['items'] if e['name']=='Project')
        jid=self.post('cleanup-batches',dict(scope_id=eid))['id'];plan=self.wait_batch(jid,('ready','failed'));self.assertEqual(plan['total'],2);self.assertEqual(plan['groups'],1)
        for group in plan['items']:
            for file in [group['keep'],*group['discard']]:self.assertTrue(Path(file['path']).is_relative_to(a.resolve()))
        self.post('cleanup-batches/'+jid+'/cancel');self.assertEqual(self.client.get('/api/cleanup-batches/'+jid).json()['status'],'cancelled');self.assertTrue(outside.exists())
    def test_batch_changed_file_stops_without_recycle(self):
        from atlas import batches
        a,b,ids=self.cleanup_group();jid=self.post('cleanup-batches',{})['id'];self.wait_batch(jid,('ready','failed'));a.write_text('modified')
        with patch.object(batches,'recycle') as recycle:
            self.post('cleanup-batches/'+jid+'/confirm',dict(confirm=True));result=self.wait_batch(jid,('stopped','completed'));recycle.assert_not_called()
        self.assertEqual(result['status'],'stopped');self.assertTrue(a.exists());self.assertTrue(b.exists())
    def test_changed_during_hash(self):
        f=self.root/'change.bin';f.write_bytes(b'a'*1000);real=os.stat;calls=[0]
        def changing(*args,**kwargs):
            calls[0]+=1
            if calls[0]==2:f.write_bytes(b'changed')
            return real(*args,**kwargs)
        with patch.object(scanner.os,'stat',side_effect=changing):
            with self.assertRaises(OSError):scanner.hash_file(f)
if __name__=='__main__':unittest.main()
