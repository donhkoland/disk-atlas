"""Opt-in lightweight JPEG previews stored only in the central data directory."""
import os,time,uuid,subprocess,shutil,threading,json
from pathlib import Path
from PIL import Image,ImageOps,UnidentifiedImageError
from . import storage
from .storage import connect
from .fileops import resolve_entry
from .cleanup import signature
from .volumes import longpath
slots=threading.BoundedSemaphore(2)
IMAGE_EXTS={'jpg','jpeg','png','tif','tiff','webp','bmp','gif','heic','heif','avif','psd'}
VIDEO_EXTS={'mov','mp4','mxf','avi','mkv','webm','m4v','mpg','mpeg'}
def cache_path(key):
    if not key or Path(key).name!=key or not key.endswith('.jpg'):raise ValueError('Invalid thumbnail')
    return storage.DATA/'thumbnails'/key

def cached(eid):
    with connect() as c:
        row=c.execute('SELECT t.* FROM thumbnails t JOIN entries e ON e.id=t.entry_id WHERE e.id=? AND e.present=1 AND t.source_size=e.size AND t.source_mtime=e.mtime',(eid,)).fetchone()
    if row:
        path=cache_path(row['cache_key'])
        if path.is_file():return path
    return None

def generate(eid,volumes):
    from .io_budget import configure,lease
    configure(volumes)
    with connect() as c:d=c.execute('SELECT d.* FROM drives d JOIN entries e ON e.drive_id=d.id WHERE e.id=?',(eid,)).fetchone()
    if not d:raise ValueError('The file does not exist')
    d=dict(d)
    with lease(d['root'],d):return _generate(eid,volumes)

def _generate(eid,volumes):
    e,d,path,before=resolve_entry(eid,volumes)
    if e['is_dir'] or e['ext'] not in IMAGE_EXTS|VIDEO_EXTS:raise ValueError('Thumbnails are not supported for this format')
    if before.st_size!=e['size'] or before.st_mtime!=e['mtime']:raise ValueError('The file changed. Update its index.')
    existing=cached(eid)
    if existing:return {'ok':True,'cached':True,'url':f'/api/entries/{eid}/thumbnail'}
    cache=storage.DATA/'thumbnails';cache.mkdir(parents=True,exist_ok=True)
    key=uuid.uuid4().hex+'.jpg';output=cache/key;temporary=cache/(uuid.uuid4().hex+'.jpg')
    try:
        with slots:
            if e['ext'] in IMAGE_EXTS:
                try:
                    with Image.open(longpath(path)) as source:
                        source.draft('RGB',(480,320))
                        source=ImageOps.exif_transpose(source)
                        source.thumbnail((480,320))
                        source.convert('RGB').save(temporary,'JPEG',quality=80)
                except (UnidentifiedImageError,Image.DecompressionBombError) as error:raise ValueError('Image format unsupported by Pillow, or image too large') from error
            else:
                ffmpeg=shutil.which('ffmpeg')
                if not ffmpeg:raise ValueError('FFmpeg is not installed; video thumbnails cannot be generated')
                args=[ffmpeg,'-nostdin','-hide_banner','-loglevel','error','-threads','1','-filter_threads','1','-protocol_whitelist','file,pipe','-i',path,'-map','0:v:0','-frames:v','1','-vf','scale=480:320:force_original_aspect_ratio=decrease','-q:v','4','-y',str(temporary)]
                try:
                    result=subprocess.run(args,capture_output=True,timeout=30,creationflags=0x08000000 if os.name=='nt' else 0)
                except subprocess.TimeoutExpired as error:raise ValueError('Video thumbnail generation exceeded the 30-second limit') from error
                if result.returncode or not temporary.is_file():raise ValueError('Could not extract a frame from this video')
            after=os.stat(longpath(path))
            if signature(before)!=signature(after):raise ValueError('The original changed during generation')
            os.replace(temporary,output)
            with connect() as c:
                previous=c.execute('SELECT cache_key FROM thumbnails WHERE entry_id=?',(eid,)).fetchone()
                c.execute('INSERT OR REPLACE INTO thumbnails VALUES(?,?,?,?,?)',(eid,key,e['size'],e['mtime'],time.time()))
            if previous:cache_path(previous[0]).unlink(missing_ok=True)
        return {'ok':True,'cached':False,'url':f'/api/entries/{eid}/thumbnail'}
    except Exception:
        output.unlink(missing_ok=True);raise
    finally:temporary.unlink(missing_ok=True)
