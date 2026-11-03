"""Stream consistent local JSON anchors and atomically replace the prior copy."""
import datetime,json,os,tempfile,threading,time
from . import storage
from .storage import connect
_lock=threading.Lock()

def filename(did):
    # Names are catalog UUIDs, never supplied filesystem paths.
    import uuid
    try:return str(uuid.UUID(did))+'.json'
    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid drive ID')

def write(did):
    name=filename(did);folder=storage.DATA/'manifests';folder.mkdir(parents=True,exist_ok=True)
    target=folder/name;temp=None
    with _lock:
        try:
            with connect() as c:
                c.execute('BEGIN')
                drive=c.execute('SELECT * FROM drives WHERE id=?',(did,)).fetchone()
                if not drive:raise ValueError('The drive does not exist')
                scan=c.execute("SELECT * FROM scans WHERE drive_id=? AND status IN ('completed','completed_with_warnings') ORDER BY finished DESC LIMIT 1",(did,)).fetchone()
                if not scan:raise ValueError('Complete the first scan before generating JSON')
                generated=time.time();count=0
                fd,temp=tempfile.mkstemp(prefix='atlas-json-',suffix='.tmp',dir=folder)
                with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as out:
                    def dump(value):json.dump(value,out,ensure_ascii=False,separators=(',',':'),allow_nan=False)
                    out.write('{"format":"disk-atlas-catalog","version":1,"generated_at":');dump(generated)
                    out.write(',"generated_at_utc":');dump(datetime.datetime.fromtimestamp(generated,datetime.timezone.utc).isoformat())
                    out.write(',"drive":');dump(dict(drive));out.write(',"last_published_scan":');dump(dict(scan))
                    out.write(',"change_counts":');dump({r['kind']:r['n'] for r in c.execute('SELECT kind,count(*) n FROM changes WHERE scan_id=? GROUP BY kind',(scan['id'],))})
                    def array(key,sql,args):
                        nonlocal count
                        out.write(',"'+key+'":[');first=True
                        for row in c.execute(sql,args):
                            if not first:out.write(',')
                            first=False;dump(dict(row))
                            if key=='entries':count+=1
                        out.write(']')
                    array('entries','SELECT * FROM entries WHERE drive_id=? AND present=1 ORDER BY id',(did,))
                    array('annotations','SELECT * FROM annotations WHERE drive_id=? ORDER BY rel',(did,))
                    array('project_profiles','SELECT * FROM project_profiles WHERE drive_id=? ORDER BY rel',(did,))
                    out.write(',"entry_count":');dump(count);out.write('}\n');out.flush();os.fsync(out.fileno())
                size=os.path.getsize(temp)
                os.replace(temp,target);temp=None
            with connect() as c:c.execute('INSERT OR REPLACE INTO json_snapshots VALUES(?,?,?,?,?,NULL)',(did,scan['id'],generated,count,size))
            return {'name':name,'scan_id':scan['id'],'generated':generated,'entry_count':count,'bytes':size}
        except Exception as exc:
            with connect() as c:c.execute('INSERT INTO json_snapshots(drive_id,error) VALUES(?,?) ON CONFLICT(drive_id) DO UPDATE SET error=excluded.error',(did,str(exc)))
            raise
        finally:
            if temp:
                try:os.unlink(temp)
                except FileNotFoundError:pass

def status(did):
    name=filename(did)
    with connect() as c:
        row=c.execute('SELECT * FROM json_snapshots WHERE drive_id=?',(did,)).fetchone()
        latest=c.execute("SELECT id FROM scans WHERE drive_id=? AND status IN ('completed','completed_with_warnings') ORDER BY finished DESC LIMIT 1",(did,)).fetchone()
    return dict(row or {},name=name,available=(storage.DATA/'manifests'/name).is_file(),current=bool(row and latest and row['scan_id']==latest['id']))
