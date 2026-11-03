import os, ctypes, json, subprocess
from pathlib import Path

def volumes():
    if os.name!='nt': return [{'root':'/','volume':'posix:/','physical':None,'label':'Filesystem','filesystem':'','capacity':0,'free':0}]
    records=[]
    try:
        ps='Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,VolumeName,FileSystem,Size,FreeSpace,VolumeSerialNumber | ConvertTo-Json -Compress'
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',ps],capture_output=True,text=True,timeout=15,creationflags=0x08000000)
        raw=json.loads(result.stdout or '[]'); raw=raw if isinstance(raw,list) else [raw]
        for d in raw:
            root=d['DeviceID']+'\\'; buf=ctypes.create_unicode_buffer(1024)
            ok=ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root,buf,1024)
            records.append(dict(root=root,volume=buf.value if ok else 'serial:'+str(d.get('VolumeSerialNumber')),physical=None,label=d.get('VolumeName') or d['DeviceID'],filesystem=d.get('FileSystem'),capacity=int(d.get('Size') or 0),free=int(d.get('FreeSpace') or 0)))
        # Disk/partition relationships are retrieved separately; missing hardware identity stays unknown.
        ps='Get-Partition | Select-Object DriveLetter,DiskNumber,PartitionNumber | ConvertTo-Json -Compress'
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',ps],capture_output=True,text=True,timeout=15,creationflags=0x08000000)
        partitions=json.loads(result.stdout or '[]'); partitions=partitions if isinstance(partitions,list) else [partitions]
        ps='Get-Disk | Select-Object Number,UniqueId,SerialNumber,FriendlyName,BusType | ConvertTo-Json -Compress'
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',ps],capture_output=True,text=True,timeout=15,creationflags=0x08000000)
        disks=json.loads(result.stdout or '[]'); disks=disks if isinstance(disks,list) else [disks]
        for v in records:
            part=next((x for x in partitions if x.get('DriveLetter')==v['root'][0]),None)
            if part:
                disk=next((x for x in disks if x['Number']==part['DiskNumber']),{})
                v.update(physical=(disk.get('UniqueId') or disk.get('SerialNumber') or '').strip() or None,partition=part['PartitionNumber'],hardware=disk.get('FriendlyName'),bus_type=disk.get('BusType'))
    except (Exception,):
        # No guessed letter-based identity: unavailable detection is explicit.
        pass
    return records

def identify(root,vs):
    root=str(Path(root).resolve())
    matches=[v for v in vs if os.path.normcase(root+os.sep).startswith(os.path.normcase(str(Path(v['root']).resolve()).rstrip(os.sep)+os.sep))]
    if not matches: raise ValueError('Volume identity unavailable. Refresh volumes and try again.')
    v=max(matches,key=lambda x:len(x['root']))
    return v,os.path.relpath(root,str(Path(v['root']).resolve()))

def locate(d,vs):
    found=[v for v in vs if v['volume']==d['volume']]
    if len(found)!=1: return None
    # Cached discovery must never authorize actions after a drive-letter swap.
    if os.name=='nt' and d['volume'].startswith('\\\\?\\Volume{'):
        buf=ctypes.create_unicode_buffer(1024)
        ok=ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(found[0]['root'],buf,1024)
        if not ok or buf.value.casefold()!=d['volume'].casefold():return None
    root=os.path.normpath(os.path.join(found[0]['root'],d['relative_root'] or '.'))
    return root if os.path.isdir(root) else None

def longpath(path):
    path=os.path.abspath(path)
    if os.name=='nt' and not path.startswith('\\\\?\\'):
        return '\\\\?\\UNC\\'+path[2:] if path.startswith('\\\\') else '\\\\?\\'+path
    return path
