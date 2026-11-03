"""Validated local file actions. Deletion always requires a one-use reviewed plan."""
import os,stat,time,secrets,json,threading
from pathlib import Path
from .storage import connect,rows
from .volumes import locate,longpath
from .scanner import lock as scan_lock
from .cleanup import idle,recycle,signature,operation_lock
trash_plans={}
def resolve_entry(eid,volumes):
    with connect() as c:
        e=c.execute('SELECT * FROM entries WHERE id=? AND present=1',(eid,)).fetchone()
        if not e:raise ValueError('This file is not available in the current catalog')
        e=dict(e);d=dict(c.execute('SELECT * FROM drives WHERE id=?',(e['drive_id'],)).fetchone())
    root=locate(d,volumes)
    if not root:raise ValueError('The drive is disconnected or its identity is ambiguous')
    rel=Path(e['rel']);base=Path(root).absolute()
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:raise ValueError('Path is outside the registered root')
    target=base/rel
    current=target
    while True:
        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):raise ValueError('Links and junctions are not allowed')
        if current==current.parent:break
        current=current.parent
    target=target.resolve(strict=True)
    if not target.is_relative_to(base.resolve()):raise ValueError('Path is outside the registered root')
    s=os.stat(longpath(str(target)),follow_symlinks=False)
    if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):raise ValueError('Unsupported file type')
    return e,d,str(target),s

def check_trash(e,path,s):
    if bool(e['is_dir'])!=stat.S_ISDIR(s.st_mode):raise ValueError('The file type changed. Update the index.')
    if s.st_nlink>1 and not e['is_dir']:raise ValueError('Hard links require manual review')
    if e['is_dir']:
        with os.scandir(longpath(path)) as it:
            if next(it,None) is not None:raise ValueError('Only empty folders can be removed. Folders with contents are kept.')
    elif s.st_size!=e['size'] or s.st_mtime!=e['mtime']:raise ValueError('The file changed. Update the index before recycling it.')

def prepare_trash(eid,volumes):
    e,d,path,s=resolve_entry(eid,volumes);idle({d['volume']});check_trash(e,path,s)
    item={'id':eid,'path':path,'drive_name':d['name'],'drive_id':d['id'],'volume':d['volume'],'rel':e['rel'],'is_dir':bool(e['is_dir']),'size':s.st_size if not e['is_dir'] else 0,'signature':signature(s),'expires':time.time()+900}
    with operation_lock:
        for key in list(trash_plans):
            if trash_plans[key]['expires']<time.time():trash_plans.pop(key,None)
        key=secrets.token_urlsafe(32);trash_plans[key]=item
    return {'plan_id':key,**{k:v for k,v in item.items() if k!='signature'}}

def execute_trash(key,confirmed,volumes,backup):
    if confirmed is not True:raise ValueError('Explicit confirmation is required')
    with operation_lock,scan_lock:
        item=trash_plans.get(key)
        if not item or item['expires']<time.time():raise ValueError('This preview expired or was already used')
        e,d,path,s=resolve_entry(item['id'],volumes);idle({d['volume']});check_trash(e,path,s)
        if path!=item['path'] or d['volume']!=item['volume'] or signature(s)!=item['signature']:raise ValueError('The file changed after confirmation. Prepare a new preview.')
        snapshot=backup();trash_plans.pop(key,None);action=secrets.token_hex(16)
        with connect() as c:c.execute('INSERT INTO cleanup_actions VALUES(?,?,?,?,?,?,?,?)',(action,time.time(),e['id'],path,'',None,'pending',json.dumps(snapshot)))
        try:
            # Validate again after the backup, before asking Windows to recycle.
            e2,d2,path2,s2=resolve_entry(e['id'],volumes)
            if path2!=path or signature(s2)!=item['signature']:raise ValueError('The file changed and was not removed')
            check_trash(e2,path2,s2);recycle(path)
        except Exception as error:
            with connect() as c:c.execute('UPDATE cleanup_actions SET status=? WHERE id=?',('error: '+str(error),action))
            raise ValueError(str(error))
        with connect() as c:
            relative=os.path.normcase(os.path.normpath(os.path.join(d['relative_root'],e['rel'])))
            for alias in rows(c,'SELECT e.id,e.rel,d.relative_root FROM entries e JOIN drives d ON d.id=e.drive_id WHERE d.volume=? AND e.inode=? AND e.present=1',(d['volume'],e['inode'])):
                if os.path.normcase(os.path.normpath(os.path.join(alias['relative_root'],alias['rel'])))==relative:c.execute('UPDATE entries SET present=0 WHERE id=?',(alias['id'],))
            c.execute("UPDATE cleanup_actions SET status='recycled' WHERE id=?",(action,))
        return {'ok':True,'path':path,'backup':snapshot,'message':'Moved to the Windows Recycle Bin. Restore it there if needed, then update the index.'}
