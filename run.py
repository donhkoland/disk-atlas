import socket,sys,time,webbrowser,urllib.request,json,threading
from atlas.storage import DATA
HOST='127.0.0.1'; PORT=8765
if '--stop' in sys.argv:
    try:
        base=f'http://{HOST}:{PORT}'; s=json.load(urllib.request.urlopen(base+'/api/session',timeout=3))
        req=urllib.request.Request(base+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Atlas-Token':s['token']})
        urllib.request.urlopen(req,timeout=5); print('Safe shutdown requested.')
    except OSError: print('DISK ATLAS is not running.')
    sys.exit()
# Hold a process-wide Windows mutex for duplicate launch prevention.
mutex=None
if sys.platform=='win32':
    import ctypes
    ctypes.windll.kernel32.CreateMutexW.restype=ctypes.c_void_p
    mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\DiskAtlasLocalServer8765')
    if ctypes.windll.kernel32.GetLastError()==183:
        (webbrowser.open(f'http://{HOST}:{PORT}') if '--no-browser' not in sys.argv else None); sys.exit()
try:
    with socket.socket() as probe: probe.bind((HOST,PORT))
except OSError:
    print('Port 8765 is occupied. No process was stopped.'); sys.exit(1)
import uvicorn
from atlas.server import app
server=uvicorn.Server(uvicorn.Config(app,host=HOST,port=PORT,log_level='info',timeout_graceful_shutdown=20))
app.state.server=server
def browser():
    for _ in range(100):
        if server.started: webbrowser.open(f'http://{HOST}:{PORT}'); return
        time.sleep(.1)
if '--no-browser' not in sys.argv: threading.Thread(target=browser,daemon=True).start()
server.run()
