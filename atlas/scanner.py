import os, stat, time, uuid, threading, json, mimetypes, hashlib
from pathlib import Path
from .storage import connect,rows,DATA
from .volumes import locate,longpath,volumes
from .io_budget import lease,configure
GROUPS={
'Photography':'jpg jpeg png tif tiff heic raw cr2 cr3 nef arw dng gif webp bmp',
'Video':'mov mp4 mxf avi mkv webm m4v', 'Audio':'wav mp3 flac aac ogg aiff',
'Graphic Design':'psd psb ai eps svg indd', 'Documents':'pdf doc docx txt rtf odt',
'Presentations':'ppt pptx keynote odp','Spreadsheets':'xls xlsx csv ods',
'3D Assets':'obj fbx glb gltf blend stl','Fonts':'ttf otf woff woff2',
'Archives':'zip rar 7z tar gz','Code / Web':'html css js ts json py c cpp rs md xml yml',
'Project Files':'prproj aep aepx drp fcpxproj sesx'}
TYPES={ext:cat for cat,exts in GROUPS.items() for ext in exts.split()}
workers={}; lock=threading.Lock()
def launch(drive_id,scan_id=None):
    with lock:
        with connect() as c:
            existing=c.execute("SELECT id FROM scans WHERE drive_id=? AND status IN ('running','paused','pausing')",(drive_id,)).fetchone()
            if existing and not scan_id: raise ValueError('Resume or cancel the existing scan first')
            sid=scan_id or str(uuid.uuid4())
            if sid in workers: raise ValueError('Scan is already running')
            if not scan_id:
                c.execute('INSERT INTO scans(id,drive_id,status,started) VALUES(?,?,?,?)',(sid,drive_id,'running',time.time()))
                c.execute('INSERT INTO queue VALUES(?,?)',(sid,''))
            else:
                previous=c.execute('SELECT status FROM scans WHERE id=? AND drive_id=?',(sid,drive_id)).fetchone()
                if not previous or previous[0]!='paused': raise ValueError('Only paused scans can be resumed')
                c.execute("UPDATE scans SET status='running',error=NULL WHERE id=?",(sid,))
        control=threading.Event(); workers[sid]=control
        threading.Thread(target=run,args=(sid,drive_id,control),daemon=True).start()
    return sid

def run(sid,did,control):
    try:
        with connect() as c: d=dict(c.execute('SELECT * FROM drives WHERE id=?',(did,)).fetchone())
        detected=volumes();configure(detected)
        root=locate(d,detected)
        if not root: raise OSError('Drive disconnected or identity ambiguous')
        while True:
            with connect() as c:
                state=c.execute('SELECT status FROM scans WHERE id=?',(sid,)).fetchone()[0]
                if state=='cancelled': return
                if control.is_set():
                    c.execute("UPDATE scans SET status='paused' WHERE id=?",(sid,)); return
                q=c.execute('SELECT rel FROM queue WHERE scan_id=? LIMIT 1',(sid,)).fetchone()
            if not q: break
            rel=q[0]; path=longpath(os.path.join(root,rel))
            # A directory is checkpointed only after complete enumeration; retries upsert staged rows.
            batch=[]
            try:
                with lease(path,d,cancel=control),os.scandir(path) as it:
                    batch=[]
                    for entry in it:
                        if control.is_set():
                            with connect() as c: c.execute("UPDATE scans SET status='paused' WHERE id=? AND status!='cancelled'",(sid,))
                            return
                        r=os.path.join(rel,entry.name)
                        if entry.name.casefold()=='$recycle.bin':
                            with connect() as c: c.execute('INSERT INTO errors VALUES(?,?,?)',(sid,r,'Windows Recycle Bin excluded from indexing'))
                            continue
                        try:
                            st=entry.stat(follow_symlinks=False)
                            if not st.st_ino: st=os.stat(entry.path,follow_symlinks=False)
                            if entry.is_symlink() or (getattr(st,'st_file_attributes',0)&0x400):
                                with connect() as c: c.execute('INSERT INTO errors VALUES(?,?,?)',(sid,r,'Link or junction skipped'))
                                continue
                            directory=stat.S_ISDIR(st.st_mode); ext=Path(entry.name).suffix.lower().lstrip('.')
                            meta={'mime':mimetypes.guess_type(entry.name)[0]}
                            batch.append((sid,r,rel,entry.name,ext,TYPES.get(ext,'Other'),int(directory),0 if directory else st.st_size,st.st_mtime,getattr(st,'st_birthtime',st.st_ctime if os.name=='nt' else None),str(st.st_ino),getattr(st,'st_file_attributes',0),json.dumps(meta)))
                            if len(batch)>=256: stage(sid,batch); batch=[]
                        except OSError as e:
                            with connect() as c: c.execute('INSERT INTO errors VALUES(?,?,?)',(sid,r,str(e)))
                    if batch: stage(sid,batch)
            except PermissionError as e:
                # Root failures and disconnections remain resumable failures.
                # Protected descendants are warnings, never a reason to stop the archive.
                if not rel or not os.path.isdir(longpath(root)): raise
                if batch: stage(sid,batch)
                with connect() as c:
                    c.execute('INSERT INTO errors VALUES(?,?,?)',(sid,rel,str(e)))
                    c.execute('DELETE FROM queue WHERE scan_id=? AND rel=?',(sid,rel))
                    c.execute('UPDATE scans SET warnings=(SELECT count(*) FROM errors WHERE scan_id=?) WHERE id=?',(sid,sid))
                continue
            if not os.path.isdir(longpath(root)): raise OSError('Drive disconnected during scan')
            with connect() as c:
                c.execute('DELETE FROM queue WHERE scan_id=? AND rel=?',(sid,rel))
                c.execute('UPDATE scans SET folders=folders+1 WHERE id=?',(sid,))
        with connect() as c:
            state=c.execute('SELECT status FROM scans WHERE id=?',(sid,)).fetchone()[0]
            if state=='cancelled': return
            warnings=c.execute('SELECT COUNT(*) FROM errors WHERE scan_id=?',(sid,)).fetchone()[0]
            if not warnings:
                c.execute("INSERT INTO changes SELECT ?,e.rel,'missing' FROM entries e WHERE drive_id=? AND present=1 AND NOT EXISTS(SELECT 1 FROM pending p WHERE p.scan_id=? AND p.rel=e.rel)",(sid,did,sid))
            c.execute("INSERT INTO changes SELECT ?,p.rel,CASE WHEN e.id IS NULL THEN 'added' WHEN e.present=0 THEN 'restored' ELSE 'modified' END FROM pending p LEFT JOIN entries e ON e.drive_id=? AND e.rel=p.rel WHERE p.scan_id=? AND (e.id IS NULL OR e.present=0 OR e.is_dir!=p.is_dir OR e.size!=p.size OR e.mtime!=p.mtime)",(sid,did,sid))
            # Warnings mean absence is inconclusive. Retain previous records as present.
            if not warnings: c.execute('UPDATE entries SET present=0 WHERE drive_id=?',(did,))
            c.execute('''INSERT INTO entries(drive_id,rel,parent,name,ext,category,is_dir,size,mtime,ctime,inode,attrs,metadata,present)
SELECT ?,rel,parent,name,ext,category,is_dir,size,mtime,ctime,inode,attrs,metadata,1 FROM pending WHERE scan_id=?
ON CONFLICT(drive_id,rel) DO UPDATE SET parent=excluded.parent,name=excluded.name,ext=excluded.ext,category=excluded.category,is_dir=excluded.is_dir,size=excluded.size,ctime=excluded.ctime,inode=excluded.inode,attrs=excluded.attrs,metadata=excluded.metadata,present=1,
sha256=CASE WHEN entries.size=excluded.size AND entries.mtime=excluded.mtime THEN entries.sha256 END,
fingerprint=CASE WHEN entries.size=excluded.size AND entries.mtime=excluded.mtime THEN entries.fingerprint END,
verified=CASE WHEN entries.size=excluded.size AND entries.mtime=excluded.mtime THEN entries.verified END,mtime=excluded.mtime''',(did,sid))
            c.execute('UPDATE drives SET last_scan=? WHERE id=?',(time.time(),did))
            c.execute("UPDATE scans SET status=?,finished=?,warnings=? WHERE id=?",('completed_with_warnings' if warnings else 'completed',time.time(),warnings,sid))
            c.execute('DELETE FROM pending WHERE scan_id=?',(sid,))
        # Snapshot failure must never roll back or pause an already published scan.
        try:
            from .snapshots import write
            write(did)
        except Exception:
            import logging
            logging.exception('JSON anchor failed after completed scan %s',sid)
    except Exception as e:
        with connect() as c: c.execute("UPDATE scans SET status='paused',error=? WHERE id=? AND status!='cancelled'",(str(e),sid))
    finally:
        with lock: workers.pop(sid,None)

def stage(sid,batch):
    with connect() as c:
        files_delta=bytes_delta=0
        for row in batch:
            previous=c.execute('SELECT is_dir,size FROM pending WHERE scan_id=? AND rel=?',(sid,row[1])).fetchone()
            files_delta+=(0 if row[6] else 1)-(0 if previous is None or previous[0] else 1)
            bytes_delta+=row[7]-(previous[1] if previous else 0)
        c.executemany('INSERT OR REPLACE INTO pending VALUES('+','.join('?'*13)+')',batch)
        c.executemany('INSERT OR IGNORE INTO queue VALUES(?,?)',[(sid,r[1]) for r in batch if r[6]])
        c.execute('UPDATE scans SET files=files+?,bytes=bytes+?,warnings=(SELECT count(*) FROM errors WHERE scan_id=?) WHERE id=?',(files_delta,bytes_delta,sid,sid))

def hash_file(path,partial=False):
    with lease(path):return _hash_file(path,partial)

def _hash_file(path,partial=False):
    before=os.stat(path); h=hashlib.sha256()
    with open(path,'rb') as f:
        if partial:
            h.update(f.read(65536)); f.seek(max(0,before.st_size-65536)); h.update(f.read(65536))
        else:
            for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    after=os.stat(path)
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino): raise OSError('File changed during hashing')
    return h.hexdigest(),after
