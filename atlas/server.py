import os,json,time,uuid,sqlite3,threading,io,csv,secrets,logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from .storage import BASE,DATA,DB,connect,init,rows,Connection
from .volumes import volumes,identify,locate,longpath
from .scanner import launch,workers,hash_file
logging.basicConfig(filename=DATA/'logs'/'atlas.log',level=logging.INFO)
TOKEN=secrets.token_urlsafe(32)
catalog_query_slots=threading.BoundedSemaphore(2)
volume_cache={'at':0,'items':[]}
volume_probe_lock=threading.Lock()

def refresh_hardware():
    if not volume_probe_lock.acquire(blocking=False):return
    try:volume_cache.update(at=time.time(),items=volumes())
    finally:volume_probe_lock.release()

def available():
    if time.time()-volume_cache['at']>60 and not volume_probe_lock.locked():
        threading.Thread(target=refresh_hardware,daemon=True).start()
    from .io_budget import configure
    configure(volume_cache['items'])
    return volume_cache['items']

def drive(d):
    detected=available();root=locate(d,detected);v=next((v for v in detected if v['volume']==d['volume']),None) if root else None
    with connect() as c:
        if v:
            c.execute('INSERT OR REPLACE INTO drive_hardware VALUES(?,?,?,?,?,?,?)',(d['id'],v.get('filesystem'),v.get('capacity'),v.get('free'),str(v.get('bus_type') or ''),v.get('hardware'),volume_cache['at'] or time.time()))
        hardware=c.execute('SELECT * FROM drive_hardware WHERE drive_id=?',(d['id'],)).fetchone()
    return dict(d,connected=bool(root),current_root=root,technical=dict(hardware) if hardware else None)
@asynccontextmanager
async def lifespan(app):
    init()
    from .updates import start
    updates_stop,updates_thread=start(available)
    yield
    updates_stop.set();updates_thread.join(timeout=5)
    from .library import wait_for_analysis
    wait_for_analysis()
    from .batches import active as batch_workers
    for event in list(batch_workers.values()): event.set()
    for event in list(workers.values()): event.set()
    until=time.time()+10
    while (workers or batch_workers) and time.time()<until: time.sleep(.1)
app=FastAPI(lifespan=lifespan)
@app.middleware('http')
async def guard(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0]
    if host not in ('127.0.0.1','localhost','testserver'): return JSONResponse({'detail':'Invalid host'},403)
    if request.method not in ('GET','HEAD') and request.headers.get('x-atlas-token')!=TOKEN: return JSONResponse({'detail':'Session token required'},403)
    try: return await call_next(request)
    except (ValueError,OSError,sqlite3.Error) as e:
        logging.exception('Request failed'); return JSONResponse({'detail':str(e)},400)
@app.get('/api/session')
def session(): return {'token':TOKEN,'version':'1.0.0'}
@app.get('/')
def index(): return FileResponse(BASE/'index.html')
@app.get('/app.js')
def js(): return FileResponse(BASE/'app.js',media_type='text/javascript')
@app.get('/style.css')
def css(): return FileResponse(BASE/'style.css',media_type='text/css')
@app.get('/api/volumes')
def get_volumes():
    refresh_hardware(); return volume_cache['items']
@app.get('/api/drives')
def get_drives():
    with connect() as c:
        return [drive(d) for d in rows(c,'SELECT d.*,count(e.id) files,coalesce(sum(e.size),0) bytes FROM drives d LEFT JOIN entries e ON e.drive_id=d.id AND e.present=1 AND e.is_dir=0 GROUP BY d.id')]
@app.post('/api/drives')
def add_drive(body:dict):
    root=str(Path(body['root']).resolve())
    if not Path(root).is_dir(): raise ValueError('Choose an existing folder or volume')
    v,relative=identify(root,available()); did=str(uuid.uuid4())
    with connect() as c:
        if c.execute('SELECT 1 FROM drives WHERE volume=? AND relative_root=?',(v['volume'],relative)).fetchone(): raise ValueError('This root is already registered')
        c.execute('INSERT INTO drives VALUES(?,?,?,?,?,?,?,?,?,?,?)',(did,body.get('name') or v['label'],body.get('label',''),body.get('description',''),body.get('tags',''),root,v['volume'],v['physical'],relative,None,time.time()))
    return {'id':did}
@app.patch('/api/drives/{did}')
def edit_drive(did:str,body:dict):
    with connect() as c:
        for k in ('name','label','description','tags'):
            if k in body: c.execute(f'UPDATE drives SET {k}=? WHERE id=?',(str(body[k]),did))
    return {'ok':True}
@app.post('/api/drives/{did}/reconnect')
def reconnect(did:str,body:dict):
    root=str(Path(body['root']).resolve()); v,relative=identify(root,available())
    if not Path(root).is_dir(): raise ValueError('Root unavailable')
    if body.get('confirm') is not True: raise ValueError('Explicit identity confirmation required')
    with connect() as c: c.execute('UPDATE drives SET root=?,volume=?,physical=?,relative_root=? WHERE id=?',(root,v['volume'],v['physical'],relative,did))
    return {'ok':True}
@app.post('/api/drives/{did}/scan')
def scan(did:str): return {'id':launch(did)}
@app.get('/api/scans')
def scans():
    with connect() as c: return rows(c,'SELECT s.*,d.name drive_name FROM scans s JOIN drives d ON d.id=s.drive_id WHERE NOT EXISTS(SELECT 1 FROM scan_visibility v WHERE v.scan_id=s.id) ORDER BY started DESC LIMIT 100')
@app.post('/api/scans/{sid}/{action}')
def control(sid:str,action:str):
    with connect() as c:
        s=c.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone()
        if not s: raise HTTPException(404)
        if action=='resume': return {'id':launch(s['drive_id'],sid)}
        if action not in ('pause','cancel'): raise ValueError('Unknown action')
        if s['status'] not in ('running','paused','pausing'): raise ValueError('Scan already finished')
        next_status='cancelled' if action=='cancel' else 'pausing'
        c.execute("UPDATE scans SET status=?,finished=CASE WHEN ?='cancelled' THEN ? ELSE finished END WHERE id=?",(next_status,next_status,time.time(),sid))
        if sid in workers: workers[sid].set()
        elif action=='pause': c.execute("UPDATE scans SET status='paused' WHERE id=?",(sid,))
    return {'ok':True}
@app.get('/api/scans/{sid}/changes')
def changes(sid:str,offset:int=0,limit:int=100,kind:str=''):
    limit=min(250,max(1,limit));offset=max(0,offset)
    if kind not in ('','added','modified','missing','restored'):raise ValueError('Invalid change type')
    with connect() as c:
        scan=c.execute('SELECT s.*,d.name drive_name FROM scans s JOIN drives d ON d.id=s.drive_id WHERE s.id=?',(sid,)).fetchone()
        if not scan:raise HTTPException(404,'The scan does not exist')
        where='scan_id=?'+(' AND kind=?' if kind else '');args=[sid]+([kind] if kind else [])
        counts={r['kind']:r['n'] for r in c.execute('SELECT kind,count(*) n FROM changes WHERE scan_id=? GROUP BY kind',(sid,))}
        warning_count=c.execute('SELECT count(*) FROM errors WHERE scan_id=?',(sid,)).fetchone()[0]
        return {'scan':dict(scan),'counts':counts,'total':sum(counts.values()) if not kind else counts.get(kind,0),'offset':offset,'limit':limit,'changes':rows(c,'SELECT * FROM changes WHERE '+where+' ORDER BY rowid LIMIT ? OFFSET ?',args+[limit,offset]),'warnings':rows(c,'SELECT * FROM errors WHERE scan_id=? ORDER BY rowid LIMIT ? OFFSET ?',(sid,limit,offset)),'warning_total':warning_count}
def query_entries(params):
    clauses=['e.present=1']; args=[]
    for key,col in [('drive','e.drive_id'),('category','e.category'),('ext','e.ext'),('parent','e.parent')]:
        if key in params and params[key]!='': clauses.append(col+'=?'); args.append(params[key])
    if params.get('directory')=='root': clauses.append("e.parent=''")
    if params.get('folder'):
        clauses.append("(e.rel=? OR substr(e.rel,1,length(?)+1)=?||?)"); args.extend([params['folder']]*3+[os.sep])
    for key,op,col in [('min_size','>=','e.size'),('max_size','<=','e.size'),('after','>=','e.mtime'),('before','<=','e.mtime')]:
        if params.get(key): clauses.append(col+op+'?'); args.append(float(params[key]))
    for key,col in [('tag','a.tags'),('project','a.project')]:
        if params.get(key): clauses.append(col+' LIKE ?'); args.append('%'+params[key]+'%')
    if params.get('favorite'): clauses.append('a.favorite=1')
    q=params.get('q','').strip()
    if q:
        if q.startswith('*.'): clauses.append('e.ext=?'); args.append(q[2:].lower())
        else:
            for match in __import__('re').finditer(r'"([^"]*)"|(\S+)',q):
                term=match.group(1) if match.group(1) is not None else match.group(2)
                term=term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                columns=('e.name','e.rel','a.tags','a.project','a.collection','a.client','a.notes','d.name','d.label','d.tags','d.description')
                clauses.append('('+' OR '.join(col+" LIKE ? ESCAPE '\\'" for col in columns)+" OR e.sha256=? OR EXISTS(SELECT 1 FROM project_profiles pp WHERE pp.drive_id=e.drive_id AND (e.rel=pp.rel OR substr(e.rel,1,length(pp.rel)+1)=pp.rel||?) AND (pp.category LIKE ? ESCAPE '\\' OR pp.year LIKE ? ESCAPE '\\')))")
                args.extend(['%'+term+'%']*len(columns)+[term,os.sep,'%'+term+'%','%'+term+'%'])
    return ' AND '.join(clauses),args
@app.get('/api/entries')
def entries(request:Request):
    with catalog_query_slots:return query_file_entries(request)

def query_file_entries(request):
    params=dict(request.query_params); where,args=query_entries(params)
    key=params.get('sort','name')
    direction=params.get('direction', 'desc' if key in ('size','date','modified') else 'asc')
    if direction not in ('asc','desc'):raise ValueError('Invalid sort order')
    # Connection state needs only drive identities, not another full catalog aggregation.
    with connect() as c:registered=rows(c,'SELECT * FROM drives')
    connected={d['id']:bool(locate(d,available())) for d in registered}
    sort_columns={'name':'e.name COLLATE NOCASE','drive':'d.name COLLATE NOCASE','location':"(d.root || e.rel) COLLATE NOCASE",'size':'e.size','date':'e.mtime','modified':'e.mtime','type':'e.ext COLLATE NOCASE'}
    order_args=[]
    if key=='status':
        online=[did for did,value in connected.items() if value]
        sort='CASE WHEN e.drive_id IN ('+','.join('?' for _ in online)+') THEN 0 ELSE 1 END' if online else '0+0'
        order_args=online
    else:sort=sort_columns.get(key,sort_columns['name'])
    order=(('e.is_dir DESC,' if 'direction' not in params or params.get('directories_first')=='1' else '')+sort+' '+direction.upper()+',e.id ASC')
    offset=max(0,int(params.get('offset',0))); limit=min(250,max(1,int(params.get('limit',100))))
    with connect() as c:
        join=' FROM entries e LEFT JOIN annotations a ON a.drive_id=e.drive_id AND a.rel=e.rel JOIN drives d ON d.id=e.drive_id WHERE '+where
        count_join=join if 'a.' in where else join.replace(' LEFT JOIN annotations a ON a.drive_id=e.drive_id AND a.rel=e.rel','')
        groups=rows(c,'SELECT d.id,d.name,d.label,e.ext,e.is_dir,count(*) matches,coalesce(sum(e.size),0) bytes'+count_join+' GROUP BY d.id,e.ext,e.is_dir',args)
        buckets={};formats={};logical_bytes=folders=0
        for g in groups:
            bucket=buckets.setdefault(g['id'],{'id':g['id'],'name':g['name'],'label':g['label'],'matches':0});bucket['matches']+=g['matches']
            if g['is_dir']:folders+=g['matches']
            else:
                logical_bytes+=g['bytes'];ext=g['ext'] or '(no extension)';formats[ext]=formats.get(ext,0)+g['matches']
        by_drive=sorted(buckets.values(),key=lambda g:-g['matches']);count=sum(d['matches'] for d in by_drive)
        online=sum(g['matches'] for g in groups if connected.get(g['id'],False))
        data=rows(c,'SELECT e.*,d.name drive_name,d.root archived_root,a.tags,a.project,a.favorite'+join+' ORDER BY '+order+' LIMIT ? OFFSET ?',args+order_args+[limit,offset])
    for e in data:e['connected']=connected.get(e['drive_id'],False)
    return {'total':count,'items':data,'by_drive':by_drive,'summary':{'bytes':logical_bytes,'folders':folders,'files':count-folders,'format_count':len(formats),'formats':sorted([{'ext':k,'count':v} for k,v in formats.items()],key=lambda g:-g['count'])[:6],'online':online,'offline':count-online}}
@app.get('/api/entries/{eid}')
def detail(eid:int):
    with connect() as c:
        e=dict(c.execute('SELECT * FROM entries WHERE id=?',(eid,)).fetchone() or {})
        if not e: raise HTTPException(404)
        d=drive(dict(c.execute('SELECT * FROM drives WHERE id=?',(e['drive_id'],)).fetchone()))
        a=dict(c.execute('SELECT * FROM annotations WHERE drive_id=? AND rel=?',(e['drive_id'],e['rel'])).fetchone() or {})
        inherited=[]
        for parent in rows(c,'SELECT * FROM annotations WHERE drive_id=? AND inherit=1',(e['drive_id'],)):
            if e['rel'].startswith(parent['rel']+os.sep): inherited.append(parent)
        related=rows(c,'SELECT id,name,drive_id,rel FROM entries WHERE present=1 AND id!=? AND name=? AND size=? LIMIT 30',(eid,e['name'],e['size']))
        totals={'files':0,'bytes':0}
        if e['is_dir']:
            prefix=e['rel']+os.sep;upper=prefix[:-1]+chr(ord(prefix[-1])+1)
            totals=dict(c.execute('SELECT count(*) files,coalesce(sum(size),0) bytes FROM entries WHERE drive_id=? AND present=1 AND is_dir=0 AND rel>=? AND rel<?',(e['drive_id'],prefix,upper)).fetchone())
    return dict(e,drive=d,annotation=a,inherited=inherited,related=related,folder_totals=totals,original_path=os.path.join(d['root'],e['rel']))
@app.post('/api/annotations')
def annotate(body:dict):
    with connect() as c:
        for eid in body['ids'][:1000]:
            e=c.execute('SELECT drive_id,rel FROM entries WHERE id=?',(int(eid),)).fetchone()
            if not e: continue
            c.execute('INSERT OR IGNORE INTO annotations(drive_id,rel) VALUES(?,?)',tuple(e))
            for key in ('tags','project','collection','client','notes','favorite','inherit'):
                if key in body: c.execute(f'UPDATE annotations SET {key}=? WHERE drive_id=? AND rel=?',(body[key],*e))
    return {'ok':True}
def original(eid):
    from .fileops import resolve_entry
    e,d,path,st=resolve_entry(eid,available())
    return e,longpath(path)
@app.post('/api/entries/{eid}/open')
def open_entry(eid:int,body:dict|None=None):
    volume_cache['at']=0
    e,path=original(eid)
    if os.name!='nt':raise ValueError('This action requires Windows')
    if (body or {}).get('mode')=='open':
        allowed=set('jpg jpeg png tif tiff heic heif avif webp bmp gif raw cr2 cr3 nef arw dng psd psb ai eps indd pdf doc docx odt rtf txt ppt pptx xls xlsx csv obj fbx glb gltf blend stl wav mp3 flac aac aiff ogg mov mp4 mxf avi mkv webm m4v zip rar 7z'.split())
        if not e['is_dir'] and e['ext'] not in allowed:raise ValueError('Use Show in Explorer for this format')
        os.startfile(path)
    else:__import__('subprocess').Popen(['explorer.exe','/select,',path])
    return {'ok':True}
@app.post('/api/entries/{eid}/hash')
def hash_entry(eid:int,body:dict):
    e,path=original(eid)
    if e['is_dir']: raise ValueError('Choose a file')
    partial=body.get('partial',False); digest,st=hash_file(path,partial)
    if st.st_size!=e['size'] or st.st_mtime!=e['mtime']: raise ValueError('File changed since scan; rescan first')
    with connect() as c: c.execute('UPDATE entries SET '+('fingerprint' if partial else 'sha256')+'=?,verified=? WHERE id=?',(digest,time.time(),eid))
    return {'hash':digest,'level':'strong candidate' if partial else 'full SHA-256'}
@app.get('/api/duplicates')
def duplicates():
    with connect() as c:
        groups=rows(c,'SELECT name,size,count(*) copies FROM entries WHERE present=1 AND is_dir=0 AND size>0 GROUP BY name,size HAVING count(*)>1 ORDER BY size DESC LIMIT 100')
        result=[]
        for g in groups:
            items=rows(c,'SELECT e.*,d.name drive_name,d.volume,d.physical,d.relative_root FROM entries e JOIN drives d ON d.id=e.drive_id WHERE e.present=1 AND e.is_dir=0 AND e.name=? AND e.size=?',(g['name'],g['size']))
            unique={}
            for e in items:
                key=(e['volume'],e['inode']) if e['inode'] and e['inode']!='0' else (e['volume'],os.path.normcase(os.path.normpath(os.path.join(e['relative_root'],e['rel']))))
                unique[key]=e
            items=list(unique.values())
            if len(items)<2: continue
            hashes={e['sha256'] for e in items}; fingerprints={e['fingerprint'] for e in items}
            level='verified' if None not in hashes and len(hashes)==1 else ('strong candidate' if None not in fingerprints and len(fingerprints)==1 else 'candidate')
            physical={e['physical'] for e in items if e['physical']}
            result.append(dict(g,items=items,copies=len(items),level=level,physical_copies=len(physical) if all(e['physical'] for e in items) else None,potential_savings=g['size']*(len(items)-1)))
        # Full hashes also find identical contents with different names.
        for g in rows(c,"SELECT sha256,size,count(*) copies FROM entries WHERE present=1 AND is_dir=0 AND sha256 IS NOT NULL GROUP BY sha256,size HAVING count(DISTINCT name)>1 LIMIT 100"):
            items=rows(c,'SELECT e.*,d.name drive_name,d.physical,d.volume FROM entries e JOIN drives d ON d.id=e.drive_id WHERE e.sha256=? AND e.present=1',(g['sha256'],))
            unique={(e['volume'],e['inode']):e for e in items}; items=list(unique.values())
            if len(items)>1: result.append(dict(g,name='Identical contents · different names',items=items,copies=len(items),level='verified',physical_copies=None,potential_savings=g['size']*(len(items)-1)))
        return result
@app.get('/api/analytics')
def analytics(drive_id:str='',category:str='',project:str='',after:float=0,before:float=0):
    where,args=query_entries(dict(drive=drive_id,category=category,project=project,after=after,before=before))
    join=' FROM entries e JOIN drives d ON d.id=e.drive_id LEFT JOIN annotations a ON a.drive_id=e.drive_id AND a.rel=e.rel WHERE '+where
    with connect() as c:
        return {'totals':dict(c.execute('SELECT count(*) entries,coalesce(sum(CASE WHEN is_dir=0 THEN 1 ELSE 0 END),0) files,coalesce(sum(is_dir),0) folders,coalesce(sum(size),0) bytes'+join,args).fetchone()),'categories':rows(c,'SELECT category,count(*) files,sum(size) bytes'+join+' AND is_dir=0 GROUP BY category ORDER BY bytes DESC',args),'years':rows(c,"SELECT strftime('%Y',mtime,'unixepoch') year,count(*) files,sum(size) bytes"+join+' AND is_dir=0 GROUP BY year',args),'largest':rows(c,'SELECT e.id,e.name,e.rel,e.ext,e.category,e.is_dir,e.size,e.mtime,e.drive_id,d.name drive_name,d.root archived_root'+join+' AND is_dir=0 ORDER BY size DESC LIMIT 20',args),'oldest':rows(c,'SELECT e.id,e.name,e.rel,e.ext,e.category,e.is_dir,e.mtime,e.size,e.drive_id,d.name drive_name,d.root archived_root'+join+' AND is_dir=0 ORDER BY mtime LIMIT 20',args),'recent':rows(c,'SELECT e.id,e.name,e.rel,e.ext,e.category,e.is_dir,e.mtime,e.size,e.drive_id,d.name drive_name,d.root archived_root'+join+' AND is_dir=0 ORDER BY mtime DESC LIMIT 20',args),'types':rows(c,'SELECT ext,count(*) files,sum(size) bytes'+join+' AND is_dir=0 GROUP BY ext ORDER BY bytes DESC LIMIT 100',args),'empty_folders':rows(c,'SELECT e.id,e.name,e.rel,e.drive_id,d.name drive_name,d.root archived_root'+join+' AND is_dir=1 AND NOT EXISTS(SELECT 1 FROM entries ch WHERE ch.drive_id=e.drive_id AND ch.parent=e.rel AND ch.present=1) LIMIT 100',args)}
@app.get('/api/export')
def export(request:Request):
    from starlette.responses import StreamingResponse
    params=dict(request.query_params); where,args=query_entries(params); fmt=params.get('format','csv')
    def stream():
        with connect() as c:
            cursor=c.execute('SELECT e.*,d.name drive_name,a.tags,a.project,a.notes FROM entries e JOIN drives d ON d.id=e.drive_id LEFT JOIN annotations a ON a.drive_id=e.drive_id AND a.rel=e.rel WHERE '+where,args)
            if fmt=='json':
                yield '['; first=True
                for row in cursor:
                    yield ('' if first else ',')+json.dumps(dict(row),ensure_ascii=False); first=False
                yield ']'
            else:
                buf=io.StringIO(); writer=csv.writer(buf); writer.writerow([d[0] for d in cursor.description]); yield '\ufeff'+buf.getvalue()
                for row in cursor:
                    buf.seek(0); buf.truncate(0); writer.writerow([("'"+v if v.startswith(('=','+','-','@')) else v) if isinstance(v,str) else v for v in row]); yield buf.getvalue()
    return StreamingResponse(stream(),media_type='application/json' if fmt=='json' else 'text/csv',headers={'Content-Disposition':f'attachment; filename="disk-atlas.{fmt}"'})
@app.post('/api/backup')
def backup():
    dest=DATA/'backups'/('atlas-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.sqlite3')
    with connect() as source, sqlite3.connect(dest,factory=Connection) as target: source.backup(target)
    return {'name':dest.name,'path':str(dest)}
@app.post('/api/drives/{did}/manifest')
def manifest(did:str):
    dest=DATA/'manifests'/(did+'.sqlite3')
    if dest.exists(): dest.unlink()
    with connect() as source,sqlite3.connect(dest,factory=Connection) as target: source.backup(target)
    with sqlite3.connect(dest,factory=Connection) as c:
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('DELETE FROM drives WHERE id!=?',(did,)); c.execute('DELETE FROM entries WHERE drive_id!=?',(did,)); c.execute('DELETE FROM annotations WHERE drive_id!=?',(did,))
        c.execute('DELETE FROM update_policies WHERE drive_id!=?',(did,))
        c.execute('DELETE FROM scan_origins WHERE scan_id NOT IN(SELECT id FROM scans WHERE drive_id=?)',(did,))
        c.execute('DELETE FROM drive_hardware WHERE drive_id!=?',(did,))
        c.execute('DELETE FROM json_snapshots')
        c.execute('DELETE FROM project_profiles WHERE drive_id!=?',(did,))
        c.execute('DELETE FROM project_containers WHERE entry_id NOT IN(SELECT id FROM entries)')
        c.execute('DELETE FROM scans WHERE drive_id!=?',(did,))
        c.execute('DELETE FROM cleanup_actions')
        c.execute('DELETE FROM cleanup_jobs')
        c.execute('DELETE FROM cleanup_job_files')
        c.execute('DELETE FROM cleanup_job_errors')
        c.execute('DELETE FROM thumbnails')
        c.execute('DELETE FROM scan_visibility')
        for table in ('pending','queue'): c.execute(f'DELETE FROM {table}')
        c.execute('DELETE FROM errors WHERE scan_id NOT IN(SELECT id FROM scans)'); c.execute('DELETE FROM changes WHERE scan_id NOT IN(SELECT id FROM scans)')
        c.commit(); c.execute('VACUUM')
    return {'path':str(dest),'name':dest.name}
@app.get('/api/download/{kind}/{name}')
def download(kind:str,name:str):
    if kind not in ('manifests','backups') or Path(name).name!=name: raise HTTPException(400)
    path=DATA/kind/name
    if not path.is_file(): raise HTTPException(404)
    return FileResponse(path,filename=name)
@app.post('/api/import')
async def import_catalog(request:Request):
    temp=DATA/'manifests'/('import-'+uuid.uuid4().hex+'.sqlite3')
    try:
        size=0
        with temp.open('wb') as out:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>8*1024**3: raise ValueError('Manifest exceeds 8 GB import limit')
                out.write(chunk)
        # Untrusted databases are data only; never execute their schema or triggers.
        with sqlite3.connect('file:'+temp.as_posix()+'?mode=ro',uri=True,factory=Connection) as src,connect() as dst:
            src.row_factory=sqlite3.Row; src.execute('PRAGMA trusted_schema=OFF')
            if src.execute('PRAGMA user_version').fetchone()[0]!=1: raise ValueError('Unsupported schema version')
            if src.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Invalid database')
            imported=0
            for row in src.execute('SELECT * FROM drives'):
                row=dict(row); old=dst.execute('SELECT volume,relative_root FROM drives WHERE id=?',(row['id'],)).fetchone()
                if old and tuple(old)!=(row['volume'],row['relative_root']): raise ValueError('Drive ID conflict: import rejected')
                columns=['id','name','label','description','tags','root','volume','physical','relative_root','last_scan','created']
                dst.execute('INSERT OR IGNORE INTO drives('+','.join(columns)+') VALUES('+','.join('?'*len(columns))+')',[row[k] for k in columns]); imported+=1
            for table,columns in [('entries','drive_id rel parent name ext category is_dir size mtime ctime inode attrs metadata sha256 fingerprint verified present'.split()),('annotations','drive_id rel tags project collection client notes favorite inherit'.split())]:
                for row in src.execute('SELECT '+','.join(columns)+' FROM '+table):
                    dst.execute('INSERT OR IGNORE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?'*len(columns))+')',tuple(row))
            if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_profiles'").fetchone():
                for row in src.execute('SELECT drive_id,rel,year,category FROM project_profiles'): dst.execute('INSERT OR IGNORE INTO project_profiles VALUES(?,?,?,?)',tuple(row))
        return {'ok':True,'drives':imported,'policy':'Existing records and annotations preserved; duplicate imports ignored'}
    finally: temp.unlink(missing_ok=True)
@app.post('/api/shutdown')
def shutdown():
    for event in list(workers.values()): event.set()
    def stop():
        time.sleep(.5)
        if hasattr(app.state,'server'): app.state.server.should_exit=True
    threading.Thread(target=stop,daemon=True).start()
    return {'ok':True}

@app.post('/api/duplicates/prepare-cleanup')
def prepare_cleanup(body:dict):
    from .cleanup import prepare
    volume_cache['at']=0
    return prepare(body.get('ids'),available())

@app.post('/api/duplicates/cleanup')
def cleanup_duplicates(body:dict):
    from .cleanup import execute
    volume_cache['at']=0
    return execute(body.get('plan_id'),body.get('keep_id'),body.get('confirm'),available(),backup)

@app.post('/api/entries/{eid}/prepare-trash')
def prepare_entry_trash(eid:int):
    from .fileops import prepare_trash
    volume_cache['at']=0
    return prepare_trash(eid,available())

@app.post('/api/trash')
def trash_entry(body:dict):
    from .fileops import execute_trash
    volume_cache['at']=0
    return execute_trash(body.get('plan_id'),body.get('confirm'),available(),backup)

@app.get('/api/media/status')
def media_status():
    import shutil
    return {'video_available':bool(shutil.which('ffmpeg')),'images_available':True}

@app.post('/api/entries/{eid}/thumbnail')
def generate_thumbnail(eid:int):
    from .media import generate
    return generate(eid,available())

@app.get('/api/entries/{eid}/thumbnail')
def thumbnail(eid:int):
    from .media import cached
    path=cached(eid)
    if not path:raise HTTPException(404,'Thumbnail unavailable; enable previews with the drive connected')
    return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'private, max-age=300'})

@app.post('/api/scans-history/clear')
def clear_scan_history(body:dict):
    from .scanner import lock
    if body.get('confirm') is not True:raise ValueError('Confirm history cleanup')
    with lock,connect() as c:
        ids=body.get('ids')
        if ids is not None and (not isinstance(ids,list) or not 1<=len(ids)<=100):raise ValueError('Invalid scan selection')
        selected=rows(c,"SELECT id,status FROM scans"+(' WHERE id IN ('+','.join('?'*len(ids))+')' if ids else ''),ids or [])
        if ids and any(s['status'] not in ('completed','completed_with_warnings','cancelled') or s['id'] in workers for s in selected):raise ValueError('Pause and cancel the scan before hiding it from history')
        count=0
        for scan in selected:
            sid=scan['id']
            if scan['status'] not in ('completed','completed_with_warnings','cancelled') or sid in workers:continue
            if c.execute('SELECT 1 FROM scan_visibility WHERE scan_id=?',(sid,)).fetchone():continue
            c.execute('INSERT INTO scan_visibility VALUES(?,?)',(sid,time.time()))
            c.execute('DELETE FROM pending WHERE scan_id=?',(sid,));c.execute('DELETE FROM queue WHERE scan_id=?',(sid,));count+=1
    return {'ok':True,'cleared':count,'message':'History hidden. Catalogs and scan events are kept.'}

@app.post('/api/scans-history/restore')
def restore_scan_history():
    with connect() as c:c.execute('DELETE FROM scan_visibility')
    return {'ok':True}

@app.get('/api/duplicates/folders')
def folder_duplicates():
    from .folders import duplicates
    return duplicates()

@app.get('/api/projects')
def project_index(root:str='',q:str='',offset:int=0,limit:int=100):
    from .folders import projects
    return projects(root,q,max(0,offset),min(100,max(1,limit)))

@app.post('/api/projects/{eid}/profile')
def edit_project_profile(eid:int,body:dict):
    year=str(body.get('year','')).strip();category=str(body.get('category','')).strip()
    if year and (not year.isdigit() or not 1900<=int(year)<=2100):raise ValueError('The year must be between 1900 and 2100')
    if len(category)>120:raise ValueError('The category is too long')
    with connect() as c:
        e=c.execute('SELECT drive_id,rel FROM entries WHERE id=? AND is_dir=1 AND present=1',(eid,)).fetchone()
        if not e:raise ValueError('This folder is not in the current catalog')
        c.execute('INSERT OR REPLACE INTO project_profiles VALUES(?,?,?,?)',(*e,year,category))
    return {'ok':True}

@app.post('/api/projects/{eid}/container')
def project_container(eid:int):
    with connect() as c:
        if not c.execute('SELECT 1 FROM entries WHERE id=? AND is_dir=1 AND present=1',(eid,)).fetchone():raise ValueError('Choose a folder from the catalog')
        c.execute('INSERT OR IGNORE INTO project_containers VALUES(?)',(eid,))
    return {'ok':True}

@app.get('/api/personal')
def personal_index(root:str='',q:str='',offset:int=0,limit:int=100):
    from .folders import personal
    return personal(root,q,max(0,offset),min(100,max(1,limit)))

@app.post('/api/cleanup-batches')
def start_cleanup_batch(body:dict):
    from .batches import start
    volume_cache['at']=0
    return start(body.get('scope_id'),available())

@app.get('/api/cleanup-batches/{jid}')
def cleanup_batch_status(jid:str,offset:int=0):
    from .batches import status
    return status(jid,max(0,offset))

@app.post('/api/cleanup-batches/{jid}/confirm')
def confirm_cleanup_batch(jid:str,body:dict):
    from .batches import confirm
    volume_cache['at']=0
    return confirm(jid,body.get('confirm'),available(),backup)

@app.post('/api/cleanup-batches/{jid}/cancel')
def cancel_cleanup_batch(jid:str):
    from .batches import cancel
    return cancel(jid)

@app.get('/api/updates')
def update_status(drive_id:str='',offset:int=0):
    from .updates import policies,history
    return {'policies':policies(),'history':history(drive_id,max(0,offset)),'scheduler':'local','requires_running_app':True}

@app.post('/api/drives/{did}/update-policy')
def update_policy(did:str,body:dict):
    from .updates import configure
    return configure(did,body.get('enabled'),body.get('interval_hours',24))

@app.post('/api/updates/run')
def update_now(body:dict):
    did=str(body.get('drive_id',''));started=[];skipped=[]
    volume_cache['at']=0;connected=available()
    with connect() as c:targets=rows(c,'SELECT * FROM drives'+(' WHERE id=?' if did else ''),[did] if did else [])
    if did and not targets:raise ValueError('The drive does not exist')
    for d in targets:
        if not locate(d,connected):skipped.append({'id':d['id'],'reason':'Drive disconnected'});continue
        try:started.append({'drive_id':d['id'],'scan_id':launch(d['id'])})
        except ValueError as e:skipped.append({'id':d['id'],'reason':str(e)})
    return {'started':started,'skipped':skipped}

@app.get('/api/drives/{did}/json-snapshot')
def json_snapshot_status(did:str):
    from .snapshots import status
    return status(did)

@app.post('/api/drives/{did}/json-snapshot')
def json_snapshot_generate(did:str):
    from .snapshots import write
    return write(did)

@app.get('/api/io-budget')
def io_budget_status():
    from .io_budget import status
    return status()

@app.get('/api/activity')
def activity_status():
    from .io_budget import status
    with connect() as c:
        scans=rows(c,"SELECT s.id,s.status,s.files,s.folders,s.bytes,d.name FROM scans s JOIN drives d ON d.id=s.drive_id WHERE s.status IN ('running','pausing')")
        batches=rows(c,"SELECT id,status,processed,total FROM cleanup_jobs WHERE status IN ('analyzing','cleaning')")
    return {'scans':scans,'batches':batches,'io':status(),'hardware_refreshing':volume_probe_lock.locked(),'hardware_at':volume_cache['at']}

@app.get('/api/entries/{eid}/location')
def entry_location(eid:int):
    with connect() as c:
        row=c.execute('SELECT id,drive_id,rel,is_dir FROM entries WHERE id=? AND present=1',(eid,)).fetchone()
        if not row:raise HTTPException(404,'This path is not in the catalog')
        return dict(row)

@app.get('/api/overview')
def overview(drive_id:str=''):
    where='present=1'+(' AND drive_id=?' if drive_id else '');args=[drive_id] if drive_id else []
    with connect() as c:
        groups=rows(c,'SELECT is_dir,category,count(*) files,coalesce(sum(size),0) bytes FROM entries WHERE '+where+' GROUP BY is_dir,category',args)
    return {'totals':{'files':sum(g['files'] for g in groups if not g['is_dir']),'folders':sum(g['files'] for g in groups if g['is_dir']),'bytes':sum(g['bytes'] for g in groups if not g['is_dir'])},'categories':sorted([g for g in groups if not g['is_dir']],key=lambda g:-g['bytes'])}

@app.get('/api/library')
def grouped_library(kind:str='projects',q:str='',offset:int=0,limit:int=100):
    from .library import grouped
    return grouped(kind,q,max(0,offset),min(100,max(1,limit)))

@app.get('/api/entries/{eid}/cover')
def library_cover(eid:int):
    from .library import cover
    return cover(eid)
