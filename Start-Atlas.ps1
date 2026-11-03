$ErrorActionPreference = 'Stop'
$atlasRoot = $PSScriptRoot
$atlasUrl = 'http://127.0.0.1:8765'
function Test-AtlasReady {
    try { $session = Invoke-RestMethod "$atlasUrl/api/session" -TimeoutSec 2; return [bool]$session.version } catch { return $false }
}
try {
    if (-not (Test-AtlasReady)) {
        $atlasPython = Join-Path $atlasRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $atlasPython)) { throw 'The Atlas Python environment is missing. Run SETUP.bat, then open Atlas again.' }
        $atlasLogs = Join-Path $atlasRoot 'data\logs'
        New-Item -ItemType Directory -Path $atlasLogs -Force | Out-Null
        $atlasProcess = Start-Process -FilePath $atlasPython -ArgumentList 'run.py --no-browser' -WorkingDirectory $atlasRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $atlasLogs 'launcher.stdout.log') -RedirectStandardError (Join-Path $atlasLogs 'launcher.stderr.log') -PassThru
        $atlasReady = $false
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            if (Test-AtlasReady) { $atlasReady = $true; break }
            if ($atlasProcess.HasExited) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $atlasReady) { throw "Atlas could not start. Check data\logs\launcher.stderr.log in this folder." }
    }
    Start-Process $atlasUrl
} catch {
    $atlasShell = New-Object -ComObject WScript.Shell
    $atlasShell.Popup($_.Exception.Message, 0, 'Disk Atlas - Startup', 16) | Out-Null
    exit 1
}
