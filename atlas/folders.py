"""Offline folder inventories, duplicate candidates and project summaries."""
import os,json,hashlib,datetime,re,time,threading
from . import storage
from collections import defaultdict
from .storage import connect,rows
from contextlib import contextmanager

def state():return {'bytes':0,'files':0,'folders':0,'latest':0,'categories':defaultdict(int),'inventory':hashlib.sha256(),'content':hashlib.sha256(),'complete':True}
def feed(digest,payload):
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8');digest.update(len(data).to_bytes(8,'big'));digest.update(data)
def inventory():
    accum={};folders=[]
    with connect() as c:
        drives={d['id']:d for d in rows(c,'SELECT * FROM drives')}
        for d in drives.values():
            scan=c.execute("SELECT warnings FROM scans WHERE drive_id=? AND status IN ('completed','completed_with_warnings') ORDER BY finished DESC LIMIT 1",(d['id'],)).fetchone()
            d['complete']=bool(scan is not None and scan['warnings']==0)
        # Children always precede their parent. Sibling order is stable across roots.
        for row in c.execute('SELECT * FROM entries WHERE present=1 ORDER BY length(rel) DESC,rel COLLATE BINARY'):
            e=dict(row);key=(e['drive_id'],e['parent']);parent=accum.setdefault(key,state())
            if not e['is_dir']:
                parent['bytes']+=e['size'];parent['files']+=1;parent['latest']=max(parent['latest'],e['mtime'] or 0);parent['categories'][e['category']]+=e['size'] or 1
                feed(parent['inventory'],['file',e['name'],e['size']])
                feed(parent['content'],['file',e['name'],e['size'],e['sha256']]);parent['complete'] &= bool(e['sha256'])
            else:
                child=accum.pop((e['drive_id'],e['rel']),state());d=drives[e['drive_id']]
                child_signature=child['inventory'].hexdigest();full=child['content'].hexdigest()
                folder=dict(e,bytes=child['bytes'],files=child['files'],folders=child['folders'],latest=child['latest'] or e['mtime'],dominant_category=max(child['categories'],key=child['categories'].get) if child['categories'] else 'Other',inventory_signature=child_signature,content_signature=full,fully_hashed=bool(child['complete'] and child['files'] and d['complete']),drive_name=d['name'],volume=d['volume'],physical=d['physical'],relative_root=d['relative_root'],archived_root=d['root'])
                folders.append(folder)
                parent['bytes']+=child['bytes'];parent['files']+=child['files'];parent['folders']+=child['folders']+1;parent['latest']=max(parent['latest'],child['latest']);parent['complete'] &= child['complete']
                for category,size in child['categories'].items():parent['categories'][category]+=size
                feed(parent['inventory'],['directory',e['name'],child_signature]);feed(parent['content'],['directory',e['name'],full])
    return folders,drives

_summary_cache={};_summary_lock=threading.Lock();_summary_scope=threading.local()
@contextmanager
def summary_context():
    previous=getattr(_summary_scope,'value',None);previous_roots=getattr(_summary_scope,'roots',None)
    try:
        if previous is None:_summary_scope.value=folder_summary()
        yield
    finally:_summary_scope.value=previous;_summary_scope.roots=previous_roots

def folder_summary():
    scoped=getattr(_summary_scope,'value',None)
    if scoped is not None:return scoped
    with connect() as c:
        ds=rows(c,'SELECT * FROM drives');key=(str(storage.DB),tuple((d['id'],d['name'],d['root'],d['last_scan']) for d in ds),tuple(c.execute('SELECT count(*),coalesce(sum(size),0) FROM entries WHERE present=1').fetchone()))
    with _summary_lock:
        if _summary_cache.get('key')==key:return _summary_cache['value']
        accum={};result=[];drives={d['id']:d for d in ds}
        def base():return {'bytes':0,'files':0,'folders':0,'latest':0,'categories':defaultdict(int)}
        with connect() as c:
            for r in c.execute('SELECT drive_id,parent,category,count(*) files,sum(size) bytes,max(mtime) latest FROM entries WHERE present=1 AND is_dir=0 GROUP BY drive_id,parent,category'):
                a=accum.setdefault((r['drive_id'],r['parent']),base());a['files']+=r['files'];a['bytes']+=r['bytes'] or 0;a['latest']=max(a['latest'],r['latest'] or 0);a['categories'][r['category']]+=r['bytes'] or r['files']
            for row in c.execute('SELECT * FROM entries WHERE present=1 AND is_dir=1 ORDER BY length(rel) DESC,rel'):
                e=dict(row);a=accum.pop((e['drive_id'],e['rel']),base());d=drives[e['drive_id']]
                result.append(dict(e,bytes=a['bytes'],files=a['files'],folders=a['folders'],latest=a['latest'] or e['mtime'],dominant_category=max(a['categories'],key=a['categories'].get) if a['categories'] else 'Other',drive_name=d['name'],archived_root=d['root']))
                parent=accum.setdefault((e['drive_id'],e['parent']),base());parent['bytes']+=a['bytes'];parent['files']+=a['files'];parent['folders']+=a['folders']+1;parent['latest']=max(parent['latest'],a['latest'])
                for cat,size in a['categories'].items():parent['categories'][cat]+=size
        _summary_cache.update(key=key,value=(result,drives));return result,drives

def integral_roots(kind):
    roots=project_roots();folders,_=folder_summary();sizes={str(e['id']):e['bytes'] for e in folders};names=('proyectos','projects') if kind=='projects' else ('personales','personal','memories')
    candidates=[r for r in roots if re.sub(r'[^a-z]','',os.path.basename(r['rel']).lower()) in names]
    # Every matching container matters, including multiple backups on one volume.
    chosen=list(candidates)
    if kind=='projects':
        with connect() as c:explicit={str(r[0]) for r in c.execute('SELECT entry_id FROM project_containers')}
        for r in roots:
            if r['key'] in explicit and not any(r['drive_id']==x['drive_id'] and (r['rel']==x['rel'] or r['rel'].startswith(x['rel']+os.sep)) for x in chosen):chosen.append(r)
    return chosen

def identity(e):
    if e['inode'] and e['inode']!='0':return e['volume'],e['inode']
    return e['volume'],os.path.normcase(os.path.normpath(os.path.join(e['relative_root'],e['rel'])))

def duplicates():
    folders,drives=inventory();by_name=defaultdict(list);by_inventory=defaultdict(list)
    for folder in folders:
        by_name[folder['name'].casefold()].append(folder)
        if folder['files']:by_inventory[folder['inventory_signature']].append(folder)
    result=[];seen=set()
    for bucket in [*by_inventory.values(),*by_name.values()]:
        unique={identity(e):e for e in bucket};items=list(unique.values())
        if len(items)<2:continue
        ids=tuple(sorted(e['id'] for e in items))
        if ids in seen:continue
        seen.add(ids)
        matching=len({e['inventory_signature'] for e in items})==1
        verified=all(e['fully_hashed'] for e in items) and len({e['content_signature'] for e in items})==1
        items.sort(key=lambda e:(-e['latest'],e['drive_name'],e['rel']))
        result.append({'kind':'folder','name':items[0]['name'] if len({e['name'] for e in items})==1 else 'Folders with matching inventories','copies':len(items),'level':'verified hashes' if verified else 'matching inventory' if matching else 'same name candidate','matching_inventory':matching,'bytes':max(e['bytes'] for e in items),'potential_savings':items[0]['bytes']*(len(items)-1) if matching else None,'items':items[:20],'truncated':len(items)>20})
    result.sort(key=lambda g:(-g['bytes'],-g['copies'],g['name']))
    return result[:100]

def project_roots():
    cached=getattr(_summary_scope,'roots',None)
    if cached is not None:return cached
    with connect() as c:
        ds=rows(c,'SELECT * FROM drives');roots=[]
        for d in ds:roots.append({'key':d['id'],'drive_id':d['id'],'rel':'','label':d['name']+' · Root'})
        folders=rows(c,"SELECT e.id,e.drive_id,e.rel,e.name,e.parent,d.name drive_name FROM entries e JOIN drives d ON d.id=e.drive_id WHERE e.is_dir=1 AND e.present=1 AND (e.parent='' OR e.id IN(SELECT entry_id FROM project_containers) OR lower(e.name) LIKE '%proyecto%' OR lower(e.name) LIKE '%project%' OR lower(e.name) LIKE '%personal%' OR lower(e.name) LIKE '%memories%') ORDER BY e.name")
        for e in folders:roots.append({'key':str(e['id']),'entry_id':e['id'],'drive_id':e['drive_id'],'rel':e['rel'],'label':e['drive_name']+' · '+e['rel']})
    if getattr(_summary_scope,'value',None) is not None:_summary_scope.roots=roots
    return roots

def _projects(root_key='',q='',offset=0,limit=100):
    if root_key=='all':
        roots=integral_roots('projects');items=[]
        for root in roots:items.extend(projects(root['key'],q,0,100000)['items'])
        unique={(e['drive_id'],e['rel']):e for e in items};items=sorted(unique.values(),key=lambda e:(e['year'],e['name'].casefold(),e['drive_name']),reverse=True)
        return {'roots':project_roots(),'selected_root':'all','total':len(items),'bytes':sum(e['bytes'] for e in items),'years':len({e['year'] for e in items}),'items':items[offset:offset+limit]}
    roots=project_roots();chosen=next((r for r in roots if r['key']==root_key),None)
    if not chosen:
        preferred=[r for r in roots if re.sub(r'[^a-z]','',os.path.basename(r['rel']).lower()) in ('proyectos','projects')]
        chosen=preferred[0] if preferred else None
    folders,drives=folder_summary();selected=[]
    with connect() as c:profiles={(r['drive_id'],r['rel']):r for r in rows(c,'SELECT * FROM project_profiles')}
    for e in folders:
        if chosen:
            if e['drive_id']!=chosen['drive_id']:continue
            # Containers with year subfolders expose projects beneath those years.
            if e['parent']==chosen['rel'] and not re.fullmatch(r'(19|20)\d{2}',e['name']):pass
            elif os.path.dirname(e['parent'])==chosen['rel'] and re.fullmatch(r'(19|20)\d{2}',os.path.basename(e['parent'])):pass
            else:continue
        elif e['parent']!='':continue
        profile=profiles.get((e['drive_id'],e['rel']),{})
        year=profile.get('year') or next((part for part in PathParts(e['rel']) if re.fullmatch(r'(19|20)\d{2}',part)),None)
        inferred=not bool(year)
        stamp=e['latest'] if e['latest'] and e['latest']<=time.time()+86400 else e['mtime']
        if not year:year=str(datetime.datetime.fromtimestamp(stamp,datetime.timezone.utc).year) if stamp and stamp<=time.time()+86400 else 'No year'
        category=profile.get('category') or e['dominant_category']
        item=dict(e,year=year,year_inferred=inferred,project_category=category,category_inferred=not bool(profile.get('category')))
        if q and q.casefold() not in ' '.join(str(item[k]) for k in ('name','rel','drive_name','year','project_category')).casefold():continue
        selected.append(item)
    selected.sort(key=lambda e:(e['year'],e['bytes'],e['name']),reverse=True)
    return {'roots':roots,'selected_root':chosen['key'] if chosen else '', 'total':len(selected),'bytes':sum(e['bytes'] for e in selected),'years':len({e['year'] for e in selected}),'items':selected[offset:offset+limit]}
def PathParts(path):return re.split(r'[\\/]',path)

MONTHS=['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
def month_number(name):
    m=re.match(r'^(0?[1-9]|1[0-2])(?:\D|$)',name.strip('- ._ '))
    if m:return int(m.group(1))
    for i,month in enumerate(MONTHS,1):
        if month.casefold() in name.casefold():return i
    return 0

def _personal(root_key='',q='',offset=0,limit=100):
    if root_key=='all':
        roots=integral_roots('personal');events=[];collections=[]
        for root in roots:
            data=personal(root['key'],q,0,100000);events.extend(data['items']);collections.extend(data['collections'])
        events=list({(e['drive_id'],e['rel']):e for e in events}.values());collections=list({(e['drive_id'],e['rel']):e for e in collections}.values());events.sort(key=lambda e:(e['year'],e['month'],e['event_name']),reverse=True)
        return {'roots':project_roots(),'selected_root':'all','items':events[offset:offset+limit],'collections':collections,'total':len(events),'bytes':sum(e['bytes'] for e in events)+sum(e['bytes'] for e in collections)}
    roots=project_roots();chosen=next((r for r in roots if r['key']==root_key),None)
    folders,drives=folder_summary();collections=[];events=[]
    if not chosen:
        candidates=[r for r in roots if re.sub(r'[^a-z]','',os.path.basename(r['rel']).lower()) in ('personales','personal','memories')]
        sizes={str(e['id']):e['bytes'] for e in folders}
        chosen=max(candidates,key=lambda r:sizes.get(r['key'],0),default=None)
    by_parent=defaultdict(list)
    for e in folders:by_parent[(e['drive_id'],e['parent'])].append(e)
    if not chosen:return {'roots':roots,'selected_root':'','collections':[],'items':[],'total':0,'bytes':0}
    prefix=chosen['rel']+os.sep if chosen['rel'] else ''
    for e in by_parent[(chosen['drive_id'],chosen['rel'])]:
        if not re.fullmatch(r'(19|20)\d{2}',e['name']):collections.append(e);continue
        year=e['name'];children=by_parent[(e['drive_id'],e['rel'])]
        if not children:events.append(dict(e,year=year,month=0,event_name=e['name']));continue
        for child in children:
            month=month_number(child['name']);occasions=by_parent[(child['drive_id'],child['rel'])] if month else []
            if occasions:
                events.extend(dict(event,year=year,month=month,event_name=event['name']) for event in occasions)
            else:events.append(dict(child,year=year,month=month,event_name=child['name']))
        # Preserve loose media/documents directly inside year/month containers as occasions.
        with connect() as c:
            parents=[e['rel']]+[child['rel'] for child in children if month_number(child['name']) and by_parent[(child['drive_id'],child['rel'])]]
            for parent in parents:
                for f in rows(c,'SELECT * FROM entries WHERE drive_id=? AND parent=? AND is_dir=0 AND present=1',(e['drive_id'],parent)):
                    events.append(dict(f,bytes=f['size'],files=1,folders=0,year=year,month=month_number(os.path.basename(parent)) if parent!=e['rel'] else 0,event_name=f['name'],drive_name=e['drive_name'],archived_root=e['archived_root']))
    if q:
        events=[e for e in events if q.casefold() in ' '.join([e['name'],e['rel'],e['year'],e['drive_name']]).casefold()]
        collections=[e for e in collections if q.casefold() in (e['name']+' '+e['rel']).casefold()]
    events.sort(key=lambda e:(e['year'],e['month'],e['event_name']),reverse=True)
    collections.sort(key=lambda e:e['name'].casefold())
    return {'roots':roots,'selected_root':chosen['key'],'collections':collections,'items':events[offset:offset+limit],'total':len(events),'bytes':sum(e['bytes'] for e in events)+sum(e['bytes'] for e in collections)}

def projects(root_key='',q='',offset=0,limit=100):
    if root_key=='all':
        with summary_context():return _projects(root_key,q,offset,limit)
    return _projects(root_key,q,offset,limit)

def personal(root_key='',q='',offset=0,limit=100):
    if root_key=='all':
        with summary_context():return _personal(root_key,q,offset,limit)
    return _personal(root_key,q,offset,limit)
