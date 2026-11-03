"""Shared conservative budget for original-file I/O; cache and catalog stay independent."""
import ctypes,os,threading,time
from contextlib import contextmanager

def machine():
    total=0
    if os.name=='nt':
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),('total',ctypes.c_ulonglong),('available',ctypes.c_ulonglong),('page_total',ctypes.c_ulonglong),('page_available',ctypes.c_ulonglong),('virtual_total',ctypes.c_ulonglong),('virtual_available',ctypes.c_ulonglong),('extended',ctypes.c_ulonglong)]
        value=Memory();value.length=ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):total=value.total
    else:
        try:total=os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
        except (ValueError,OSError,AttributeError):pass
    return {'cpu_threads':os.cpu_count() or 1,'ram_bytes':total}
SYSTEM=machine();GLOBAL_LIMIT=2 if SYSTEM['cpu_threads']>=4 and SYSTEM['ram_bytes']>=8*1024**3 else 1
_global=threading.BoundedSemaphore(GLOBAL_LIMIT);_lock=threading.Lock();_local=threading.local();_volumes=[];_devices={}

def configure(volumes):
    global _volumes
    with _lock:_volumes=[dict(v) for v in volumes]

def normal(path):
    value=str(path)
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    elif value.startswith('\\\\?\\'):value=value[4:]
    return os.path.normcase(os.path.abspath(value))

def device(path,d=None):
    path=normal(path)
    matches=[v for v in _volumes if path==normal(v['root']) or path.startswith(normal(v['root']).rstrip(os.sep)+os.sep)]
    v=max(matches,key=lambda v:len(normal(v['root']))) if matches else {}
    physical=v.get('physical') or (d or {}).get('physical');volume=v.get('volume') or (d or {}).get('volume')
    # Unknown targets share a single conservative bucket, never drive letters as identity.
    key='physical:'+physical if physical else 'volume:'+volume if volume else 'unknown'
    bus=str(v.get('bus_type') or 'Unknown');fast=bus.casefold() in ('nvme','17')
    limit=2 if fast and GLOBAL_LIMIT>=2 and SYSTEM['ram_bytes']>=16*1024**3 and SYSTEM['cpu_threads']>=8 else 1
    if key not in _devices:_devices[key]={'semaphore':threading.BoundedSemaphore(limit),'limit':limit,'active':0,'queued':0,'bus_type':bus}
    return key,_devices[key]

@contextmanager
def lease(path,d=None,cancel=None):
    if getattr(_local,'depth',0):
        _local.depth+=1
        try:yield
        finally:_local.depth-=1
        return
    with _lock:key,state=device(path,d);state['queued']+=1
    acquired_device=acquired_global=False;active=False
    try:
        while not acquired_device:
            if cancel and cancel.is_set():raise OSError('Operation stopped while waiting for disk access')
            acquired_device=state['semaphore'].acquire(timeout=.2)
        while not acquired_global:
            if cancel and cancel.is_set():raise OSError('Operation stopped while waiting for resources')
            acquired_global=_global.acquire(timeout=.2)
        with _lock:state['queued']-=1;state['active']+=1;active=True
        _local.depth=1
        yield
    finally:
        _local.depth=0
        with _lock:
            if active:state['active']-=1
            else:state['queued']-=1
        if acquired_global:_global.release()
        if acquired_device:state['semaphore'].release()

def status():
    with _lock:return dict(SYSTEM,global_limit=GLOBAL_LIMIT,devices=[{'identity':key,**{k:v for k,v in state.items() if k!='semaphore'}} for key,state in _devices.items()],policy='USB, network, and unknown device types: 1 heavy read per device. Identified NVMe: up to 2 when resources allow. Partitions on the same physical drive share the limit.')
