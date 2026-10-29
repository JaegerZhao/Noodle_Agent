param(
    [ValidateSet('Start', 'Status', 'Restart', 'Stop')][string]$Action = 'Start',
    [switch]$Open,
    [string]$RunDirectory = $env:NOODLE_PRODUCTION_RUN,
    [string]$PythonExecutable = $env:NOODLE_PYTHON,
    [string]$DataDirectory = $env:NOODLE_NEXT_DATA_DIR,
    [int]$ApiPort = 8011,
    [int]$HarnessPort = 3090
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $RunDirectory) {
    $instances = @(Get-ChildItem -LiteralPath (Join-Path $repo '.local-only') -Directory -ErrorAction SilentlyContinue | Where-Object {
        $settings = Join-Path $_.FullName 'runtime-overrides.json'
        (Test-Path -LiteralPath $settings) -and ((Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json).runtime_mode -eq 'production')
    })
    $live = @($instances | Where-Object {
        $receipt = Join-Path $_.FullName 'api-process.json'
        if (-not (Test-Path -LiteralPath $receipt)) { return $false }
        $record = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        $stamp = if ($record.started_at -is [DateTime]) { $record.started_at.ToUniversalTime() }
                 else { [DateTimeOffset]::Parse([string]$record.started_at).UtcDateTime }
        $null -ne $process -and $process.ProcessName -eq 'python' -and
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $stamp).TotalSeconds) -lt 15
    })
    if ($live.Count -eq 1) { $RunDirectory = $live[0].FullName }
    elseif ($instances.Count -eq 1) { $RunDirectory = $instances[0].FullName }
    elseif ($instances.Count -gt 1) { throw 'Multiple instances found. Specify -RunDirectory or NOODLE_PRODUCTION_RUN.' }
    else { $RunDirectory = Join-Path $repo '.local-only/production' }
}
$run = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($RunDirectory)) { $RunDirectory } else { Join-Path $repo $RunDirectory }))
if (-not $PythonExecutable) {
    $venv = Join-Path $repo '.venv/Scripts/python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $venv) { $venv } else { (Get-Command python -ErrorAction Stop).Source }
}
$python = $PythonExecutable
$runner = Join-Path $repo 'scripts\formal-workspace.py'
if (-not (Test-Path -LiteralPath (Join-Path $run 'manifest.json'))) {
    if ($Action -in @('Status', 'Stop')) { Write-Host 'No initialized production instance.'; return }
    if (-not $DataDirectory) { $DataDirectory = Join-Path $repo 'data/production' }
    & $python -X utf8 (Join-Path $repo 'scripts/production-instance.py') --run $run --library $DataDirectory --port $ApiPort --harness-port $HarnessPort
    if ($LASTEXITCODE -ne 0) { throw 'Production initialization failed; existing instances were not changed.' }
}
$manifest = Get-Content -LiteralPath (Join-Path $run 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$override = Get-Content -LiteralPath (Join-Path $run 'runtime-overrides.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($override.runtime_mode -ne 'production' -or $override.require_parse_reuse -ne $false) {
    throw 'This workspace is not configured for production.'
}

function Get-ReceiptUtc($Value) {
    # PowerShell 7 can deserialize JSON ISO dates into DateTime automatically.
    # Parsing that object as a locale string would apply the timezone twice.
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime() }
    if ($Value -is [DateTimeOffset]) { return $Value.UtcDateTime }
    return [DateTimeOffset]::Parse([string]$Value).UtcDateTime
}

function Get-TrackedService([string]$Role) {
    $receiptPath = Join-Path $run "$Role-process.json"
    if (-not (Test-Path -LiteralPath $receiptPath)) { return $null }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $process = Get-Process -Id $receipt.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $expectedName = if ($Role -eq 'harness') { 'node' } else { 'python' }
    $recordedStart = Get-ReceiptUtc $receipt.started_at
    if ($process.ProcessName -ne $expectedName -or
        [Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 15) {
        Write-Warning "Stale PID receipt for $Role; unrelated process $($receipt.pid) is left untouched."
        return $null
    }
    if ($receipt.runtime_mode -ne 'production' -or $receipt.require_parse_reuse) {
        throw "$Role is running with an older configuration; do not launch a duplicate."
    }
    return $process
}

function Test-Endpoint([string]$Url) {
    try { return (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}

function Stop-TrackedService([string]$Role) {
    $serviceProcess = Get-TrackedService $Role
    $childProcessId = $null
    $childReceiptPath = if ($Role -eq 'api') { Join-Path $run 'api-child-process.json' } elseif ($Role -eq 'harness') { Join-Path $run 'runtime/local/harness.json' } else { $null }
    if ($null -ne $childReceiptPath -and (Test-Path -LiteralPath $childReceiptPath)) {
        $childReceipt = Get-Content -LiteralPath $childReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $childProcessId = if ($Role -eq 'api') { $childReceipt.pid } else { $childReceipt.child }
        $childProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$childProcessId"
        if ($null -ne $childProcess) {
            $expectedMarker = if ($Role -eq 'api') { 'formal-workspace.py' } else { 'node_modules\@deepseek-ai\dsh\lib\bin.js' }
            $normalizedCommand = $childProcess.CommandLine.Replace('/', '\')
            if (-not $normalizedCommand.Contains($expectedMarker) -or -not $normalizedCommand.Contains($run) -or
                [Math]::Abs(((Get-Process -Id $childProcessId).StartTime.ToUniversalTime() - (Get-ReceiptUtc $childReceipt.started_at)).TotalSeconds) -gt 15) {
                throw "Stale child receipt for $Role; refusing to stop an unrelated process."
            }
        } else { $childProcessId = $null }
    }
    # Stop the supervisor first so a terminating child cannot be respawned.
    if ($null -ne $serviceProcess) { Stop-Process -Id $serviceProcess.Id -ErrorAction Stop }
    if ($null -ne $childProcessId) { Stop-Process -Id $childProcessId -ErrorAction SilentlyContinue }
    foreach ($serviceProcessId in @($serviceProcess.Id, $childProcessId)) {
        if ($null -ne $serviceProcessId) { Wait-Process -Id $serviceProcessId -Timeout 10 -ErrorAction SilentlyContinue }
    }
}

Push-Location $repo
try {
    if ($Action -in @('Restart', 'Stop')) {
        foreach ($role in @('harness', 'worker', 'api')) { Stop-TrackedService $role }
        if ($Action -eq 'Stop') {
            foreach ($role in @('api', 'worker', 'harness')) {
                if ($null -ne (Get-TrackedService $role)) { throw "$role is still running." }
                Write-Host "$role : STOPPED"
            }
            if ((Test-Endpoint "http://127.0.0.1:$($manifest.port)/health") -or
                (Test-Endpoint "http://127.0.0.1:$($manifest.harness_port)/")) {
                throw 'A service endpoint is still responding. Check for an untracked process; it was not stopped automatically.'
            }
            Write-Host 'Production services stopped. Knowledge, task checkpoints and conversation history are preserved.'
            return
        }
        $Action = 'Start'
    }
    foreach ($role in @('api', 'worker', 'harness')) {
        $process = Get-TrackedService $role
        if ($null -eq $process -and $Action -eq 'Start') {
            $servicePort = if ($role -eq 'api') { $manifest.port } elseif ($role -eq 'harness') { $manifest.harness_port } else { $null }
            if ($null -ne $servicePort) {
                $probe = [System.Net.Sockets.TcpClient]::new()
                try {
                    $probe.Connect('127.0.0.1', [int]$servicePort)
                    throw "Port $servicePort is already occupied by an untracked service."
                } catch [System.Net.Sockets.SocketException] {
                    # No listener: safe to start this role.
                } finally { $probe.Dispose() }
            }
            & $python -X utf8 $runner start $run --role $role
            if ($LASTEXITCODE -ne 0) { throw "Failed to start $role." }
            $process = Get-TrackedService $role
        }
        $state = if ($null -ne $process) { "RUNNING (PID $($process.Id))" } else { 'STOPPED' }
        Write-Host "$role : $state"
    }
    $apiReady = $false
    $harnessReady = $false
    $attempts = if ($Action -eq 'Start') { 30 } else { 1 }
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $apiReady = Test-Endpoint "http://127.0.0.1:$($manifest.port)/health"
        $harnessReady = Test-Endpoint "http://127.0.0.1:$($manifest.harness_port)/"
        if ($apiReady -and $harnessReady) { break }
        if ($attempt + 1 -lt $attempts) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $apiReady -or -not $harnessReady) {
        if ($Action -eq 'Status') {
            Write-Host "Production not ready. API ready: $apiReady; DSH ready: $harnessReady."
            return
        }
        throw "Services are not ready. Check logs in $run"
    }
    $url = "http://127.0.0.1:$($manifest.port)/app/#view=files"
    Write-Host "Production ready: $url"
    Write-Host "Library: $($manifest.library)"
    Write-Host 'Existing queued tasks resume when the worker starts; REVIEW tasks require manual retry.'
    if ($Open) { Start-Process $url }
} finally { Pop-Location }
