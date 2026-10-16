param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
try {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
    $condaExe = 'D:\Miniforge3\Scripts\conda.exe'
    $environmentPath = Join-Path $repoRoot '.venv'
    $serverPath = Join-Path $PSScriptRoot 'server.py'
    $tailscaleExe = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    foreach ($requiredPath in @($condaExe, $serverPath, $tailscaleExe)) {
        if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Missing: $requiredPath" }
    }
    $statusText = & $tailscaleExe status --json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read Tailscale status.' }
    $tailStatus = $statusText | ConvertFrom-Json
    $allowedHost = ([string]$tailStatus.Self.DNSName).TrimEnd('.')
    if ($allowedHost -notmatch '^[a-zA-Z0-9.-]+\.ts\.net$') {
        throw 'No valid Tailscale device domain. Please sign in to Tailscale first.'
    }

    # Only stop this project's Python replay listener, never an unrelated port owner.
    $listeners = @(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue)
    foreach ($listenerId in @($listeners.OwningProcess | Sort-Object -Unique)) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerId"
        $expectedPython = Join-Path $environmentPath 'python.exe'
        $commandLine = ([string]$processInfo.CommandLine).Replace('\', '/')
        $absoluteScript = $serverPath.Replace('\', '/')
        $scriptMatches = $commandLine.Contains($absoluteScript) -or
            $commandLine -match '(?:^|\s)tools/navigation_replay/server\.py(?:\s|$)'
        if ($processInfo.ExecutablePath -ine $expectedPython -or -not $scriptMatches) {
            throw "Port 8766 belongs to another program (PID $listenerId); nothing was stopped."
        }
        Write-Host "Restarting replay server (PID $listenerId)..."
        Stop-Process -Id $listenerId
        Wait-Process -Id $listenerId -Timeout 10 -ErrorAction SilentlyContinue
    }

    $logFolder = Join-Path $repoRoot 'artifacts\navigation-replay-cache\launcher'
    New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
    $logName = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdoutPath = Join-Path $logFolder "$logName.stdout.log"
    $stderrPath = Join-Path $logFolder "$logName.stderr.log"
    $arguments = @('run', '--prefix', ('"{0}"' -f $environmentPath),
        '--no-capture-output', 'python', '-B', ('"{0}"' -f $serverPath),
        '--port', '8766', '--allow-host', $allowedHost)
    $launcher = Start-Process -FilePath $condaExe -ArgumentList $arguments -WorkingDirectory $repoRoot `
        -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8766/' -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        if ($launcher.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "Replay server did not start. Check $stderrPath" }
    Write-Host 'Replay is ready: http://127.0.0.1:8766/'
    Write-Host "Tailscale address (existing Serve configuration): https://$allowedHost/"
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8766/' }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
