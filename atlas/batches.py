"""Background review and confirmed recycling for global/project duplicate cleanup."""
import json,os,time,secrets,threading
from pathlib import Path
from .storage import connect,rows
from .fileops import resolve_entry,check_trash
from .cleanup import signature,idle,operation_lock,recycle
from .scanner import hash_file,lock as scan_lock
from .volumes import longpath
active={};MAX_ERRORS=100

def update(jid,**fields):
    with connect() as c:
        for key,value in fields.items():c.execute('UPDATE cleanup_jobs SET '+key+'=? WHERE id=?',(value,jid))
        c.execute('UPDATE cleanup_jobs SET updated=? WHERE id=?',(time.time(),jid))

def start(scope_id,volumes):
    scope=None
    if scope_id is not None:
        with connect() as c:
            e=c.execute('SELECT * FROM entries WHERE id=? AND is_dir=1 AND present=1',(int(scope_id),)).fetchone()
            if not e:raise ValueError('The project must be an indexed folder')
            scope=dict(e)
    jid=secrets.token_urlsafe(24);cancel=threading.Event()
    with connect() as c:c.execute('INSERT INTO cleanup_jobs(id,status,scope_id,scope_name,created,updated) VALUES(?,?,?,?,?,?)',(jid,'analyzing',scope_id,scope['name'] if scope else 'All connected catalogs',time.time(),time.time()))
    active[jid]=cancel
    threading.Thread(target=analyze,args=(jid,scope,volumes,cancel),daemon=True).start()
    return {'id':jid}

def warning(jid,message):
    with connect() as c:
        c.execute('UPDATE cleanup_jobs SET warnings=warnings+1 WHERE id=?',(jid,))
        if c.execute('SELECT count(*) FROM cleanup_job_errors WHERE job_id=?',(jid,)).fetchone()[0]<MAX_ERRORS:c.execute('INSERT INTO cleanup_job_errors VALUES(?,?,?)',(jid,time.time(),str(message)))

def analyze(jid,scope,volumes,cancel):
    try:
        where='e.present=1 AND e.is_dir=0 AND e.size>0';args=[]
        if scope:
            where+=' AND e.drive_id=? AND substr(e.rel,1,length(?)+1)=?||?';args=[scope['drive_id'],scope['rel'],scope['rel'],os.sep]
        with connect() as c:
            # Equal size is necessary for byte-identical contents, even with different filenames.
            sql='SELECT e.id FROM entries e WHERE '+where+' AND e.size IN(SELECT e.size FROM entries e WHERE '+where+' GROUP BY e.size HAVING count(*)>1) ORDER BY e.size DESC,e.id'
            total=c.execute('SELECT count(*) FROM ('+sql+')',args+args).fetchone()[0];update(jid,total=total)
            processed=0
            for row in c.execute(sql,args+args):
                if cancel.is_set():update(jid,status='cancelled');return
                eid=row['id']
                try:
                    e,d,path,before=resolve_entry(eid,volumes);idle({d['volume']});check_trash(e,path,before)
                    digest,after=hash_file(longpath(path))
                    if signature(before)!=signature(after):raise ValueError('Changed during verification')
                    identity=d['volume']+'|'+(str(before.st_ino) if before.st_ino else os.path.normcase(path))
                    payload={'id':eid,'path':path,'drive_name':d['name'],'drive_id':d['id'],'volume':d['volume'],'rel':e['rel'],'relative_root':d['relative_root'],'size':before.st_size,'mtime':before.st_mtime,'signature':signature(before),'sha256':digest}
                    with connect() as out:
                        out.execute('INSERT OR IGNORE INTO cleanup_job_files VALUES(?,?,?,?,?,?)',(jid,eid,digest,identity,json.dumps(payload), 'review'))
                        out.execute('UPDATE entries SET sha256=?,verified=? WHERE id=?',(digest,time.time(),eid))
                except Exception as error:warning(jid,str(eid)+': '+str(error))
                processed+=1;update(jid,processed=processed)
        if cancel.is_set():update(jid,status='cancelled');return
        with connect() as c:
            # Root aliases share identity; keep just one representation in this proposal.
            c.execute('DELETE FROM cleanup_job_files WHERE job_id=? AND rowid NOT IN(SELECT min(rowid) FROM cleanup_job_files WHERE job_id=? GROUP BY identity)',(jid,jid))
            c.execute('DELETE FROM cleanup_job_files WHERE job_id=? AND sha256 NOT IN(SELECT sha256 FROM cleanup_job_files WHERE job_id=? GROUP BY sha256 HAVING count(*)>1)',(jid,jid))
            groups=c.execute('SELECT count(DISTINCT sha256) FROM cleanup_job_files WHERE job_id=?',(jid,)).fetchone()[0]
            count=0;size=0
            for row in c.execute('SELECT payload FROM cleanup_job_files WHERE job_id=?',(jid,)):
                p=json.loads(row[0]);count+=1;size+=p['size']
            keep_size=0
            for row in c.execute('SELECT payload FROM cleanup_job_files WHERE job_id=? GROUP BY sha256',(jid,)):keep_size+=json.loads(row[0])['size']
        update(jid,status='ready',groups=groups,remove_count=count-groups,bytes=size-keep_size,expires=time.time()+1800)
    except Exception as error:update(jid,status='failed',error=str(error))
    finally:
        if active.get(jid) is cancel:active.pop(jid,None)

def status(jid,offset=0):
    with connect() as c:
        job=c.execute('SELECT * FROM cleanup_jobs WHERE id=?',(jid,)).fetchone()
        if not job:raise ValueError('Plan not found')
        job=dict(job);job['errors']=rows(c,'SELECT message FROM cleanup_job_errors WHERE job_id=? ORDER BY created LIMIT 100',(jid,));job['items']=[]
        if job['status'] in ('ready','cleaning','completed','stopped','failed'):
            hashes=rows(c,'SELECT sha256 FROM cleanup_job_files WHERE job_id=? GROUP BY sha256 ORDER BY sha256 LIMIT 10 OFFSET ?',(jid,max(0,offset)))
            for h in hashes:
                items=[dict(json.loads(r['payload']),action_status=r['status']) for r in rows(c,'SELECT payload,status FROM cleanup_job_files WHERE job_id=? AND sha256=?',(jid,h['sha256']))]
                items.sort(key=lambda x:(-x['mtime'],x['path'].casefold()))
                job['items'].append({'sha256':h['sha256'],'keep':items[0],'discard':items[1:]})
    return job

def validate(item,volumes):
    e,d,path,st=resolve_entry(item['id'],volumes);check_trash(e,path,st)
    if path!=item['path'] or d['volume']!=item['volume'] or list(signature(st))!=item['signature']:raise ValueError('The file changed after the plan was prepared: '+item['path'])
    digest,after=hash_file(longpath(path))
    if digest!=item['sha256'] or list(signature(after))!=item['signature']:raise ValueError('Contents changed: '+path)
    return path

def confirm(jid,confirmed,volumes,backup):
    if confirmed is not True:raise ValueError('Explicit confirmation is required')
    with operation_lock,connect() as c:
        job=c.execute('SELECT * FROM cleanup_jobs WHERE id=?',(jid,)).fetchone()
        if not job or job['status']!='ready' or job['expires']<time.time():raise ValueError('This plan expired or was already used. Analyze again.')
        if not job['groups']:raise ValueError('No verified exact duplicates to clean up')
        c.execute("UPDATE cleanup_jobs SET status='cleaning' WHERE id=?",(jid,))
    cancel=threading.Event();active[jid]=cancel
    threading.Thread(target=clean,args=(jid,volumes,backup,cancel),daemon=True).start()
    return {'ok':True,'id':jid}

def clean(jid,volumes,backup,cancel):
    try:
        with operation_lock,scan_lock:
            with connect() as c:
                all_volumes={json.loads(r[0])['volume'] for r in c.execute('SELECT payload FROM cleanup_job_files WHERE job_id=?',(jid,))}
                hashes=[r[0] for r in c.execute('SELECT DISTINCT sha256 FROM cleanup_job_files WHERE job_id=?',(jid,))]
            idle(all_volumes);snapshot=backup();update(jid,backup=json.dumps(snapshot))
            for digest in hashes:
                if cancel.is_set():update(jid,status='stopped');return
                with connect() as c:items=[json.loads(r[0]) for r in c.execute('SELECT payload FROM cleanup_job_files WHERE job_id=? AND sha256=?',(jid,digest))]
                items.sort(key=lambda x:(-x['mtime'],x['path'].casefold()));keeper=items[0]
                # Validate every member before recycling anything in this group.
                for item in items:validate(item,volumes)
                with connect() as c:c.execute("UPDATE cleanup_job_files SET status='kept' WHERE job_id=? AND entry_id=?",(jid,keeper['id']))
                for item in items[1:]:
                    if cancel.is_set():update(jid,status='stopped');return
                    validate(keeper,volumes);path=validate(item,volumes);aid=secrets.token_hex(16)
                    with connect() as c:c.execute('INSERT INTO cleanup_actions VALUES(?,?,?,?,?,?,?,?)',(aid,time.time(),item['id'],path,keeper['path'],digest,'pending',json.dumps(snapshot)))
                    try:recycle(path)
                    except Exception as error:
                        with connect() as c:c.execute('UPDATE cleanup_actions SET status=? WHERE id=?',('error: '+str(error),aid))
                        raise
                    with connect() as c:
                        relative=os.path.normcase(os.path.normpath(os.path.join(item['relative_root'],item['rel'])))
                        for alias in rows(c,'SELECT e.id,e.rel,d.relative_root FROM entries e JOIN drives d ON d.id=e.drive_id WHERE d.volume=? AND e.inode=? AND e.present=1',(item['volume'],str(item['signature'][1]))):
                            if os.path.normcase(os.path.normpath(os.path.join(alias['relative_root'],alias['rel'])))==relative:c.execute('UPDATE entries SET present=0 WHERE id=?',(alias['id'],))
                        c.execute("UPDATE cleanup_actions SET status='recycled' WHERE id=?",(aid,));c.execute("UPDATE cleanup_job_files SET status='recycled' WHERE job_id=? AND entry_id=?",(jid,item['id']));c.execute('UPDATE cleanup_jobs SET recycled=recycled+1,updated=? WHERE id=?',(time.time(),jid))
            update(jid,status='completed')
    except Exception as error:warning(jid,str(error));update(jid,status='stopped',error=str(error))
    finally:
        if active.get(jid) is cancel:active.pop(jid,None)

def cancel(jid):
    event=active.get(jid)
    if event:event.set()
    with connect() as c:c.execute("UPDATE cleanup_jobs SET status='cancelled' WHERE id=? AND status='ready'",(jid,))
    return {'ok':True}
