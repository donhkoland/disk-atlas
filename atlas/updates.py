"""Local update scheduling and durable scan history. No AI or external services."""
import logging,threading,time
from .storage import connect,rows
from .volumes import locate
from .scanner import launch

def policies():
    with connect() as c:
        return rows(c,"SELECT d.id,d.name,d.last_scan,coalesce(p.enabled,0) enabled,coalesce(p.interval_hours,24) interval_hours,p.last_attempt,p.last_error,(SELECT status FROM scans WHERE drive_id=d.id ORDER BY started DESC LIMIT 1) latest_status FROM drives d LEFT JOIN update_policies p ON p.drive_id=d.id ORDER BY d.name")

def configure(did,enabled,hours):
    if not isinstance(enabled,bool):raise ValueError('Enabled must be true or false')
    if isinstance(hours,bool) or not isinstance(hours,int) or not 1<=hours<=720:raise ValueError('Use a whole-number interval from 1 to 720 hours')
    with connect() as c:
        if not c.execute('SELECT 1 FROM drives WHERE id=?',(did,)).fetchone():raise ValueError('The drive does not exist')
        c.execute('INSERT INTO update_policies(drive_id,enabled,interval_hours) VALUES(?,?,?) ON CONFLICT(drive_id) DO UPDATE SET enabled=excluded.enabled,interval_hours=excluded.interval_hours',(did,int(enabled),hours))
    return {'ok':True}

def tick(volumes,now=None):
    now=time.time() if now is None else now
    with connect() as c:
        candidates=rows(c,"SELECT d.*,p.interval_hours,p.last_attempt FROM drives d JOIN update_policies p ON p.drive_id=d.id WHERE p.enabled=1 AND NOT EXISTS(SELECT 1 FROM scans s WHERE s.drive_id=d.id AND s.status IN ('running','pausing','paused'))")
    for d in candidates:
        if now-max(d['last_scan'] or 0,d['last_attempt'] or 0)<d['interval_hours']*3600:continue
        if not locate(d,volumes):continue
        # One local scheduled scan at a time; check again on the next tick.
        with connect() as c:
            if c.execute("SELECT 1 FROM scans WHERE status IN ('running','pausing') LIMIT 1").fetchone():return
            c.execute('UPDATE update_policies SET last_attempt=?,last_error=NULL WHERE drive_id=?',(now,d['id']))
        try:
            sid=launch(d['id'])
            with connect() as c:c.execute('INSERT OR REPLACE INTO scan_origins VALUES(?,?)',(sid,'scheduled'))
        except (OSError,ValueError) as e:
            with connect() as c:c.execute('UPDATE update_policies SET last_error=? WHERE drive_id=?',(str(e),d['id']))
        return

def start(volume_provider):
    stop=threading.Event()
    def loop():
        # Let startup finish before accessing connected volumes.
        while not stop.wait(60):
            try:tick(volume_provider())
            except Exception:logging.exception('Local update scheduler failed')
    thread=threading.Thread(target=loop,name='atlas-local-updates',daemon=True);thread.start()
    return stop,thread

def history(drive_id='',offset=0,limit=20):
    where='WHERE s.drive_id=?' if drive_id else '';args=[drive_id] if drive_id else []
    with connect() as c:
        total=c.execute('SELECT count(*) FROM scans s '+where,args).fetchone()[0]
        scans=rows(c,"SELECT s.*,d.name drive_name,coalesce(o.origin,'manual / anterior') origin,EXISTS(SELECT 1 FROM scan_visibility v WHERE v.scan_id=s.id) hidden FROM scans s JOIN drives d ON d.id=s.drive_id LEFT JOIN scan_origins o ON o.scan_id=s.id "+where+' ORDER BY s.started DESC,s.id LIMIT ? OFFSET ?',args+[limit,offset])
        for scan in scans:
            scan['changes']={r['kind']:r['n'] for r in c.execute('SELECT kind,count(*) n FROM changes WHERE scan_id=? GROUP BY kind',(scan['id'],))}
    return {'items':scans,'total':total,'offset':offset,'limit':limit}
