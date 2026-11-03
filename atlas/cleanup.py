"""Explicit, reviewable cleanup of freshly verified byte-identical files only."""

import os,time,secrets,threading,json,stat

from pathlib import Path

from .storage import connect,rows

from .scanner import hash_file,lock as scan_lock

from .volumes import locate,longpath

plans={}

operation_lock=threading.Lock()

TTL=900



def signature(s):

    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_nlink)



def recycle(path):

    if os.name!='nt': raise ValueError('Cleanup requires the Windows Recycle Bin')

    # Never fall back to legacy SHFileOperation or permanent deletion.

    try:
        import pythoncom
        from win32com.shell import shell,shellcon
        from send2trash.win.IFileOperationProgressSink import FileOperationProgressSink
    except ImportError: raise ValueError('Run SETUP.bat to install native Windows recycling support')
    class RecycleOnlySink(FileOperationProgressSink):
        def PreDeleteItem(self,flags,item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise pythoncom.com_error(-2147467259,'Only moving to the Recycle Bin is allowed',None,None)
            return 0
    pythoncom.CoInitialize()
    try:
        operation=pythoncom.CoCreateInstance(shell.CLSID_FileOperation,None,pythoncom.CLSCTX_INPROC_SERVER,shell.IID_IFileOperation)
        # RECYCLEONDELETE + EARLYFAILURE; never retry without these flags.
        operation.SetOperationFlags(shellcon.FOF_NOCONFIRMATION|shellcon.FOF_NOERRORUI|shellcon.FOF_SILENT|shellcon.FOFX_EARLYFAILURE|0x20000000|0x00080000)
        sink_object=RecycleOnlySink()
        sink=pythoncom.WrapObject(sink_object,shell.IID_IFileOperationProgressSink)
        item=shell.SHCreateItemFromParsingName(path,None,shell.IID_IShellItem)
        operation.DeleteItem(item,sink)
        result=operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted() or not sink_object.newItem:
            raise OSError('Windows did not confirm the move to the Recycle Bin')
        if os.path.lexists(path): raise OSError('The file is still at its original location')
    finally: pythoncom.CoUninitialize()

def safe_path(entry,drive,volumes):

    root=locate(drive,volumes)

    if not root: raise ValueError('Connect every drive in the group; volume identity is unavailable')

    rel=Path(entry['rel'])

    if rel.is_absolute() or '..' in rel.parts or not rel.parts: raise ValueError('Invalid archived path')

    base=Path(root).absolute();target=base/rel

    for part in (base,*base.parents):

        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()): raise ValueError('Cleanup does not follow roots through links or junctions')

    current=target

    while current!=base:

        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()): raise ValueError('Cleanup does not follow links or junctions')

        current=current.parent

    resolved=target.resolve(strict=True)

    if not resolved.is_relative_to(base.resolve()): raise ValueError('Path is outside the registered root')

    s=os.stat(longpath(str(resolved)),follow_symlinks=False)

    if not stat.S_ISREG(s.st_mode): raise ValueError('Only regular files can be cleaned up')

    if s.st_nlink!=1: raise ValueError('This group contains hard links and needs manual review')

    return str(resolved),s



def idle(volumes):

    with connect() as c:

        for row in c.execute("SELECT d.volume FROM scans s JOIN drives d ON d.id=s.drive_id WHERE s.status IN ('running','paused','pausing')"):

            if row[0] in volumes: raise ValueError('Finish or cancel pending scans on these drives before cleanup')



def prepare(ids,volumes):

    if not isinstance(ids,list) or not 2<=len(ids)<=100: raise ValueError('Choose a group of 2 to 100 copies')

    ids=list(dict.fromkeys(int(x) for x in ids))

    with connect() as c:

        entries=[]

        for eid in ids:

            e=c.execute('SELECT * FROM entries WHERE id=? AND present=1 AND is_dir=0',(eid,)).fetchone()

            if not e: raise ValueError('The catalog changed. Refresh the duplicate list.')

            e=dict(e);d=dict(c.execute('SELECT * FROM drives WHERE id=?',(e['drive_id'],)).fetchone());entries.append((e,d))

    idle({d['volume'] for e,d in entries})

    unique={};expected=None;now=time.time()

    for e,d in entries:

        path,s=safe_path(e,d,volumes)

        if s.st_size!=e['size'] or s.st_mtime!=e['mtime']: raise ValueError('A file changed since the scan. Update its index.')

        identity=(s.st_dev,s.st_ino) if s.st_ino else os.path.normcase(path)

        if identity in unique: continue

        digest,after=hash_file(longpath(path))

        if signature(s)!=signature(after): raise ValueError('A file changed during verification')

        if expected is not None and digest!=expected: raise ValueError('This group contains different versions. It cannot be cleaned up as an exact duplicate, and quality cannot be chosen automatically.')

        expected=digest

        unique[identity]={'id':e['id'],'path':path,'drive_id':d['id'],'drive_name':d['name'],'volume':d['volume'],'rel':e['rel'],'size':s.st_size,'mtime':s.st_mtime,'signature':signature(s),'sha256':digest}

        with connect() as c: c.execute('UPDATE entries SET sha256=?,verified=? WHERE id=?',(digest,now,e['id']))

    items=list(unique.values())

    if len(items)<2: raise ValueError('These roots point to the same file; there are no independent copies to clean up')

    items.sort(key=lambda e:(-e['mtime'],e['path'].casefold()))

    plan={'items':items,'sha256':expected,'expires':now+TTL,'recommended_keep_id':items[0]['id']}

    with operation_lock:

        for key in list(plans):

            if plans[key]['expires']<now:plans.pop(key,None)

        key=secrets.token_urlsafe(32);plans[key]=plan

    return {'plan_id':key,'expires':plan['expires'],'recommended_keep_id':items[0]['id'],'items':[{k:v for k,v in e.items() if k!='signature'} for e in items],'bytes_to_recycle':sum(e['size'] for e in items[1:]),'reason':'Identical contents verified with SHA-256. Quality is the same; the latest modification is the suggested keeper.'}



def validate(item,volumes,expected):

    with connect() as c:

        e=c.execute('SELECT * FROM entries WHERE id=? AND present=1',(item['id'],)).fetchone()

        if not e: raise ValueError('The catalog changed. Prepare a new preview.')

        e=dict(e);d=dict(c.execute('SELECT * FROM drives WHERE id=?',(e['drive_id'],)).fetchone())

    path,s=safe_path(e,d,volumes)

    if os.path.normcase(path)!=os.path.normcase(item['path']) or signature(s)!=item['signature']: raise ValueError('A file changed after the preview and was not recycled')

    digest,after=hash_file(longpath(path))

    if digest!=expected or signature(after)!=item['signature']: raise ValueError('Contents changed. Prepare a new preview.')

    return path



def execute(key,keep_id,confirmed,volumes,backup):

    if confirmed is not True: raise ValueError('Explicit confirmation is required')

    with operation_lock,scan_lock:

        plan=plans.get(key)

        if not plan or plan['expires']<time.time(): raise ValueError('This preview expired or was already used. Verify again.')

        items=plan['items'];keeper=next((e for e in items if e['id']==keep_id),None)

        if keeper is None: raise ValueError('Choose one copy to keep')

        idle({e['volume'] for e in items})

        for item in items: validate(item,volumes,plan['sha256'])

        snapshot=backup()

        plans.pop(key,None) # A submitted plan cannot be replayed, even after partial failure.

        removed=[];errors=[]

        for item in items:

            if item['id']==keep_id:continue

            audit_id=secrets.token_hex(16)

            try:

                validate(keeper,volumes,plan['sha256'])

                path=validate(item,volumes,plan['sha256'])

                with connect() as c:

                    c.execute('INSERT INTO cleanup_actions VALUES(?,?,?,?,?,?,?,?)',(audit_id,time.time(),item['id'],path,keeper['path'],plan['sha256'],'pending',json.dumps(snapshot)))

                recycle(path)

                # Mark all catalog aliases of this exact pathname; preserve annotations/history.

                with connect() as c:

                    d=c.execute('SELECT relative_root FROM drives WHERE id=?',(item['drive_id'],)).fetchone()

                    target_path=os.path.normcase(os.path.normpath(os.path.join(d[0],item['rel'])))

                    for alias in rows(c,'SELECT e.id,e.rel,d.relative_root FROM entries e JOIN drives d ON d.id=e.drive_id WHERE d.volume=? AND e.present=1 AND e.inode=?',(item['volume'],str(item['signature'][1]))):

                        alias_path=os.path.normcase(os.path.normpath(os.path.join(alias['relative_root'],alias['rel'])))

                        if alias_path==target_path:c.execute('UPDATE entries SET present=0 WHERE id=?',(alias['id'],))

                    c.execute("UPDATE cleanup_actions SET status='recycled' WHERE id=?",(audit_id,))

                removed.append({'id':item['id'],'path':path})

            except Exception as e:

                with connect() as c: c.execute('UPDATE cleanup_actions SET status=? WHERE id=?',('error: '+str(e),audit_id))

                errors.append({'path':item['path'],'message':str(e)})

                break # Stop immediately after a failure; never silently continue.

        return {'recycled':removed,'errors':errors,'kept':keeper['path'],'backup':snapshot,'message':'To undo, restore files from the Windows Recycle Bin. The SQLite backup restores the catalog only. Space is not freed until the Recycle Bin is emptied.'}
