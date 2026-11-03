"""Grouped, offline project and personal explorers. Same-name grouping is not content verification."""
import hashlib,os,re,unicodedata,json,threading,time
from . import storage
from .folders import inventory,identity
from .storage import connect,rows
from .folders import projects,personal,folder_summary,summary_context

def normalized(name):return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',name).strip()).casefold()

def grouped(kind='projects',q='',offset=0,limit=100):
    if kind not in ('projects','personal'):raise ValueError('Invalid library')
    with summary_context():
        data=projects('all',limit=100000) if kind=='projects' else personal('all',limit=100000)
        seeds=data['items'] if kind=='projects' else [*data.get('collections',[]),*data['items']]
        folders,drives=folder_summary();by_name={}
        for e in folders:by_name.setdefault(normalized(e['name']),[]).append(e)
        groups={}
        for e in seeds:
            name=normalized(e['name'])
            context='' if kind=='projects' else str(e.get('year','collection'))+':'+str(e.get('month',''))
            key=name+'|'+context
            g=groups.setdefault(key,{'key':hashlib.sha256((kind+'|'+key).encode()).hexdigest()[:24],'name':e['name'],'year':e.get('year',''),'category':e.get('project_category',''),'locations':{},'kind':kind})
            candidates=by_name.get(name,[e]) if kind=='projects' else [e]
            for candidate in candidates:
                d=drives[candidate['drive_id']];location=dict(candidate,path=os.path.join(d['root'],candidate['rel']),drive_name=d['name'])
                g['locations'][(location['drive_id'],location['rel'])]=location
        evidence=analysis_snapshot()
        result=[]
        for g in groups.values():
            locations=list(g['locations'].values());locations.sort(key=lambda e:(e['drive_name'].casefold(),e['rel'].casefold()))
            # Known aliases of the same registered volume path share one physical location.
            unique={}
            for e in locations:
                d=drives[e['drive_id']];identity=(d['volume'],os.path.normcase(os.path.normpath(os.path.join(d['relative_root'] or '.',e['rel']))))
                unique.setdefault(identity,e)
            locations=list(unique.values());g['locations']=locations;g['location_count']=len(locations);g['drive_count']=len({e['drive_id'] for e in locations});g['bytes']=sum(e['bytes'] for e in locations);g['files']=max((e['files'] for e in locations),default=0)
            if q and normalized(q) not in normalized(' '.join([g['name'],str(g['year']),g['category'],*[e['path']+' '+e['drive_name'] for e in locations]])):continue
            g['duplicate_analysis']=group_evidence(locations,evidence)
            result.append(g)
        result.sort(key=lambda g:({'critical':0,'inventory':1,'name':2,'none':3}[g['duplicate_analysis']['level']],g['name'].casefold()))
        return {'analysis_status':evidence['status'],'coverage':catalog_coverage(drives),'kind':kind,'total':len(result),'items':result[offset:offset+limit],'offset':offset,'limit':limit}

def cover(eid):
    with connect() as c:
        e=c.execute('SELECT drive_id,rel,is_dir FROM entries WHERE id=? AND present=1',(eid,)).fetchone()
        if not e:raise ValueError('This entry is no longer in the catalog')
        if not e['is_dir']:
            row=c.execute('SELECT id,name,ext,category,is_dir,mtime,drive_id FROM entries WHERE id=?',(eid,)).fetchone();return {'entry':dict(row)}
        prefix=e['rel']+os.sep;upper=prefix[:-1]+chr(ord(prefix[-1])+1)
        for extensions in [('jpg','jpeg','png','webp','avif'),('mov','mp4','mkv','webm','tif','tiff','gif','bmp','psd')]:
            row=c.execute('SELECT id,name,ext,category,is_dir,mtime,drive_id FROM entries WHERE drive_id=? AND present=1 AND is_dir=0 AND rel>=? AND rel<? AND ext IN ('+','.join('?' for _ in extensions)+') LIMIT 1',(e['drive_id'],prefix,upper,*extensions)).fetchone()
            if row:return {'entry':dict(row)}
    return {'entry':None}


_analysis_lock=threading.Lock()
_analysis_cache={}
_analysis_workers=[]

def wait_for_analysis():
    for worker in list(_analysis_workers):worker.join()
    _analysis_workers.clear()

def catalog_coverage(drives):
    return {'registered_drives':len(drives),'indexed_drives':sum(bool(d['last_scan']) for d in drives.values()),'drives':[{'id':d['id'],'name':d['name'],'last_scan':d['last_scan']} for d in drives.values()]}

def analysis_revision():
    with connect() as c:
        return hashlib.sha256(json.dumps([str(storage.DB),[tuple(r) for r in c.execute('SELECT id,last_scan FROM drives ORDER BY id')],tuple(c.execute('SELECT count(*),sum(size),max(verified),count(sha256) FROM entries WHERE present=1').fetchone())],default=str).encode()).hexdigest()

def analysis_snapshot():
    revision=analysis_revision();key=str(storage.DB)
    with _analysis_lock:
        cached=_analysis_cache.get(key)
        if cached and cached['revision']==revision:return cached
        target=storage.DATA/'manifests'/'library-analysis.json'
        try:
            saved=json.loads(target.read_text(encoding='utf-8'))
            if saved['revision']==revision:
                _analysis_cache[key]=saved;return saved
        except (OSError,ValueError,KeyError):pass
        pending={'revision':revision,'status':'analyzing','folders':{}}
        _analysis_cache[key]=pending
        def run():
            try:
                folders,_=inventory()
                report={'revision':revision,'status':'completed','updated':time.time(),'folders':{str(e['id']):{k:e[k] for k in ('inventory_signature','content_signature','fully_hashed','files','bytes')} for e in folders}}
                if analysis_revision()!=revision:
                    with _analysis_lock:_analysis_cache.pop(key,None)
                    return
                temp=target.with_suffix('.json.tmp')
                with temp.open('w',encoding='utf-8') as stream:
                    json.dump(report,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
                os.replace(temp,target)
                with _analysis_lock:_analysis_cache[key]=report
            except Exception as exc:
                with _analysis_lock:_analysis_cache[key]={'revision':revision,'status':'failed','error':str(exc),'folders':{}}
        worker=threading.Thread(target=run,daemon=True,name='library-catalog-analysis')
        _analysis_workers[:]=[w for w in _analysis_workers if w.is_alive()]
        _analysis_workers.append(worker);worker.start()
        return pending

def group_evidence(locations,snapshot):
    buckets={};verified={}
    for location in locations:
        e=snapshot['folders'].get(str(location['id']))
        if not e or not e['files']:continue
        buckets.setdefault(e['inventory_signature'],[]).append(e)
        if e['fully_hashed']:verified.setdefault(e['content_signature'],[]).append(e)
    # Savings are per group, never a sum across overlapping projects.
    exact=max((items[0]['bytes']*(len(items)-1) for items in verified.values() if len(items)>1),default=0)
    potential=max((items[0]['bytes']*(len(items)-1) for items in buckets.values() if len(items)>1),default=0)
    level='critical' if any(len(items)>1 for items in verified.values()) else 'inventory' if any(len(items)>1 for items in buckets.values()) else 'name' if len(locations)>1 else 'none'
    return {'level':level,'verified_savings':exact,'potential_savings':potential,'status':snapshot['status'],'updated':snapshot.get('updated')}
