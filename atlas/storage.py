import sqlite3, json, time
from pathlib import Path
BASE=Path(__file__).resolve().parent.parent
DATA=BASE/'data'
for folder in ('manifests','backups','config','logs'): (DATA/folder).mkdir(parents=True,exist_ok=True)
DB=DATA/'atlas.sqlite3'
class Connection(sqlite3.Connection):
    def __exit__(self,*args):
        try: return super().__exit__(*args)
        finally: self.close()

def connect(path=None):
    c=sqlite3.connect(path or DB,timeout=30,factory=Connection)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c
def init():
    with connect() as c:
        c.executescript('''
PRAGMA journal_mode=WAL;
PRAGMA user_version=1;
CREATE TABLE IF NOT EXISTS drives(id TEXT PRIMARY KEY,name TEXT NOT NULL,label TEXT,description TEXT,tags TEXT DEFAULT '',root TEXT NOT NULL,volume TEXT,physical TEXT,relative_root TEXT,last_scan REAL,created REAL);
CREATE TABLE IF NOT EXISTS drive_hardware(drive_id TEXT PRIMARY KEY,filesystem TEXT,capacity INTEGER,free INTEGER,bus_type TEXT,hardware TEXT,observed REAL);
CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,drive_id TEXT REFERENCES drives(id),status TEXT,started REAL,finished REAL,files INTEGER DEFAULT 0,folders INTEGER DEFAULT 0,bytes INTEGER DEFAULT 0,warnings INTEGER DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY,drive_id TEXT REFERENCES drives(id),rel TEXT NOT NULL,parent TEXT,name TEXT,ext TEXT,category TEXT,is_dir INTEGER,size INTEGER,mtime REAL,ctime REAL,inode TEXT,attrs INTEGER,metadata TEXT DEFAULT '{}',sha256 TEXT,fingerprint TEXT,verified REAL,present INTEGER DEFAULT 1,UNIQUE(drive_id,rel));
CREATE INDEX IF NOT EXISTS entry_parent ON entries(drive_id,parent,present);
CREATE INDEX IF NOT EXISTS entry_size ON entries(size,name);
CREATE INDEX IF NOT EXISTS entry_inode ON entries(inode);
CREATE INDEX IF NOT EXISTS entry_live_drive ON entries(present,drive_id);
CREATE INDEX IF NOT EXISTS entry_live_summary ON entries(present,drive_id,ext,is_dir,size);
CREATE INDEX IF NOT EXISTS entry_live_directories ON entries(present,is_dir,drive_id,parent);
CREATE INDEX IF NOT EXISTS entry_live_name ON entries(present,name COLLATE NOCASE,id);
CREATE INDEX IF NOT EXISTS entry_live_size ON entries(present,size,id);
CREATE INDEX IF NOT EXISTS entry_live_modified ON entries(present,mtime,id);
CREATE TABLE IF NOT EXISTS json_snapshots(drive_id TEXT PRIMARY KEY,scan_id TEXT,generated REAL,entry_count INTEGER,bytes INTEGER,error TEXT);
CREATE TABLE IF NOT EXISTS thumbnails(entry_id INTEGER PRIMARY KEY,cache_key TEXT,source_size INTEGER,source_mtime REAL,created REAL);
CREATE TABLE IF NOT EXISTS scan_visibility(scan_id TEXT PRIMARY KEY,hidden REAL);
CREATE TABLE IF NOT EXISTS project_profiles(drive_id TEXT,rel TEXT,year TEXT,category TEXT,PRIMARY KEY(drive_id,rel));
CREATE TABLE IF NOT EXISTS project_containers(entry_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS cleanup_jobs(id TEXT PRIMARY KEY,status TEXT,scope_id INTEGER,scope_name TEXT,created REAL,updated REAL,total INTEGER DEFAULT 0,processed INTEGER DEFAULT 0,groups INTEGER DEFAULT 0,remove_count INTEGER DEFAULT 0,bytes INTEGER DEFAULT 0,recycled INTEGER DEFAULT 0,warnings INTEGER DEFAULT 0,error TEXT,expires REAL,backup TEXT);
CREATE TABLE IF NOT EXISTS cleanup_job_files(job_id TEXT,entry_id INTEGER,sha256 TEXT,identity TEXT,payload TEXT,status TEXT,PRIMARY KEY(job_id,entry_id));
CREATE INDEX IF NOT EXISTS job_digest ON cleanup_job_files(job_id,sha256);
CREATE TABLE IF NOT EXISTS cleanup_job_errors(job_id TEXT,created REAL,message TEXT);
CREATE TABLE IF NOT EXISTS annotations(drive_id TEXT,rel TEXT,tags TEXT DEFAULT '',project TEXT DEFAULT '',collection TEXT DEFAULT '',client TEXT DEFAULT '',notes TEXT DEFAULT '',favorite INTEGER DEFAULT 0,inherit INTEGER DEFAULT 0,PRIMARY KEY(drive_id,rel));
CREATE TABLE IF NOT EXISTS pending(scan_id TEXT,rel TEXT,parent TEXT,name TEXT,ext TEXT,category TEXT,is_dir INTEGER,size INTEGER,mtime REAL,ctime REAL,inode TEXT,attrs INTEGER,metadata TEXT,PRIMARY KEY(scan_id,rel));
CREATE TABLE IF NOT EXISTS queue(scan_id TEXT,rel TEXT,PRIMARY KEY(scan_id,rel));
CREATE TABLE IF NOT EXISTS errors(scan_id TEXT,path TEXT,message TEXT);
CREATE TABLE IF NOT EXISTS changes(scan_id TEXT,rel TEXT,kind TEXT);
CREATE INDEX IF NOT EXISTS changes_scan_kind ON changes(scan_id,kind);
CREATE TABLE IF NOT EXISTS update_policies(drive_id TEXT PRIMARY KEY,enabled INTEGER DEFAULT 0,interval_hours INTEGER DEFAULT 24,last_attempt REAL,last_error TEXT);
CREATE TABLE IF NOT EXISTS scan_origins(scan_id TEXT PRIMARY KEY,origin TEXT);
CREATE TABLE IF NOT EXISTS cleanup_actions(id TEXT PRIMARY KEY,created REAL,entry_id INTEGER,path TEXT,kept_path TEXT,sha256 TEXT,status TEXT,backup TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(name,rel,content='entries',content_rowid='id',tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS entry_ai AFTER INSERT ON entries BEGIN INSERT INTO search(rowid,name,rel) VALUES(new.id,new.name,new.rel); END;
CREATE TRIGGER IF NOT EXISTS entry_ad AFTER DELETE ON entries BEGIN INSERT INTO search(search,rowid,name,rel) VALUES('delete',old.id,old.name,old.rel); END;
CREATE TRIGGER IF NOT EXISTS entry_au AFTER UPDATE ON entries BEGIN INSERT INTO search(search,rowid,name,rel) VALUES('delete',old.id,old.name,old.rel); INSERT INTO search(rowid,name,rel) VALUES(new.id,new.name,new.rel); END;
''')
        c.execute("UPDATE cleanup_jobs SET status='stopped',error='Application restarted; prepare a new cleanup plan' WHERE status IN ('analyzing','cleaning','ready')")
        c.execute("UPDATE scans SET status='paused',error='Application interrupted; resume available' WHERE status IN ('running','pausing')")
def rows(c,sql,args=()): return [dict(r) for r in c.execute(sql,args)]
